from __future__ import annotations

import base64
import hashlib
import http.client
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode

import paramiko
import pytest

from aruba_mm_cleanup.cleanup import MmCleanupRunner, build_query_command
from aruba_mm_cleanup.gui_app import ArubaMmCleanupGui
from aruba_mm_cleanup.hostkeys import (
    HostKeyApprovalRequired,
    HostKeyChangedError,
    HostKeyObservation,
    KnownHostStore,
    ensure_host_key_trusted,
)
from aruba_mm_cleanup.models import (
    CleanupPlan,
    CleanupRunSummary,
    CleanupSettings,
    MmConnectionConfig,
    QueryResult,
    UserEntry,
)
from aruba_mm_cleanup.session import MmSession, ensure_unpaged_output
from aruba_mm_cleanup.validation import validate_connection_fields, validate_port, validate_timeout
from aruba_mm_cleanup.web_app import WebAppState, _make_handler, _render_page
from aruba_mm_cleanup.web_app import main as web_main
from aruba_mm_cleanup.web_support import WebRunRequest


class RecordingConnection:
    def __init__(self, responses=None, failures=None):
        self.responses = responses or {}
        self.failures = failures or {}
        self.commands = []
        self.disconnected = False

    def send_command_timing(self, *, command_string, **_kwargs):
        self.commands.append(command_string)
        if command_string in self.failures:
            raise self.failures[command_string]
        return self.responses.get(command_string, "")

    def disconnect(self):
        self.disconnected = True


def _observation(host: str, key: paramiko.PKey, *, port: int = 22) -> HostKeyObservation:
    digest = hashlib.sha256(key.asbytes()).digest()
    return HostKeyObservation(
        host=host,
        port=port,
        key_type=key.get_name(),
        fingerprint="SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("="),
        key=key,
    )


def test_connection_and_web_request_repr_hide_passwords(tmp_path):
    config = MmConnectionConfig(
        host="192.0.2.10",
        username="operator",
        password="ssh-private-value",
        enable_password="enable-private-value",
    )
    request = WebRunRequest(
        host=config.host,
        username=config.username,
        password=config.password,
        enable_password=config.enable_password,
        port=22,
        role="profiling",
        timeout=60,
        output_dir=tmp_path,
    )

    assert "ssh-private-value" not in repr(config)
    assert "enable-private-value" not in repr(config)
    assert "ssh-private-value" not in repr(request)
    assert "enable-private-value" not in repr(request)


@pytest.mark.parametrize("value", [True, 22.5, object(), " 22", "+22", "22.0"])
def test_port_validation_rejects_implicit_or_noncanonical_values(value):
    with pytest.raises(ValueError):
        validate_port(value)


@pytest.mark.parametrize("value", [True, 60.5, object(), " 60", "+60", "60.0", 601])
def test_timeout_validation_rejects_implicit_or_out_of_range_values(value):
    with pytest.raises(ValueError):
        validate_timeout(value)


def test_connection_validation_rejects_secret_string_subclasses():
    class SecretText(str):
        pass

    with pytest.raises(ValueError, match="암호"):
        validate_connection_fields(
            host="192.0.2.10",
            username="operator",
            password=SecretText("private"),
            port=22,
        )


@pytest.mark.parametrize("role", ["profile name", "profiling;reload", "profiling$(id)", "../profiling?"])
def test_role_validation_rejects_shell_and_cli_metacharacters(role):
    with pytest.raises(ValueError):
        build_query_command(role)


def test_known_hosts_approves_first_key_and_blocks_changed_key(tmp_path):
    store = KnownHostStore(tmp_path / "known_hosts")
    first = _observation("192.0.2.10", paramiko.RSAKey.generate(1024))
    changed = _observation("192.0.2.10", paramiko.RSAKey.generate(1024))

    assert store.check(first) == "unknown"
    store.approve(first)
    assert store.check(first) == "trusted"
    assert store.check(changed) == "changed"
    with pytest.raises(HostKeyChangedError):
        store.approve(changed)


def test_known_hosts_tracks_nonstandard_ports_independently(tmp_path):
    store = KnownHostStore(tmp_path / "known_hosts")
    port_22 = _observation("192.0.2.10", paramiko.RSAKey.generate(1024))
    port_2222 = _observation("192.0.2.10", paramiko.RSAKey.generate(1024), port=2222)

    store.approve(port_22)
    assert store.check(port_2222) == "unknown"
    store.approve(port_2222)
    assert store.check(port_22) == "trusted"
    assert store.check(port_2222) == "trusted"


def test_first_host_key_requires_explicit_callback_approval(monkeypatch, tmp_path):
    store = KnownHostStore(tmp_path / "known_hosts")
    observation = _observation("192.0.2.10", paramiko.RSAKey.generate(1024))
    config = MmConnectionConfig(host="192.0.2.10", username="operator", password="private")
    monkeypatch.setattr("aruba_mm_cleanup.hostkeys.probe_host_key", lambda *_args, **_kwargs: observation)

    with pytest.raises(HostKeyApprovalRequired):
        ensure_host_key_trusted(config, timeout=5, store=store)
    assert store.check(observation) == "unknown"

    seen = []
    assert (
        ensure_host_key_trusted(
            config,
            timeout=5,
            store=store,
            approval_callback=lambda item: seen.append(item) is None,
        )
        == observation
    )
    assert seen == [observation]
    assert store.check(observation) == "trusted"


def test_paging_failure_blocks_identity_and_later_commands():
    connection = RecordingConnection(failures={"no paging": RuntimeError("rejected")})
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=True,
    )

    with pytest.raises(RuntimeError, match="페이징 해제 실패"):
        session.run_command(
            MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
            CleanupSettings(),
            "show global-user-table list role profiling",
        )

    assert connection.commands == ["no paging"]
    assert connection.disconnected is True


def test_wrong_device_identity_blocks_query():
    connection = RecordingConnection(
        responses={
            "no paging": "",
            "show version": "ArubaOS-Switch 16.11 JL255A",
        }
    )
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=True,
    )

    with pytest.raises(RuntimeError, match="신원을 확인할 수 없어"):
        session.run_command(
            MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
            CleanupSettings(),
            "show global-user-table list role profiling",
        )

    assert connection.commands == ["no paging", "show version"]


def test_supported_mm_identity_allows_read_only_query():
    query_command = "show global-user-table list role profiling"
    connection = RecordingConnection(
        responses={
            "no paging": "",
            "show version": "ArubaOS (MODEL: ArubaMM), Version 8.10.0.0",
            query_command: "",
        }
    )
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=True,
    )

    assert (
        session.run_command(
            MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
            CleanupSettings(),
            query_command,
        )
        == ""
    )
    assert connection.commands == ["no paging", "show version", query_command]


@pytest.mark.parametrize("marker", ["--More--", "Press any key to continue", "<--- MORE --->"])
def test_paging_marker_in_query_output_is_fail_closed(marker):
    with pytest.raises(RuntimeError, match="페이징 표시"):
        ensure_unpaged_output(f"row one\n{marker}\nrow two")


def test_runner_without_target_approval_never_calls_delete(tmp_path):
    query = QueryResult(
        command="show global-user-table list role profiling",
        entries=[UserEntry(mac="02:00:00:00:00:01", role="profiling")],
    )
    runner = MmCleanupRunner(session=SimpleNamespace(disconnect=lambda **_kwargs: None))
    runner._query_users = lambda *_args, **_kwargs: query  # type: ignore[method-assign]
    runner._delete_macs = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("delete must not run without approval")
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
        CleanupSettings(delete_delay_seconds=0),
        output_dir=tmp_path,
    )

    assert summary.canceled is True
    assert summary.delete_results == []
    assert summary.target_macs == ["02:00:00:00:00:01"]


def test_runner_rejects_concurrent_execution(tmp_path):
    query = QueryResult(
        command="show global-user-table list role profiling",
        entries=[UserEntry(mac="02:00:00:00:00:01", role="profiling")],
    )
    runner = MmCleanupRunner(session=SimpleNamespace(disconnect=lambda **_kwargs: None))
    runner._query_users = lambda *_args, **_kwargs: query  # type: ignore[method-assign]
    approval_started = threading.Event()
    approval_release = threading.Event()

    def blocking_approval(_plan):
        approval_started.set()
        approval_release.wait(5)
        return False

    worker = threading.Thread(
        target=runner.run_once,
        args=(
            MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
            CleanupSettings(delete_delay_seconds=0),
        ),
        kwargs={"output_dir": tmp_path, "approve_targets": blocking_approval},
    )
    worker.start()
    assert approval_started.wait(2)
    try:
        with pytest.raises(RuntimeError, match="다른 조회 또는 삭제 작업"):
            runner.run_once(
                MmConnectionConfig(host="192.0.2.10", username="operator", password="private"),
                CleanupSettings(delete_delay_seconds=0),
                output_dir=tmp_path,
                approve_targets=lambda _plan: True,
            )
    finally:
        approval_release.set()
        worker.join(5)
    assert worker.is_alive() is False


def test_gui_run_path_passes_explicit_target_approval_each_cycle(tmp_path):
    approvals = []
    summaries = []
    target = "02:00:00:00:00:01"

    class FakeRunner:
        def run_once(self, config, settings, **kwargs):
            plan = CleanupPlan(
                plan_id="opaque",
                created_at=datetime.now(),
                host=config.host,
                port=config.port,
                username=config.username,
                role=settings.role,
                query_command="show global-user-table list role profiling",
                queried_count=1,
                target_macs=(target,),
            )
            assert kwargs["approve_targets"](plan) is True
            approvals.append(plan.target_macs)
            return CleanupRunSummary(started_at=datetime.now(), role=settings.role)

    app = SimpleNamespace(
        runner=FakeRunner(),
        runner_lock=threading.Lock(),
        _confirm_targets_from_worker=lambda _plan: True,
        _should_cancel_run=lambda: False,
        _enqueue_event=lambda event, payload: summaries.append((event, payload)) or True,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="operator", password="private")
    settings = CleanupSettings()

    assert ArubaMmCleanupGui._run_summary(app, config, settings, tmp_path) is True
    assert ArubaMmCleanupGui._run_summary(app, config, settings, tmp_path) is True
    assert approvals == [(target,), (target,)]
    assert [event for event, _payload in summaries] == ["summary", "summary"]


def test_web_page_has_csrf_and_preview_first_flow(tmp_path):
    state = WebAppState(output_dir=tmp_path)
    html = _render_page(state)

    assert state.csrf_token in html
    assert 'action="/preview"' in html
    assert 'action="/run"' not in html
    assert 'name="password"' in html


def test_web_server_has_no_non_loopback_bind_option():
    with pytest.raises(SystemExit):
        web_main(["--host", "0.0.0.0"])


def test_web_rejects_non_loopback_host_header_and_missing_csrf(tmp_path):
    state = WebAppState(output_dir=tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request(
            "POST",
            "/disconnect",
            body=urlencode({}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert "CSRF" in response.read().decode("utf-8")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_web_requires_preview_phrase_and_exact_fresh_snapshot(tmp_path):
    target = "02:00:00:00:00:01"

    class FakeRunner:
        def __init__(self):
            self.executed = False
            self.fresh_target = target

        def query_users(self, *_args, **_kwargs):
            return QueryResult(
                command="show global-user-table list role profiling",
                entries=[UserEntry(mac=target, role="profiling")],
            )

        def run_once(self, config, settings, **kwargs):
            plan = CleanupPlan(
                plan_id="opaque",
                created_at=datetime.now(),
                host=config.host,
                port=config.port,
                username=config.username,
                role=settings.role,
                query_command="show global-user-table list role profiling",
                queried_count=1,
                target_macs=(self.fresh_target,),
            )
            self.executed = kwargs["approve_targets"](plan)
            return CleanupRunSummary(
                started_at=datetime.now(),
                role=settings.role,
                queried_count=1,
                delete_success_count=1 if self.executed else 0,
                canceled=not self.executed,
            )

        def close_session(self, **_kwargs):
            return None

    state = WebAppState(output_dir=tmp_path)
    fake_runner = FakeRunner()
    state.runner = fake_runner  # type: ignore[assignment]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        preview_form = urlencode(
            {
                "csrf_token": state.csrf_token,
                "host": "192.0.2.10",
                "port": "22",
                "username": "operator",
                "password": "private",
                "enable_password": "",
                "role": "profiling",
                "timeout": "60",
                "output_dir": str(tmp_path),
            }
        )
        connection.request(
            "POST",
            "/preview",
            body=preview_form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 200
        preview_html = response.read().decode("utf-8")
        assert target in preview_html
        assert "DELETE 1" in preview_html

        connection.request(
            "POST",
            "/run",
            body=urlencode({"csrf_token": state.csrf_token, "confirmation": "DELETE 2"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        assert fake_runner.executed is False

        fake_runner.fresh_target = "02:00:00:00:00:02"
        connection.request(
            "POST",
            "/run",
            body=urlencode({"csrf_token": state.csrf_token, "confirmation": "DELETE 1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert fake_runner.executed is False

        fake_runner.fresh_target = target
        connection.request(
            "POST",
            "/preview",
            body=preview_form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()

        connection.request(
            "POST",
            "/run",
            body=urlencode({"csrf_token": state.csrf_token, "confirmation": "DELETE 1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert fake_runner.executed is True
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
