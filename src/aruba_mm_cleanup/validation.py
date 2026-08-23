"""Strict validation shared by CLI, GUI, web, and command builders."""

from __future__ import annotations

import ipaddress
import re

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,63}$")
_USERNAME = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")


def validate_host(value: object) -> str:
    if type(value) is not str:
        raise ValueError("MM 주소가 올바르지 않습니다.")
    host = value.strip()
    if not host or len(host) > 253:
        raise ValueError("MM 주소가 올바르지 않습니다.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if any(not _HOST_LABEL.fullmatch(label) for label in host.rstrip(".").split(".")):
        raise ValueError("MM 주소는 IP 주소 또는 올바른 DNS 이름이어야 합니다.")
    return host


def validate_username(value: object) -> str:
    if type(value) is not str:
        raise ValueError("계정이 올바르지 않습니다.")
    username = value.strip()
    if not _USERNAME.fullmatch(username):
        raise ValueError("계정에는 공백이나 제어 문자를 사용할 수 없습니다.")
    return username


def validate_role(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Role이 올바르지 않습니다.")
    role = value.strip() or "profiling"
    if not _ROLE.fullmatch(role):
        raise ValueError("Role은 1~64자의 영문·숫자 및 . _ : @ / - 만 사용할 수 있습니다.")
    return role


def validate_port(value: object) -> int:
    port = _strict_positive_integer(value, "SSH 포트")
    if port < 1 or port > 65535:
        raise ValueError("SSH 포트는 1~65535 사이여야 합니다.")
    return port


def validate_timeout(value: object) -> int:
    timeout = _strict_positive_integer(value, "장비 응답 대기")
    if timeout < 1 or timeout > 600:
        raise ValueError("장비 응답 대기는 1~600초 사이여야 합니다.")
    return timeout


def validate_connection_fields(
    *,
    host: object,
    username: object,
    password: object,
    port: object,
    device_type: object = "aruba_os",
    enable_password: object = "",
) -> tuple[str, str, str, int, str]:
    valid_host = validate_host(host)
    valid_username = validate_username(username)
    if type(password) is not str or not password or len(password) > 4096:
        raise ValueError("암호가 올바르지 않습니다.")
    if type(enable_password) is not str or len(enable_password) > 4096:
        raise ValueError("Enable 암호가 올바르지 않습니다.")
    if device_type != "aruba_os":
        raise ValueError("지원하지 않는 장비 드라이버입니다.")
    return valid_host, valid_username, password, validate_port(port), enable_password


def _strict_positive_integer(value: object, label: str) -> int:
    if type(value) is int:
        return value
    if type(value) is str and re.fullmatch(r"[0-9]{1,6}", value):
        return int(value)
    raise ValueError(f"{label} 값이 올바르지 않습니다.")
