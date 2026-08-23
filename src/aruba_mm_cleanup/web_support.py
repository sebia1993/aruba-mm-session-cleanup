"""Shared support logic for the lightweight web app."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .cleanup import build_query_command
from .models import CleanupRunSummary, CleanupSettings, MmConnectionConfig, _safe_text
from .validation import validate_host, validate_username


@dataclass(frozen=True)
class WebRunRequest:
    host: str
    username: str
    password: str = field(repr=False)
    enable_password: str = field(repr=False)
    port: int
    role: str
    timeout: int
    output_dir: Path


def parse_run_request(
    form: Mapping[str, object],
    *,
    default_output_dir: Path = Path("outputs"),
) -> WebRunRequest:
    host = validate_host(_form_text(form, "host"))
    username = validate_username(_form_text(form, "username"))

    password = _form_text(form, "password")
    if not password:
        raise ValueError("암호를 입력하세요.")

    role = _form_text(form, "role").strip() or "profiling"
    try:
        build_query_command(role)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    port = _positive_int(_form_text(form, "port").strip() or "22", "SSH 포트")
    if port > 65535:
        raise ValueError("SSH 포트는 1~65535 사이여야 합니다.")

    timeout = _positive_int(_form_text(form, "timeout").strip() or "60", "장비 응답 대기")
    if timeout > 600:
        raise ValueError("장비 응답 대기는 1~600초 사이여야 합니다.")

    output_dir_text = _form_text(form, "output_dir").strip()
    output_dir = Path(output_dir_text) if output_dir_text else default_output_dir

    return WebRunRequest(
        host=host,
        username=username,
        password=password,
        enable_password=_form_text(form, "enable_password"),
        port=port,
        role=role,
        timeout=timeout,
        output_dir=output_dir,
    )


def connection_config_from_request(request: WebRunRequest) -> MmConnectionConfig:
    return MmConnectionConfig(
        host=request.host,
        username=request.username,
        password=request.password,
        port=request.port,
        enable_password=request.enable_password,
    )


def cleanup_settings_from_request(request: WebRunRequest) -> CleanupSettings:
    return CleanupSettings(role=request.role, timeout=request.timeout, delete_delay_seconds=0)


def summary_view(summary: CleanupRunSummary) -> dict[str, object]:
    return {
        "queried_count": _safe_int(getattr(summary, "queried_count", 0)),
        "delete_success_count": _safe_int(getattr(summary, "delete_success_count", 0)),
        "delete_failure_count": _safe_int(getattr(summary, "delete_failure_count", 0)),
        "remaining_count": _safe_int(getattr(summary, "remaining_count", 0)),
        "reappeared_count": _safe_int(getattr(summary, "reappeared_count", 0)),
        "audit_path": _safe_text(getattr(summary, "audit_path", "")),
        "history_path": _safe_text(getattr(summary, "history_path", "")),
        "audit_error": _safe_text(getattr(summary, "audit_error", "")),
        "history_error": _safe_text(getattr(summary, "history_error", "")),
        "error": _safe_text(getattr(summary, "error", "")),
    }


def smoke_status() -> str:
    return "webapp smoke ok"


def _form_text(form: Mapping[str, object], name: str) -> str:
    try:
        value = form.get(name, "")
    except Exception:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _positive_int(value: str, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} 값이 올바르지 않습니다.") from exc
    if number < 1:
        raise ValueError(f"{label} 값은 1 이상이어야 합니다.")
    return number


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0
