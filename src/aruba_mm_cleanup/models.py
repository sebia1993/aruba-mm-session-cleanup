"""Shared models for Aruba MM cleanup runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class MmConnectionConfig:
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 22
    device_type: str = "aruba_os"
    enable_password: str = field(default="", repr=False)


@dataclass(frozen=True)
class CleanupSettings:
    role: str = "profiling"
    timeout: int = 60
    delete_delay_seconds: int = 60


@dataclass(frozen=True)
class UserEntry:
    mac: str
    role: str = ""
    username: str = ""
    ip_address: str = ""
    user_type: str = ""
    type_na: bool = False


@dataclass(frozen=True)
class ParseDecision:
    line_number: int
    action: str
    reason: str
    mac: str = ""
    role: str = ""
    user_type: str = ""
    type_na: bool = False


@dataclass(frozen=True)
class ParseResult:
    entries: list[UserEntry]
    decisions: list[ParseDecision] = field(default_factory=list)


@dataclass(frozen=True)
class QueryResult:
    command: str
    entries: list[UserEntry]
    parse_decisions: list[ParseDecision] = field(default_factory=list)

    @property
    def macs(self) -> list[str]:
        macs: list[str] = []
        try:
            entries_iter = iter(self.entries)
        except Exception:
            return macs
        while True:
            try:
                entry = next(entries_iter)
            except StopIteration:
                break
            except Exception:
                break
            try:
                macs.append(entry.mac)
            except Exception:
                continue
        return macs


@dataclass(frozen=True)
class CleanupPlan:
    """Immutable query snapshot that must be explicitly approved before deletion."""

    plan_id: str = field(repr=False)
    created_at: datetime
    host: str
    port: int
    username: str
    role: str
    query_command: str
    queried_count: int
    target_macs: tuple[str, ...]
    query_parse_decisions: tuple[ParseDecision, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeleteResult:
    mac: str
    success: bool
    command: str
    error: str = ""
    status: str = ""
    response_status: str = ""
    verified_absent: Optional[bool] = None


@dataclass
class CleanupRunSummary:
    started_at: datetime
    role: str
    queried_count: int = 0
    delete_success_count: int = 0
    delete_failure_count: int = 0
    remaining_count: int = 0
    reappeared_count: int = 0
    canceled: bool = False
    query_command: str = ""
    target_macs: list[str] = field(default_factory=list)
    reappeared_macs: list[str] = field(default_factory=list)
    query_parse_decisions: list[ParseDecision] = field(default_factory=list)
    verify_parse_decisions: list[ParseDecision] = field(default_factory=list)
    delete_results: list[DeleteResult] = field(default_factory=list)
    verification_skipped: bool = False
    audit_path: Optional[Path] = None
    history_path: Optional[Path] = None
    audit_error: str = ""
    history_error: str = ""
    error: str = ""

    def as_audit_dict(self, *, host: str) -> dict[str, Any]:
        return {
            "started_at": _safe_timestamp_text(self.started_at),
            "host": _safe_text(host),
            "role": _safe_text(self.role),
            "query_command": _safe_text(self.query_command),
            "queried_count": _safe_int(self.queried_count),
            "delete_success_count": _safe_int(self.delete_success_count),
            "delete_failure_count": _safe_int(self.delete_failure_count),
            "remaining_count": _safe_int(self.remaining_count),
            "reappeared_count": _safe_int(self.reappeared_count),
            "canceled": _safe_bool(self.canceled),
            "verification_skipped": _safe_bool(self.verification_skipped),
            "target_macs": [_safe_text(item) for item in _safe_list_items(self.target_macs)],
            "reappeared_macs": [_safe_text(item) for item in _safe_list_items(self.reappeared_macs)],
            "query_parse_decisions": [
                _parse_decision_audit_dict(item) for item in _safe_list_items(self.query_parse_decisions)
            ],
            "verify_parse_decisions": [
                _parse_decision_audit_dict(item) for item in _safe_list_items(self.verify_parse_decisions)
            ],
            "delete_results": [_delete_result_audit_dict(item) for item in _safe_list_items(self.delete_results)],
            "audit_error": _safe_text(self.audit_error),
            "history_error": _safe_text(self.history_error),
            "error": _safe_text(self.error),
        }


def _parse_decision_audit_dict(item: Any) -> dict[str, Any]:
    return {
        "line_number": _safe_int(_item_value(item, "line_number", 0)),
        "action": _safe_text(_item_value(item, "action", "")),
        "reason": _safe_text(_item_value(item, "reason", "")),
        "mac": _safe_text(_item_value(item, "mac", "")),
        "role": _safe_text(_item_value(item, "role", "")),
        "user_type": _safe_text(_item_value(item, "user_type", "")),
        "type_na": _safe_bool(_item_value(item, "type_na", False)),
    }


def _delete_result_audit_dict(item: Any) -> dict[str, Any]:
    success = _safe_bool(_item_value(item, "success", False))
    status = _safe_text(_item_value(item, "status", ""))
    return {
        "mac": _safe_text(_item_value(item, "mac", "")),
        "success": success,
        "status": status or ("deleted" if success else "failed"),
        "response_status": _safe_text(_item_value(item, "response_status", "")),
        "verified_absent": _safe_optional_bool(_item_value(item, "verified_absent", None)),
        "command": _safe_text(_item_value(item, "command", "")),
        "error": _safe_text(_item_value(item, "error", "")),
    }


def _item_value(item: Any, name: str, default: Any) -> Any:
    try:
        if isinstance(item, Mapping):
            return item.get(name, default)
        return getattr(item, name, default)
    except Exception:
        return default


def _safe_list_items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        try:
            return list(value)
        except Exception:
            return []
    return []


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return ""


def _safe_timestamp_text(value: Any) -> str:
    try:
        return value.isoformat(timespec="seconds")
    except TypeError:
        try:
            return value.isoformat()
        except Exception:
            return _safe_text(value)
    except Exception:
        return _safe_text(value)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_bool(value: Any) -> bool:
    try:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y"}
        return bool(value)
    except Exception:
        return False


def _safe_optional_bool(value: Any) -> Optional[bool]:
    try:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "y"}:
                return True
            if normalized in {"0", "false", "no", "n"}:
                return False
    except Exception:
        return None
    return None
