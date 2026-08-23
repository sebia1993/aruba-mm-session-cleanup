"""Application-owned SSH host-key trust store and first-use approval gate."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .models import MmConnectionConfig

APP_DIR_NAME = "ArubaMMSessionCleanup"
KNOWN_HOSTS_FILE_NAME = "known_hosts"


@dataclass(frozen=True)
class HostKeyObservation:
    host: str
    port: int
    key_type: str
    fingerprint: str
    key: Any = field(repr=False, compare=False)


HostKeyApproval = Callable[[HostKeyObservation], bool]


class HostKeyApprovalRequired(RuntimeError):
    def __init__(self, observation: HostKeyObservation) -> None:
        self.observation = observation
        super().__init__(
            "최초 SSH 서버 지문 승인이 필요합니다: "
            f"{observation.host}:{observation.port} {observation.key_type} {observation.fingerprint}"
        )


class HostKeyChangedError(RuntimeError):
    """Raised when a previously trusted endpoint presents another key."""


class KnownHostsError(RuntimeError):
    """Raised when the application trust store cannot be read safely."""


def default_known_hosts_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME / KNOWN_HOSTS_FILE_NAME
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "aruba-mm-session-cleanup" / KNOWN_HOSTS_FILE_NAME
    return Path.home() / ".config" / "aruba-mm-session-cleanup" / KNOWN_HOSTS_FILE_NAME


class KnownHostStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or default_known_hosts_path()).expanduser()
        self._lock = threading.RLock()

    def check(self, observation: HostKeyObservation) -> str:
        with self._lock:
            host_keys = self._load()
            trusted_for_host = host_keys.lookup(_known_hosts_name(observation))
            if not trusted_for_host:
                return "unknown"
            trusted_key = trusted_for_host.get(observation.key_type)
            if trusted_key is not None and trusted_key == observation.key:
                return "trusted"
            return "changed"

    def approve(self, observation: HostKeyObservation) -> None:
        with self._lock:
            host_keys = self._load()
            known_hosts_name = _known_hosts_name(observation)
            trusted_for_host = host_keys.lookup(known_hosts_name)
            if trusted_for_host:
                trusted_key = trusted_for_host.get(observation.key_type)
                if trusted_key is not None and trusted_key == observation.key:
                    return
                raise HostKeyChangedError(
                    "저장된 SSH 서버 지문과 현재 지문이 달라 승인을 차단했습니다: "
                    f"{observation.host}:{observation.port}"
                )
            host_keys.add(known_hosts_name, observation.key_type, observation.key)
            self._save(host_keys)

    def ensure_file(self) -> Path:
        with self._lock:
            if not self.path.exists():
                self._save(self._new_host_keys())
            return self.path

    @staticmethod
    def _new_host_keys():
        try:
            from paramiko.hostkeys import HostKeys
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("paramiko is required for SSH host-key verification") from exc
        return HostKeys()

    def _load(self):
        host_keys = self._new_host_keys()
        if not self.path.exists():
            return host_keys
        try:
            host_keys.load(str(self.path))
        except Exception as exc:
            raise KnownHostsError(f"앱 known_hosts 파일을 안전하게 읽을 수 없습니다: {self.path}") from exc
        return host_keys

    def _save(self, host_keys: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            host_keys.save(str(tmp_path))
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass
            tmp_path.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                tmp_path.unlink()
            except (FileNotFoundError, OSError):
                pass
            raise


def probe_host_key(config: MmConnectionConfig, *, timeout: int) -> HostKeyObservation:
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("paramiko is required for SSH host-key verification") from exc

    sock = socket.create_connection((config.host, config.port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        digest = hashlib.sha256(key.asbytes()).digest()
        fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        return HostKeyObservation(
            host=config.host,
            port=config.port,
            key_type=key.get_name(),
            fingerprint=fingerprint,
            key=key,
        )
    finally:
        try:
            transport.close()
        finally:
            try:
                sock.close()
            except OSError:
                pass


def ensure_host_key_trusted(
    config: MmConnectionConfig,
    *,
    timeout: int,
    store: KnownHostStore,
    approval_callback: Optional[HostKeyApproval] = None,
) -> HostKeyObservation:
    observation = probe_host_key(config, timeout=timeout)
    status = store.check(observation)
    if status == "trusted":
        return observation
    if status == "changed":
        raise HostKeyChangedError(
            "저장된 SSH 서버 지문과 현재 지문이 달라 연결을 차단했습니다: "
            f"{observation.host}:{observation.port}"
        )
    approved = False
    if approval_callback is not None:
        try:
            approved = approval_callback(observation) is True
        except Exception:
            approved = False
    if not approved:
        raise HostKeyApprovalRequired(observation)
    store.approve(observation)
    return observation


def _known_hosts_name(observation: HostKeyObservation) -> str:
    if observation.port == 22:
        return observation.host
    return f"[{observation.host}]:{observation.port}"
