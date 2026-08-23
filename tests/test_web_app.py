from pathlib import Path
from types import SimpleNamespace

import pytest

from aruba_mm_cleanup.models import CleanupRunSummary
from aruba_mm_cleanup.web_app import WebAppState, _render_page
from aruba_mm_cleanup.web_app import main as web_main
from aruba_mm_cleanup.web_support import (
    cleanup_settings_from_request,
    connection_config_from_request,
    parse_run_request,
    smoke_status,
    summary_view,
)


def test_web_smoke_main_returns_marker(capsys):
    assert web_main(["--smoke"]) == 0

    assert smoke_status() in capsys.readouterr().out


def test_web_run_request_builds_config_and_settings(tmp_path):
    request = parse_run_request(
        {
            "host": [" 192.0.2.10 "],
            "port": ["2222"],
            "username": [" admin "],
            "password": ["secret"],
            "enable_password": ["enable-secret"],
            "role": [" profiling "],
            "timeout": ["7"],
            "output_dir": [str(tmp_path / "outputs")],
        }
    )

    config = connection_config_from_request(request)
    settings = cleanup_settings_from_request(request)

    assert config.host == "192.0.2.10"
    assert config.port == 2222
    assert config.username == "admin"
    assert config.password == "secret"
    assert config.enable_password == "enable-secret"
    assert settings.role == "profiling"
    assert settings.timeout == 7
    assert settings.delete_delay_seconds == 0
    assert request.output_dir == tmp_path / "outputs"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("host", "", "MM 주소"),
        ("username", "", "계정"),
        ("password", "", "암호"),
        ("port", "0", "SSH 포트"),
        ("timeout", "0", "장비 응답 대기"),
        ("role", "profiling\nshow version", "Role"),
    ],
)
def test_web_run_request_rejects_invalid_required_values(field, value, message):
    form = {
        "host": ["192.0.2.10"],
        "port": ["22"],
        "username": ["admin"],
        "password": ["secret"],
        "role": ["profiling"],
        "timeout": ["60"],
    }
    form[field] = [value]

    with pytest.raises(ValueError) as exc_info:
        parse_run_request(form)

    assert message in str(exc_info.value)


def test_web_summary_view_tolerates_missing_and_bad_values():
    class BadInt:
        def __int__(self):
            raise RuntimeError("bad int")

    summary = CleanupRunSummary(started_at=SimpleNamespace(), role="profiling")  # type: ignore[arg-type]
    summary.queried_count = BadInt()  # type: ignore[assignment]
    summary.delete_success_count = 2
    summary.audit_path = Path("outputs/cleanup_summary.json")

    view = summary_view(summary)

    assert view["queried_count"] == 0
    assert view["delete_success_count"] == 2
    assert view["audit_path"] == "outputs/cleanup_summary.json"


def test_web_page_uses_cumulative_dashboard_labels(tmp_path):
    state = WebAppState(output_dir=tmp_path / "outputs")
    state.cumulative_queried_count = 7
    state.cumulative_deleted_count = 3
    state.last_summary = {
        "queried_count": 2,
        "delete_success_count": 1,
        "delete_failure_count": 1,
        "remaining_count": 1,
        "reappeared_count": 0,
    }

    html = _render_page(state)

    assert "누적 조회 MAC" in html
    assert "누적 삭제 MAC" in html
    assert "<div>조회 MAC</div>" not in html
    assert "<div>삭제 완료</div>" not in html
    assert "최근 실행" in html
