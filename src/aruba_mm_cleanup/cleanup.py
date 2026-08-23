"""Cleanup runner for Aruba MM profiling-role users."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .hostkeys import HostKeyApproval, HostKeyObservation, KnownHostStore
from .models import (
    CleanupPlan,
    CleanupRunSummary,
    CleanupSettings,
    DeleteResult,
    MmConnectionConfig,
    QueryResult,
    _safe_list_items,
    _safe_optional_bool,
    _safe_text,
    _safe_timestamp_text,
)
from .parser import normalize_mac, parse_global_user_table_explained
from .session import ConnectionFactory, MmSession, ensure_unpaged_output
from .validation import validate_role

ProgressCallback = Callable[[str, dict[str, object]], None]
CancelCheck = Callable[[], bool]
SleepFunc = Callable[[float], None]
TargetApproval = Callable[[CleanupPlan], bool]
HISTORY_FILE_NAME = "deletion_history.jsonl"
_HISTORY_WRITE_LOCK = threading.Lock()


def build_query_command(role: str) -> str:
    role_value = validate_role(role)
    return f"show global-user-table list role {role_value}"


def build_delete_command(mac: str) -> str:
    mac_value = normalize_mac(mac)
    if not mac_value:
        raise ValueError("MAC 주소가 올바르지 않습니다.")
    return f"aaa user delete mac {mac_value}"


class MmCleanupRunner:
    def __init__(
        self,
        *,
        connection_factory: Optional[ConnectionFactory] = None,
        session: Optional[MmSession] = None,
        persistent_session: bool = False,
        sleep_func: Optional[SleepFunc] = None,
        known_hosts_store: Optional[KnownHostStore] = None,
        host_key_approval_callback: Optional[HostKeyApproval] = None,
        enforce_connection_safety: Optional[bool] = None,
    ) -> None:
        self.session = session or MmSession(
            connection_factory=connection_factory,
            known_hosts_store=known_hosts_store,
            host_key_approval_callback=host_key_approval_callback,
            enforce_connection_safety=enforce_connection_safety,
        )
        self.persistent_session = persistent_session
        self.sleep_func = sleep_func or time.sleep
        self._run_lock = threading.Lock()

    def approve_host_key(self, observation: HostKeyObservation) -> None:
        self.session.approve_host_key(observation)

    def query_users(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> QueryResult:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("다른 조회 또는 삭제 작업이 실행 중입니다.")
        try:
            return self._query_users(config, settings, progress_callback=progress_callback)
        finally:
            if not self.persistent_session:
                try:
                    self.close_session(progress_callback=progress_callback, reason="run_complete")
                except Exception as exc:
                    error = _exception_text(exc)
                    self._emit(
                        progress_callback,
                        "warning",
                        message=f"session close failed: {error}",
                        reason="run_complete",
                    )
            self._run_lock.release()

    def _query_users(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        *,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> QueryResult:
        command = build_query_command(settings.role)
        self._emit(progress_callback, "query_start", command=command, role=settings.role)
        output = self.session.run_command(config, settings, command, progress_callback=progress_callback)
        ensure_unpaged_output(output)
        parsed = parse_global_user_table_explained(output, role_filter=settings.role)
        try:
            entries = parsed.entries
        except Exception:
            entries = []
        try:
            decisions = parsed.decisions
        except Exception:
            decisions = []
        query_macs: list[str] = []
        type_na_macs: list[str] = []
        entries_count: Optional[int] = None
        try:
            entries_count = len(entries)
        except Exception:
            entries_count = None
        try:
            entries_iter = iter(entries)
        except Exception:
            entries_iter = iter(())
        while True:
            try:
                entry = next(entries_iter)
            except StopIteration:
                break
            except Exception:
                break
            try:
                mac = entry.mac
            except Exception:
                continue
            query_macs.append(mac)
            try:
                is_type_na = bool(entry.type_na)
            except Exception:
                is_type_na = False
            if is_type_na:
                type_na_macs.append(mac)
        if entries_count is None:
            entries_count = len(query_macs)
        parse_decisions: list[dict[str, object]] = []
        try:
            decisions_iter = iter(decisions)
        except Exception:
            decisions_iter = iter(())
        while True:
            try:
                item = next(decisions_iter)
            except StopIteration:
                break
            except Exception:
                break
            parse_decisions.append(
                {
                    "line_number": _safe_attr(item, "line_number", 0),
                    "action": _safe_attr(item, "action", ""),
                    "reason": _safe_attr(item, "reason", ""),
                    "mac": _safe_attr(item, "mac", ""),
                    "role": _safe_attr(item, "role", ""),
                    "user_type": _safe_attr(item, "user_type", ""),
                    "type_na": _safe_attr(item, "type_na", False),
                }
            )
        self._emit(
            progress_callback,
            "query_done",
            command=command,
            count=entries_count,
            macs=query_macs,
            parse_decisions=parse_decisions,
            type_na_macs=type_na_macs,
        )
        return QueryResult(command=command, entries=entries, parse_decisions=decisions)

    def run_once(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        *,
        output_dir: Path,
        progress_callback: Optional[ProgressCallback] = None,
        should_cancel: Optional[CancelCheck] = None,
        approve_targets: Optional[TargetApproval] = None,
    ) -> CleanupRunSummary:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("다른 조회 또는 삭제 작업이 실행 중입니다.")
        try:
            return self._run_once_locked(
                config,
                settings,
                output_dir=output_dir,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
                approve_targets=approve_targets,
            )
        finally:
            self._run_lock.release()

    def _run_once_locked(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        *,
        output_dir: Path,
        progress_callback: Optional[ProgressCallback] = None,
        should_cancel: Optional[CancelCheck] = None,
        approve_targets: Optional[TargetApproval] = None,
    ) -> CleanupRunSummary:
        started_at = datetime.now()
        summary = CleanupRunSummary(started_at=started_at, role=settings.role)
        cancel_check = should_cancel or (lambda: False)
        try:
            query = self._query_users(config, settings, progress_callback=progress_callback)
            summary.query_command = query.command
            summary.target_macs = _unique_macs(query.macs)
            try:
                summary.queried_count = len(query.entries)
            except Exception:
                summary.queried_count = len(summary.target_macs)
            summary.query_parse_decisions = query.parse_decisions
            try:
                has_query_entries = bool(query.entries)
            except Exception:
                has_query_entries = bool(summary.target_macs)
            if not has_query_entries:
                self._emit(progress_callback, "run_done", queried_count=0, remaining_count=0)
                return self._finalize_summary(summary, output_dir=output_dir, host=config.host, progress_callback=progress_callback)

            plan = CleanupPlan(
                plan_id=secrets.token_urlsafe(24),
                created_at=started_at,
                host=config.host,
                port=config.port,
                username=config.username,
                role=settings.role,
                query_command=query.command,
                queried_count=summary.queried_count,
                target_macs=tuple(summary.target_macs),
                query_parse_decisions=tuple(query.parse_decisions),
            )
            self._emit(
                progress_callback,
                "preview_ready",
                count=len(plan.target_macs),
                macs=list(plan.target_macs),
                role=plan.role,
            )
            approved = False
            if approve_targets is not None:
                try:
                    approved = approve_targets(plan) is True
                except Exception:
                    approved = False
            if not approved:
                summary.canceled = True
                summary.remaining_count = summary.queried_count
                self._emit(progress_callback, "approval_denied", count=len(plan.target_macs))
                return self._finalize_summary(
                    summary,
                    output_dir=output_dir,
                    host=config.host,
                    progress_callback=progress_callback,
                )

            if not self._countdown(settings.delete_delay_seconds, progress_callback, cancel_check):
                summary.canceled = True
                summary.remaining_count = summary.queried_count
                self._emit(progress_callback, "delete_canceled", count=summary.queried_count)
                return self._finalize_summary(summary, output_dir=output_dir, host=config.host, progress_callback=progress_callback)

            summary.delete_results = self._delete_macs(
                config,
                settings,
                summary.target_macs,
                progress_callback,
                should_cancel=cancel_check,
            )
            try:
                canceled_after_delete = cancel_check()
            except Exception:
                canceled_after_delete = True
            if canceled_after_delete:
                summary.canceled = True
                summary.verification_skipped = True
                summary.delete_results = _safe_list_items(summary.delete_results)
                summary.delete_success_count, summary.delete_failure_count = _count_delete_results(summary.delete_results)
                summary.remaining_count = max(summary.queried_count - summary.delete_success_count, 0)
                self._emit(progress_callback, "delete_canceled", count=max(len(summary.target_macs) - len(summary.delete_results), 0))
                return self._finalize_summary(summary, output_dir=output_dir, host=config.host, progress_callback=progress_callback)

            # Catch stop/cancel requests that arrive after the delete loop but
            # before the verification query starts.
            try:
                canceled_before_verify = cancel_check()
            except Exception:
                canceled_before_verify = True
            if canceled_before_verify:
                summary.canceled = True
                summary.verification_skipped = True
                summary.delete_results = _safe_list_items(summary.delete_results)
                summary.delete_success_count, summary.delete_failure_count = _count_delete_results(summary.delete_results)
                summary.remaining_count = max(summary.queried_count - summary.delete_success_count, 0)
                self._emit(progress_callback, "delete_canceled", count=0)
                return self._finalize_summary(summary, output_dir=output_dir, host=config.host, progress_callback=progress_callback)

            verify = self._query_users(config, settings, progress_callback=progress_callback)
            summary.verify_parse_decisions = verify.parse_decisions
            try:
                summary.remaining_count = len(verify.entries)
            except Exception:
                summary.remaining_count = len(_unique_macs(verify.macs))
            summary.delete_results = _apply_verification(summary.delete_results, verify.macs)
            summary.delete_success_count, summary.delete_failure_count = _count_delete_results(summary.delete_results)
            summary.reappeared_macs = _reappeared_deleted_macs(summary.delete_results, verify.macs)
            summary.reappeared_count = len(summary.reappeared_macs)
            if summary.reappeared_macs:
                self._emit(
                    progress_callback,
                    "reappeared_macs",
                    count=summary.reappeared_count,
                    macs=summary.reappeared_macs,
                )
            self._emit(
                progress_callback,
                "run_done",
                queried_count=summary.queried_count,
                delete_success_count=summary.delete_success_count,
                delete_failure_count=summary.delete_failure_count,
                remaining_count=summary.remaining_count,
                reappeared_count=summary.reappeared_count,
            )
        except Exception as exc:
            error = _exception_text(exc)
            summary.error = error
            self._emit(progress_callback, "run_error", error=error)
        return self._finalize_summary(summary, output_dir=output_dir, host=config.host, progress_callback=progress_callback)

    def _delete_macs(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        macs: list[str],
        progress_callback: Optional[ProgressCallback],
        *,
        should_cancel: Optional[CancelCheck] = None,
    ) -> list[DeleteResult]:
        unique_macs = _unique_macs(macs)
        self._emit(progress_callback, "delete_batch_start", count=len(unique_macs))
        results: list[DeleteResult] = []
        for index, mac in enumerate(unique_macs, start=1):
            if should_cancel is not None:
                try:
                    if should_cancel():
                        self._emit(progress_callback, "delete_canceled", count=len(unique_macs) - index + 1)
                        break
                except Exception:
                    self._emit(progress_callback, "delete_canceled", count=len(unique_macs) - index + 1)
                    break
            try:
                command = build_delete_command(mac)
            except ValueError as exc:
                error = f"확인 필요: 삭제 대상 MAC 오류 - {exc}"
                results.append(
                    DeleteResult(
                        mac=mac,
                        success=False,
                        command="",
                        error=error,
                        status="unknown",
                        response_status="unknown",
                    )
                )
                self._emit(
                    progress_callback,
                    "delete_unknown",
                    index=index,
                    total=len(unique_macs),
                    mac=mac,
                    command="",
                    error=error,
                )
                continue
            self._emit(progress_callback, "delete_start", index=index, total=len(unique_macs), mac=mac, command=command)
            try:
                output = self.session.run_command(
                    config,
                    settings,
                    command,
                    progress_callback=progress_callback,
                    retry_once=False,
                )
                status, error = classify_delete_response(output)
                success = status == "deleted"
                results.append(
                    DeleteResult(
                        mac=mac,
                        success=success,
                        command=command,
                        error=error,
                        status=status,
                        response_status=status,
                    )
                )
                self._emit(
                    progress_callback,
                    _delete_event_name(status),
                    index=index,
                    total=len(unique_macs),
                    mac=mac,
                    command=command,
                    error=error,
                )
            except Exception as exc:
                error = f"확인 필요: 삭제 명령 응답 실패 - {_exception_text(exc)}"
                results.append(
                    DeleteResult(
                        mac=mac,
                        success=False,
                        command=command,
                        error=error,
                        status="unknown",
                        response_status="unknown",
                    )
                )
                self._emit(
                    progress_callback,
                    "delete_unknown",
                    index=index,
                    total=len(unique_macs),
                    mac=mac,
                    command=command,
                    error=error,
                )
        return results

    def close_session(
        self,
        *,
        progress_callback: Optional[ProgressCallback] = None,
        reason: str = "manual",
    ) -> None:
        self.session.disconnect(progress_callback=progress_callback, reason=reason)

    def _countdown(
        self,
        seconds: int,
        progress_callback: Optional[ProgressCallback],
        should_cancel: CancelCheck,
    ) -> bool:
        try:
            remaining = max(0, int(seconds))
        except Exception:
            return False
        while remaining > 0:
            try:
                if should_cancel():
                    return False
            except Exception:
                return False
            self._emit(progress_callback, "countdown", remaining=remaining)
            try:
                self.sleep_func(1)
            except Exception:
                return False
            remaining -= 1
        try:
            if should_cancel():
                return False
        except Exception:
            return False
        self._emit(progress_callback, "countdown", remaining=0)
        return True

    def _finalize_summary(
        self,
        summary: CleanupRunSummary,
        *,
        output_dir: Path,
        host: str,
        progress_callback: Optional[ProgressCallback],
    ) -> CleanupRunSummary:
        if not self.persistent_session:
            try:
                self.close_session(progress_callback=progress_callback, reason="run_complete")
            except Exception as exc:
                error = _exception_text(exc)
                self._emit(
                    progress_callback,
                    "warning",
                    message=f"session close failed: {error}",
                    reason="run_complete",
                )
        try:
            summary.audit_path = write_audit_summary(summary, output_dir=output_dir, host=host)
        except Exception as exc:
            error = _exception_text(exc)
            summary.audit_error = error
            self._emit(progress_callback, "warning", message=f"audit summary save failed: {error}")
        try:
            summary.history_path = append_history_records(summary, output_dir=output_dir, host=host)
        except Exception as exc:
            error = _exception_text(exc)
            summary.history_error = error
            self._emit(progress_callback, "warning", message=f"deletion history save failed: {error}")
        return summary

    @staticmethod
    def _emit(callback: Optional[ProgressCallback], event: str, **payload: object) -> None:
        if callback is not None:
            try:
                callback(event, payload)
            except Exception:
                pass


def write_audit_summary(summary: CleanupRunSummary, *, output_dir: Path, host: str) -> Path:
    run_dir = _make_unique_summary_run_dir(output_dir, _summary_run_dir_name(summary))
    path = run_dir / "cleanup_summary.json"
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(summary.as_audit_dict(host=host), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    return path


def _make_unique_summary_run_dir(output_dir: Path, base_name: str) -> Path:
    for index in range(1000):
        run_name = base_name if index == 0 else f"{base_name}-{index}"
        run_dir = output_dir / run_name
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create unique audit directory for {base_name!r}")


def append_history_records(summary: CleanupRunSummary, *, output_dir: Path, host: str) -> Optional[Path]:
    try:
        raw_delete_results = getattr(summary, "delete_results", None)
    except Exception:
        raw_delete_results = None
    delete_results = _safe_list_items(raw_delete_results)
    if not delete_results:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / HISTORY_FILE_NAME
    try:
        started_at = getattr(summary, "started_at", None)
    except Exception:
        started_at = None
    run_at = _safe_timestamp_text(started_at)
    try:
        role = _safe_text(summary.role)
    except Exception:
        role = ""
    host_text = _safe_text(host)
    lines: list[str] = []
    for item in delete_results:
        if not isinstance(item, DeleteResult):
            continue
        success = _delete_result_success(item)
        status = _safe_text(_safe_attr(item, "status", ""))
        error_text = _safe_text(_safe_attr(item, "error", ""))
        record = {
            "run_at": run_at,
            "host": host_text,
            "role": role,
            "mac": _safe_text(_safe_attr(item, "mac", "")),
            "result": _history_result_label(item),
            "success": success,
            "status": status or ("deleted" if success else "failed"),
            "response_status": _safe_text(_safe_attr(item, "response_status", "")),
            "verified_absent": _safe_optional_bool(_safe_attr(item, "verified_absent", None)),
            "error": error_text,
            "reappeared": status == "reappeared",
        }
        lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    if not lines:
        return None
    with _HISTORY_WRITE_LOCK:
        tmp_path = _history_tmp_path(path)
        try:
            with tmp_path.open("wb") as tmp_handle:
                try:
                    with path.open("rb") as existing_handle:
                        shutil.copyfileobj(existing_handle, tmp_handle)
                except FileNotFoundError:
                    pass
                tmp_handle.write("".join(lines).encode("utf-8"))
            tmp_path.replace(path)
        except Exception:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise
    return path


def _history_tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _summary_run_dir_name(summary: CleanupRunSummary) -> str:
    started_at = getattr(summary, "started_at", None)
    try:
        return started_at.strftime("%Y%m%d_%H%M%S_%f")
    except Exception:
        return _safe_path_fragment(_safe_timestamp_text(started_at))


def _safe_path_fragment(value: str) -> str:
    try:
        text = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    except Exception:
        text = ""
    return text[:80] or "unknown-started-at"


def classify_delete_response(output: str) -> tuple[str, str]:
    if output is not None and not isinstance(output, str):
        return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {_safe_text(output)}"
    try:
        text = (output or "").strip()
    except Exception:
        return "unknown", "확인 필요: 삭제 명령 응답 판정 불가"
    try:
        normalized = text.casefold()
    except Exception:
        return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"
    if not text:
        return "unknown", "확인 필요: 삭제 명령 응답이 비어 있음"

    failure_markers = (
        "invalid input",
        "permission denied",
        "not authorized",
        "authorization failed",
        "authentication failed",
        "access denied",
        "not found",
        "no such",
        "does not exist",
        "error",
        "failed",
    )
    for marker in failure_markers:
        try:
            if marker in normalized:
                return "failed", text
        except Exception:
            return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"

    unknown_markers = (
        "incomplete",
        "ambiguous",
        "timed out",
        "timeout",
        "try again",
        "confirm",
        "continue",
    )
    for marker in unknown_markers:
        try:
            if marker in normalized:
                return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"
        except Exception:
            return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"

    success_markers = (
        "user deleted",
        "deleted",
        "delete successful",
        "successfully deleted",
        "removed",
        "success",
    )
    for marker in success_markers:
        try:
            if marker in normalized:
                return "deleted", ""
        except Exception:
            return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"

    return "unknown", f"확인 필요: 삭제 명령 응답 판정 불가 - {text}"


def _delete_error_from_output(output: str) -> str:
    status, error = classify_delete_response(output)
    return "" if status == "deleted" else error


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _unique_macs(macs: list[str]) -> list[str]:
    if not isinstance(macs, (list, tuple, set)):
        return []
    try:
        macs_iter = iter(macs)
    except Exception:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    while True:
        try:
            mac = next(macs_iter)
        except StopIteration:
            break
        except Exception:
            break
        try:
            text = mac.strip() if isinstance(mac, str) else ""
            normalized = normalize_mac(text) or text.casefold()
        except Exception:
            continue
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _reappeared_deleted_macs(delete_results: list[DeleteResult], verify_macs: list[str]) -> list[str]:
    reappeared: list[str] = []
    for item in _safe_list_items(delete_results):
        if _safe_text(_safe_attr(item, "status", "")) != "reappeared":
            continue
        mac = _safe_text(_safe_attr(item, "mac", ""))
        if mac:
            reappeared.append(mac)
    return reappeared


def _apply_verification(delete_results: list[DeleteResult], verify_macs: list[str]) -> list[DeleteResult]:
    remaining = set(_unique_macs(verify_macs))
    verified: list[DeleteResult] = []
    for item in _safe_list_items(delete_results):
        if not isinstance(item, DeleteResult):
            verified.append(
                DeleteResult(
                    mac=_safe_text(_safe_attr(item, "mac", "")),
                    success=False,
                    command=_safe_text(_safe_attr(item, "command", "")),
                    error="확인 필요: 삭제 결과 형식 오류",
                    status="unknown",
                    response_status="unknown",
                    verified_absent=None,
                )
            )
            continue
        response_status = _safe_text(_safe_attr(item, "response_status", "")) or _safe_text(
            _safe_attr(item, "status", "")
        )
        command = _safe_text(_safe_attr(item, "command", ""))
        error = _safe_text(_safe_attr(item, "error", ""))
        try:
            item_mac = item.mac if isinstance(item.mac, str) else ""
            comparable_mac = normalize_mac(item_mac) or item_mac.strip().casefold()
        except Exception:
            comparable_mac = ""
            item_mac = _safe_text(_safe_attr(item, "mac", ""))
        if not comparable_mac:
            verified.append(
                DeleteResult(
                    mac=_safe_text(item_mac),
                    success=False,
                    command=command,
                    error=error or "확인 필요: 삭제 결과 MAC 오류",
                    status="unknown",
                    response_status=response_status or "unknown",
                    verified_absent=None,
                )
            )
            continue
        absent = comparable_mac not in remaining
        if response_status == "deleted" and absent:
            verified.append(
                DeleteResult(
                    mac=item_mac,
                    success=True,
                    command=command,
                    error=error,
                    status="verified_deleted",
                    response_status=response_status,
                    verified_absent=True,
                )
            )
        elif response_status == "deleted":
            verified.append(
                DeleteResult(
                    mac=item_mac,
                    success=False,
                    command=command,
                    error=error or "삭제 응답은 성공이었지만 검증 조회에서 다시 발견",
                    status="reappeared",
                    response_status=response_status,
                    verified_absent=False,
                )
            )
        else:
            verified.append(
                DeleteResult(
                    mac=item_mac,
                    success=False,
                    command=command,
                    error=error,
                    status=response_status or "unknown",
                    response_status=response_status,
                    verified_absent=absent,
                )
            )
    return verified


def _delete_event_name(status: str) -> str:
    if status == "deleted":
        return "delete_done"
    if status == "failed":
        return "delete_error"
    return "delete_unknown"


def _delete_result_success(item: object) -> bool:
    try:
        return bool(item.success)  # type: ignore[attr-defined]
    except Exception:
        return False


def _count_delete_results(results: object) -> tuple[int, int]:
    success_count = 0
    failure_count = 0
    for item in _safe_list_items(results):
        if _delete_result_success(item):
            success_count += 1
        else:
            failure_count += 1
    return success_count, failure_count


def _history_result_label(item: DeleteResult) -> str:
    status = _safe_text(_safe_attr(item, "status", ""))
    if status == "reappeared":
        return "재조회됨"
    if status == "unknown":
        return "확인 필요"
    if _delete_result_success(item):
        return "삭제 완료"
    return "삭제 실패"


def _safe_attr(item: object, name: str, default: object) -> object:
    try:
        return getattr(item, name, default)
    except Exception:
        return default


def _exception_text(exc: BaseException) -> str:
    return _safe_text(exc) or exc.__class__.__name__
