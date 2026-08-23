"""Live Aruba MM command boundary."""

from __future__ import annotations

from typing import Optional, Protocol

from .hostkeys import HostKeyApproval, KnownHostStore, ensure_host_key_trusted
from .models import MmConnectionConfig
from .validation import validate_connection_fields, validate_timeout


class CommandConnection(Protocol):
    def send_command_timing(self, *, command_string: str, **kwargs) -> str: ...

    def disconnect(self) -> None: ...


def connect_to_mm(
    config: MmConnectionConfig,
    *,
    timeout: int,
    known_hosts_store: Optional[KnownHostStore] = None,
    host_key_approval_callback: Optional[HostKeyApproval] = None,
):
    host, username, password, port, enable_password = validate_connection_fields(
        host=config.host,
        username=config.username,
        password=config.password,
        port=config.port,
        device_type=config.device_type,
        enable_password=config.enable_password,
    )
    valid_timeout = validate_timeout(timeout)
    store = known_hosts_store or KnownHostStore()
    validated_config = MmConnectionConfig(
        host=host,
        username=username,
        password=password,
        port=port,
        device_type=config.device_type,
        enable_password=enable_password,
    )
    ensure_host_key_trusted(
        validated_config,
        timeout=valid_timeout,
        store=store,
        approval_callback=host_key_approval_callback,
    )
    known_hosts_path = store.ensure_file()
    try:
        from netmiko import ConnectHandler
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("netmiko is required for live Aruba MM access") from exc

    params = {
        "device_type": config.device_type,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "secret": enable_password or None,
        "timeout": valid_timeout,
        "conn_timeout": valid_timeout,
        "auth_timeout": valid_timeout,
        "banner_timeout": valid_timeout,
        "fast_cli": False,
        "ssh_strict": True,
        "system_host_keys": False,
        "alt_host_keys": True,
        "alt_key_file": str(known_hosts_path),
    }
    connection = ConnectHandler(**params)
    try:
        if enable_password:
            connection.enable()
    except Exception:
        try:
            connection.disconnect()
        except Exception:
            pass
        raise
    return connection


def run_command(connection: CommandConnection, command: str, *, timeout: int) -> str:
    return connection.send_command_timing(
        command_string=command,
        strip_prompt=False,
        strip_command=False,
        cmd_verify=False,
        read_timeout=timeout,
    )
