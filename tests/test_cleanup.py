import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import aruba_mm_cleanup.cleanup as cleanup_module
from aruba_mm_cleanup.cleanup import (
    HISTORY_FILE_NAME,
    MmCleanupRunner,
    append_history_records,
    build_delete_command,
    build_query_command,
    classify_delete_response,
    write_audit_summary,
)
from aruba_mm_cleanup.connection import connect_to_mm, run_command
from aruba_mm_cleanup.models import (
    CleanupRunSummary,
    CleanupSettings,
    DeleteResult,
    MmConnectionConfig,
    ParseDecision,
    QueryResult,
    UserEntry,
)
from aruba_mm_cleanup.session import MmSession


class FakeConnection:
    def __init__(self, responses=None, failures=None):
        self.responses = responses or {}
        self.failures = failures or {}
        self.commands = []
        self.disconnected = False

    def send_command_timing(self, *, command_string, **_kwargs):
        self.commands.append(command_string)
        if command_string in self.failures:
            raise self.failures[command_string]
        response = self.responses.get(command_string, "")
        if isinstance(response, list):
            return response.pop(0)
        return response

    def disconnect(self):
        self.disconnected = True


class BadErrorText(Exception):
    def __str__(self):
        raise RuntimeError("bad error text")

    def __repr__(self):
        raise RuntimeError("bad error repr")


class FailingDisconnectConnection(FakeConnection):
    def disconnect(self):
        self.disconnected = True
        raise RuntimeError("disconnect failed")


class UnprintableDisconnectConnection(FakeConnection):
    def disconnect(self):
        self.disconnected = True
        raise BadErrorText()


class InvalidUnprintableDisconnectConnection:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True
        raise BadErrorText()


class MissingCommandConnection:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


class FailingCommandAttributeConnection:
    def __init__(self):
        self.disconnected = False

    @property
    def send_command_timing(self):
        raise RuntimeError("bad command attribute")

    def disconnect(self):
        self.disconnected = True


def test_run_command_passes_device_response_timeout_as_read_timeout():
    class TimeoutRecordingConnection:
        def __init__(self):
            self.calls = []

        def send_command_timing(self, *, command_string, **kwargs):
            self.calls.append((command_string, kwargs))
            return "ok"

    connection = TimeoutRecordingConnection()

    assert run_command(connection, "show version", timeout=17) == "ok"
    assert connection.calls == [
        (
            "show version",
            {
                "strip_prompt": False,
                "strip_command": False,
                "cmd_verify": False,
                "read_timeout": 17,
            },
        )
    ]


def test_query_result_macs_tolerates_unreadable_entries():
    class UnreadableEntries(list):
        def __iter__(self):
            raise RuntimeError("bad entries")

    query = QueryResult(command="show", entries=UnreadableEntries([UserEntry(mac="aa:bb:cc:00:00:01")]))

    assert query.macs == []


def test_query_result_macs_skips_entries_with_failing_mac_access():
    class FailingMacEntry:
        @property
        def mac(self):
            raise RuntimeError("bad mac")

    query = QueryResult(
        command="show",
        entries=[FailingMacEntry(), UserEntry(mac="aa:bb:cc:00:00:02")],  # type: ignore[list-item]
    )

    assert query.macs == ["aa:bb:cc:00:00:02"]


def test_build_commands_use_role_and_mac():
    assert build_query_command("profiling") == "show global-user-table list role profiling"
    assert build_delete_command("aa:bb:cc:00:00:01") == "aaa user delete mac aa:bb:cc:00:00:01"


def test_build_delete_command_rejects_invalid_mac():
    try:
        build_delete_command("not-a-mac")
    except ValueError as exc:
        assert "MAC" in str(exc)
    else:
        raise AssertionError("build_delete_command should reject invalid MAC values")


def test_build_delete_command_rejects_missing_mac_without_attribute_error():
    try:
        build_delete_command(None)  # type: ignore[arg-type]
    except ValueError as exc:
        assert "MAC" in str(exc)
    else:
        raise AssertionError("build_delete_command should reject missing MAC values")


def test_build_delete_command_rejects_unstrippable_mac_without_runtime_error():
    class BadMac(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    try:
        build_delete_command(BadMac("aa:bb:cc:00:00:01"))  # type: ignore[arg-type]
    except ValueError as exc:
        assert "MAC" in str(exc)
    else:
        raise AssertionError("build_delete_command should reject unstrippable MAC values")


def test_build_query_command_rejects_control_characters_in_role():
    try:
        build_query_command("profiling\nshow version")
    except ValueError as exc:
        assert "Role" in str(exc)
    else:
        raise AssertionError("build_query_command should reject role control characters")


def test_build_query_command_rejects_unstrippable_role_without_runtime_error():
    class BadRole(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    try:
        build_query_command(BadRole("profiling"))  # type: ignore[arg-type]
    except ValueError as exc:
        assert "Role" in str(exc)
    else:
        raise AssertionError("build_query_command should reject unstrippable role values")


def test_build_query_command_rejects_uniterable_role_without_runtime_error():
    class BadRole(str):
        def strip(self, *_args, **_kwargs):
            return self

        def __iter__(self):
            raise RuntimeError("bad iter")

    try:
        build_query_command(BadRole("profiling"))  # type: ignore[arg-type]
    except ValueError as exc:
        assert "Role" in str(exc)
    else:
        raise AssertionError("build_query_command should reject uniterable role values")


def test_build_query_command_rejects_unformattable_role_without_runtime_error():
    class BadRole(str):
        def strip(self, *_args, **_kwargs):
            return self

        def __format__(self, _format_spec):
            raise RuntimeError("bad format")

    try:
        build_query_command(BadRole("profiling"))  # type: ignore[arg-type]
    except ValueError as exc:
        assert "Role" in str(exc)
    else:
        raise AssertionError("build_query_command should reject unformattable role values")


def test_build_query_command_rejects_missing_role_without_attribute_error():
    try:
        build_query_command(None)  # type: ignore[arg-type]
    except ValueError as exc:
        assert "Role" in str(exc)
    else:
        raise AssertionError("build_query_command should reject missing role values")


def test_connect_to_mm_closes_connection_when_enable_fails(monkeypatch, tmp_path):
    class EnableFailingConnection(FakeConnection):
        def enable(self):
            raise RuntimeError("enable failed")

    connection = EnableFailingConnection()
    captured_params = {}

    def fake_connect_handler(**params):
        captured_params.update(params)
        return connection

    monkeypatch.setitem(sys.modules, "netmiko", SimpleNamespace(ConnectHandler=fake_connect_handler))
    monkeypatch.setattr("aruba_mm_cleanup.connection.ensure_host_key_trusted", lambda *_args, **_kwargs: None)
    known_hosts_store = SimpleNamespace(ensure_file=lambda: tmp_path / "known_hosts")

    config = MmConnectionConfig(
        host="192.0.2.10",
        username="admin",
        password="secret",
        enable_password="enable-secret",
    )

    try:
        connect_to_mm(config, timeout=7, known_hosts_store=known_hosts_store)
    except RuntimeError as exc:
        assert str(exc) == "enable failed"
    else:
        raise AssertionError("connect_to_mm should re-raise enable failure")

    assert connection.disconnected is True
    assert captured_params["host"] == "192.0.2.10"
    assert captured_params["secret"] == "enable-secret"
    assert captured_params["timeout"] == 7
    assert captured_params["conn_timeout"] == 7
    assert captured_params["auth_timeout"] == 7
    assert captured_params["banner_timeout"] == 7
    assert captured_params["ssh_strict"] is True
    assert captured_params["alt_host_keys"] is True
    assert captured_params["fast_cli"] is False


def test_session_disconnect_failure_is_reported_and_session_is_cleared():
    connection = FailingDisconnectConnection()
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    assert session.run_command(config, settings, "show version") == ""

    session.disconnect(progress_callback=lambda event, payload: events.append((event, payload)), reason="manual")

    assert connection.disconnected is True
    assert session.is_connected is False
    assert ("warning", {"message": "disconnect failed: disconnect failed", "reason": "manual"}) in events
    assert ("session_disconnected", {"reason": "manual"}) in events


def test_session_disconnect_unprintable_failure_is_reported_and_session_is_cleared():
    connection = UnprintableDisconnectConnection()
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    assert session.run_command(config, settings, "show version") == ""

    session.disconnect(progress_callback=lambda event, payload: events.append((event, payload)), reason="manual")

    assert connection.disconnected is True
    assert session.is_connected is False
    assert ("warning", {"message": "disconnect failed: BadErrorText", "reason": "manual"}) in events
    assert ("session_disconnected", {"reason": "manual"}) in events


def test_session_disconnect_waits_for_inflight_command():
    command = "show version"
    command_started = threading.Event()
    command_release = threading.Event()
    disconnect_attempted = threading.Event()
    disconnected = threading.Event()

    class BlockingConnection(FakeConnection):
        def send_command_timing(self, *, command_string, **_kwargs):
            self.commands.append(command_string)
            if command_string == command:
                command_started.set()
                assert command_release.wait(timeout=2)
            return ""

        def disconnect(self):
            self.disconnected = True
            disconnected.set()

    connection = BlockingConnection(responses={"no paging": "", command: ""})
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)
    result = {}

    def run_command():
        result["output"] = session.run_command(config, settings, command)

    def disconnect_session():
        disconnect_attempted.set()
        session.disconnect(reason="manual")

    command_thread = threading.Thread(target=run_command)
    command_thread.start()
    assert command_started.wait(timeout=2)

    disconnect_thread = threading.Thread(target=disconnect_session)
    disconnect_thread.start()
    assert disconnect_attempted.wait(timeout=2)
    assert not disconnected.wait(timeout=0.05)

    command_release.set()
    command_thread.join(timeout=2)
    disconnect_thread.join(timeout=2)

    assert result["output"] == ""
    assert connection.commands == ["no paging", command]
    assert connection.disconnected is True
    assert session.is_connected is False


def test_session_rejects_invalid_connection_object_and_clears_state():
    connection = MissingCommandConnection()
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,  # type: ignore[arg-type]
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    try:
        session.run_command(
            config,
            settings,
            "show version",
            progress_callback=lambda event, payload: events.append((event, payload)),
        )
    except RuntimeError as exc:
        assert "MM 연결 객체" in str(exc)
    else:
        raise AssertionError("session should reject invalid connection objects")

    assert connection.disconnected is True
    assert session.is_connected is False
    assert ("connect_start", {"host": "192.0.2.10"}) in events
    assert not any(event == "connect_done" for event, _payload in events)


def test_session_cleans_up_when_command_attribute_check_fails():
    connection = FailingCommandAttributeConnection()
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,  # type: ignore[arg-type]
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    try:
        session.run_command(
            config,
            settings,
            "show version",
            progress_callback=lambda event, payload: events.append((event, payload)),
        )
    except RuntimeError as exc:
        assert "MM 연결 객체" in str(exc)
    else:
        raise AssertionError("session should reject broken connection attribute access")

    assert connection.disconnected is True
    assert session.is_connected is False
    assert ("connect_start", {"host": "192.0.2.10"}) in events
    assert not any(event == "connect_done" for event, _payload in events)


def test_session_invalid_connection_unprintable_cleanup_failure_is_reported_and_rejected():
    connection = InvalidUnprintableDisconnectConnection()
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,  # type: ignore[arg-type]
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    try:
        session.run_command(
            config,
            settings,
            "show version",
            progress_callback=lambda event, payload: events.append((event, payload)),
        )
    except RuntimeError as exc:
        assert "MM 연결 객체" in str(exc)
    else:
        raise AssertionError("session should reject invalid connection objects")

    assert connection.disconnected is True
    assert session.is_connected is False
    assert ("warning", {"message": "invalid connection cleanup failed: BadErrorText"}) in events
    assert not any(event == "connect_done" for event, _payload in events)


def test_session_no_paging_unprintable_failure_warns_and_allows_command():
    class BadErrorText(Exception):
        def __str__(self):
            raise RuntimeError("bad error text")

        def __repr__(self):
            raise RuntimeError("bad error repr")

    command = "show version"
    connection = FakeConnection(responses={command: "ok"}, failures={"no paging": BadErrorText()})
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    assert session.run_command(
        config,
        settings,
        command,
        progress_callback=lambda event, payload: events.append((event, payload)),
    ) == "ok"

    assert session.is_connected is True
    assert connection.commands == ["no paging", command]
    assert ("warning", {"message": "no paging failed: BadErrorText"}) in events


def test_query_users_keeps_result_when_session_close_fails():
    class FailingCloseSession:
        def __init__(self):
            self.disconnect_called = False

        def run_command(self, *_args, **_kwargs):
            return "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"

        def disconnect(self, *_args, **_kwargs):
            self.disconnect_called = True
            raise RuntimeError("close failed")

    session = FailingCloseSession()
    events = []
    runner = MmCleanupRunner(session=session)

    query = runner.query_users(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert query.macs == ["aa:bb:cc:00:00:01"]
    assert session.disconnect_called is True
    assert ("warning", {"message": "session close failed: close failed", "reason": "run_complete"}) in events


def test_query_users_progress_tolerates_failing_parse_result_fields(monkeypatch):
    class FailingEntry:
        @property
        def mac(self):
            raise RuntimeError("bad entry mac")

        @property
        def type_na(self):
            raise RuntimeError("bad entry type")

    class FailingDecision:
        @property
        def line_number(self):
            raise RuntimeError("bad line")

        @property
        def action(self):
            raise RuntimeError("bad action")

        @property
        def reason(self):
            raise RuntimeError("bad reason")

        @property
        def mac(self):
            raise RuntimeError("bad mac")

        @property
        def role(self):
            raise RuntimeError("bad role")

        @property
        def user_type(self):
            raise RuntimeError("bad type")

        @property
        def type_na(self):
            raise RuntimeError("bad type flag")

    good_entry = UserEntry(mac="aa:bb:cc:00:00:02", type_na=True)
    good_decision = ParseDecision(
        line_number=2,
        action="selected",
        reason="matched",
        mac="aa:bb:cc:00:00:02",
        role="profiling",
        user_type="N/A",
        type_na=True,
    )

    monkeypatch.setattr(
        cleanup_module,
        "parse_global_user_table_explained",
        lambda *_args, **_kwargs: SimpleNamespace(
            entries=[FailingEntry(), good_entry],
            decisions=[FailingDecision(), good_decision],
        ),
    )
    session = SimpleNamespace(
        run_command=lambda *_args, **_kwargs: "raw output",
        disconnect=lambda **_kwargs: None,
    )
    events = []
    runner = MmCleanupRunner(session=session)

    query = runner.query_users(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    payload = next(payload for event, payload in events if event == "query_done")
    assert len(query.entries) == 2
    assert payload["macs"] == ["aa:bb:cc:00:00:02"]
    assert payload["type_na_macs"] == ["aa:bb:cc:00:00:02"]
    assert payload["parse_decisions"][0]["line_number"] == 0
    assert payload["parse_decisions"][0]["action"] == ""
    assert payload["parse_decisions"][1]["mac"] == "aa:bb:cc:00:00:02"


def test_run_once_deletes_snapshot_and_verifies_remaining(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    verify_query = ""
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, verify_query],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
            "aaa user delete mac aa:bb:cc:00:00:02": "User deleted",
        }
    )
    connections = [connection]
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=1),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.queried_count == 2
    assert summary.delete_success_count == 2
    assert summary.delete_failure_count == 0
    assert summary.remaining_count == 0
    assert [item.status for item in summary.delete_results] == ["verified_deleted", "verified_deleted"]
    assert summary.audit_path and summary.audit_path.exists()
    assert summary.history_path and summary.history_path.exists()
    assert connections == []
    assert connection.disconnected is True
    assert connection.commands == [
        "no paging",
        "show global-user-table list role profiling",
        "aaa user delete mac aa:bb:cc:00:00:01",
        "aaa user delete mac aa:bb:cc:00:00:02",
        "show global-user-table list role profiling",
    ]
    assert any(event == "countdown" and payload["remaining"] == 1 for event, payload in events)


def test_run_once_tolerates_query_entries_length_failure(tmp_path):
    class UnreadableLengthEntries(list):
        def __len__(self):
            raise RuntimeError("bad entries length")

        def __bool__(self):
            raise RuntimeError("bad entries bool")

    first_query = QueryResult(
        command="show",
        entries=UnreadableLengthEntries([UserEntry(mac="aa:bb:cc:00:00:01")]),
    )
    verify_query = QueryResult(command="show", entries=UnreadableLengthEntries([]))
    queries = [first_query, verify_query]
    runner = MmCleanupRunner(sleep_func=lambda _seconds: None)
    runner._query_users = lambda *_args, **_kwargs: queries.pop(0)  # type: ignore[method-assign]
    runner._delete_macs = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        DeleteResult(
            mac="aa:bb:cc:00:00:01",
            success=True,
            command="cmd",
            status="deleted",
            response_status="deleted",
        )
    ]

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.error == ""
    assert summary.queried_count == 1
    assert summary.target_macs == ["aa:bb:cc:00:00:01"]
    assert summary.delete_success_count == 1


def test_run_once_records_partial_delete_failure(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [
                first_query,
                "192.0.2.11 aa:bb:cc:00:00:02 user-b profiling",
            ],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
            "aaa user delete mac aa:bb:cc:00:00:02": "Error: not found",
        }
    )
    connections = [connection]
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.delete_success_count == 1
    assert summary.delete_failure_count == 1
    assert summary.remaining_count == 1
    assert summary.delete_results[1].error == "Error: not found"
    assert connections == []
    assert connection.disconnected is True


def test_run_once_zero_delete_delay_starts_delete_after_countdown_zero(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.delete_success_count == 1
    countdown_events = [payload["remaining"] for event, payload in events if event == "countdown"]
    assert countdown_events == [0]
    assert "aaa user delete mac aa:bb:cc:00:00:01" in connection.commands


def test_run_once_cancels_when_delete_delay_conversion_fails(tmp_path):
    class BadDelay:
        def __int__(self):
            raise RuntimeError("bad delay")

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary_settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)
    object.__setattr__(summary_settings, "delete_delay_seconds", BadDelay())

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        summary_settings,
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.canceled is True
    assert summary.error == ""
    assert summary.remaining_count == 1
    assert summary.delete_results == []
    assert "aaa user delete mac aa:bb:cc:00:00:01" not in connection.commands
    assert any(event == "delete_canceled" and payload["count"] == 1 for event, payload in events)


def test_run_once_reports_type_na_macs_without_blocking_delete(tmp_path):
    header = f"{'IP':<16}{'MAC Address':<21}{'User':<14}{'Role':<12}{'Type':<8}{'BSSID'}"
    first_query = "\n".join(
        [
            header,
            f"{'192.0.2.10':<16}{'aa:bb:cc:00:00:01':<21}{'user-a':<14}{'profiling':<12}{'N/A':<8}{'11:22:33:44:55:66'}",
        ]
    )
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert "aaa user delete mac aa:bb:cc:00:00:01" in connection.commands
    query_done_payload = next(payload for event, payload in events if event == "query_done" and payload["macs"])
    assert query_done_payload["type_na_macs"] == ["aa:bb:cc:00:00:01"]
    assert any(item.mac == "aa:bb:cc:00:00:01" and item.type_na for item in summary.query_parse_decisions)
    audit = json.loads(summary.audit_path.read_text(encoding="utf-8"))
    selected = [item for item in audit["query_parse_decisions"] if item["action"] == "selected"]
    assert selected[0]["user_type"] == "N/A"
    assert selected[0]["type_na"] is True


def test_run_once_flags_successfully_deleted_mac_that_reappears(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    verify_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, verify_query],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
            "aaa user delete mac aa:bb:cc:00:00:02": "Error: not found",
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 2
    assert summary.remaining_count == 2
    assert summary.reappeared_count == 1
    assert summary.delete_results[0].status == "reappeared"
    assert summary.reappeared_macs == ["aa:bb:cc:00:00:01"]
    assert any(
        event == "reappeared_macs" and payload["macs"] == ["aa:bb:cc:00:00:01"]
        for event, payload in events
    )

    audit = json.loads(summary.audit_path.read_text(encoding="utf-8"))
    assert audit["reappeared_count"] == 1
    assert audit["reappeared_macs"] == ["aa:bb:cc:00:00:01"]


def test_reappeared_deleted_macs_tolerates_unreadable_items():
    class FailingStatusResult(DeleteResult):
        def __getattribute__(self, name):
            if name == "status":
                raise RuntimeError("bad status")
            return super().__getattribute__(name)

    class FailingMacResult(DeleteResult):
        def __getattribute__(self, name):
            if name == "mac":
                raise RuntimeError("bad mac")
            return super().__getattribute__(name)

    results = [
        FailingStatusResult(
            mac="aa:bb:cc:00:00:01",
            success=False,
            command="cmd",
            status="reappeared",
        ),
        FailingMacResult(
            mac="aa:bb:cc:00:00:02",
            success=False,
            command="cmd",
            status="reappeared",
        ),
        DeleteResult(
            mac="aa:bb:cc:00:00:03",
            success=False,
            command="cmd",
            status="reappeared",
        ),
    ]

    assert cleanup_module._reappeared_deleted_macs(results, []) == ["aa:bb:cc:00:00:03"]


def test_run_once_verification_handles_malformed_delete_result_mac(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        DeleteResult(
            mac=["aa:bb:cc:00:00:01"],  # type: ignore[arg-type]
            success=True,
            command="cmd",
            status="deleted",
            response_status="deleted",
        )
    ]

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.error == ""
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 1
    assert summary.delete_results[0].status == "unknown"
    assert summary.delete_results[0].verified_absent is None
    assert "삭제 결과 MAC 오류" in summary.delete_results[0].error


def test_run_once_verification_handles_unprintable_delete_result_status(tmp_path):
    class BadStatus:
        def __bool__(self):
            raise RuntimeError("bad status bool")

        def __str__(self):
            raise RuntimeError("bad status str")

        def __repr__(self):
            raise RuntimeError("bad status repr")

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        DeleteResult(
            mac="aa:bb:cc:00:00:01",
            success=True,
            command="cmd",
            status="",
            response_status=BadStatus(),  # type: ignore[arg-type]
        )
    ]

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.error == ""
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 1
    assert summary.delete_results[0].status == "unknown"
    assert summary.delete_results[0].verified_absent is True


def test_apply_verification_tolerates_unreadable_delete_result_command():
    class FailingCommandResult(DeleteResult):
        def __getattribute__(self, name):
            if name == "command":
                raise RuntimeError("bad command")
            return super().__getattribute__(name)

    result = FailingCommandResult(
        mac="aa:bb:cc:00:00:01",
        success=True,
        command="cmd",
        status="deleted",
        response_status="deleted",
    )

    verified = cleanup_module._apply_verification([result], [])

    assert verified[0].mac == "aa:bb:cc:00:00:01"
    assert verified[0].command == ""
    assert verified[0].success is True
    assert verified[0].status == "verified_deleted"
    assert verified[0].verified_absent is True


def test_run_once_verification_tolerates_unreadable_delete_results(tmp_path):
    class UnreadableDeleteResults(list):
        def __iter__(self):
            raise RuntimeError("bad delete result iterator")

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: UnreadableDeleteResults(  # type: ignore[method-assign]
        [
            DeleteResult(
                mac="aa:bb:cc:00:00:01",
                success=True,
                command="cmd",
                status="deleted",
                response_status="deleted",
            )
        ]
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.error == ""
    assert summary.delete_results == []
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 0


def test_run_once_verification_treats_malformed_delete_result_as_failure(tmp_path):
    class MalformedDeleteResult:
        mac = "aa:bb:cc:00:00:01"
        command = "cmd"

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: [MalformedDeleteResult()]  # type: ignore[method-assign]

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert summary.error == ""
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 1
    assert summary.delete_results[0].mac == "aa:bb:cc:00:00:01"
    assert summary.delete_results[0].status == "unknown"
    assert summary.delete_results[0].verified_absent is None
    assert "삭제 결과 형식 오류" in summary.delete_results[0].error


def test_run_once_can_cancel_during_countdown(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    query_conn = FakeConnection(
        responses={"no paging": "", "show global-user-table list role profiling": [first_query]}
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    checks = iter([False, True])

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=3),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        should_cancel=lambda: next(checks, True),
    )

    assert summary.canceled is True
    assert summary.delete_results == []
    assert summary.remaining_count == 1
    assert query_conn.disconnected is True


def test_run_once_cancels_when_cancel_check_fails_during_countdown(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    query_conn = FakeConnection(
        responses={"no paging": "", "show global-user-table list role profiling": [first_query]}
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    def failing_cancel_check():
        raise RuntimeError("cancel check failed")

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=3),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
        should_cancel=failing_cancel_check,
    )

    assert summary.canceled is True
    assert summary.error == ""
    assert summary.remaining_count == 1
    assert summary.delete_results == []
    assert "aaa user delete mac aa:bb:cc:00:00:01" not in query_conn.commands
    assert any(event == "delete_canceled" and payload["count"] == 1 for event, payload in events)


def test_run_once_cancels_when_countdown_sleep_fails(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    query_conn = FakeConnection(
        responses={"no paging": "", "show global-user-table list role profiling": [first_query]}
    )
    events = []

    def failing_sleep(_seconds):
        raise RuntimeError("sleep interrupted")

    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=failing_sleep,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=3),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.canceled is True
    assert summary.error == ""
    assert summary.remaining_count == 1
    assert summary.delete_results == []
    assert summary.audit_path and summary.audit_path.exists()
    assert "aaa user delete mac aa:bb:cc:00:00:01" not in query_conn.commands
    assert any(event == "delete_canceled" and payload["count"] == 1 for event, payload in events)


def test_run_once_can_cancel_during_delete_loop_before_next_mac(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    checks = iter([False, False, True])

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        should_cancel=lambda: next(checks, True),
    )

    assert summary.canceled is True
    assert summary.verification_skipped is True
    assert len(summary.delete_results) == 1
    assert connection.commands.count("aaa user delete mac aa:bb:cc:00:00:01") == 1
    assert "aaa user delete mac aa:bb:cc:00:00:02" not in connection.commands
    assert connection.commands.count("show global-user-table list role profiling") == 1


def test_run_once_cancels_when_cancel_check_fails_during_delete_loop(tmp_path):
    first_query = (
        "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling\n"
        "192.0.2.11 aa:bb:cc:00:00:02 user-b profiling"
    )
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    first_check = True

    def cancel_check():
        nonlocal first_check
        if first_check:
            first_check = False
            return False
        raise RuntimeError("cancel check failed")

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
        should_cancel=cancel_check,
    )

    assert summary.canceled is True
    assert summary.error == ""
    assert summary.verification_skipped is True
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 0
    assert summary.remaining_count == 2
    assert summary.delete_results == []
    assert "aaa user delete mac aa:bb:cc:00:00:01" not in connection.commands
    assert "aaa user delete mac aa:bb:cc:00:00:02" not in connection.commands
    assert any(event == "delete_canceled" and payload["count"] == 2 for event, payload in events)


def test_run_once_skips_verify_when_canceled_after_delete_loop(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    checks = iter([False, False, True])

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        should_cancel=lambda: next(checks, True),
    )

    assert summary.canceled is True
    assert summary.verification_skipped is True
    assert connection.commands.count("show global-user-table list role profiling") == 1


def test_run_once_cancel_after_delete_tolerates_unreadable_delete_results(tmp_path):
    class UnreadableDeleteResults(list):
        def __iter__(self):
            raise RuntimeError("bad delete result iterator")

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: UnreadableDeleteResults(  # type: ignore[method-assign]
        [
            DeleteResult(
                mac="aa:bb:cc:00:00:01",
                success=True,
                command="cmd",
                status="deleted",
                response_status="deleted",
            )
        ]
    )
    checks = iter([False, False, True])

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        should_cancel=lambda: next(checks, True),
    )

    assert summary.error == ""
    assert summary.canceled is True
    assert summary.verification_skipped is True
    assert summary.delete_results == []
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 0


def test_run_once_cancel_after_delete_treats_unreadable_success_as_failure(tmp_path):
    class BadSuccess:
        def __bool__(self):
            raise RuntimeError("bad success bool")

    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query],
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )
    runner._delete_macs = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        DeleteResult(
            mac="aa:bb:cc:00:00:01",
            success=BadSuccess(),  # type: ignore[arg-type]
            command="cmd",
            status="unknown",
            response_status="unknown",
        )
    ]
    checks = iter([False, False, True])

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        should_cancel=lambda: next(checks, True),
    )

    assert summary.error == ""
    assert summary.canceled is True
    assert summary.delete_success_count == 0
    assert summary.delete_failure_count == 1


def test_count_delete_results_tolerates_unreadable_container():
    class UnreadableDeleteResults(list):
        def __iter__(self):
            raise RuntimeError("bad delete result iterator")

    results = UnreadableDeleteResults(
        [
            DeleteResult(
                mac="aa:bb:cc:00:00:01",
                success=True,
                command="cmd",
                status="deleted",
                response_status="deleted",
            )
        ]
    )

    assert cleanup_module._count_delete_results(results) == (0, 0)


def test_zero_query_writes_audit_without_delete(tmp_path):
    query_conn = FakeConnection(responses={"no paging": "", "show global-user-table list role profiling": ""})
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=Path(tmp_path),
    )

    assert summary.queried_count == 0
    assert summary.delete_success_count == 0
    assert summary.audit_path and summary.audit_path.exists()
    assert query_conn.disconnected is True


def test_run_once_keeps_summary_when_session_close_fails(tmp_path):
    class FailingCloseSession:
        def __init__(self):
            self.disconnect_called = False

        def run_command(self, *_args, **_kwargs):
            return ""

        def disconnect(self, *_args, **_kwargs):
            self.disconnect_called = True
            raise RuntimeError("close failed")

    session = FailingCloseSession()
    events = []
    runner = MmCleanupRunner(session=session, sleep_func=lambda _seconds: None)

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.queried_count == 0
    assert summary.audit_path and summary.audit_path.exists()
    assert session.disconnect_called is True
    assert ("warning", {"message": "session close failed: close failed", "reason": "run_complete"}) in events


def test_non_string_query_response_is_reported_without_delete(tmp_path):
    query_conn = FakeConnection(responses={"no paging": "", "show global-user-table list role profiling": None})
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=Path(tmp_path),
    )

    assert "장비 조회 응답" in summary.error
    assert summary.delete_results == []
    assert query_conn.commands == ["no paging", "show global-user-table list role profiling"]
    assert query_conn.disconnected is True


def test_run_once_records_unprintable_top_level_error(tmp_path):
    class BadErrorText(Exception):
        def __str__(self):
            raise RuntimeError("bad error text")

        def __repr__(self):
            raise RuntimeError("bad error repr")

    events = []
    runner = MmCleanupRunner(sleep_func=lambda _seconds: None)
    runner._query_users = lambda *_args, **_kwargs: (_ for _ in ()).throw(BadErrorText())  # type: ignore[method-assign]

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=Path(tmp_path),
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == "BadErrorText"
    assert summary.delete_results == []
    assert summary.audit_path and summary.audit_path.exists()
    assert any(event == "run_error" and payload["error"] == "BadErrorText" for event, payload in events)


def test_progress_callback_failure_does_not_abort_run(tmp_path):
    query_conn = FakeConnection(responses={"no paging": "", "show global-user-table list role profiling": ""})
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: query_conn,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    def failing_progress(_event, _payload):
        raise RuntimeError("progress failed")

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=failing_progress,
    )

    assert summary.error == ""
    assert summary.queried_count == 0
    assert query_conn.commands == ["no paging", "show global-user-table list role profiling"]
    assert query_conn.disconnected is True


def test_persistent_runner_reuses_session_until_closed(tmp_path):
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, "", first_query, ""],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    factory_calls = []
    runner = MmCleanupRunner(
        connection_factory=lambda config, _timeout: factory_calls.append(config) or connection,
        enforce_connection_safety=False,
        persistent_session=True,
        sleep_func=lambda _seconds: None,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    first_summary = runner.run_once(
        config,
        settings,
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )
    second_summary = runner.run_once(
        config,
        settings,
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
    )

    assert first_summary.delete_success_count == 1
    assert second_summary.delete_success_count == 1
    assert len(factory_calls) == 1
    assert connection.disconnected is False

    runner.close_session()

    assert connection.disconnected is True


def test_persistent_runner_survives_repeated_query_reconnects(tmp_path):
    query_command = build_query_command("profiling")

    class DropOnQueryConnection(FakeConnection):
        def __init__(self, *, drop_on_query_number: int):
            super().__init__(
                responses={
                    "no paging": "",
                    query_command: ["192.0.2.10 aa:bb:cc:00:00:01 user-a profiling", ""],
                    "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
                }
            )
            self.query_count = 0
            self.drop_on_query_number = drop_on_query_number

        def send_command_timing(self, *, command_string, **kwargs):
            if command_string == query_command:
                self.query_count += 1
                if self.query_count == self.drop_on_query_number:
                    self.commands.append(command_string)
                    raise RuntimeError("socket closed")
            return super().send_command_timing(command_string=command_string, **kwargs)

    first_connection = DropOnQueryConnection(drop_on_query_number=3)
    second_connection = DropOnQueryConnection(drop_on_query_number=3)
    third_connection = DropOnQueryConnection(drop_on_query_number=99)
    connections = [first_connection, second_connection, third_connection]
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        persistent_session=True,
        sleep_func=lambda _seconds: None,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    summaries = []
    for _index in range(3):
        summaries.append(
            runner.run_once(
                config,
                settings,
                output_dir=tmp_path,
        approve_targets=lambda _plan: True,
                progress_callback=lambda event, payload: events.append((event, payload)),
            )
        )

    assert [summary.error for summary in summaries] == ["", "", ""]
    assert [summary.delete_success_count for summary in summaries] == [1, 1, 1]
    assert [summary.remaining_count for summary in summaries] == [0, 0, 0]
    assert connections == []
    assert first_connection.disconnected is True
    assert second_connection.disconnected is True
    assert third_connection.disconnected is False
    assert sum(1 for event, _payload in events if event == "session_reconnect_start") == 2
    assert len(list(tmp_path.glob("*/cleanup_summary.json"))) == 3
    history_path = tmp_path / HISTORY_FILE_NAME
    assert len(history_path.read_text(encoding="utf-8").splitlines()) == 3

    runner.close_session()

    assert third_connection.disconnected is True


def test_stale_session_reconnects_and_retries_command_once(tmp_path):
    query_command = build_query_command("profiling")
    stale_connection = FakeConnection(responses={"no paging": ""}, failures={query_command: RuntimeError("socket closed")})
    fresh_connection = FakeConnection(responses={"no paging": "", query_command: ""})
    connections = [stale_connection, fresh_connection]
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.queried_count == 0
    assert connections == []
    assert stale_connection.disconnected is True
    assert fresh_connection.disconnected is True
    assert stale_connection.commands == ["no paging", query_command]
    assert fresh_connection.commands == ["no paging", query_command]
    assert any(event == "session_reconnect_start" for event, _payload in events)


def test_stale_session_retries_even_when_initial_error_text_fails(tmp_path):
    class BadErrorText(Exception):
        def __str__(self):
            raise RuntimeError("bad error text")

        def __repr__(self):
            raise RuntimeError("bad error repr")

    query_command = build_query_command("profiling")
    stale_connection = FakeConnection(responses={"no paging": ""}, failures={query_command: BadErrorText()})
    fresh_connection = FakeConnection(responses={"no paging": "", query_command: ""})
    connections = [stale_connection, fresh_connection]
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.queried_count == 0
    assert connections == []
    assert stale_connection.disconnected is True
    assert fresh_connection.disconnected is True
    assert stale_connection.commands == ["no paging", query_command]
    assert fresh_connection.commands == ["no paging", query_command]
    reconnect_payload = next(payload for event, payload in events if event == "session_reconnect_start")
    assert reconnect_payload["error"] == "BadErrorText"


def test_reconnect_failure_reports_initial_and_retry_errors(tmp_path):
    query_command = build_query_command("profiling")
    stale_connection = FakeConnection(responses={"no paging": ""}, failures={query_command: RuntimeError("socket closed")})
    factory_calls = []
    events = []

    def failing_factory(config, _timeout):
        factory_calls.append(config)
        if len(factory_calls) == 1:
            return stale_connection
        raise RuntimeError("reconnect denied")

    runner = MmCleanupRunner(
        connection_factory=failing_factory,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert "socket closed" in summary.error
    assert "reconnect denied" in summary.error
    assert stale_connection.disconnected is True
    assert len(factory_calls) == 2
    assert any(
        event == "session_reconnect_start" and payload["error"] == "socket closed"
        for event, payload in events
    )


def test_session_disconnects_retry_connection_after_retry_command_failure():
    command = "show version"
    stale_connection = FakeConnection(responses={"no paging": ""}, failures={command: RuntimeError("socket closed")})
    retry_connection = FakeConnection(responses={"no paging": ""}, failures={command: RuntimeError("socket still closed")})
    connections = [stale_connection, retry_connection]
    events = []
    session = MmSession(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
    )
    config = MmConnectionConfig(host="192.0.2.10", username="admin", password="secret")
    settings = CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0)

    try:
        session.run_command(
            config,
            settings,
            command,
            progress_callback=lambda event, payload: events.append((event, payload)),
        )
    except RuntimeError as exc:
        assert "socket closed" in str(exc)
        assert "socket still closed" in str(exc)
    else:
        raise AssertionError("session retry command failure should be reported")

    assert stale_connection.disconnected is True
    assert retry_connection.disconnected is True
    assert session.is_connected is False
    assert ("session_disconnected", {"reason": "command_failed"}) in events


def test_delete_macs_sends_one_command_per_normalized_mac():
    connection = FakeConnection(
        responses={
            "no paging": "",
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        ["AA-BB-CC-00-00-01", "aa:bb:cc:00:00:01", "aabb.cc00.0001"],
        None,
    )

    assert len(results) == 1
    assert results[0].success is True
    assert connection.commands.count("aaa user delete mac aa:bb:cc:00:00:01") == 1


def test_delete_macs_skips_unstrippable_mac_items():
    class BadMac(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    connection = FakeConnection(
        responses={
            "no paging": "",
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        [BadMac("aa:bb:cc:00:00:02"), "aa:bb:cc:00:00:01"],  # type: ignore[list-item]
        None,
    )

    assert len(results) == 1
    assert results[0].mac == "aa:bb:cc:00:00:01"
    assert results[0].success is True
    assert connection.commands == ["no paging", "aaa user delete mac aa:bb:cc:00:00:01"]


def test_delete_macs_tolerates_invalid_mac_container():
    connection = FakeConnection(responses={"no paging": ""})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        object(),  # type: ignore[arg-type]
        lambda event, payload: events.append((event, payload)),
    )

    assert results == []
    assert connection.commands == []
    assert ("delete_batch_start", {"count": 0}) in events


def test_delete_macs_tolerates_unreadable_mac_container():
    class UnreadableMacs(list):
        def __iter__(self):
            raise RuntimeError("bad mac iterator")

    connection = FakeConnection(responses={"no paging": ""})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        UnreadableMacs(["aa:bb:cc:00:00:01"]),  # type: ignore[arg-type]
        lambda event, payload: events.append((event, payload)),
    )

    assert results == []
    assert connection.commands == []
    assert ("delete_batch_start", {"count": 0}) in events


def test_delete_macs_records_invalid_mac_without_sending_command():
    connection = FakeConnection(responses={"no paging": ""})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        ["not-a-mac"],
        lambda event, payload: events.append((event, payload)),
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].status == "unknown"
    assert "MAC" in results[0].error
    assert connection.commands == []
    assert any(event == "delete_unknown" for event, _payload in events)


def test_delete_macs_skips_missing_mac_values_without_sending_command():
    connection = FakeConnection(responses={"no paging": ""})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        [None, ""],  # type: ignore[list-item]
        lambda event, payload: events.append((event, payload)),
    )

    assert results == []
    assert connection.commands == []
    assert ("delete_batch_start", {"count": 0}) in events


def test_delete_command_exception_is_unknown_without_retry():
    command = "aaa user delete mac aa:bb:cc:00:00:01"
    connection = FakeConnection(responses={"no paging": ""}, failures={command: RuntimeError("socket timeout")})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        ["aa:bb:cc:00:00:01"],
        lambda event, payload: events.append((event, payload)),
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].status == "unknown"
    assert "확인 필요" in results[0].error
    assert connection.commands.count(command) == 1
    assert not any(event == "session_reconnect_start" for event, _payload in events)
    assert any(event == "delete_unknown" for event, _payload in events)


def test_delete_command_exception_with_unprintable_error_is_unknown_without_retry():
    class BadErrorText(Exception):
        def __str__(self):
            raise RuntimeError("bad error text")

        def __repr__(self):
            raise RuntimeError("bad error repr")

    failed_command = "aaa user delete mac aa:bb:cc:00:00:01"
    next_command = "aaa user delete mac aa:bb:cc:00:00:02"
    stale_connection = FakeConnection(responses={"no paging": ""}, failures={failed_command: BadErrorText()})
    fresh_connection = FakeConnection(responses={"no paging": "", next_command: "User deleted"})
    connections = [stale_connection, fresh_connection]
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
        lambda event, payload: events.append((event, payload)),
    )

    assert [item.status for item in results] == ["unknown", "deleted"]
    assert results[0].error == "확인 필요: 삭제 명령 응답 실패 - BadErrorText"
    assert stale_connection.commands.count(failed_command) == 1
    assert stale_connection.disconnected is True
    assert fresh_connection.commands == ["no paging", next_command]
    assert connections == []
    assert not any(event == "session_reconnect_start" for event, _payload in events)
    assert any(
        event == "delete_unknown" and payload["error"] == "확인 필요: 삭제 명령 응답 실패 - BadErrorText"
        for event, payload in events
    )


def test_delete_command_exception_closes_session_before_next_mac_without_retry():
    failed_command = "aaa user delete mac aa:bb:cc:00:00:01"
    next_command = "aaa user delete mac aa:bb:cc:00:00:02"
    stale_connection = FakeConnection(
        responses={"no paging": ""},
        failures={failed_command: RuntimeError("socket timeout")},
    )
    fresh_connection = FakeConnection(responses={"no paging": "", next_command: "User deleted"})
    connections = [stale_connection, fresh_connection]
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connections.pop(0),
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    results = runner._delete_macs(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
        None,
    )

    assert [item.status for item in results] == ["unknown", "deleted"]
    assert stale_connection.commands.count(failed_command) == 1
    assert stale_connection.disconnected is True
    assert fresh_connection.commands == ["no paging", next_command]
    assert connections == []


def test_classify_delete_response_handles_failure_unknown_and_success():
    assert classify_delete_response("User deleted") == ("deleted", "")
    assert classify_delete_response("Permission denied") == ("failed", "Permission denied")
    assert classify_delete_response("Invalid input detected at '^' marker.") == (
        "failed",
        "Invalid input detected at '^' marker.",
    )
    assert classify_delete_response("") == ("unknown", "확인 필요: 삭제 명령 응답이 비어 있음")
    status, error = classify_delete_response("aaa user delete mac aa:bb:cc:00:00:01")
    assert status == "unknown"
    assert "판정 불가" in error


def test_classify_delete_response_handles_non_string_output():
    status, error = classify_delete_response({"unexpected": "response"})  # type: ignore[arg-type]

    assert status == "unknown"
    assert "삭제 명령 응답 판정 불가" in error


def test_classify_delete_response_handles_unprintable_non_string_output():
    class BadResponse:
        def __str__(self):
            raise RuntimeError("bad str")

        def __repr__(self):
            raise RuntimeError("bad repr")

    status, error = classify_delete_response(BadResponse())  # type: ignore[arg-type]

    assert status == "unknown"
    assert "삭제 명령 응답 판정 불가" in error


def test_audit_save_failure_does_not_break_summary(tmp_path):
    blocked_output_dir = tmp_path / "not-a-directory"
    blocked_output_dir.write_text("file blocks directory creation", encoding="utf-8")
    connection = FakeConnection(responses={"no paging": "", "show global-user-table list role profiling": ""})
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=blocked_output_dir,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.queried_count == 0
    assert summary.audit_path is None
    assert summary.audit_error
    assert any(event == "warning" and "audit summary save failed" in payload["message"] for event, payload in events)


def test_audit_summary_tolerates_malformed_internal_items(tmp_path):
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role=object())  # type: ignore[arg-type]
    summary.query_command = object()  # type: ignore[assignment]
    summary.queried_count = object()  # type: ignore[assignment]
    summary.delete_success_count = "2"  # type: ignore[assignment]
    summary.delete_failure_count = object()  # type: ignore[assignment]
    summary.remaining_count = object()  # type: ignore[assignment]
    summary.reappeared_count = object()  # type: ignore[assignment]
    summary.canceled = "true"  # type: ignore[assignment]
    summary.verification_skipped = "false"  # type: ignore[assignment]
    summary.audit_error = object()  # type: ignore[assignment]
    summary.history_error = object()  # type: ignore[assignment]
    summary.error = object()  # type: ignore[assignment]
    summary.target_macs = ["aa:bb:cc:00:00:01", object(), None]  # type: ignore[list-item]
    summary.reappeared_macs = ["aa:bb:cc:00:00:02", object()]  # type: ignore[list-item]
    summary.query_parse_decisions = [
        ParseDecision(1, "selected", "selected_identity_mac_before_role", mac="aa:bb:cc:00:00:01"),
        object(),  # type: ignore[list-item]
    ]
    summary.verify_parse_decisions = [
        {"line_number": "bad", "action": object(), "type_na": "false"},  # type: ignore[list-item]
    ]
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=False, command="cmd", error=object()),  # type: ignore[arg-type]
        {"mac": "aa:bb:cc:00:00:02", "success": "true", "command": object(), "verified_absent": "true"},  # type: ignore[list-item]
    ]

    path = write_audit_summary(summary, output_dir=tmp_path, host=object())  # type: ignore[arg-type]

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["host"]
    assert audit["role"]
    assert audit["query_command"]
    assert audit["queried_count"] == 0
    assert audit["delete_success_count"] == 2
    assert audit["delete_failure_count"] == 0
    assert audit["remaining_count"] == 0
    assert audit["reappeared_count"] == 0
    assert audit["canceled"] is True
    assert audit["verification_skipped"] is False
    assert audit["audit_error"]
    assert audit["history_error"]
    assert audit["error"]
    assert audit["target_macs"][0] == "aa:bb:cc:00:00:01"
    assert audit["target_macs"][1]
    assert audit["target_macs"][2] == ""
    assert audit["reappeared_macs"][0] == "aa:bb:cc:00:00:02"
    assert audit["reappeared_macs"][1]
    assert audit["query_parse_decisions"][0]["mac"] == "aa:bb:cc:00:00:01"
    assert audit["query_parse_decisions"][1]["line_number"] == 0
    assert audit["verify_parse_decisions"][0]["line_number"] == 0
    assert audit["verify_parse_decisions"][0]["type_na"] is False
    assert audit["delete_results"][0]["status"] == "failed"
    assert audit["delete_results"][0]["error"]
    assert audit["delete_results"][1]["success"] is True
    assert audit["delete_results"][1]["verified_absent"] is True


def test_audit_summary_tolerates_unprintable_text_values(tmp_path):
    class BadText:
        def __str__(self):
            raise RuntimeError("bad str")

        def __repr__(self):
            raise RuntimeError("bad repr")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role=BadText())  # type: ignore[arg-type]
    summary.query_command = BadText()  # type: ignore[assignment]
    summary.audit_error = BadText()  # type: ignore[assignment]
    summary.target_macs = [BadText()]  # type: ignore[list-item]
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=False, command="cmd", error=BadText()),  # type: ignore[arg-type]
    ]

    path = write_audit_summary(summary, output_dir=tmp_path, host=BadText())  # type: ignore[arg-type]

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["host"] == ""
    assert audit["role"] == ""
    assert audit["query_command"] == ""
    assert audit["audit_error"] == ""
    assert audit["target_macs"] == [""]
    assert audit["delete_results"][0]["error"] == ""


def test_audit_summary_tolerates_failing_item_value_access(tmp_path):
    class FailingMapping(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("bad mapping get")

    class FailingAttrs:
        @property
        def line_number(self):
            raise RuntimeError("bad line")

        @property
        def action(self):
            raise RuntimeError("bad action")

        @property
        def reason(self):
            raise RuntimeError("bad reason")

        @property
        def mac(self):
            raise RuntimeError("bad mac")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.query_parse_decisions = [FailingMapping({"line_number": 9, "action": "selected"})]  # type: ignore[list-item]
    summary.verify_parse_decisions = [FailingAttrs()]  # type: ignore[list-item]
    summary.delete_results = [FailingMapping({"mac": "aa:bb:cc:00:00:01", "success": True})]  # type: ignore[list-item]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["query_parse_decisions"][0]["line_number"] == 0
    assert audit["query_parse_decisions"][0]["action"] == ""
    assert audit["verify_parse_decisions"][0]["line_number"] == 0
    assert audit["verify_parse_decisions"][0]["mac"] == ""
    assert audit["delete_results"][0]["mac"] == ""
    assert audit["delete_results"][0]["success"] is False
    assert audit["delete_results"][0]["status"] == "failed"


def test_audit_summary_tolerates_failing_scalar_conversions(tmp_path):
    class BadInt:
        def __int__(self):
            raise RuntimeError("bad int")

    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.queried_count = BadInt()  # type: ignore[assignment]
    summary.delete_success_count = BadInt()  # type: ignore[assignment]
    summary.canceled = BadBool()  # type: ignore[assignment]
    summary.verification_skipped = BadBool()  # type: ignore[assignment]
    summary.query_parse_decisions = [{"line_number": BadInt(), "type_na": BadBool()}]  # type: ignore[list-item]
    summary.delete_results = [{"mac": "aa:bb:cc:00:00:01", "success": BadBool()}]  # type: ignore[list-item]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["queried_count"] == 0
    assert audit["delete_success_count"] == 0
    assert audit["canceled"] is False
    assert audit["verification_skipped"] is False
    assert audit["query_parse_decisions"][0]["line_number"] == 0
    assert audit["query_parse_decisions"][0]["type_na"] is False
    assert audit["delete_results"][0]["success"] is False
    assert audit["delete_results"][0]["status"] == "failed"


def test_audit_summary_tolerates_failing_optional_bool_conversion(tmp_path):
    class BadOptionalBool(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        {
            "mac": "aa:bb:cc:00:00:01",
            "success": True,
            "verified_absent": BadOptionalBool("true"),
        }
    ]  # type: ignore[list-item]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["delete_results"][0]["success"] is True
    assert audit["delete_results"][0]["verified_absent"] is None


def test_summary_writes_tolerate_malformed_started_at(tmp_path):
    class BadStartedAt:
        def isoformat(self, *_args, **_kwargs):
            raise TypeError("bad isoformat")

        def strftime(self, _format):
            raise TypeError("bad strftime")

        def __str__(self):
            return "bad-started-at"

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.started_at = BadStartedAt()  # type: ignore[assignment]
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]

    audit_path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")
    history_path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert audit_path.parent.name == "bad-started-at"
    assert audit["started_at"] == "bad-started-at"
    assert history[-1]["run_at"] == "bad-started-at"


def test_summary_run_dir_tolerates_unstrippable_started_at_text(tmp_path):
    class BadPathText(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    class BadStartedAt:
        def isoformat(self, *_args, **_kwargs):
            return BadPathText("bad-started-at")

        def strftime(self, _format):
            raise TypeError("bad strftime")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.started_at = BadStartedAt()  # type: ignore[assignment]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    assert path.parent.name == "unknown-started-at"
    assert path.exists()


def test_audit_summary_tolerates_invalid_list_containers(tmp_path):
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.target_macs = "aa:bb:cc:00:00:01"  # type: ignore[assignment]
    summary.reappeared_macs = object()  # type: ignore[assignment]
    summary.query_parse_decisions = None  # type: ignore[assignment]
    summary.verify_parse_decisions = "not-a-decision-list"  # type: ignore[assignment]
    summary.delete_results = object()  # type: ignore[assignment]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["target_macs"] == []
    assert audit["reappeared_macs"] == []
    assert audit["query_parse_decisions"] == []
    assert audit["verify_parse_decisions"] == []
    assert audit["delete_results"] == []


def test_audit_summary_tolerates_unreadable_list_containers(tmp_path):
    class UnreadableList(list):
        def __iter__(self):
            raise RuntimeError("bad list iterator")

    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.target_macs = UnreadableList(["aa:bb:cc:00:00:01"])  # type: ignore[assignment]
    summary.reappeared_macs = UnreadableList(["aa:bb:cc:00:00:02"])  # type: ignore[assignment]
    summary.query_parse_decisions = UnreadableList(
        [ParseDecision(line_number=1, action="selected", reason="matched", mac="aa:bb:cc:00:00:01")]
    )  # type: ignore[assignment]
    summary.verify_parse_decisions = UnreadableList(
        [ParseDecision(line_number=1, action="selected", reason="matched", mac="aa:bb:cc:00:00:02")]
    )  # type: ignore[assignment]
    summary.delete_results = UnreadableList(
        [DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd")]
    )  # type: ignore[assignment]

    path = write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")

    audit = json.loads(path.read_text(encoding="utf-8"))
    assert audit["target_macs"] == []
    assert audit["reappeared_macs"] == []
    assert audit["query_parse_decisions"] == []
    assert audit["verify_parse_decisions"] == []
    assert audit["delete_results"] == []


def test_history_append_tolerates_invalid_delete_result_container(tmp_path):
    history_path = tmp_path / HISTORY_FILE_NAME
    original_content = json.dumps({"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}) + "\n"
    history_path.write_text(original_content, encoding="utf-8")
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = object()  # type: ignore[assignment]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    assert path is None
    assert history_path.read_text(encoding="utf-8") == original_content


def test_history_append_tolerates_failing_delete_results_access(tmp_path):
    class FailingDeleteResultsSummary:
        @property
        def delete_results(self):
            raise RuntimeError("bad delete results")

    history_path = tmp_path / HISTORY_FILE_NAME
    original_content = json.dumps({"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}) + "\n"
    history_path.write_text(original_content, encoding="utf-8")

    path = append_history_records(
        FailingDeleteResultsSummary(),
        output_dir=tmp_path,
        host="192.0.2.10",
    )  # type: ignore[arg-type]

    assert path is None
    assert history_path.read_text(encoding="utf-8") == original_content


def test_history_append_tolerates_failing_started_at_access(tmp_path):
    class FailingStartedAtSummary:
        role = "profiling"
        delete_results = [
            DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
        ]

        @property
        def started_at(self):
            raise RuntimeError("bad started_at")

    path = append_history_records(
        FailingStartedAtSummary(),
        output_dir=tmp_path,
        host="192.0.2.10",
    )  # type: ignore[arg-type]

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[-1]["run_at"] == ""
    assert history[-1]["mac"] == "aa:bb:cc:00:00:01"


def test_history_append_skips_invalid_delete_result_items(tmp_path):
    history_path = tmp_path / HISTORY_FILE_NAME
    original_record = {"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}
    history_path.write_text(json.dumps(original_record) + "\n", encoding="utf-8")
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        object(),  # type: ignore[list-item]
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[0] == original_record
    assert history[1]["mac"] == "aa:bb:cc:00:00:01"
    assert history[1]["result"] == "삭제 완료"


def test_history_append_tolerates_failing_role_access(tmp_path):
    class FailingRoleSummary:
        started_at = datetime(2026, 7, 2, 13, 0, 0)
        delete_results = [
            DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
        ]

        @property
        def role(self):
            raise RuntimeError("bad role")

    path = append_history_records(FailingRoleSummary(), output_dir=tmp_path, host="192.0.2.10")  # type: ignore[arg-type]

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[-1]["mac"] == "aa:bb:cc:00:00:01"
    assert history[-1]["role"] == ""
    assert history[-1]["result"] == "삭제 완료"


def test_history_append_treats_unreadable_success_as_failure(tmp_path):
    class BadSuccess:
        def __bool__(self):
            raise RuntimeError("bad success bool")

    result = DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd")
    object.__setattr__(result, "success", BadSuccess())
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [result]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[-1]["mac"] == "aa:bb:cc:00:00:01"
    assert history[-1]["success"] is False
    assert history[-1]["status"] == "failed"
    assert history[-1]["result"] == "삭제 실패"


def test_history_append_tolerates_unserializable_optional_fields(tmp_path):
    class BadOptionalBool(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    result = DeleteResult(
        mac="aa:bb:cc:00:00:01",
        success=True,
        command="cmd",
        response_status="deleted",
        verified_absent=True,
    )
    object.__setattr__(result, "response_status", object())
    object.__setattr__(result, "verified_absent", BadOptionalBool("true"))
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [result]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[-1]["mac"] == "aa:bb:cc:00:00:01"
    assert history[-1]["response_status"]
    assert history[-1]["verified_absent"] is None


def test_history_append_tolerates_failing_delete_result_error_access(tmp_path):
    class FailingErrorDeleteResult(DeleteResult):
        def __getattribute__(self, name):
            if name == "error":
                raise RuntimeError("bad error")
            return super().__getattribute__(name)

    result = FailingErrorDeleteResult(
        mac="aa:bb:cc:00:00:01",
        success=False,
        command="cmd",
        status="unknown",
        response_status="unknown",
    )
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [result]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[-1]["mac"] == "aa:bb:cc:00:00:01"
    assert history[-1]["result"] == "확인 필요"
    assert history[-1]["error"] == ""


def test_run_once_audit_unprintable_write_failure_keeps_summary(tmp_path, monkeypatch):
    def failing_write_audit_summary(*_args, **_kwargs):
        raise BadErrorText()

    monkeypatch.setattr("aruba_mm_cleanup.cleanup.write_audit_summary", failing_write_audit_summary)
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": "",
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.audit_error == "BadErrorText"
    assert summary.queried_count == 0
    assert ("warning", {"message": "audit summary save failed: BadErrorText"}) in events


def test_run_once_history_unprintable_write_failure_keeps_summary(tmp_path, monkeypatch):
    def failing_append_history_records(*_args, **_kwargs):
        raise BadErrorText()

    monkeypatch.setattr("aruba_mm_cleanup.cleanup.append_history_records", failing_append_history_records)
    first_query = "192.0.2.10 aa:bb:cc:00:00:01 user-a profiling"
    connection = FakeConnection(
        responses={
            "no paging": "",
            "show global-user-table list role profiling": [first_query, ""],
            "aaa user delete mac aa:bb:cc:00:00:01": "User deleted",
        }
    )
    events = []
    runner = MmCleanupRunner(
        connection_factory=lambda _config, _timeout: connection,
        enforce_connection_safety=False,
        sleep_func=lambda _seconds: None,
    )

    summary = runner.run_once(
        MmConnectionConfig(host="192.0.2.10", username="admin", password="secret"),
        CleanupSettings(role="profiling", timeout=5, delete_delay_seconds=0),
        output_dir=tmp_path,
        approve_targets=lambda _plan: True,
        progress_callback=lambda event, payload: events.append((event, payload)),
    )

    assert summary.error == ""
    assert summary.history_error == "BadErrorText"
    assert summary.delete_success_count == 1
    assert summary.audit_path and summary.audit_path.exists()
    assert ("warning", {"message": "deletion history save failed: BadErrorText"}) in events


def test_audit_summary_write_failure_does_not_leave_partial_final_file(tmp_path, monkeypatch):
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    original_write_text = Path.write_text

    def failing_tmp_write(path, data, *args, **kwargs):
        if path.name == "cleanup_summary.json.tmp":
            original_write_text(path, data[:20], *args, **kwargs)
            raise OSError("disk full")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_tmp_write)

    try:
        write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")
    except OSError as exc:
        assert "disk full" in str(exc)
    else:
        raise AssertionError("write_audit_summary should report write failure")

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "cleanup_summary.json").exists()
    assert not (run_dirs[0] / "cleanup_summary.json.tmp").exists()


def test_audit_summary_replace_failure_removes_tmp_file(tmp_path, monkeypatch):
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    original_replace = Path.replace

    def failing_summary_replace(path, target):
        if path.name == "cleanup_summary.json.tmp":
            raise OSError("replace denied")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_summary_replace)

    try:
        write_audit_summary(summary, output_dir=tmp_path, host="192.0.2.10")
    except OSError as exc:
        assert "replace denied" in str(exc)
    else:
        raise AssertionError("write_audit_summary should report replace failure")

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "cleanup_summary.json").exists()
    assert not (run_dirs[0] / "cleanup_summary.json.tmp").exists()


def test_audit_summary_uses_unique_run_dir_when_summary_path_exists(tmp_path):
    first_summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    second_summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")

    first_path = write_audit_summary(first_summary, output_dir=tmp_path, host="192.0.2.10")
    second_path = write_audit_summary(second_summary, output_dir=tmp_path, host="192.0.2.11")

    assert first_path != second_path
    assert first_path.parent.name == "20260702_130000_000000"
    assert second_path.parent.name == "20260702_130000_000000-1"
    assert json.loads(first_path.read_text(encoding="utf-8"))["host"] == "192.0.2.10"
    assert json.loads(second_path.read_text(encoding="utf-8"))["host"] == "192.0.2.11"


def test_history_append_converts_non_serializable_error(tmp_path):
    class NonSerializableError:
        def __str__(self):
            return "non-serializable error"

    history_path = tmp_path / HISTORY_FILE_NAME
    original_content = json.dumps({"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}) + "\n"
    history_path.write_text(original_content, encoding="utf-8")
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
        DeleteResult(
            mac="aa:bb:cc:00:00:02",
            success=False,
            command="cmd",
            error=NonSerializableError(),  # type: ignore[arg-type]
        ),
    ]

    append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    records = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert records[0] == {"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}
    assert records[1]["mac"] == "aa:bb:cc:00:00:01"
    assert records[2]["mac"] == "aa:bb:cc:00:00:02"
    assert records[2]["error"] == "non-serializable error"


def test_history_append_streams_existing_history_without_whole_file_read(tmp_path, monkeypatch):
    history_path = tmp_path / HISTORY_FILE_NAME
    original_record = {"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}
    history_path.write_text(json.dumps(original_record) + "\n", encoding="utf-8")
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]

    def fail_whole_file_read(_path):
        raise AssertionError("history file should not be read into memory at once")

    monkeypatch.setattr(Path, "read_bytes", fail_whole_file_read)

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    history = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert history[0] == original_record
    assert history[1]["mac"] == "aa:bb:cc:00:00:01"


def test_history_append_write_failure_does_not_leave_partial_record(tmp_path, monkeypatch):
    history_path = tmp_path / HISTORY_FILE_NAME
    original_content = b'{"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}\n'
    history_path.write_bytes(original_content)
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]
    original_open = Path.open

    class FailingHistoryAppend:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def writelines(self, lines):
            with original_open(history_path, "a", encoding="utf-8") as handle:
                handle.write(lines[0][:20])
            raise OSError("disk full")

    class FailingHistoryTmpWrite:
        tmp_path = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, data):
            tmp_path = self.tmp_path
            assert tmp_path is not None
            with original_open(tmp_path, "wb") as handle:
                handle.write(data[:20])
            raise OSError("disk full")

    def failing_history_open(path, mode="r", *args, **kwargs):
        if path == history_path and "a" in mode:
            return FailingHistoryAppend()
        if path.name.startswith(f"{history_path.name}.") and path.name.endswith(".tmp") and "w" in mode:
            writer = FailingHistoryTmpWrite()
            writer.tmp_path = path
            return writer
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_history_open)

    try:
        append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")
    except OSError as exc:
        assert "disk full" in str(exc)
    else:
        raise AssertionError("append_history_records should report write failure")

    assert history_path.read_bytes() == original_content
    assert not list(tmp_path.glob(f"{history_path.name}.*.tmp"))


def test_history_append_replace_failure_preserves_existing_history(tmp_path, monkeypatch):
    history_path = tmp_path / HISTORY_FILE_NAME
    original_content = b'{"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}\n'
    history_path.write_bytes(original_content)
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]
    original_replace = Path.replace

    def failing_tmp_replace(path, target):
        if path.name.startswith(f"{history_path.name}.") and path.name.endswith(".tmp"):
            raise OSError("replace denied")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_tmp_replace)

    try:
        append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")
    except OSError as exc:
        assert "replace denied" in str(exc)
    else:
        raise AssertionError("append_history_records should report replace failure")

    assert history_path.read_bytes() == original_content
    assert not list(tmp_path.glob(f"{history_path.name}.*.tmp"))


def test_history_append_does_not_overwrite_stale_shared_tmp_file(tmp_path):
    history_path = tmp_path / HISTORY_FILE_NAME
    stale_tmp = history_path.with_name(f"{history_path.name}.tmp")
    history_path.write_text(json.dumps({"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}) + "\n", encoding="utf-8")
    stale_tmp.write_text("stale temp content", encoding="utf-8")
    summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]

    path = append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")

    assert path == history_path
    assert stale_tmp.read_text(encoding="utf-8") == "stale temp content"
    assert not list(tmp_path.glob(f"{history_path.name}.*.tmp"))
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert [record["mac"] for record in history] == ["aa:bb:cc:00:00:ff", "aa:bb:cc:00:00:01"]


def test_history_append_serializes_same_process_writers(tmp_path, monkeypatch):
    history_path = tmp_path / HISTORY_FILE_NAME
    history_path.write_text(json.dumps({"run_at": "existing", "mac": "aa:bb:cc:00:00:ff"}) + "\n", encoding="utf-8")
    first_summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 0, 0), role="profiling")
    first_summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:01", success=True, command="cmd"),
    ]
    second_summary = CleanupRunSummary(started_at=datetime(2026, 7, 2, 13, 1, 0), role="profiling")
    second_summary.delete_results = [
        DeleteResult(mac="aa:bb:cc:00:00:02", success=True, command="cmd"),
    ]
    original_copyfileobj = cleanup_module.shutil.copyfileobj
    copy_call_count = 0
    copy_call_lock = threading.Lock()
    first_copy_started = threading.Event()
    second_thread_started = threading.Event()
    second_copy_started = threading.Event()
    release_first_copy = threading.Event()
    errors = []

    def blocking_copyfileobj(source, destination, *args, **kwargs):
        nonlocal copy_call_count
        with copy_call_lock:
            copy_call_count += 1
            call_number = copy_call_count
        result = original_copyfileobj(source, destination, *args, **kwargs)
        if call_number == 1:
            first_copy_started.set()
            if not release_first_copy.wait(timeout=2):
                raise AssertionError("first history copy was not released")
        elif call_number == 2:
            second_copy_started.set()
        return result

    def append_summary(summary, started_event=None):
        if started_event is not None:
            started_event.set()
        try:
            append_history_records(summary, output_dir=tmp_path, host="192.0.2.10")
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(cleanup_module.shutil, "copyfileobj", blocking_copyfileobj)

    first_thread = threading.Thread(target=append_summary, args=(first_summary,))
    second_thread = threading.Thread(target=append_summary, args=(second_summary, second_thread_started))
    try:
        first_thread.start()
        assert first_copy_started.wait(timeout=2)
        second_thread.start()
        assert second_thread_started.wait(timeout=2)
        assert not second_copy_started.wait(timeout=0.2)
    finally:
        release_first_copy.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert [record["mac"] for record in history] == [
        "aa:bb:cc:00:00:ff",
        "aa:bb:cc:00:00:01",
        "aa:bb:cc:00:00:02",
    ]
