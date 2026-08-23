"""Reusable Aruba MM command session."""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .connection import CommandConnection, connect_to_mm
from .connection import run_command as send_mm_command
from .hostkeys import HostKeyApproval, HostKeyObservation, KnownHostStore
from .models import CleanupSettings, MmConnectionConfig, _safe_text

ProgressCallback = Callable[[str, dict[str, object]], None]
ConnectionFactory = Callable[[MmConnectionConfig, int], CommandConnection]


class MmSession:
    """Keep one MM connection alive until config changes or explicit close."""

    def __init__(
        self,
        *,
        connection_factory: Optional[ConnectionFactory] = None,
        known_hosts_store: Optional[KnownHostStore] = None,
        host_key_approval_callback: Optional[HostKeyApproval] = None,
        enforce_connection_safety: Optional[bool] = None,
    ) -> None:
        self.known_hosts_store = known_hosts_store or KnownHostStore()
        self.host_key_approval_callback = host_key_approval_callback
        self.connection_factory = connection_factory or (
            lambda config, timeout: connect_to_mm(
                config,
                timeout=timeout,
                known_hosts_store=self.known_hosts_store,
                host_key_approval_callback=self.host_key_approval_callback,
            )
        )
        self.enforce_connection_safety = True if enforce_connection_safety is None else bool(enforce_connection_safety)
        self._connection: Optional[CommandConnection] = None
        self._config: Optional[MmConnectionConfig] = None
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connection is not None

    def run_command(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        command: str,
        *,
        progress_callback: Optional[ProgressCallback] = None,
        retry_once: bool = True,
    ) -> str:
        with self._lock:
            connection = self._ensure_connected(config, settings, progress_callback=progress_callback)
            try:
                return send_mm_command(connection, command, timeout=settings.timeout)
            except Exception as exc:
                if not retry_once:
                    self.disconnect(progress_callback=progress_callback, reason="command_failed")
                    raise
                self._emit(
                    progress_callback,
                    "session_reconnect_start",
                    host=config.host,
                    command=command,
                    error=_exception_text(exc),
                )
                self.disconnect(progress_callback=progress_callback, reason="reconnect")
                try:
                    connection = self._ensure_connected(config, settings, progress_callback=progress_callback)
                    return send_mm_command(connection, command, timeout=settings.timeout)
                except Exception as retry_exc:
                    self.disconnect(progress_callback=progress_callback, reason="command_failed")
                    raise RuntimeError(
                        "MM 명령 실행 실패 후 재시도 실패: "
                        f"최초 오류={_exception_text(exc)}; 재시도 오류={_exception_text(retry_exc)}"
                    ) from retry_exc

    def disconnect(
        self,
        *,
        progress_callback: Optional[ProgressCallback] = None,
        reason: str = "manual",
    ) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            self._config = None
            if connection is None:
                return
            try:
                connection.disconnect()
            except Exception as exc:
                self._emit(
                    progress_callback,
                    "warning",
                    message=f"disconnect failed: {_exception_text(exc)}",
                    reason=reason,
                )
            self._emit(progress_callback, "session_disconnected", reason=reason)

    def approve_host_key(self, observation: HostKeyObservation) -> None:
        self.known_hosts_store.approve(observation)

    def _ensure_connected(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback],
    ) -> CommandConnection:
        if self._connection is not None and self._config == config:
            self._emit(progress_callback, "session_reuse", host=config.host)
            return self._connection

        if self._connection is not None:
            self.disconnect(progress_callback=progress_callback, reason="config_changed")

        self._emit(progress_callback, "connect_start", host=config.host)
        connection = self.connection_factory(config, settings.timeout)
        try:
            disconnect = getattr(connection, "disconnect", None)
            send_command = getattr(connection, "send_command_timing", None)
        except Exception as attr_exc:
            try:
                disconnect = getattr(connection, "disconnect", None)
            except Exception:
                disconnect = None
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as cleanup_exc:
                    self._emit(
                        progress_callback,
                        "warning",
                        message=f"invalid connection cleanup failed: {_exception_text(cleanup_exc)}",
                    )
            raise RuntimeError("MM 연결 객체가 올바르지 않습니다.") from attr_exc
        if not callable(send_command) or not callable(disconnect):
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as exc:
                    self._emit(
                        progress_callback,
                        "warning",
                        message=f"invalid connection cleanup failed: {_exception_text(exc)}",
                    )
            raise RuntimeError("MM 연결 객체가 올바르지 않습니다.")
        self._connection = connection
        self._config = config
        self._emit(progress_callback, "connect_done", host=config.host)
        try:
            if self.enforce_connection_safety:
                self._require_no_paging(settings, progress_callback=progress_callback)
                self._require_device_identity(settings, progress_callback=progress_callback)
            else:
                self._safe_no_paging(settings, progress_callback=progress_callback)
        except Exception:
            self.disconnect(progress_callback=progress_callback, reason="safety_gate_failed")
            raise
        return self._connection

    def _require_no_paging(
        self,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        if self._connection is None:
            raise RuntimeError("페이징 해제 전에 MM 연결이 종료되었습니다.")
        try:
            output = send_mm_command(self._connection, "no paging", timeout=settings.timeout)
        except Exception as exc:
            raise RuntimeError(f"페이징 해제 실패로 삭제를 차단했습니다: {_exception_text(exc)}") from exc
        if not isinstance(output, str) or _command_rejected(output):
            raise RuntimeError("페이징 해제 명령이 거부되어 삭제를 차단했습니다.")
        self._emit(progress_callback, "paging_disabled")

    def _require_device_identity(
        self,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        if self._connection is None:
            raise RuntimeError("장비 신원 확인 전에 MM 연결이 종료되었습니다.")
        try:
            output = send_mm_command(self._connection, "show version", timeout=settings.timeout)
        except Exception as exc:
            raise RuntimeError(f"MM/WLC 장비 신원 확인 실패: {_exception_text(exc)}") from exc
        if not is_supported_mm_identity(output):
            raise RuntimeError("Aruba Mobility Master/Conductor 또는 WLC 신원을 확인할 수 없어 삭제를 차단했습니다.")
        self._emit(progress_callback, "device_identity_verified", family="Aruba Mobility")

    def _safe_no_paging(
        self,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        if self._connection is None:
            return
        try:
            send_mm_command(self._connection, "no paging", timeout=settings.timeout)
        except Exception as exc:
            self._emit(progress_callback, "warning", message=f"no paging failed: {_exception_text(exc)}")

    @staticmethod
    def _emit(callback: Optional[ProgressCallback], event: str, **payload: object) -> None:
        if callback is not None:
            try:
                callback(event, payload)
            except Exception:
                pass


def _exception_text(exc: BaseException) -> str:
    return _safe_text(exc) or exc.__class__.__name__


def _command_rejected(output: str) -> bool:
    normalized = output.casefold()
    return any(
        marker in normalized
        for marker in (
            "invalid input",
            "unknown command",
            "incomplete command",
            "permission denied",
            "not authorized",
            "failed",
            "error:",
        )
    )


def ensure_unpaged_output(output: str) -> None:
    if not isinstance(output, str):
        raise RuntimeError("장비 조회 응답이 올바르지 않습니다.")
    normalized = output.casefold()
    if any(
        marker in normalized
        for marker in (
            "--more--",
            "<--- more --->",
            "press any key to continue",
            "press space to continue",
            "press <space>",
        )
    ):
        raise RuntimeError("조회 응답에서 페이징 표시를 발견해 삭제를 차단했습니다.")


def is_supported_mm_identity(output: object) -> bool:
    if not isinstance(output, str):
        return False
    normalized = " ".join(output.casefold().split())
    if "arubaos" not in normalized and "aruba operating system software" not in normalized:
        return False
    markers = (
        "mobility master",
        "mobility conductor",
        "wireless controller",
        "aruba controller",
        "model: arubamm",
        "model: arubamc",
        "model: aruba70",
        "model: aruba72",
        "model: aruba90",
        "model: aruba91",
        "model: aruba92",
    )
    return any(marker in normalized for marker in markers)
