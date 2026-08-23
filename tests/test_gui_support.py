import json
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import aruba_mm_cleanup.gui_app as gui_app_module
from aruba_mm_cleanup import __version__
from aruba_mm_cleanup.gui_app import (
    ACCENT,
    APP_TITLE,
    BG,
    CARD_BG,
    DANGER_ACTIVE,
    DANGER_SOFT,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_ROLE,
    HISTORY_FILE_NAME,
    MAX_HISTORY_ROWS,
    MAX_LOG_LINES,
    MIN_INTERVAL_SECONDS,
    SHUTDOWN_GRACE_MS,
    TEXT,
    TYPE_NA_MESSAGE,
    ArubaMmCleanupGui,
)


class FakeVar:
    def __init__(self, value="0"):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)


class FailingGetVar(FakeVar):
    def get(self):
        raise tk.TclError("invalid command name")


class UnexpectedGetFailingVar(FakeVar):
    def get(self):
        raise RuntimeError("var get failed")


class FailingSetVar(FakeVar):
    def set(self, _value):
        raise tk.TclError("invalid command name")


class UnexpectedSetFailingVar(FakeVar):
    def set(self, _value):
        raise RuntimeError("var set failed")


class BadErrorText(Exception):
    def __str__(self):
        raise RuntimeError("bad error text")

    def __repr__(self):
        raise RuntimeError("bad error repr")


class BadValueErrorText(ValueError):
    def __str__(self):
        raise RuntimeError("bad error text")

    def __repr__(self):
        raise RuntimeError("bad error repr")


class BadQueueItem:
    def __iter__(self):
        raise BadValueErrorText()


class RuntimeFailingQueueItem:
    def __iter__(self):
        raise RuntimeError("queue item failed")


class FakeButton:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


class FailingConfigureButton(FakeButton):
    def configure(self, **_kwargs):
        raise tk.TclError("invalid command name")


class UnexpectedConfigureFailingButton(FakeButton):
    def configure(self, **_kwargs):
        raise RuntimeError("button configure failed")


class FakeSettingsFrame:
    def __init__(self):
        self.hidden = False
        self.grid_remove_calls = 0
        self.grid_calls = 0

    def grid_remove(self):
        self.grid_remove_calls += 1
        self.hidden = True

    def grid(self):
        self.grid_calls += 1
        self.hidden = False


class DestroyedSettingsFrame(FakeSettingsFrame):
    def grid_remove(self):
        raise tk.TclError("invalid command name")

    def grid(self):
        raise tk.TclError("invalid command name")


class UnexpectedFailingSettingsFrame(FakeSettingsFrame):
    def grid_remove(self):
        raise RuntimeError("settings frame failed")

    def grid(self):
        raise RuntimeError("settings frame failed")


class FakeHistoryTable:
    def __init__(self):
        self.rows = {}
        self.order = []

    def insert(self, _parent, _index, iid, values, tags=()):
        self.rows[iid] = {"values": values, "tags": tags}
        self.order.append(iid)

    def delete(self, *items):
        for item in items:
            if item in self.rows:
                del self.rows[item]
            if item in self.order:
                self.order.remove(item)

    def get_children(self):
        return tuple(self.order)


class FakeTreeTable(FakeHistoryTable):
    def __init__(self):
        super().__init__()
        self.click_column = "#1"
        self.click_row = ""

    def exists(self, item):
        return item in self.rows

    def item(self, item, option=None, **kwargs):
        if kwargs:
            self.rows[item].update(kwargs)
            return None
        if option:
            return self.rows[item][option]
        return self.rows[item]

    def identify_column(self, _x):
        return self.click_column

    def identify_row(self, _y):
        if self.click_row:
            return self.click_row
        if self.order:
            return self.order[0]
        return ""


class DestroyedTreeTable(FakeTreeTable):
    def get_children(self):
        raise tk.TclError("invalid command name")

    def delete(self, *items):
        raise tk.TclError("invalid command name")

    def insert(self, _parent, _index, iid, values, tags=()):
        raise tk.TclError("invalid command name")

    def exists(self, item):
        raise tk.TclError("invalid command name")

    def item(self, item, option=None, **kwargs):
        raise tk.TclError("invalid command name")


class UnexpectedItemFailingTreeTable(FakeTreeTable):
    def item(self, item, option=None, **kwargs):
        raise RuntimeError("table item failed")


class UnexpectedExistsFailingTreeTable(FakeTreeTable):
    def exists(self, item):
        raise RuntimeError("table exists failed")


class UnexpectedDeleteFailingTreeTable(FakeTreeTable):
    def get_children(self):
        return tuple(self.order)

    def delete(self, *items):
        raise RuntimeError("table delete failed")


class UnexpectedInsertFailingTreeTable(FakeTreeTable):
    def insert(self, _parent, _index, iid, values, tags=()):
        raise RuntimeError("table insert failed")


class UnexpectedChildrenFailingTreeTable(FakeTreeTable):
    def get_children(self):
        raise RuntimeError("table children failed")


class UnexpectedUpdateFailingTreeTable(FakeTreeTable):
    def item(self, item, option=None, **kwargs):
        if kwargs:
            raise RuntimeError("table update failed")
        return super().item(item, option, **kwargs)


class IdentifyFailingTreeTable(FakeTreeTable):
    def identify_column(self, _x):
        raise tk.TclError("invalid command name")

    def identify_row(self, _y):
        raise tk.TclError("invalid command name")


class UnexpectedIdentifyFailingTreeTable(FakeTreeTable):
    def identify_column(self, _x):
        raise RuntimeError("table identify failed")


class DestroyedHistoryTable(FakeHistoryTable):
    def get_children(self):
        raise tk.TclError("invalid command name")

    def delete(self, *items):
        raise tk.TclError("invalid command name")


class UnexpectedDeleteFailingHistoryTable(FakeHistoryTable):
    def get_children(self):
        raise RuntimeError("history children failed")

    def delete(self, *items):
        raise RuntimeError("history delete failed")


class UnexpectedCapDeleteFailingHistoryTable(FakeHistoryTable):
    def delete(self, *items):
        raise RuntimeError("history cap delete failed")


class InsertFailingHistoryTable(FakeHistoryTable):
    def insert(self, _parent, _index, iid, values, tags=()):
        raise tk.TclError("invalid command name")


class UnexpectedInsertFailingHistoryTable(FakeHistoryTable):
    def insert(self, _parent, _index, iid, values, tags=()):
        raise RuntimeError("history insert failed")


class FakeClickEvent:
    x = 1
    y = 1


class FakeLogText:
    def __init__(self):
        self.lines = []
        self.state = "disabled"

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]

    def insert(self, _index, text):
        self.lines.extend(text.splitlines())

    def index(self, _index):
        return f"{max(len(self.lines), 1)}.0"

    def delete(self, start, end):
        if start == "1.0" and end.endswith(".0"):
            count = int(end.split(".")[0]) - 1
            del self.lines[:count]
            return
        self.lines = []

    def see(self, _index):
        pass


class BadIndexLogText(FakeLogText):
    def index(self, _index):
        return "bad-index"


class DeleteFailingLogText(FakeLogText):
    def delete(self, _start, _end):
        raise tk.TclError("invalid command name")


class UnexpectedDeleteFailingLogText(FakeLogText):
    def delete(self, _start, _end):
        raise RuntimeError("log delete failed")


class InsertFailingLogText(FakeLogText):
    def insert(self, _index, _text):
        raise tk.TclError("invalid command name")


class UnexpectedInsertFailingLogText(FakeLogText):
    def insert(self, _index, _text):
        raise RuntimeError("log insert failed")


class UnexpectedSeeFailingLogText(FakeLogText):
    def see(self, _index):
        raise RuntimeError("log see failed")


class ConfigureFailingLogText(FakeLogText):
    def configure(self, **_kwargs):
        raise tk.TclError("invalid command name")


class UnexpectedConfigureFailingLogText(FakeLogText):
    def configure(self, **_kwargs):
        raise RuntimeError("log configure failed")


class FakeOverlayFrame:
    def __init__(self):
        self.place_calls = []
        self.lift_calls = 0
        self.hidden = True

    def place(self, **kwargs):
        self.place_calls.append(kwargs)
        self.hidden = False

    def lift(self):
        self.lift_calls += 1

    def place_forget(self):
        self.hidden = True


class PlacementFailingOverlayFrame(FakeOverlayFrame):
    def place(self, **_kwargs):
        raise tk.TclError("invalid command name")


class UnexpectedPlacementFailingOverlayFrame(FakeOverlayFrame):
    def place(self, **_kwargs):
        raise RuntimeError("overlay placement failed")


class HideFailingOverlayFrame(FakeOverlayFrame):
    def place_forget(self):
        raise tk.TclError("invalid command name")


class UnexpectedHideFailingOverlayFrame(FakeOverlayFrame):
    def place_forget(self):
        raise RuntimeError("overlay hide failed")


def make_headless_gui():
    app = object.__new__(ArubaMmCleanupGui)
    app.counter_vars = {
        "queried": FakeVar("7"),
        "deleted": FakeVar("3"),
    }
    app.cumulative_queried_count = 7
    app.cumulative_deleted_count = 3
    app.current_run_queried_count = 0
    app.current_run_query_counted = False
    app.current_run_delete_counted = False
    app.status_var = FakeVar()
    app.copy_notice_title_var = FakeVar("")
    app.copy_notice_mac_var = FakeVar("")
    app.copy_notice_after_id = None
    app.copy_notice_frame = FakeOverlayFrame()
    app.cancel_button = FakeButton()
    app.manual_button = FakeButton()
    app.schedule_button = FakeButton()
    app.stop_schedule_button = FakeButton()
    app.event_queue = queue.Queue()
    app.cancel_event = threading.Event()
    app.scheduler_stop_event = threading.Event()
    app.session_close_lock = threading.Lock()
    app.scheduler_running = False
    app.is_running = False
    app.closing = False
    app.rows = []
    app.logs = []
    app.timers = []
    app.clipboard_values = []
    app.scheduled_callbacks = []
    app.canceled_after_ids = []
    app.history_summaries = []
    app.reappeared_rows = []
    app.clipboard_clear = lambda: app.clipboard_values.clear()
    app.clipboard_append = lambda value: app.clipboard_values.append(value)
    app.after = lambda ms, callback: app.scheduled_callbacks.append((ms, callback)) or f"after-{len(app.scheduled_callbacks)}"
    app.after_cancel = lambda after_id: app.canceled_after_ids.append(after_id)
    app._set_row_status = lambda mac, status, error: app.rows.append((mac, status, error))
    app._log = lambda message: app.logs.append(message)
    app._set_timer = lambda value, state: app.timers.append((value, state))
    app._sync_settings_visibility = lambda: None
    app._append_history_rows = lambda summary: app.history_summaries.append(summary)
    app._mark_reappeared_rows = lambda macs: app.reappeared_rows.append(macs)
    return app


def make_input_gui():
    app = object.__new__(ArubaMmCleanupGui)
    app.host_var = FakeVar("192.0.2.10")
    app.port_var = FakeVar("22")
    app.username_var = FakeVar("admin")
    app.password_var = FakeVar("secret")
    app.enable_password_var = FakeVar("")
    app.role_var = FakeVar("profiling")
    app.timeout_var = FakeVar("15")
    app.interval_var = FakeVar("1")
    app.output_dir_var = FakeVar("/tmp/aruba-mm-cleanup")
    return app


def test_version_and_gui_constants():
    assert __version__ == "0.2.0"
    assert APP_TITLE == "Aruba MM Cleanup Dashboard"
    assert ACCENT == "#3e6ae1"
    assert DANGER_ACTIVE == "#8f1d14"
    assert DANGER_SOFT == "#fff4f2"
    assert BG == "#f4f4f4"
    assert TEXT == "#171a20"
    assert CARD_BG == "#ffffff"
    assert DEFAULT_ROLE == "profiling"
    assert MAX_HISTORY_ROWS == 500
    assert DEFAULT_INTERVAL_SECONDS == 300
    assert MIN_INTERVAL_SECONDS == 1


def test_initial_history_load_failure_does_not_abort_gui_init(monkeypatch):
    logs = []

    monkeypatch.setattr(gui_app_module.tk.Tk, "__init__", lambda self: None)
    monkeypatch.setattr(gui_app_module.tk, "StringVar", FakeVar)
    monkeypatch.setattr(ArubaMmCleanupGui, "title", lambda self, _title: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "geometry", lambda self, _geometry: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "minsize", lambda self, _width, _height: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "configure", lambda self, **_kwargs: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "_build_styles", lambda self: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "_build_layout", lambda self: None)
    monkeypatch.setattr(
        ArubaMmCleanupGui,
        "_load_history_from_output_dir",
        lambda self, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history load failed")),
    )
    monkeypatch.setattr(ArubaMmCleanupGui, "_log", lambda self, message: logs.append(message))
    monkeypatch.setattr(ArubaMmCleanupGui, "protocol", lambda self, _name, _callback: None)
    monkeypatch.setattr(ArubaMmCleanupGui, "after", lambda self, _ms, _callback: "after-1")

    app = ArubaMmCleanupGui()

    assert app._drain_after_id == "after-1"
    assert logs == ["WARNING: 이력 로드 실패 - history load failed"]


def test_read_inputs_uses_immediate_delete_and_device_timeout():
    app = make_input_gui()

    config, settings, output_dir = ArubaMmCleanupGui._read_inputs(app)

    assert config.host == "192.0.2.10"
    assert settings.timeout == 15
    assert settings.delete_delay_seconds == 0
    assert str(output_dir) == "/tmp/aruba-mm-cleanup"


def test_read_inputs_uses_actual_one_second_device_timeout():
    app = make_input_gui()
    app.timeout_var.set("1")

    _config, settings, _output_dir = ArubaMmCleanupGui._read_inputs(app)

    assert settings.timeout == 1


def test_read_inputs_expands_user_home_output_dir():
    app = make_input_gui()
    app.output_dir_var.set("~/aruba-mm-cleanup")

    _config, _settings, output_dir = ArubaMmCleanupGui._read_inputs(app)

    assert output_dir == Path.home() / "aruba-mm-cleanup"


def test_read_inputs_reports_clear_timeout_errors():
    app = make_input_gui()
    app.timeout_var.set("slow")

    with pytest.raises(ValueError, match="장비 응답 대기"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_read_inputs_rejects_non_positive_device_timeout(value):
    app = make_input_gui()
    app.timeout_var.set(value)

    with pytest.raises(ValueError, match="장비 응답 대기"):
        ArubaMmCleanupGui._read_inputs(app)


def test_read_inputs_rejects_role_control_characters():
    app = make_input_gui()
    app.role_var.set("profiling\nshow version")

    with pytest.raises(ValueError, match="Role"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize("value", ["0", "-1", "65536"])
def test_read_inputs_rejects_out_of_range_ports(value):
    app = make_input_gui()
    app.port_var.set(value)

    with pytest.raises(ValueError, match="Port"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize(
    "field_name",
    [
        "host_var",
        "username_var",
        "password_var",
        "port_var",
        "timeout_var",
        "role_var",
        "enable_password_var",
        "output_dir_var",
    ],
)
def test_read_inputs_reports_destroyed_input_variables(field_name):
    app = make_input_gui()
    setattr(app, field_name, FailingGetVar(""))

    with pytest.raises(ValueError, match="입력값"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize(
    "field_name",
    [
        "host_var",
        "username_var",
        "password_var",
        "port_var",
        "timeout_var",
        "role_var",
        "enable_password_var",
        "output_dir_var",
    ],
)
def test_read_inputs_reports_unexpected_input_variable_failures(field_name):
    app = make_input_gui()
    setattr(app, field_name, UnexpectedGetFailingVar(""))

    with pytest.raises(ValueError, match="입력값"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize(
    "field_name",
    [
        "host_var",
        "username_var",
        "port_var",
        "timeout_var",
        "role_var",
        "output_dir_var",
    ],
)
def test_read_inputs_reports_malformed_text_variables(field_name):
    app = make_input_gui()
    setattr(app, field_name, FakeVar(None))

    with pytest.raises(ValueError, match="입력값"):
        ArubaMmCleanupGui._read_inputs(app)


@pytest.mark.parametrize(
    ("field_name", "message"),
    [
        ("port_var", "Port"),
        ("timeout_var", "장비 응답 대기"),
    ],
)
@pytest.mark.parametrize("bad_number", [object(), float("inf")])
def test_read_inputs_reports_malformed_numeric_variables(field_name, message, bad_number):
    class BadNumericText:
        def strip(self):
            return bad_number

    app = make_input_gui()
    setattr(app, field_name, FakeVar(BadNumericText()))

    with pytest.raises(ValueError, match=message):
        ArubaMmCleanupGui._read_inputs(app)


def test_read_interval_uses_actual_input_value():
    app = make_input_gui()
    app.interval_var.set("1")

    assert ArubaMmCleanupGui._read_interval(app) == 1


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_read_interval_rejects_invalid_values(value):
    app = make_input_gui()
    app.interval_var.set(value)

    with pytest.raises(ValueError, match="주기\\(초\\)"):
        ArubaMmCleanupGui._read_interval(app)


def test_read_interval_reports_destroyed_interval_variable():
    app = make_input_gui()
    app.interval_var = FailingGetVar("1")

    with pytest.raises(ValueError, match="주기\\(초\\)"):
        ArubaMmCleanupGui._read_interval(app)


def test_read_interval_reports_unexpected_interval_variable_failure():
    app = make_input_gui()
    app.interval_var = UnexpectedGetFailingVar("1")

    with pytest.raises(ValueError, match="주기\\(초\\)"):
        ArubaMmCleanupGui._read_interval(app)


def test_read_interval_reports_malformed_interval_variable():
    app = make_input_gui()
    app.interval_var = FakeVar(None)

    with pytest.raises(ValueError, match="주기\\(초\\)"):
        ArubaMmCleanupGui._read_interval(app)


@pytest.mark.parametrize("bad_number", [object(), float("inf")])
def test_read_interval_reports_malformed_numeric_variable(bad_number):
    class BadNumericText:
        def strip(self):
            return bad_number

    app = make_input_gui()
    app.interval_var = FakeVar(BadNumericText())

    with pytest.raises(ValueError, match="주기\\(초\\)"):
        ArubaMmCleanupGui._read_interval(app)


def test_browse_output_dir_updates_output_dir_and_loads_history(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FakeVar("/tmp/current")
    loaded = []
    asked_initial_dirs = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **kwargs: asked_initial_dirs.append(kwargs["initialdir"]) or "/tmp/selected",
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert asked_initial_dirs == ["/tmp/current"]
    assert app.output_dir_var.get() == "/tmp/selected"
    assert loaded == [(Path("/tmp/selected"), True)]


def test_browse_output_dir_ignores_dialog_tcl_error(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FakeVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: (_ for _ in ()).throw(tk.TclError("invalid command name")),
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert app.output_dir_var.get() == "/tmp/current"
    assert loaded == []


def test_browse_output_dir_ignores_unexpected_dialog_failure(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FakeVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("dialog failed")),
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert app.output_dir_var.get() == "/tmp/current"
    assert loaded == []


def test_browse_output_dir_ignores_destroyed_output_dir_variable(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FailingGetVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: "/tmp/selected",
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert loaded == []


def test_browse_output_dir_ignores_unexpected_output_dir_variable_failure(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = UnexpectedGetFailingVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: "/tmp/selected",
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert loaded == []


def test_browse_output_dir_ignores_destroyed_output_dir_set(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FailingSetVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: "/tmp/selected",
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert loaded == []


def test_browse_output_dir_ignores_unexpected_output_dir_set_failure(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = UnexpectedSetFailingVar("/tmp/current")
    loaded = []
    app._load_history_from_output_dir = lambda path, force=False: loaded.append((path, force))
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: "/tmp/selected",
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert loaded == []


def test_browse_output_dir_logs_history_load_failure(monkeypatch):
    app = make_headless_gui()
    app.output_dir_var = FakeVar("/tmp/current")
    monkeypatch.setattr(
        gui_app_module.filedialog,
        "askdirectory",
        lambda **_kwargs: "/tmp/selected",
    )
    app._load_history_from_output_dir = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("history load failed")
    )

    ArubaMmCleanupGui.browse_output_dir(app)

    assert app.output_dir_var.get() == "/tmp/selected"
    assert "WARNING: 이력 로드 실패 - history load failed" in app.logs


def test_manual_run_input_error_dialog_failure_does_not_start_worker(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(ValueError("bad input"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("worker should not start"))
    monkeypatch.setattr(
        gui_app_module.messagebox,
        "showerror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(tk.TclError("invalid command name")),
    )

    ArubaMmCleanupGui.start_manual_run(app)

    assert app.is_running is False
    assert app.event_queue.empty()


def test_manual_run_unexpected_input_error_dialog_failure_does_not_start_worker(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(ValueError("bad input"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("worker should not start"))
    monkeypatch.setattr(
        gui_app_module.messagebox,
        "showerror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dialog failed")),
    )

    ArubaMmCleanupGui.start_manual_run(app)

    assert app.is_running is False
    assert app.event_queue.empty()


def test_manual_run_unprintable_input_error_does_not_start_worker(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(BadValueErrorText())
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("worker should not start"))
    errors = []
    monkeypatch.setattr(gui_app_module.messagebox, "showerror", lambda title, message: errors.append((title, message)))

    ArubaMmCleanupGui.start_manual_run(app)

    assert errors == [("입력 오류", "BadValueErrorText")]
    assert app.is_running is False
    assert app.event_queue.empty()


def test_manual_run_history_load_failure_still_starts_worker(monkeypatch):
    class FakeThread:
        def __init__(self, *_args, **_kwargs):
            self.started = False

        def start(self):
            self.started = True

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("history load failed")
    )
    monkeypatch.setattr(gui_app_module.threading, "Thread", FakeThread)

    ArubaMmCleanupGui.start_manual_run(app)

    assert app.is_running is True
    assert app.worker.started is True
    assert "WARNING: 이력 로드 실패 - history load failed" in app.logs


def test_manual_run_thread_start_failure_resets_running_state(monkeypatch):
    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    monkeypatch.setattr(gui_app_module.threading, "Thread", FailingThread)

    ArubaMmCleanupGui.start_manual_run(app)

    assert app.is_running is False
    assert app.worker is None
    assert app.manual_button.config["state"] == "normal"
    assert app.schedule_button.config["state"] == "normal"
    assert app.cancel_button.config["state"] == "disabled"
    assert app.timers[-1] == ("-", "대기")
    assert "WARNING: 작업 스레드 시작 실패 - thread start failed" in app.logs


def test_manual_run_thread_create_failure_resets_running_state(monkeypatch):
    def failing_thread(*_args, **_kwargs):
        raise RuntimeError("thread create failed")

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    monkeypatch.setattr(gui_app_module.threading, "Thread", failing_thread)

    ArubaMmCleanupGui.start_manual_run(app)

    assert app.is_running is False
    assert app.worker is None
    assert app.manual_button.config["state"] == "normal"
    assert app.schedule_button.config["state"] == "normal"
    assert app.cancel_button.config["state"] == "disabled"
    assert app.timers[-1] == ("-", "대기")
    assert "WARNING: 작업 스레드 시작 실패 - thread create failed" in app.logs


def test_scheduler_input_error_dialog_failure_does_not_start_scheduler(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(ValueError("bad input"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("scheduler should not start"))
    monkeypatch.setattr(
        gui_app_module.messagebox,
        "showerror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(tk.TclError("invalid command name")),
    )

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is False
    assert app.event_queue.empty()


def test_scheduler_unexpected_input_error_dialog_failure_does_not_start_scheduler(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(ValueError("bad input"))
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("scheduler should not start"))
    monkeypatch.setattr(
        gui_app_module.messagebox,
        "showerror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dialog failed")),
    )

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is False
    assert app.event_queue.empty()


def test_scheduler_unprintable_input_error_does_not_start_scheduler(monkeypatch):
    app = make_headless_gui()
    app._read_inputs = lambda: (_ for _ in ()).throw(BadValueErrorText())
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app._set_running = lambda _running: (_ for _ in ()).throw(AssertionError("scheduler should not start"))
    errors = []
    monkeypatch.setattr(gui_app_module.messagebox, "showerror", lambda title, message: errors.append((title, message)))

    ArubaMmCleanupGui.start_scheduler(app)

    assert errors == [("입력 오류", "BadValueErrorText")]
    assert app.scheduler_running is False
    assert app.event_queue.empty()


def test_scheduler_history_load_failure_still_starts_scheduler(monkeypatch):
    class FakeThread:
        def __init__(self, *_args, **_kwargs):
            self.started = False

        def start(self):
            self.started = True

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._read_interval = lambda: 5
    app._load_history_from_output_dir = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("history load failed")
    )
    monkeypatch.setattr(gui_app_module.threading, "Thread", FakeThread)

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is True
    assert app.scheduler_worker.started is True
    assert "WARNING: 이력 로드 실패 - history load failed" in app.logs
    assert "주기 실행 시작: 5초 간격" in app.logs


def test_scheduler_unexpected_button_failure_still_starts_scheduler(monkeypatch):
    class FakeThread:
        def __init__(self, *_args, **_kwargs):
            self.started = False

        def start(self):
            self.started = True

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._read_interval = lambda: 5
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app.manual_button = UnexpectedConfigureFailingButton()
    app.schedule_button = UnexpectedConfigureFailingButton()
    app.stop_schedule_button = UnexpectedConfigureFailingButton()
    monkeypatch.setattr(gui_app_module.threading, "Thread", FakeThread)

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is True
    assert app.scheduler_worker.started is True
    assert "주기 실행 시작: 5초 간격" in app.logs


def test_scheduler_thread_start_failure_resets_scheduler_state(monkeypatch):
    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._read_interval = lambda: 5
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    monkeypatch.setattr(gui_app_module.threading, "Thread", FailingThread)

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is False
    assert app.scheduler_worker is None
    assert app.scheduler_stop_event.is_set()
    assert app.manual_button.config["state"] == "normal"
    assert app.schedule_button.config["state"] == "normal"
    assert app.stop_schedule_button.config["state"] == "disabled"
    assert app.timers[-1] == ("-", "대기")
    assert "WARNING: 주기 실행 스레드 시작 실패 - thread start failed" in app.logs


def test_scheduler_thread_start_failure_with_unexpected_button_failure_still_resets_state(monkeypatch):
    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._read_interval = lambda: 5
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    app.manual_button = UnexpectedConfigureFailingButton()
    app.schedule_button = UnexpectedConfigureFailingButton()
    app.stop_schedule_button = UnexpectedConfigureFailingButton()
    monkeypatch.setattr(gui_app_module.threading, "Thread", FailingThread)

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is False
    assert app.scheduler_worker is None
    assert app.scheduler_stop_event.is_set()
    assert app.timers[-1] == ("-", "대기")
    assert "WARNING: 주기 실행 스레드 시작 실패 - thread start failed" in app.logs


def test_scheduler_thread_create_failure_resets_scheduler_state(monkeypatch):
    def failing_thread(*_args, **_kwargs):
        raise RuntimeError("thread create failed")

    app = make_headless_gui()
    app._read_inputs = lambda: (object(), object(), Path("outputs"))
    app._read_interval = lambda: 5
    app._load_history_from_output_dir = lambda *_args, **_kwargs: None
    monkeypatch.setattr(gui_app_module.threading, "Thread", failing_thread)

    ArubaMmCleanupGui.start_scheduler(app)

    assert app.scheduler_running is False
    assert app.scheduler_worker is None
    assert app.scheduler_stop_event.is_set()
    assert app.manual_button.config["state"] == "normal"
    assert app.schedule_button.config["state"] == "normal"
    assert app.stop_schedule_button.config["state"] == "disabled"
    assert app.timers[-1] == ("-", "대기")
    assert "WARNING: 주기 실행 스레드 시작 실패 - thread create failed" in app.logs


def test_delete_progress_events_update_rows_without_confirmed_delete_count():
    app = make_headless_gui()

    app._handle_progress("delete_done", {"mac": "aa:bb:cc:00:00:01"})
    app._handle_progress("delete_done", {"mac": "aa:bb:cc:00:00:02"})
    app._handle_progress("delete_error", {"mac": "aa:bb:cc:00:00:03", "error": "Error"})
    app._handle_progress("delete_unknown", {"mac": "aa:bb:cc:00:00:04", "error": "timeout"})

    assert app.counter_vars["deleted"].get() == "3"
    assert app.rows == [
        ("aa:bb:cc:00:00:01", "삭제 완료", ""),
        ("aa:bb:cc:00:00:02", "삭제 완료", ""),
        ("aa:bb:cc:00:00:03", "삭제 실패", "Error"),
        ("aa:bb:cc:00:00:04", "확인 필요", "timeout"),
    ]


def test_delete_start_progress_handles_unprintable_mac_without_losing_row_or_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "delete_start", {"mac": BadErrorText()})

    assert app.status_var.get() == "MAC 삭제 중"
    assert app.timers[-1] == ("실행 중", "삭제 처리")
    assert app.rows == [("BadErrorText", "삭제 중", "")]
    assert "DELETE START: BadErrorText" in app.logs


def test_delete_done_progress_handles_unprintable_mac_without_losing_row_or_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "delete_done", {"mac": BadErrorText()})

    assert app.rows == [("BadErrorText", "삭제 완료", "")]
    assert "DELETE OK: BadErrorText" in app.logs


def test_delete_error_progress_handles_unprintable_error_without_losing_row_or_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(
        app,
        "delete_error",
        {"mac": "aa:bb:cc:00:00:03", "error": BadErrorText()},
    )

    assert app.rows == [("aa:bb:cc:00:00:03", "삭제 실패", "BadErrorText")]
    assert "DELETE ERROR: aa:bb:cc:00:00:03 | BadErrorText" in app.logs


def test_delete_unknown_progress_handles_unprintable_error_without_losing_row_or_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(
        app,
        "delete_unknown",
        {"mac": "aa:bb:cc:00:00:04", "error": BadErrorText()},
    )

    assert app.rows == [("aa:bb:cc:00:00:04", "확인 필요", "BadErrorText")]
    assert "DELETE UNKNOWN: aa:bb:cc:00:00:04 | BadErrorText" in app.logs


def test_progress_status_update_failure_does_not_skip_followup_work():
    app = make_headless_gui()
    app.status_var = FailingSetVar()

    ArubaMmCleanupGui._handle_progress(app, "connect_start", {"host": "192.0.2.10"})

    assert "CONNECT: 192.0.2.10" in app.logs


def test_progress_unexpected_status_update_failure_does_not_skip_followup_work():
    app = make_headless_gui()
    app.status_var = UnexpectedSetFailingVar()

    ArubaMmCleanupGui._handle_progress(app, "connect_start", {"host": "192.0.2.10"})

    assert "CONNECT: 192.0.2.10" in app.logs


def test_connection_progress_handles_unprintable_payload_without_losing_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "connect_start", {"host": BadErrorText()})
    ArubaMmCleanupGui._handle_progress(app, "connect_done", {"host": BadErrorText()})
    ArubaMmCleanupGui._handle_progress(app, "session_disconnected", {"reason": BadErrorText()})

    assert "CONNECT: BadErrorText" in app.logs
    assert "CONNECT OK: BadErrorText" in app.logs
    assert "DISCONNECT: BadErrorText" in app.logs


def test_disconnect_status_update_failure_does_not_skip_log():
    app = make_headless_gui()
    app.status_var = FailingSetVar()
    close_reasons = []
    app._start_session_close = lambda **kwargs: close_reasons.append(kwargs)

    ArubaMmCleanupGui.disconnect_session(app)

    assert close_reasons == [{"reason": "manual", "enqueue_progress": True}]
    assert "SESSION DISCONNECT REQUEST" in app.logs


def test_disconnect_unexpected_status_update_failure_does_not_skip_log():
    app = make_headless_gui()
    app.status_var = UnexpectedSetFailingVar()
    close_reasons = []
    app._start_session_close = lambda **kwargs: close_reasons.append(kwargs)

    ArubaMmCleanupGui.disconnect_session(app)

    assert close_reasons == [{"reason": "manual", "enqueue_progress": True}]
    assert "SESSION DISCONNECT REQUEST" in app.logs


def test_disconnect_session_does_not_close_session_while_run_is_active():
    app = make_headless_gui()
    app.is_running = True
    close_reasons = []
    app._start_session_close = lambda **kwargs: close_reasons.append(kwargs)

    ArubaMmCleanupGui.disconnect_session(app)

    assert close_reasons == []
    assert "실행 중에는 세션 연결 해제를 건너뜁니다." in app.logs
    assert "SESSION DISCONNECT REQUEST" not in app.logs


def test_warning_progress_handles_unprintable_message_without_losing_warning():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "warning", {"message": BadErrorText()})

    assert "WARNING: BadErrorText" in app.logs


def test_reconnect_progress_handles_unprintable_payload_without_losing_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(
        app,
        "session_reconnect_start",
        {"command": BadErrorText(), "error": BadErrorText()},
    )

    assert app.status_var.get() == "MM 세션 재접속 중"
    assert "RECONNECT: BadErrorText | BadErrorText" in app.logs


def test_run_error_progress_handles_unprintable_error_without_losing_log():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "run_error", {"error": BadErrorText()})

    assert app.status_var.get() == "실패"
    assert app.timers[-1] == ("-", "대기")
    assert app.cancel_button.config["state"] == "disabled"
    assert "ERROR: BadErrorText" in app.logs


def test_run_error_unexpected_button_failure_does_not_skip_log():
    app = make_headless_gui()
    app.cancel_button = UnexpectedConfigureFailingButton()

    ArubaMmCleanupGui._handle_progress(app, "run_error", {"error": BadErrorText()})

    assert app.status_var.get() == "실패"
    assert app.timers[-1] == ("-", "대기")
    assert "ERROR: BadErrorText" in app.logs


def test_delete_canceled_button_failure_does_not_skip_log():
    app = make_headless_gui()
    app.cancel_button = FailingConfigureButton()
    app.table = FakeTreeTable()

    ArubaMmCleanupGui._handle_progress(app, "delete_canceled", {"count": 2})

    assert "CANCELED: 2 pending MAC(s)" in app.logs


def test_delete_canceled_unexpected_button_failure_does_not_skip_log():
    app = make_headless_gui()
    app.cancel_button = UnexpectedConfigureFailingButton()
    app.table = FakeTreeTable()

    ArubaMmCleanupGui._handle_progress(app, "delete_canceled", {"count": 2})

    assert "CANCELED: 2 pending MAC(s)" in app.logs


def test_delete_canceled_unreadable_pending_rows_still_logs():
    class UnreadableChildren:
        def __iter__(self):
            raise RuntimeError("pending rows unreadable")

    class UnreadableChildrenTable(FakeTreeTable):
        def get_children(self):
            return UnreadableChildren()

    app = make_headless_gui()
    app.table = UnreadableChildrenTable()

    ArubaMmCleanupGui._handle_progress(app, "delete_canceled", {"count": 2})

    assert app.status_var.get() == "이번 삭제 취소됨"
    assert app.timers[-1] == ("-", "대기")
    assert "CANCELED: 2 pending MAC(s)" in app.logs


def test_delete_canceled_unprintable_count_still_logs():
    app = make_headless_gui()
    app.table = FakeTreeTable()

    ArubaMmCleanupGui._handle_progress(app, "delete_canceled", {"count": BadErrorText()})

    assert app.status_var.get() == "이번 삭제 취소됨"
    assert app.timers[-1] == ("-", "대기")
    assert "CANCELED: BadErrorText pending MAC(s)" in app.logs


def test_countdown_progress_handles_invalid_remaining_payload():
    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "countdown", {"remaining": object()})

    assert app.timers[-1] == ("0s", "삭제 시작")
    assert app.status_var.get() == "삭제 시작"
    assert app.cancel_button.config["state"] == "disabled"


def test_countdown_progress_handles_failing_remaining_conversion():
    class BadRemaining:
        def __int__(self):
            raise RuntimeError("bad remaining")

    app = make_headless_gui()

    ArubaMmCleanupGui._handle_progress(app, "countdown", {"remaining": BadRemaining()})

    assert app.timers[-1] == ("0s", "삭제 시작")
    assert app.status_var.get() == "삭제 시작"
    assert app.cancel_button.config["state"] == "disabled"


def test_countdown_progress_unexpected_button_failure_keeps_status_update():
    app = make_headless_gui()
    app.cancel_button = UnexpectedConfigureFailingButton()

    ArubaMmCleanupGui._handle_progress(app, "countdown", {"remaining": 5})

    assert app.timers[-1] == ("5s", "삭제 시작 대기")
    assert app.status_var.get() == "5초 후 삭제 시작"


def test_query_done_adds_unique_display_macs_to_cumulative_total():
    app = make_headless_gui()
    replaced = []
    app._replace_table = lambda macs, status, **kwargs: replaced.append((macs, status, kwargs))

    app._handle_progress(
        "query_done",
        {
            "count": 3,
            "macs": ["aa-bb-cc-00-00-01", "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
        },
    )

    assert app.counter_vars["queried"].get() == "9"
    assert replaced == [
        (["aa-bb-cc-00-00-01", "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"], "삭제 대상", {"type_na_macs": []})
    ]


def test_query_progress_handles_unprintable_payload_without_losing_log():
    app = make_headless_gui()
    replaced = []
    app._replace_table = lambda macs, status, **kwargs: replaced.append((macs, status, kwargs))

    ArubaMmCleanupGui._handle_progress(app, "query_start", {"command": BadErrorText()})
    ArubaMmCleanupGui._handle_progress(
        app,
        "query_done",
        {"count": BadErrorText(), "macs": ["aa:bb:cc:00:00:01"]},
    )

    assert app.status_var.get() == "global-user-table 조회 중"
    assert app.timers[-1] == ("실행 중", "조회 처리")
    assert "QUERY: BadErrorText" in app.logs
    assert "QUERY DONE: BadErrorText MAC(s)" in app.logs
    assert replaced == [(["aa:bb:cc:00:00:01"], "삭제 대상", {"type_na_macs": []})]


def test_query_done_ignores_string_macs_payload_without_character_rows():
    app = make_headless_gui()
    replaced = []
    app._replace_table = lambda macs, status, **kwargs: replaced.append((macs, status, kwargs))

    app._handle_progress("query_done", {"count": 1, "macs": "aa:bb:cc:00:00:01"})

    assert app.counter_vars["queried"].get() == "7"
    assert replaced == [([], "삭제 대상", {"type_na_macs": []})]


def test_query_done_ignores_unreadable_mac_payloads_without_stopping_log():
    class UnreadableMacs(list):
        def __iter__(self):
            raise RuntimeError("bad macs")

    app = make_headless_gui()
    replaced = []
    app._replace_table = lambda macs, status, **kwargs: replaced.append((macs, status, kwargs))

    app._handle_progress(
        "query_done",
        {
            "count": 1,
            "macs": UnreadableMacs(["aa:bb:cc:00:00:01"]),
            "type_na_macs": UnreadableMacs(["aa:bb:cc:00:00:01"]),
        },
    )

    assert app.counter_vars["queried"].get() == "7"
    assert replaced == [([], "삭제 대상", {"type_na_macs": []})]
    assert "QUERY DONE: 1 MAC(s)" in app.logs


def test_replace_table_tolerates_unreadable_display_mac_container():
    class UnreadableMacs(list):
        def __iter__(self):
            raise RuntimeError("bad macs")

    app = make_headless_gui()
    app.table = FakeTreeTable()

    ArubaMmCleanupGui._replace_table(
        app,
        UnreadableMacs(["aa:bb:cc:00:00:01"]),
        "삭제 대상",
    )

    assert app.table.get_children() == ()


def test_replace_table_tolerates_unreadable_type_na_container():
    class UnreadableLengthMacs(list):
        def __len__(self):
            raise RuntimeError("bad type n/a macs length")

    app = make_headless_gui()
    app.table = FakeTreeTable()

    ArubaMmCleanupGui._replace_table(
        app,
        ["aa:bb:cc:00:00:01"],
        "삭제 대상",
        type_na_macs=UnreadableLengthMacs(["aa:bb:cc:00:00:01"]),
    )

    assert app.table.get_children() == ("aa:bb:cc:00:00:01",)
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][4] == TYPE_NA_MESSAGE


def test_query_done_ignores_non_string_mac_items_without_table_rows():
    app = make_headless_gui()
    app.table = FakeTreeTable()

    app._handle_progress(
        "query_done",
        {
            "count": 2,
            "macs": [["aa:bb:cc:00:00:01"], "aa:bb:cc:00:00:02"],
        },
    )

    assert app.counter_vars["queried"].get() == "8"
    assert app.table.get_children() == ("aa:bb:cc:00:00:02",)


def test_query_done_skips_bad_mac_text_without_stopping_table_update():
    class BadMac(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    app = make_headless_gui()
    app.table = FakeTreeTable()

    app._handle_progress(
        "query_done",
        {
            "count": 2,
            "macs": [BadMac("aa:bb:cc:00:00:01"), "aa:bb:cc:00:00:02"],
        },
    )

    assert app.counter_vars["queried"].get() == "8"
    assert app.table.get_children() == ("aa:bb:cc:00:00:02",)


def test_query_done_marks_type_na_rows_and_logs_admin_guidance():
    app = make_headless_gui()
    app.table = FakeTreeTable()

    app._handle_progress(
        "query_done",
        {
            "count": 2,
            "macs": ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
            "type_na_macs": ["aa:bb:cc:00:00:02"],
        },
    )

    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][4] == ""
    assert app.table.rows["aa:bb:cc:00:00:02"]["values"][4] == TYPE_NA_MESSAGE
    assert "TYPE N/A: aa:bb:cc:00:00:02 - 관리자 직접 장비 지정 필요" in app.logs


def test_query_done_skips_bad_type_na_mac_text_without_stopping_table_update():
    class BadText:
        def __str__(self):
            raise RuntimeError("bad type")

    app = make_headless_gui()
    app.table = FakeTreeTable()

    app._handle_progress(
        "query_done",
        {
            "count": 2,
            "macs": ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
            "type_na_macs": [BadText(), "aa:bb:cc:00:00:02"],
        },
    )

    assert app.counter_vars["queried"].get() == "9"
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][4] == ""
    assert app.table.rows["aa:bb:cc:00:00:02"]["values"][4] == TYPE_NA_MESSAGE
    assert "TYPE N/A: aa:bb:cc:00:00:02 - 관리자 직접 장비 지정 필요" in app.logs


def test_reappeared_macs_ignores_string_payload_without_character_rows():
    app = make_headless_gui()

    app._handle_progress("reappeared_macs", {"macs": "aa:bb:cc:00:00:01"})

    assert app.reappeared_rows == [[]]
    assert not any(message.startswith("REAPPEARED:") for message in app.logs)


def test_reappeared_macs_skips_bad_mac_text_without_stopping_highlight():
    class BadText:
        def __str__(self):
            raise RuntimeError("bad reappeared mac")

    app = make_headless_gui()

    app._handle_progress(
        "reappeared_macs",
        {"macs": [BadText(), "aa:bb:cc:00:00:01"]},
    )

    assert app.status_var.get() == "삭제 MAC 재조회됨"
    assert app.reappeared_rows == [["aa:bb:cc:00:00:01"]]
    assert "REAPPEARED: aa:bb:cc:00:00:01" in app.logs


def test_reappeared_macs_unexpected_status_update_failure_still_marks_rows():
    app = make_headless_gui()
    app.status_var = UnexpectedSetFailingVar()

    app._handle_progress("reappeared_macs", {"macs": ["aa:bb:cc:00:00:01"]})

    assert app.reappeared_rows == [["aa:bb:cc:00:00:01"]]
    assert "REAPPEARED: aa:bb:cc:00:00:01" in app.logs


def test_type_na_message_survives_delete_status_updates():
    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", TYPE_NA_MESSAGE),
    )

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "삭제 완료", "")
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][4] == TYPE_NA_MESSAGE

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "확인 필요", "timeout")
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][4] == f"{TYPE_NA_MESSAGE} | timeout"


def test_set_row_status_ignores_malformed_table_row_values():
    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01",),
    )

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "삭제 완료", "")

    assert app.table.rows["aa:bb:cc:00:00:01"]["values"] == ("aa:bb:cc:00:00:01",)


def test_set_row_status_handles_unprintable_existing_message():
    class BadMessage:
        def __bool__(self):
            raise RuntimeError("bad bool")

        def __str__(self):
            raise RuntimeError("bad message")

    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", BadMessage()),
    )

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "확인 필요", "timeout")

    values = app.table.rows["aa:bb:cc:00:00:01"]["values"]
    assert values[1] == "확인 필요"
    assert values[4] == "timeout"


def test_set_row_status_handles_unprintable_update_message():
    class BadMessage:
        def __str__(self):
            raise RuntimeError("bad message")

    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", TYPE_NA_MESSAGE),
    )

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "확인 필요", BadMessage())  # type: ignore[arg-type]

    values = app.table.rows["aa:bb:cc:00:00:01"]["values"]
    assert values[1] == "확인 필요"
    assert values[4] == TYPE_NA_MESSAGE


def test_set_row_status_ignores_unexpected_exists_failure():
    app = make_headless_gui()
    app.table = UnexpectedExistsFailingTreeTable()

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "삭제 완료", "")

    assert app.table.rows == {}


def test_set_row_status_ignores_unexpected_item_failure():
    app = make_headless_gui()
    app.table = UnexpectedItemFailingTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "삭제 완료", "")

    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][1] == "삭제 대상"


def test_set_all_pending_status_skips_malformed_rows_and_updates_valid_rows():
    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert("", "end", iid="bad-row", values=("bad-row",))
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows["bad-row"]["values"] == ("bad-row",)
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][1] == "취소됨"


def test_set_all_pending_status_skips_unhashable_status_values_and_updates_valid_rows():
    app = make_headless_gui()
    app.table = FakeTreeTable()
    app.table.insert(
        "",
        "end",
        iid="bad-row",
        values=("bad-row", ["삭제 대상"], "2026-07-02 13:00:00", "", ""),
    )
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 중", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows["bad-row"]["values"][1] == ["삭제 대상"]
    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][1] == "취소됨"


def test_set_all_pending_status_ignores_unexpected_children_failure():
    app = make_headless_gui()
    app.table = UnexpectedChildrenFailingTreeTable()

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows == {}


def test_set_all_pending_status_ignores_unexpected_item_read_failure():
    app = make_headless_gui()
    app.table = UnexpectedItemFailingTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][1] == "삭제 대상"


def test_set_all_pending_status_ignores_unexpected_item_update_failure():
    app = make_headless_gui()
    app.table = UnexpectedUpdateFailingTreeTable()
    app.table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows["aa:bb:cc:00:00:01"]["values"][1] == "삭제 대상"


def test_set_all_pending_status_continues_after_per_row_table_failures():
    class PartiallyFailingPendingTable(FakeTreeTable):
        def item(self, item, option=None, **kwargs):
            if item == "bad-read":
                raise RuntimeError("table read failed")
            if item == "bad-update" and kwargs:
                raise RuntimeError("table update failed")
            return super().item(item, option, **kwargs)

    app = make_headless_gui()
    app.table = PartiallyFailingPendingTable()
    app.table.insert("", "end", iid="bad-read", values=("bad-read", "삭제 대상", "", "", ""))
    app.table.insert("", "end", iid="bad-update", values=("bad-update", "삭제 대상", "", "", ""))
    app.table.insert("", "end", iid="good-row", values=("good-row", "삭제 중", "", "", ""))

    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows["bad-read"]["values"][1] == "삭제 대상"
    assert app.table.rows["bad-update"]["values"][1] == "삭제 대상"
    assert app.table.rows["good-row"]["values"][1] == "취소됨"


def test_result_table_updates_ignore_destroyed_table():
    app = make_headless_gui()
    app.table = DestroyedTreeTable()

    ArubaMmCleanupGui._replace_table(app, ["aa:bb:cc:00:00:01"], "삭제 대상")
    ArubaMmCleanupGui._set_row_status(app, "aa:bb:cc:00:00:01", "삭제 완료", "")
    ArubaMmCleanupGui._mark_reappeared_rows(app, ["aa:bb:cc:00:00:01"])
    ArubaMmCleanupGui._set_all_pending_status(app, "취소됨")

    assert app.table.rows == {}


def test_replace_table_ignores_unexpected_delete_failure():
    app = make_headless_gui()
    app.table = UnexpectedDeleteFailingTreeTable()
    app.table.rows["old"] = {"values": ("old",), "tags": ()}
    app.table.order.append("old")

    ArubaMmCleanupGui._replace_table(app, ["aa:bb:cc:00:00:01"], "삭제 대상")

    assert app.table.rows["old"]["values"] == ("old",)


def test_replace_table_ignores_unexpected_insert_failure():
    app = make_headless_gui()
    app.table = UnexpectedInsertFailingTreeTable()

    ArubaMmCleanupGui._replace_table(app, ["aa:bb:cc:00:00:01"], "삭제 대상")

    assert app.table.rows == {}


def test_mark_reappeared_rows_ignores_unexpected_exists_failure():
    app = make_headless_gui()
    app.table = UnexpectedExistsFailingTreeTable()

    ArubaMmCleanupGui._mark_reappeared_rows(app, ["aa:bb:cc:00:00:01"])

    assert app.table.rows == {}


def test_mark_reappeared_rows_ignores_unexpected_insert_failure():
    app = make_headless_gui()
    app.table = UnexpectedInsertFailingTreeTable()

    ArubaMmCleanupGui._mark_reappeared_rows(app, ["aa:bb:cc:00:00:01"])

    assert app.table.rows == {}


def test_mark_reappeared_rows_continues_after_per_row_table_failures():
    class PartiallyFailingReappearedTable(FakeTreeTable):
        def exists(self, item):
            if item == "aa:bb:cc:00:00:01":
                raise RuntimeError("table exists failed")
            return super().exists(item)

        def insert(self, _parent, _index, iid, values, tags=()):
            if iid == "aa:bb:cc:00:00:02":
                raise RuntimeError("table insert failed")
            return super().insert(_parent, _index, iid, values, tags=tags)

    app = make_headless_gui()
    app.table = PartiallyFailingReappearedTable()

    ArubaMmCleanupGui._mark_reappeared_rows(
        app,
        ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:03"],
    )

    assert app.table.get_children() == ("aa:bb:cc:00:00:03",)
    assert app.table.rows["aa:bb:cc:00:00:03"]["tags"] == ("reappeared",)


def test_history_cap_ignores_destroyed_history_table():
    app = make_headless_gui()
    app.history_table = DestroyedHistoryTable()

    ArubaMmCleanupGui._cap_history_rows(app)

    assert app.history_table.rows == {}


def test_history_cap_ignores_unexpected_children_failure():
    app = make_headless_gui()
    app.history_table = UnexpectedDeleteFailingHistoryTable()

    ArubaMmCleanupGui._cap_history_rows(app)

    assert app.history_table.rows == {}


def test_history_cap_ignores_unreadable_children_length():
    class UnreadableChildren:
        def __len__(self):
            raise RuntimeError("bad history children length")

    class UnreadableChildrenHistoryTable(FakeHistoryTable):
        def get_children(self):
            return UnreadableChildren()

    app = make_headless_gui()
    app.history_table = UnreadableChildrenHistoryTable()

    ArubaMmCleanupGui._cap_history_rows(app)

    assert app.history_table.rows == {}


def test_history_cap_ignores_unexpected_delete_failure():
    app = make_headless_gui()
    app.history_table = UnexpectedCapDeleteFailingHistoryTable()
    for index in range(MAX_HISTORY_ROWS + 1):
        app.history_table.insert("", "end", iid=f"history-{index}", values=("", "", "", ""))

    ArubaMmCleanupGui._cap_history_rows(app)

    assert len(app.history_table.get_children()) == MAX_HISTORY_ROWS + 1


def test_running_state_resets_current_run_counters():
    app = make_headless_gui()

    app._set_running(True)

    assert app.counter_vars["deleted"].get() == "3"
    assert app.counter_vars["queried"].get() == "7"
    assert app.current_run_query_counted is False
    assert app.current_run_delete_counted is False
    assert app.timers[-1] == ("실행 중", "조회/삭제 처리")
    assert app.cancel_button.config["state"] == "disabled"


def test_running_state_button_failure_still_updates_timer():
    app = make_headless_gui()
    app.manual_button = FailingConfigureButton()
    app.schedule_button = FailingConfigureButton()
    app.cancel_button = FailingConfigureButton()

    ArubaMmCleanupGui._set_running(app, True)

    assert app.is_running is True
    assert app.timers[-1] == ("실행 중", "조회/삭제 처리")
    assert app.current_run_query_counted is False


def test_running_state_unexpected_button_failure_still_updates_timer():
    app = make_headless_gui()
    app.manual_button = UnexpectedConfigureFailingButton()
    app.schedule_button = UnexpectedConfigureFailingButton()
    app.cancel_button = UnexpectedConfigureFailingButton()

    ArubaMmCleanupGui._set_running(app, True)

    assert app.is_running is True
    assert app.timers[-1] == ("실행 중", "조회/삭제 처리")
    assert app.current_run_query_counted is False


def test_count_current_query_handles_bad_count_without_stopping_dashboard():
    class BadCount:
        def __str__(self):
            raise RuntimeError("bad count")

    app = make_headless_gui()

    ArubaMmCleanupGui._count_current_query(app, BadCount())  # type: ignore[arg-type]

    assert app.current_run_queried_count == 0
    assert app.current_run_query_counted is True
    assert app.counter_vars["queried"].get() == "7"


def test_set_timer_ignores_destroyed_timer_variables():
    app = make_headless_gui()
    app.timer_value_var = FailingSetVar()
    app.timer_state_var = FailingSetVar()

    ArubaMmCleanupGui._set_timer(app, "실행 중", "조회/삭제 처리")

    assert app.timer_value_var.value == "0"
    assert app.timer_state_var.value == "0"


def test_set_timer_ignores_unexpected_timer_variable_failure():
    app = make_headless_gui()
    app.timer_value_var = UnexpectedSetFailingVar()
    app.timer_state_var = UnexpectedSetFailingVar()

    ArubaMmCleanupGui._set_timer(app, "실행 중", "조회/삭제 처리")

    assert app.timer_value_var.value == "0"
    assert app.timer_state_var.value == "0"


def test_sync_counter_vars_ignores_destroyed_counter_variables():
    app = make_headless_gui()
    app.counter_vars = {
        "queried": FailingSetVar("7"),
        "deleted": FailingSetVar("3"),
    }
    app.cumulative_queried_count = 9
    app.cumulative_deleted_count = 5

    ArubaMmCleanupGui._sync_counter_vars(app)

    assert app.counter_vars["queried"].value == "7"
    assert app.counter_vars["deleted"].value == "3"


def test_sync_counter_vars_ignores_unexpected_counter_variable_failure():
    app = make_headless_gui()
    app.counter_vars = {
        "queried": UnexpectedSetFailingVar("7"),
        "deleted": UnexpectedSetFailingVar("3"),
    }
    app.cumulative_queried_count = 9
    app.cumulative_deleted_count = 5

    ArubaMmCleanupGui._sync_counter_vars(app)

    assert app.counter_vars["queried"].value == "7"
    assert app.counter_vars["deleted"].value == "3"


def test_ensure_cumulative_counters_handles_destroyed_counter_reads():
    app = make_headless_gui()
    del app.cumulative_queried_count
    del app.cumulative_deleted_count
    app.counter_vars = {
        "queried": FailingGetVar("7"),
        "deleted": FailingGetVar("3"),
    }

    ArubaMmCleanupGui._ensure_cumulative_counters(app)

    assert app.cumulative_queried_count == 0
    assert app.cumulative_deleted_count == 0
    assert app.current_run_queried_count == 0
    assert app.current_run_query_counted is False
    assert app.current_run_delete_counted is False


def test_ensure_cumulative_counters_handles_unexpected_counter_reads():
    app = make_headless_gui()
    del app.cumulative_queried_count
    del app.cumulative_deleted_count
    app.counter_vars = {
        "queried": UnexpectedGetFailingVar("7"),
        "deleted": UnexpectedGetFailingVar("3"),
    }

    ArubaMmCleanupGui._ensure_cumulative_counters(app)

    assert app.cumulative_queried_count == 0
    assert app.cumulative_deleted_count == 0
    assert app.current_run_queried_count == 0
    assert app.current_run_query_counted is False
    assert app.current_run_delete_counted is False


def test_enqueue_event_drops_worker_events_after_closing():
    app = make_headless_gui()

    assert app._enqueue_event("running", True) is True
    app.closing = True
    assert app._enqueue_event("running", False) is False

    assert app.event_queue.get_nowait() == ("running", True)
    assert app.event_queue.empty()


def test_enqueue_event_returns_false_when_queue_put_fails():
    class PutFailingQueue:
        def put(self, _item):
            raise RuntimeError("queue put failed")

    app = make_headless_gui()
    app.event_queue = PutFailingQueue()

    assert ArubaMmCleanupGui._enqueue_event(app, "running", True) is False


def test_drain_events_handles_bad_countdown_payload_and_continues():
    app = make_headless_gui()
    app.event_queue.put(("progress", ("countdown", {"remaining": "bad"})))
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert not any("이벤트 처리 실패(progress)" in message for message in app.logs)
    assert app.scheduler_running is False
    assert app.stop_schedule_button.config["state"] == "disabled"
    assert app.timers[-1] == ("-", "대기")
    assert app.scheduled_callbacks[-1][0] == 150


def test_drain_events_logs_malformed_queue_item_and_continues():
    app = make_headless_gui()
    app.scheduler_running = True
    app.event_queue.put(("progress",))
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert any("이벤트 형식 오류" in message for message in app.logs)
    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")
    assert app.scheduled_callbacks[-1][0] == 150


def test_drain_events_logs_malformed_progress_payload_and_continues():
    app = make_headless_gui()
    app.scheduler_running = True
    app.event_queue.put(("progress", None))
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert any("진행 이벤트 형식 오류" in message for message in app.logs)
    assert not any("이벤트 처리 실패(progress)" in message for message in app.logs)
    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_drain_events_logs_unprintable_malformed_queue_error_and_continues():
    app = make_headless_gui()
    app.scheduler_running = True
    app.event_queue.put(BadQueueItem())
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert "WARNING: 이벤트 형식 오류 - BadValueErrorText" in app.logs
    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_drain_events_logs_unexpected_malformed_queue_error_and_continues():
    app = make_headless_gui()
    app.scheduler_running = True
    app.event_queue.put(RuntimeFailingQueueItem())
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert "WARNING: 이벤트 형식 오류 - queue item failed" in app.logs
    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_drain_events_logs_unprintable_event_processing_error_and_continues():
    app = make_headless_gui()
    app.scheduler_running = True

    def fail_summary(_payload):
        raise BadErrorText()

    app._handle_summary = fail_summary
    app.event_queue.put(("summary", object()))
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert "WARNING: 이벤트 처리 실패(summary) - BadErrorText" in app.logs
    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_scheduler_stopped_button_failure_still_updates_timer():
    app = make_headless_gui()
    app.scheduler_running = True
    app.manual_button = FailingConfigureButton()
    app.schedule_button = FailingConfigureButton()
    app.stop_schedule_button = FailingConfigureButton()
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_scheduler_stopped_unexpected_button_failure_still_updates_timer():
    app = make_headless_gui()
    app.scheduler_running = True
    app.manual_button = UnexpectedConfigureFailingButton()
    app.schedule_button = UnexpectedConfigureFailingButton()
    app.stop_schedule_button = UnexpectedConfigureFailingButton()
    app.event_queue.put(("scheduler_stopped", None))

    ArubaMmCleanupGui._drain_events(app)

    assert app.scheduler_running is False
    assert app.timers[-1] == ("-", "대기")


def test_sync_settings_visibility_toggles_settings_frame():
    app = make_headless_gui()
    app.settings_frame = FakeSettingsFrame()
    app.is_running = True
    app.scheduler_running = False

    ArubaMmCleanupGui._sync_settings_visibility(app)

    assert app.settings_frame.hidden is True
    assert app.settings_frame.grid_remove_calls == 1

    app.is_running = False
    ArubaMmCleanupGui._sync_settings_visibility(app)

    assert app.settings_frame.hidden is False
    assert app.settings_frame.grid_calls == 1


def test_sync_settings_visibility_ignores_destroyed_settings_frame():
    app = make_headless_gui()
    app.settings_frame = DestroyedSettingsFrame()
    app.is_running = True
    app.scheduler_running = False

    ArubaMmCleanupGui._sync_settings_visibility(app)

    app.is_running = False
    ArubaMmCleanupGui._sync_settings_visibility(app)


def test_sync_settings_visibility_ignores_unexpected_settings_frame_failure():
    app = make_headless_gui()
    app.settings_frame = UnexpectedFailingSettingsFrame()
    app.is_running = True
    app.scheduler_running = False

    ArubaMmCleanupGui._sync_settings_visibility(app)

    app.is_running = False
    ArubaMmCleanupGui._sync_settings_visibility(app)


def test_drain_events_handles_missing_progress_payload_as_empty_dict():
    app = make_headless_gui()
    app.event_queue.put(("progress", ("connect_start", None)))

    ArubaMmCleanupGui._drain_events(app)

    assert app.status_var.get() == "MM 접속 중"
    assert "CONNECT: None" in app.logs
    assert not any("이벤트 처리 실패(progress)" in message for message in app.logs)


def test_drain_events_handles_unprintable_progress_event_name():
    class BadProgressEvent:
        def __str__(self):
            raise RuntimeError("bad progress event")

    app = make_headless_gui()
    app.event_queue.put(("progress", (BadProgressEvent(), {})))

    ArubaMmCleanupGui._drain_events(app)

    assert not any("이벤트 처리 실패(progress)" in message for message in app.logs)


def test_drain_events_treats_unreadable_running_payload_as_stopped():
    class BadRunning:
        def __bool__(self):
            raise RuntimeError("bad running bool")

    app = make_headless_gui()
    app.is_running = True
    app.event_queue.put(("running", BadRunning()))

    ArubaMmCleanupGui._drain_events(app)

    assert app.is_running is False
    assert app.timers[-1] == ("-", "대기")
    assert not any("이벤트 처리 실패(running)" in message for message in app.logs)


def test_drain_events_handles_unprintable_next_run_payload():
    class BadNextRun:
        def __str__(self):
            raise RuntimeError("bad next run text")

    app = make_headless_gui()
    app.event_queue.put(("next_run", BadNextRun()))

    ArubaMmCleanupGui._drain_events(app)

    assert app.timers[-1] == ("BadNextRuns", "다음 실행")
    assert not any("이벤트 처리 실패(next_run)" in message for message in app.logs)


def test_drain_events_logs_queue_get_failure_and_reschedules():
    class GetFailingQueue:
        def get_nowait(self):
            raise RuntimeError("queue get failed")

    app = make_headless_gui()
    app.event_queue = GetFailingQueue()

    ArubaMmCleanupGui._drain_events(app)

    assert "WARNING: 이벤트 큐 처리 실패 - queue get failed" in app.logs
    assert app.scheduled_callbacks[-1][0] == 150


def test_drain_events_ignores_reschedule_failure():
    app = make_headless_gui()
    app._drain_after_id = "old-after"
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(tk.TclError("invalid command name"))

    ArubaMmCleanupGui._drain_events(app)

    assert app._drain_after_id is None


def test_drain_events_ignores_unexpected_reschedule_failure():
    app = make_headless_gui()
    app._drain_after_id = "old-after"
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(RuntimeError("after failed"))

    ArubaMmCleanupGui._drain_events(app)

    assert app._drain_after_id is None


def test_run_once_worker_reports_unexpected_runner_failure_and_resets_running():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(
        run_once=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runner failed"))
    )

    ArubaMmCleanupGui._run_once_worker(app, object(), object(), Path("outputs"))

    assert app.event_queue.get_nowait() == ("progress", ("run_error", {"error": "runner failed"}))
    assert app.event_queue.get_nowait() == ("running", False)
    assert app.event_queue.empty()


def test_run_once_worker_reports_direct_run_summary_failure_and_resets_running():
    app = make_headless_gui()

    def fail_summary(*_args, **_kwargs):
        raise RuntimeError("summary failed")

    app._run_summary = fail_summary

    ArubaMmCleanupGui._run_once_worker(app, object(), object(), Path("outputs"))

    assert app.event_queue.get_nowait() == ("progress", ("run_error", {"error": "summary failed"}))
    assert app.event_queue.get_nowait() == ("running", False)
    assert app.event_queue.empty()


def test_run_summary_reports_unprintable_unexpected_runner_failure():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(run_once=lambda *_args, **_kwargs: (_ for _ in ()).throw(BadErrorText()))

    ArubaMmCleanupGui._run_summary(app, object(), object(), Path("outputs"))

    assert app.event_queue.get_nowait() == ("progress", ("run_error", {"error": "BadErrorText"}))
    assert app.event_queue.empty()


def test_scheduler_loop_reports_unexpected_runner_failure_and_stops_cleanly():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()

    def fail_and_stop(*_args, **_kwargs):
        app.scheduler_stop_event.set()
        raise RuntimeError("runner failed")

    app.runner = SimpleNamespace(run_once=fail_and_stop)

    ArubaMmCleanupGui._scheduler_loop(app, object(), object(), Path("outputs"), 1)

    assert app.event_queue.get_nowait() == ("running", True)
    assert app.event_queue.get_nowait() == ("progress", ("run_error", {"error": "runner failed"}))
    assert app.event_queue.get_nowait() == ("running", False)
    assert app.event_queue.get_nowait() == ("scheduler_stopped", None)
    assert app.event_queue.empty()


def test_scheduler_loop_stops_after_runner_failure_without_waiting_next_run():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    wait_calls = []
    app.runner = SimpleNamespace(
        run_once=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runner failed"))
    )

    def wait_after_failure(seconds):
        wait_calls.append(seconds)
        app.scheduler_stop_event.set()
        return True

    app.scheduler_stop_event.wait = wait_after_failure  # type: ignore[method-assign]

    ArubaMmCleanupGui._scheduler_loop(app, object(), object(), Path("outputs"), 1)

    assert wait_calls == []
    assert list(app.event_queue.queue) == [
        ("running", True),
        ("progress", ("run_error", {"error": "runner failed"})),
        ("running", False),
        ("scheduler_stopped", None),
    ]


def test_scheduler_loop_reports_direct_run_summary_failure_and_stops_cleanly():
    app = make_headless_gui()

    def fail_summary(*_args, **_kwargs):
        raise RuntimeError("summary failed")

    app._run_summary = fail_summary

    ArubaMmCleanupGui._scheduler_loop(app, object(), object(), Path("outputs"), 1)

    assert app.event_queue.get_nowait() == ("running", True)
    assert app.event_queue.get_nowait() == ("progress", ("run_error", {"error": "summary failed"}))
    assert app.event_queue.get_nowait() == ("running", False)
    assert app.event_queue.get_nowait() == ("scheduler_stopped", None)
    assert app.event_queue.empty()


def test_scheduler_loop_repeats_cycles_without_stale_cancel_or_running_state():
    app = make_headless_gui()
    runs = []
    app.scheduler_stop_event.wait = lambda _seconds: False  # type: ignore[method-assign]

    def run_summary(config, settings, output_dir):
        runs.append((config, settings, output_dir, app.cancel_event.is_set()))
        app.cancel_event.set()
        if len(runs) == 3:
            app.scheduler_stop_event.set()

    app._run_summary = run_summary

    ArubaMmCleanupGui._scheduler_loop(app, "config", "settings", Path("outputs"), 1)

    assert runs == [
        ("config", "settings", Path("outputs"), False),
        ("config", "settings", Path("outputs"), False),
        ("config", "settings", Path("outputs"), False),
    ]
    assert list(app.event_queue.queue) == [
        ("running", True),
        ("running", False),
        ("next_run", 1),
        ("running", True),
        ("running", False),
        ("next_run", 1),
        ("running", True),
        ("running", False),
        ("scheduler_stopped", None),
    ]


def test_scheduler_loop_coerces_unexpected_interval_and_stops_cleanly():
    class BadInterval:
        def __str__(self):
            raise RuntimeError("bad interval")

    app = make_headless_gui()
    runs = []

    def run_summary(config, settings, output_dir):
        runs.append((config, settings, output_dir))

    def wait_once(seconds):
        app.scheduler_stop_event.set()
        return True

    app._run_summary = run_summary
    app.scheduler_stop_event.wait = wait_once  # type: ignore[method-assign]

    ArubaMmCleanupGui._scheduler_loop(app, "config", "settings", Path("outputs"), BadInterval())  # type: ignore[arg-type]

    assert runs == [("config", "settings", Path("outputs"))]
    assert list(app.event_queue.queue) == [
        ("running", True),
        ("running", False),
        ("next_run", MIN_INTERVAL_SECONDS),
        ("scheduler_stopped", None),
    ]


def test_scheduler_loop_stops_cleanly_when_wait_fails():
    app = make_headless_gui()
    runs = []

    def run_summary(config, settings, output_dir):
        runs.append((config, settings, output_dir))

    def failing_wait(_seconds):
        raise RuntimeError("wait failed")

    app._run_summary = run_summary
    app.scheduler_stop_event.wait = failing_wait  # type: ignore[method-assign]

    ArubaMmCleanupGui._scheduler_loop(app, "config", "settings", Path("outputs"), 1)

    assert runs == [("config", "settings", Path("outputs"))]
    assert list(app.event_queue.queue) == [
        ("running", True),
        ("running", False),
        ("next_run", 1),
        ("scheduler_stopped", None),
    ]


def test_on_close_sets_flags_and_schedules_bounded_destroy_without_direct_close():
    app = make_headless_gui()
    app._drain_after_id = "drain-id"
    canceled = []
    scheduled = []
    close_calls = []
    app.after_cancel = lambda after_id: canceled.append(after_id)
    app.after = lambda ms, callback: scheduled.append((ms, callback)) or "shutdown-id"
    app._start_session_close = lambda **kwargs: close_calls.append(kwargs)

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert app.scheduler_stop_event.is_set()
    assert app.cancel_event.is_set()
    assert canceled == ["drain-id"]
    assert close_calls == [{"reason": "app_close", "enqueue_progress": False}]
    assert scheduled[0][0] == SHUTDOWN_GRACE_MS


def test_scheduler_stop_cancel_disconnect_and_close_do_not_duplicate_session_close():
    class AliveCloseWorker:
        def is_alive(self):
            return True

    app = make_headless_gui()
    app.scheduler_running = True
    app.is_running = False
    app.session_close_worker = AliveCloseWorker()
    app._drain_after_id = "drain-id"
    app.copy_notice_after_id = "copy-notice-id"
    scheduled = []
    app.after = lambda ms, callback: scheduled.append((ms, callback)) or "shutdown-id"

    ArubaMmCleanupGui.stop_scheduler(app)
    ArubaMmCleanupGui.cancel_current_delete(app)
    ArubaMmCleanupGui.disconnect_session(app)
    ArubaMmCleanupGui.on_close(app)

    assert app.scheduler_running is False
    assert app.scheduler_stop_event.is_set()
    assert app.cancel_event.is_set()
    assert app.closing is True
    assert isinstance(app.session_close_worker, AliveCloseWorker)
    assert app.canceled_after_ids == ["drain-id", "copy-notice-id"]
    assert scheduled[0][0] == SHUTDOWN_GRACE_MS
    assert "주기 실행 정지 요청" in app.logs
    assert "이번 삭제 취소 요청" in app.logs
    assert "SESSION DISCONNECT REQUEST" in app.logs


def test_stop_scheduler_unexpected_button_failure_still_finishes_stop():
    app = make_headless_gui()
    app.scheduler_running = True
    app.is_running = False
    app.manual_button = UnexpectedConfigureFailingButton()
    app.schedule_button = UnexpectedConfigureFailingButton()
    app.stop_schedule_button = UnexpectedConfigureFailingButton()

    ArubaMmCleanupGui.stop_scheduler(app)

    assert app.scheduler_running is False
    assert app.scheduler_stop_event.is_set()
    assert app.cancel_event.is_set()
    assert app.timers[-1] == ("-", "대기")
    assert "주기 실행 정지 요청" in app.logs


def test_on_close_destroys_window_when_shutdown_after_fails():
    app = make_headless_gui()
    app._drain_after_id = None
    app.copy_notice_after_id = None
    close_calls = []
    destroy_calls = []
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(tk.TclError("invalid command name"))
    app._start_session_close = lambda **kwargs: close_calls.append(kwargs)
    app._destroy_window = lambda: destroy_calls.append("destroyed")

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert close_calls == [{"reason": "app_close", "enqueue_progress": False}]
    assert destroy_calls == ["destroyed"]


def test_on_close_continues_when_drain_after_cancel_unexpectedly_fails():
    app = make_headless_gui()
    app._drain_after_id = "drain-id"
    app.copy_notice_after_id = None
    close_calls = []
    scheduled = []
    app.after_cancel = lambda _after_id: (_ for _ in ()).throw(RuntimeError("after cancel failed"))
    app.after = lambda ms, callback: scheduled.append((ms, callback)) or "shutdown-id"
    app._start_session_close = lambda **kwargs: close_calls.append(kwargs)

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert app._drain_after_id is None
    assert close_calls == [{"reason": "app_close", "enqueue_progress": False}]
    assert scheduled[0][0] == SHUTDOWN_GRACE_MS


def test_on_close_continues_when_copy_notice_after_cancel_unexpectedly_fails():
    app = make_headless_gui()
    app._drain_after_id = None
    app.copy_notice_after_id = "copy-notice-id"
    close_calls = []
    scheduled = []
    app.after_cancel = lambda _after_id: (_ for _ in ()).throw(RuntimeError("after cancel failed"))
    app.after = lambda ms, callback: scheduled.append((ms, callback)) or "shutdown-id"
    app._start_session_close = lambda **kwargs: close_calls.append(kwargs)

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert app.copy_notice_after_id is None
    assert close_calls == [{"reason": "app_close", "enqueue_progress": False}]
    assert scheduled[0][0] == SHUTDOWN_GRACE_MS


def test_on_close_destroys_window_when_shutdown_after_unexpectedly_fails():
    app = make_headless_gui()
    app._drain_after_id = None
    app.copy_notice_after_id = None
    close_calls = []
    destroy_calls = []
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(RuntimeError("after failed"))
    app._start_session_close = lambda **kwargs: close_calls.append(kwargs)
    app._destroy_window = lambda: destroy_calls.append("destroyed")

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert close_calls == [{"reason": "app_close", "enqueue_progress": False}]
    assert destroy_calls == ["destroyed"]


def test_destroy_window_ignores_unexpected_destroy_failure():
    app = make_headless_gui()
    app.destroy = lambda: (_ for _ in ()).throw(RuntimeError("destroy failed"))

    ArubaMmCleanupGui._destroy_window(app)


def test_gui_smoke_main_uses_safe_destroy_when_destroy_raises(monkeypatch):
    class SmokeApp:
        def __init__(self):
            self._drain_after_id = "after-1"
            self.closing = False
            self.canceled_after_ids = []
            self.safe_destroy_calls = 0

        def update_idletasks(self):
            pass

        def after_cancel(self, after_id):
            self.canceled_after_ids.append(after_id)

        def destroy(self):
            raise tk.TclError("invalid command name")

        def _destroy_window(self):
            self.safe_destroy_calls += 1
            try:
                self.destroy()
            except tk.TclError:
                pass

    app = SmokeApp()
    monkeypatch.setenv("ARUBA_MM_CLEANUP_GUI_SMOKE", "1")
    monkeypatch.setattr(gui_app_module, "ArubaMmCleanupGui", lambda: app)

    assert gui_app_module.main() == 0
    assert app.closing is True
    assert app.canceled_after_ids == ["after-1"]
    assert app.safe_destroy_calls == 1


def test_gui_smoke_main_ignores_unexpected_after_cancel_failure(monkeypatch):
    class SmokeApp:
        def __init__(self):
            self._drain_after_id = "after-1"
            self.closing = False
            self.safe_destroy_calls = 0

        def update_idletasks(self):
            pass

        def after_cancel(self, _after_id):
            raise RuntimeError("after cancel failed")

        def _destroy_window(self):
            self.safe_destroy_calls += 1

    app = SmokeApp()
    monkeypatch.setenv("ARUBA_MM_CLEANUP_GUI_SMOKE", "1")
    monkeypatch.setattr(gui_app_module, "ArubaMmCleanupGui", lambda: app)

    assert gui_app_module.main() == 0
    assert app.closing is True
    assert app.safe_destroy_calls == 1


def test_start_session_close_returns_without_waiting_for_runner_lock():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner_lock.acquire()
    close_calls = []
    app.runner = SimpleNamespace(close_session=lambda **_kwargs: close_calls.append("closed"))
    app.session_close_worker = None

    started = time.monotonic()
    app._start_session_close(reason="manual", enqueue_progress=False)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert close_calls == []
    app.runner_lock.release()
    app.session_close_worker.join(timeout=2)
    assert close_calls == ["closed"]


def test_start_session_close_thread_start_failure_does_not_raise(monkeypatch):
    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    app = make_headless_gui()
    app.session_close_worker = None
    monkeypatch.setattr(gui_app_module.threading, "Thread", FailingThread)

    ArubaMmCleanupGui._start_session_close(app, reason="manual", enqueue_progress=True)

    assert app.session_close_worker is None
    assert app.event_queue.get_nowait() == (
        "progress",
        (
            "warning",
            {"message": "세션 종료 스레드 시작 실패 - thread start failed", "reason": "manual"},
        ),
    )
    assert app.event_queue.empty()


def test_start_session_close_thread_create_failure_does_not_raise(monkeypatch):
    def failing_thread(*_args, **_kwargs):
        raise RuntimeError("thread create failed")

    app = make_headless_gui()
    app.session_close_worker = None
    monkeypatch.setattr(gui_app_module.threading, "Thread", failing_thread)

    ArubaMmCleanupGui._start_session_close(app, reason="manual", enqueue_progress=True)

    assert app.session_close_worker is None
    assert app.event_queue.get_nowait() == (
        "progress",
        (
            "warning",
            {"message": "세션 종료 스레드 시작 실패 - thread create failed", "reason": "manual"},
        ),
    )
    assert app.event_queue.empty()


def test_start_session_close_recovers_when_existing_worker_state_fails():
    class BrokenWorker:
        def is_alive(self):
            raise RuntimeError("worker state failed")

    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    close_calls = []
    app.runner = SimpleNamespace(close_session=lambda **kwargs: close_calls.append(kwargs))
    app.session_close_worker = BrokenWorker()

    ArubaMmCleanupGui._start_session_close(app, reason="manual", enqueue_progress=False)

    app.session_close_worker.join(timeout=2)
    assert not app.session_close_worker.is_alive()
    assert len(close_calls) == 1
    assert close_calls[0]["progress_callback"] is None
    assert close_calls[0]["reason"] == "manual"


def test_start_session_close_serializes_concurrent_worker_creation(monkeypatch):
    original_thread = threading.Thread
    constructor_entered = threading.Event()
    release_constructor = threading.Event()
    created_workers = []

    class SlowCloseWorker:
        def __init__(self, *_args, **_kwargs):
            created_workers.append(self)
            constructor_entered.set()
            release_constructor.wait(timeout=2)

        def start(self):
            pass

        def is_alive(self):
            return True

    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(close_session=lambda **_kwargs: None)
    app.session_close_worker = None
    monkeypatch.setattr(gui_app_module.threading, "Thread", SlowCloseWorker)

    first = original_thread(
        target=lambda: ArubaMmCleanupGui._start_session_close(app, reason="manual", enqueue_progress=False)
    )
    second = original_thread(
        target=lambda: ArubaMmCleanupGui._start_session_close(app, reason="app_close", enqueue_progress=False)
    )

    first.start()
    assert constructor_entered.wait(timeout=2)
    second.start()
    time.sleep(0.05)

    assert len(created_workers) == 1

    release_constructor.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(created_workers) == 1


def test_on_close_continues_when_session_close_thread_start_fails(monkeypatch):
    class FailingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    app = make_headless_gui()
    app.session_close_worker = None
    app._drain_after_id = None
    app.copy_notice_after_id = None
    scheduled = []
    app.after = lambda ms, callback: scheduled.append((ms, callback)) or "shutdown-id"
    monkeypatch.setattr(gui_app_module.threading, "Thread", FailingThread)

    ArubaMmCleanupGui.on_close(app)

    assert app.closing is True
    assert app.session_close_worker is None
    assert scheduled[0][0] == SHUTDOWN_GRACE_MS
    assert "WARNING: 세션 종료 스레드 시작 실패 - thread start failed" in app.logs


def test_close_runner_session_reports_manual_close_failure():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(
        close_session=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("close failed"))
    )

    ArubaMmCleanupGui._close_runner_session(app, reason="manual", enqueue_progress=True)

    assert app.event_queue.get_nowait() == (
        "progress",
        ("warning", {"message": "session close failed: close failed", "reason": "manual"}),
    )
    assert app.event_queue.empty()


def test_close_runner_session_reports_unprintable_manual_close_failure():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(close_session=lambda **_kwargs: (_ for _ in ()).throw(BadErrorText()))

    ArubaMmCleanupGui._close_runner_session(app, reason="manual", enqueue_progress=True)

    assert app.event_queue.get_nowait() == (
        "progress",
        ("warning", {"message": "session close failed: BadErrorText", "reason": "manual"}),
    )
    assert app.event_queue.empty()


def test_close_runner_session_ignores_app_close_failure_without_progress():
    app = make_headless_gui()
    app.runner_lock = threading.Lock()
    app.runner = SimpleNamespace(
        close_session=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("close failed"))
    )

    ArubaMmCleanupGui._close_runner_session(app, reason="app_close", enqueue_progress=False)

    assert app.event_queue.empty()


def test_history_load_restores_jsonl_rows(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    history_path = output_dir / HISTORY_FILE_NAME
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_at": "2026-07-02T13:00:00",
                        "mac": "aa:bb:cc:00:00:01",
                        "result": "삭제 완료",
                        "status": "verified_deleted",
                        "success": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "run_at": "2026-07-02T13:01:00",
                        "mac": "aa:bb:cc:00:00:02",
                        "status": "reappeared",
                        "success": False,
                        "error": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [
        ("2026-07-02 13:00:00", "aa:bb:cc:00:00:01", "삭제 완료", ""),
        ("2026-07-02 13:01:00", "aa:bb:cc:00:00:02", "재조회됨", "삭제 성공 후 검증 조회에서 다시 발견"),
    ]


def test_history_read_keeps_only_recent_jsonl_records(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    history_path = output_dir / HISTORY_FILE_NAME
    records = [
        json.dumps(
            {
                "run_at": f"2026-07-02T13:{index % 60:02d}:00",
                "mac": f"aa:bb:cc:00:{index // 256:02x}:{index % 256:02x}",
                "status": "verified_deleted",
            },
            ensure_ascii=False,
        )
        for index in range(MAX_HISTORY_ROWS + 3)
    ]
    history_path.write_text("\n".join(records), encoding="utf-8")
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert len(loaded) == MAX_HISTORY_ROWS
    assert loaded[0]["mac"] == "aa:bb:cc:00:00:03"
    assert loaded[-1]["mac"] == "aa:bb:cc:00:01:f6"


def test_history_row_display_handles_bad_record_value_conversion():
    class BadText:
        def __str__(self):
            raise RuntimeError("bad text")

    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    app = make_headless_gui()

    result, error, tags = ArubaMmCleanupGui._history_row_display(
        app,
        {
            "status": BadText(),
            "result": BadBool(),
            "error": BadText(),
            "success": BadBool(),
            "reappeared": BadBool(),
        },
    )

    assert (result, error, tags) == ("삭제 실패", "", ())


def test_clear_history_ignores_destroyed_history_table():
    app = make_headless_gui()
    app.history_table = DestroyedHistoryTable()
    app.history_table.insert("", "end", iid="history-0", values=("run", "mac", "result", ""))
    app.history_row_counter = 1

    ArubaMmCleanupGui.clear_history(app)

    assert app.history_row_counter == 1


def test_clear_history_ignores_unexpected_history_table_delete_failure():
    app = make_headless_gui()
    app.history_table = UnexpectedDeleteFailingHistoryTable()
    app.history_row_counter = 1

    ArubaMmCleanupGui.clear_history(app)

    assert app.history_row_counter == 1


def test_history_load_ignores_destroyed_history_table(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = DestroyedHistoryTable()
    app.history_table.insert("", "end", iid="history-0", values=("run", "mac", "result", ""))
    app.history_row_counter = 1
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_row_counter == 1


def test_history_load_ignores_output_dir_expanduser_failure():
    class BadOutputDir:
        def expanduser(self):
            raise RuntimeError("bad expanduser")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_table.insert(
        "",
        "end",
        iid="history-0",
        values=("old-run", "old-mac", "old-result", ""),
    )
    app.history_row_counter = 1
    app.loaded_history_dir = Path("old-output")

    app._load_history_from_output_dir(BadOutputDir(), force=True)  # type: ignore[arg-type]

    assert app.history_table.get_children() == ("history-0",)
    assert app.history_table.rows["history-0"]["values"] == (
        "old-run",
        "old-mac",
        "old-result",
        "",
    )
    assert app.history_row_counter == 1
    assert app.loaded_history_dir == Path("old-output")


def test_history_load_retries_same_directory_after_read_failure(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None
    attempts = []

    def flaky_read_history(_output_dir):
        attempts.append("read")
        if len(attempts) == 1:
            raise RuntimeError("history read failed")
        return [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]

    app._read_history_records = flaky_read_history

    app._load_history_from_output_dir(output_dir)
    app._load_history_from_output_dir(output_dir)

    assert attempts == ["read", "read"]
    assert app.loaded_history_dir == output_dir
    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [("2026-07-02 13:00:00", "aa:bb:cc:00:00:01", "삭제 실패", "")]


def test_history_load_ignores_unreadable_records_container(tmp_path):
    class UnreadableRecords(list):
        def __getitem__(self, key):
            if isinstance(key, slice):
                raise RuntimeError("bad history records slice")
            return super().__getitem__(key)

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None
    app._read_history_records = lambda _output_dir: UnreadableRecords(
        [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]
    )

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_load_preserves_existing_rows_when_records_slice_fails(tmp_path):
    class UnreadableRecords(list):
        def __getitem__(self, key):
            if isinstance(key, slice):
                raise RuntimeError("bad history records slice")
            return super().__getitem__(key)

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_table.insert(
        "",
        "end",
        iid="history-0",
        values=("old-run", "old-mac", "old-result", ""),
    )
    app.history_row_counter = 1
    app.loaded_history_dir = None
    app._read_history_records = lambda _output_dir: UnreadableRecords(
        [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]
    )

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ("history-0",)
    assert app.history_table.rows["history-0"]["values"] == (
        "old-run",
        "old-mac",
        "old-result",
        "",
    )
    assert app.history_row_counter == 1
    assert app.loaded_history_dir is None


def test_history_load_retries_same_directory_after_records_slice_failure(tmp_path):
    class UnreadableRecords(list):
        def __getitem__(self, key):
            if isinstance(key, slice):
                raise RuntimeError("bad history records slice")
            return super().__getitem__(key)

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None
    attempts = []

    def flaky_read_history(_output_dir):
        attempts.append("read")
        if len(attempts) == 1:
            return UnreadableRecords(
                [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:ff"}]
            )
        return [{"run_at": "2026-07-02T13:01:00", "mac": "aa:bb:cc:00:00:01"}]

    app._read_history_records = flaky_read_history

    app._load_history_from_output_dir(output_dir)
    app._load_history_from_output_dir(output_dir)

    assert attempts == ["read", "read"]
    assert app.loaded_history_dir == output_dir
    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [("2026-07-02 13:01:00", "aa:bb:cc:00:00:01", "삭제 실패", "")]


def test_history_load_ignores_unexpected_history_table_delete_failure(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = UnexpectedDeleteFailingHistoryTable()
    app.history_row_counter = 1
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_row_counter == 1


def test_history_load_ignores_history_row_insert_failure(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = InsertFailingHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_row_counter == 0


def test_history_load_ignores_unexpected_history_row_insert_failure(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = UnexpectedInsertFailingHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_row_counter == 0


def test_history_load_skips_records_with_unprintable_run_at_or_mac(tmp_path):
    class BadText:
        def __str__(self):
            raise RuntimeError("bad text")

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None
    app._read_history_records = lambda _output_dir: [
        {"run_at": BadText(), "mac": "aa:bb:cc:00:00:01", "status": "verified_deleted", "success": True},
        {"run_at": "2026-07-02T13:01:00", "mac": BadText(), "status": "verified_deleted", "success": True},
        {"run_at": "2026-07-02T13:02:00", "mac": "aa:bb:cc:00:00:03", "status": "verified_deleted", "success": True},
    ]

    app._load_history_from_output_dir(output_dir)

    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [
        ("", "aa:bb:cc:00:00:01", "삭제 완료", ""),
        ("2026-07-02 13:02:00", "aa:bb:cc:00:00:03", "삭제 완료", ""),
    ]


def test_history_load_ignores_non_string_jsonl_mac(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    history_path = output_dir / HISTORY_FILE_NAME
    history_path.write_text(
        json.dumps(
            {
                "run_at": "2026-07-02T13:00:00",
                "mac": ["aa:bb:cc:00:00:01"],
                "result": "삭제 완료",
                "status": "verified_deleted",
                "success": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_load_ignores_unreadable_jsonl_path(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).mkdir()
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_read_uses_audit_fallback_when_jsonl_exists_check_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_exists = Path.exists

    def failing_history_exists(path):
        if path.name == HISTORY_FILE_NAME:
            raise OSError("history path unavailable")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", failing_history_exists)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_uses_audit_fallback_when_jsonl_exists_unexpectedly_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_exists = Path.exists

    def failing_history_exists(path):
        if path.name == HISTORY_FILE_NAME:
            raise RuntimeError("history path state unavailable")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", failing_history_exists)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_uses_audit_fallback_when_jsonl_read_fails(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (output_dir / HISTORY_FILE_NAME).write_bytes(b"\xff\xfeinvalid history")
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_uses_audit_fallback_when_jsonl_stream_unexpectedly_fails(tmp_path, monkeypatch):
    class FailingHistoryHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            raise RuntimeError("history stream failed")

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    history_path = output_dir / HISTORY_FILE_NAME
    history_path.write_text("placeholder", encoding="utf-8")
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_open = Path.open

    def failing_history_open(path, *args, **kwargs):
        if path == history_path:
            return FailingHistoryHandle()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_history_open)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_returns_empty_when_audit_glob_unexpectedly_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    original_exists = Path.exists

    def no_jsonl_history(path):
        if path.name == HISTORY_FILE_NAME:
            return False
        return original_exists(path)

    def failing_audit_glob(path, pattern):
        if path == output_dir and pattern == "*/cleanup_summary.json":
            raise RuntimeError("audit glob failed")
        return ()

    monkeypatch.setattr(Path, "exists", no_jsonl_history)
    monkeypatch.setattr(Path, "glob", failing_audit_glob)
    app = make_headless_gui()

    assert app._read_history_records(output_dir) == []


def test_history_read_audit_fallback_reads_only_recent_audit_paths(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    audit_paths = [
        output_dir / f"20260702_{index:06d}" / "cleanup_summary.json"
        for index in range(MAX_HISTORY_ROWS + 3)
    ]
    blocked_oldest = audit_paths[0]
    path_indexes = {path: index for index, path in enumerate(audit_paths)}
    original_exists = Path.exists
    original_glob = Path.glob
    original_read_text = Path.read_text

    def no_jsonl_history(path):
        if path.name == HISTORY_FILE_NAME:
            return False
        return original_exists(path)

    def fake_audit_glob(path, pattern):
        if path == output_dir and pattern == "*/cleanup_summary.json":
            return iter(audit_paths)
        return original_glob(path, pattern)

    def fake_audit_read_text(path, *args, **kwargs):
        if path == blocked_oldest:
            raise AssertionError("old audit path should not be read")
        if path in path_indexes:
            index = path_indexes[path]
            return json.dumps(
                {
                    "started_at": f"2026-07-02T13:{index % 60:02d}:00",
                    "delete_results": [
                        {
                            "mac": f"aa:bb:cc:{index // 65536:02x}:{(index // 256) % 256:02x}:{index % 256:02x}",
                            "status": "verified_deleted",
                            "success": True,
                        }
                    ],
                }
            )
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", no_jsonl_history)
    monkeypatch.setattr(Path, "glob", fake_audit_glob)
    monkeypatch.setattr(Path, "read_text", fake_audit_read_text)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert len(loaded) == MAX_HISTORY_ROWS
    assert loaded[0]["mac"] == "aa:bb:cc:00:00:03"
    assert loaded[-1]["mac"] == "aa:bb:cc:00:01:f6"


def test_history_load_ignores_invalid_encoding_jsonl(tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_bytes(b"\xff\xfeinvalid history")
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_read_skips_recursion_error_jsonl_record(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        "\n".join(
            [
                "bad-recursive-json",
                json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
            ]
        ),
        encoding="utf-8",
    )
    original_loads = json.loads

    def fake_loads(payload):
        if payload.strip() == "bad-recursive-json":
            raise RecursionError("too deeply nested")
        return original_loads(payload)

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]


def test_history_read_skips_runtime_error_jsonl_record(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        "\n".join(
            [
                "bad-runtime-json",
                json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
            ]
        ),
        encoding="utf-8",
    )
    original_loads = json.loads

    def fake_loads(payload):
        if payload.strip() == "bad-runtime-json":
            raise RuntimeError("history record failed")
        return original_loads(payload)

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]


def test_history_read_skips_unicode_error_jsonl_record(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        "\n".join(
            [
                "bad-unicode-json",
                json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
            ]
        ),
        encoding="utf-8",
    )
    original_loads = json.loads

    def fake_loads(payload):
        if payload.strip() == "bad-unicode-json":
            raise UnicodeError("bad unicode record")
        return original_loads(payload)

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]


def test_history_read_skips_jsonl_record_when_mac_access_fails(tmp_path, monkeypatch):
    class FailingMacRecord(dict):
        def get(self, key, default=None):
            if key == "mac":
                raise RuntimeError("bad mac")
            return super().get(key, default)

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text(
        "\n".join(
            [
                "bad-mac-record",
                json.dumps({"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}),
            ]
        ),
        encoding="utf-8",
    )
    original_loads = json.loads

    def fake_loads(payload):
        if payload.strip() == "bad-mac-record":
            return FailingMacRecord({"mac": "aa:bb:cc:00:00:ff"})
        return original_loads(payload)

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [{"run_at": "2026-07-02T13:00:00", "mac": "aa:bb:cc:00:00:01"}]


def test_history_load_handles_jsonl_record_when_optional_access_fails(tmp_path, monkeypatch):
    class FailingOptionalRecord(dict):
        def get(self, key, default=None):
            if key in {"run_at", "status", "result", "error", "success", "reappeared"}:
                raise RuntimeError(f"bad {key}")
            return super().get(key, default)

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / HISTORY_FILE_NAME).write_text("bad-optional-record", encoding="utf-8")
    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: FailingOptionalRecord({"mac": "aa:bb:cc:00:00:01"}),
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [("", "aa:bb:cc:00:00:01", "삭제 실패", "")]


def test_history_load_ignores_invalid_encoding_audit_fallback(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_bytes(b"\xff\xfeinvalid audit")
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_read_skips_recursion_error_audit_fallback(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    bad_run_dir = output_dir / "20260702_125900_000000"
    good_run_dir = output_dir / "20260702_130000_000000"
    bad_run_dir.mkdir(parents=True)
    good_run_dir.mkdir(parents=True)
    (bad_run_dir / "cleanup_summary.json").write_text("bad-recursive-audit", encoding="utf-8")
    (good_run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_loads = json.loads

    def fake_loads(payload):
        if payload == "bad-recursive-audit":
            raise RecursionError("too deeply nested")
        return original_loads(payload)

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_skips_unexpected_audit_read_failure(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs"
    bad_run_dir = output_dir / "20260702_125900_000000"
    good_run_dir = output_dir / "20260702_130000_000000"
    bad_run_dir.mkdir(parents=True)
    good_run_dir.mkdir(parents=True)
    bad_audit = bad_run_dir / "cleanup_summary.json"
    good_audit = good_run_dir / "cleanup_summary.json"
    bad_audit.write_text("locked audit", encoding="utf-8")
    good_audit.write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def failing_read_text(path, *args, **kwargs):
        if path == bad_audit:
            raise RuntimeError("audit read failed")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_load_ignores_non_object_audit_fallback(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("[]", encoding="utf-8")
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_read_audit_fallback_handles_unprintable_started_at(tmp_path, monkeypatch):
    class BadText:
        def __str__(self):
            raise RuntimeError("bad started_at")

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")

    def fake_loads(_payload):
        return {
            "started_at": BadText(),
            "delete_results": [
                {
                    "mac": "aa:bb:cc:00:00:01",
                    "status": "verified_deleted",
                    "success": True,
                }
            ],
        }

    monkeypatch.setattr(gui_app_module.json, "loads", fake_loads)
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "",
            "reappeared": False,
        }
    ]


def test_history_read_audit_fallback_handles_failing_optional_fields(tmp_path, monkeypatch):
    class FailingAudit(dict):
        def get(self, key, default=None):
            if key in {"reappeared_macs", "delete_results"}:
                raise RuntimeError(f"bad {key}")
            return super().get(key, default)

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")

    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: FailingAudit({"started_at": "2026-07-02T13:00:00"}),
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == []


def test_history_read_audit_fallback_handles_failing_delete_result_fields(tmp_path, monkeypatch):
    class FailingMacResult(dict):
        def get(self, key, default=None):
            if key == "mac":
                raise RuntimeError("bad mac")
            return super().get(key, default)

    class FailingStatusResult(dict):
        def get(self, key, default=None):
            if key == "status":
                raise RuntimeError("bad status")
            return super().get(key, default)

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")

    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: {
            "started_at": "2026-07-02T13:00:00",
            "delete_results": [
                FailingMacResult({"mac": "aa:bb:cc:00:00:01", "status": "verified_deleted"}),
                FailingStatusResult({"mac": "aa:bb:cc:00:00:02", "status": "reappeared"}),
                {"mac": "aa:bb:cc:00:00:03", "status": "verified_deleted"},
            ],
        },
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:02",
            "status": "reappeared",
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        },
        {
            "mac": "aa:bb:cc:00:00:03",
            "status": "verified_deleted",
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        },
    ]


def test_history_load_ignores_invalid_reappeared_macs_type(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "reappeared_macs": 1,
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [("2026-07-02 13:00:00", "aa:bb:cc:00:00:01", "삭제 완료", "")]


def test_history_load_ignores_invalid_reappeared_mac_items(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "reappeared_macs": [["aa:bb:cc:00:00:01"]],
                "delete_results": [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    rows = [app.history_table.rows[item]["values"] for item in app.history_table.get_children()]
    assert rows == [("2026-07-02 13:00:00", "aa:bb:cc:00:00:01", "삭제 완료", "")]


def test_history_read_audit_fallback_handles_unreadable_reappeared_macs(tmp_path, monkeypatch):
    class UnreadableReappearedMacs(list):
        def __iter__(self):
            raise RuntimeError("bad reappeared macs")

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")
    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: {
            "started_at": "2026-07-02T13:00:00",
            "reappeared_macs": UnreadableReappearedMacs(["aa:bb:cc:00:00:01"]),
            "delete_results": [
                {
                    "mac": "aa:bb:cc:00:00:01",
                    "status": "verified_deleted",
                    "success": True,
                }
            ],
        },
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": False,
        }
    ]


def test_history_read_audit_fallback_handles_unreadable_optional_list_lengths(tmp_path, monkeypatch):
    class UnreadableLengthList(list):
        def __len__(self):
            raise RuntimeError("bad optional list length")

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")
    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: {
            "started_at": "2026-07-02T13:00:00",
            "reappeared_macs": UnreadableLengthList(["aa:bb:cc:00:00:01"]),
            "delete_results": UnreadableLengthList(
                [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ]
            ),
        },
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == [
        {
            "mac": "aa:bb:cc:00:00:01",
            "status": "verified_deleted",
            "success": True,
            "run_at": "2026-07-02T13:00:00",
            "reappeared": True,
        }
    ]


def test_history_load_ignores_invalid_delete_results_type(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "delete_results": 1,
            }
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_history_read_audit_fallback_handles_unreadable_delete_results(tmp_path, monkeypatch):
    class UnreadableDeleteResults(list):
        def __iter__(self):
            raise RuntimeError("bad delete results")

    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text("bad audit payload", encoding="utf-8")
    monkeypatch.setattr(
        gui_app_module.json,
        "loads",
        lambda _payload: {
            "started_at": "2026-07-02T13:00:00",
            "delete_results": UnreadableDeleteResults(
                [
                    {
                        "mac": "aa:bb:cc:00:00:01",
                        "status": "verified_deleted",
                        "success": True,
                    }
                ]
            ),
        },
    )
    app = make_headless_gui()

    loaded = app._read_history_records(output_dir)

    assert loaded == []


def test_history_load_ignores_invalid_audit_mac_type(tmp_path):
    output_dir = tmp_path / "outputs"
    run_dir = output_dir / "20260702_130000_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "cleanup_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-02T13:00:00",
                "reappeared_macs": ["aa:bb:cc:00:00:01"],
                "delete_results": [
                    {
                        "mac": ["aa:bb:cc:00:00:01"],
                        "status": "verified_deleted",
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    app.loaded_history_dir = None

    app._load_history_from_output_dir(output_dir)

    assert app.history_table.get_children() == ()


def test_log_text_is_capped_to_max_lines():
    app = make_headless_gui()
    app.log_text = FakeLogText()

    for index in range(MAX_LOG_LINES + 5):
        ArubaMmCleanupGui._log(app, f"line {index}")

    assert len(app.log_text.lines) == MAX_LOG_LINES
    assert "line 0" not in app.log_text.lines[0]


def test_log_keeps_message_when_line_index_is_unexpected():
    app = make_headless_gui()
    app.log_text = BadIndexLogText()

    ArubaMmCleanupGui._log(app, "line still recorded")

    assert len(app.log_text.lines) == 1
    assert app.log_text.lines[0].endswith("line still recorded")
    assert app.log_text.state == "disabled"


def test_cap_log_lines_ignores_unexpected_index_failure():
    class UnexpectedIndexFailingLogText(FakeLogText):
        def index(self, _index):
            raise RuntimeError("log index failed")

    app = make_headless_gui()
    app.log_text = UnexpectedIndexFailingLogText()
    app.log_text.lines = [f"line {index}" for index in range(MAX_LOG_LINES + 1)]

    ArubaMmCleanupGui._cap_log_lines(app)

    assert len(app.log_text.lines) == MAX_LOG_LINES + 1


def test_log_stays_disabled_when_cap_delete_fails():
    app = make_headless_gui()
    app.log_text = DeleteFailingLogText()
    app.log_text.lines = [f"line {index}" for index in range(MAX_LOG_LINES + 1)]

    ArubaMmCleanupGui._log(app, "line still recorded")

    assert app.log_text.lines[-1].endswith("line still recorded")
    assert app.log_text.state == "disabled"


def test_log_stays_disabled_when_cap_delete_unexpectedly_fails():
    app = make_headless_gui()
    app.log_text = UnexpectedDeleteFailingLogText()
    app.log_text.lines = [f"line {index}" for index in range(MAX_LOG_LINES + 1)]

    ArubaMmCleanupGui._log(app, "line still recorded")

    assert app.log_text.lines[-1].endswith("line still recorded")
    assert app.log_text.state == "disabled"


def test_log_restores_disabled_state_when_insert_fails():
    app = make_headless_gui()
    app.log_text = InsertFailingLogText()

    ArubaMmCleanupGui._log(app, "line cannot be inserted")

    assert app.log_text.state == "disabled"
    assert app.log_text.lines == []


def test_log_restores_disabled_state_when_insert_unexpectedly_fails():
    app = make_headless_gui()
    app.log_text = UnexpectedInsertFailingLogText()

    ArubaMmCleanupGui._log(app, "line cannot be inserted")

    assert app.log_text.state == "disabled"
    assert app.log_text.lines == []


def test_log_restores_disabled_state_when_see_unexpectedly_fails():
    app = make_headless_gui()
    app.log_text = UnexpectedSeeFailingLogText()

    ArubaMmCleanupGui._log(app, "line still recorded")

    assert app.log_text.lines[-1].endswith("line still recorded")
    assert app.log_text.state == "disabled"


def test_log_ignores_destroyed_log_widget():
    app = make_headless_gui()
    app.log_text = ConfigureFailingLogText()

    ArubaMmCleanupGui._log(app, "line cannot be written")

    assert app.log_text.lines == []


def test_log_ignores_unexpected_configure_failure():
    app = make_headless_gui()
    app.log_text = UnexpectedConfigureFailingLogText()

    ArubaMmCleanupGui._log(app, "line cannot be written")

    assert app.log_text.lines == []


def test_clear_log_restores_disabled_state_when_delete_fails():
    app = make_headless_gui()
    app.log_text = DeleteFailingLogText()

    ArubaMmCleanupGui.clear_log(app)

    assert app.log_text.state == "disabled"


def test_clear_log_restores_disabled_state_when_delete_unexpectedly_fails():
    app = make_headless_gui()
    app.log_text = UnexpectedDeleteFailingLogText()

    ArubaMmCleanupGui.clear_log(app)

    assert app.log_text.state == "disabled"


def test_clear_log_ignores_unexpected_disabled_restore_failure():
    app = make_headless_gui()
    app.log_text = UnexpectedConfigureFailingLogText()

    ArubaMmCleanupGui.clear_log(app)

    assert app.log_text.lines == []


def test_append_history_rows_ignores_missing_delete_results():
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(started_at=datetime(2026, 7, 2, 13, 0, 0), reappeared_macs=[])

    ArubaMmCleanupGui._append_history_rows(app, summary)

    assert app.history_table.get_children() == ()


def test_append_history_rows_ignores_unreadable_delete_results():
    class UnreadableDeleteResults(list):
        def __iter__(self):
            raise RuntimeError("bad delete results")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=datetime(2026, 7, 2, 13, 0, 0),
        reappeared_macs=[],
        delete_results=UnreadableDeleteResults(
            [
                SimpleNamespace(
                    mac="aa:bb:cc:00:00:01",
                    status="verified_deleted",
                    success=True,
                    error="",
                )
            ]
        ),
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    assert app.history_table.get_children() == ()


def test_append_history_rows_ignores_unreadable_delete_results_length():
    class UnreadableLengthDeleteResults(list):
        def __len__(self):
            raise RuntimeError("bad delete results length")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=datetime(2026, 7, 2, 13, 0, 0),
        reappeared_macs=[],
        delete_results=UnreadableLengthDeleteResults(
            [
                SimpleNamespace(
                    mac="aa:bb:cc:00:00:01",
                    status="verified_deleted",
                    success=True,
                    error="",
                )
            ]
        ),
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    assert app.history_table.get_children() == ()


def test_append_history_rows_ignores_invalid_reappeared_mac_items():
    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=datetime(2026, 7, 2, 13, 0, 0),
        reappeared_macs=[["aa:bb:cc:00:00:01"]],
        delete_results=[
            SimpleNamespace(
                mac="aa:bb:cc:00:00:01",
                status="verified_deleted",
                success=True,
                error="",
            )
        ],
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    row_id = app.history_table.get_children()[0]
    assert app.history_table.rows[row_id]["values"] == (
        "2026-07-02 13:00:00",
        "aa:bb:cc:00:00:01",
        "삭제 완료",
        "",
    )


def test_append_history_rows_ignores_unreadable_reappeared_macs():
    class UnreadableReappearedMacs(list):
        def __iter__(self):
            raise RuntimeError("bad reappeared macs")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=datetime(2026, 7, 2, 13, 0, 0),
        reappeared_macs=UnreadableReappearedMacs(["aa:bb:cc:00:00:01"]),
        delete_results=[
            SimpleNamespace(
                mac="aa:bb:cc:00:00:01",
                status="verified_deleted",
                success=True,
                error="",
            )
        ],
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    row_id = app.history_table.get_children()[0]
    assert app.history_table.rows[row_id]["values"] == (
        "2026-07-02 13:00:00",
        "aa:bb:cc:00:00:01",
        "삭제 완료",
        "",
    )


def test_append_history_rows_handles_started_at_format_failure():
    class BrokenStartedAt:
        def strftime(self, _format):
            raise RuntimeError("bad started_at")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=BrokenStartedAt(),
        reappeared_macs=[],
        delete_results=[
            SimpleNamespace(
                mac="aa:bb:cc:00:00:01",
                status="verified_deleted",
                success=True,
                error="",
            )
        ],
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    row_id = app.history_table.get_children()[0]
    assert app.history_table.rows[row_id]["values"] == (
        "",
        "aa:bb:cc:00:00:01",
        "삭제 완료",
        "",
    )


def test_append_history_rows_handles_failing_summary_attribute_access():
    class FailingSummary:
        delete_results = [
            SimpleNamespace(
                mac="aa:bb:cc:00:00:01",
                status="verified_deleted",
                success=True,
                error="",
            )
        ]

        @property
        def started_at(self):
            raise RuntimeError("bad started_at")

        @property
        def reappeared_macs(self):
            raise RuntimeError("bad reappeared_macs")

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0

    ArubaMmCleanupGui._append_history_rows(app, FailingSummary())

    row_id = app.history_table.get_children()[0]
    assert app.history_table.rows[row_id]["values"] == (
        "",
        "aa:bb:cc:00:00:01",
        "삭제 완료",
        "",
    )


def test_append_history_rows_skips_failing_delete_result_fields():
    class FailingMacResult:
        @property
        def mac(self):
            raise RuntimeError("bad mac")

    class FailingStatusResult:
        mac = "aa:bb:cc:00:00:02"

        @property
        def status(self):
            raise RuntimeError("bad status")

        @property
        def success(self):
            raise RuntimeError("bad success")

        @property
        def error(self):
            raise RuntimeError("bad error")

    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    class FailingSuccessBoolResult:
        mac = "aa:bb:cc:00:00:03"
        status = ""
        success = BadBool()
        error = ""

    app = make_headless_gui()
    app.history_table = FakeHistoryTable()
    app.history_row_counter = 0
    summary = SimpleNamespace(
        started_at=datetime(2026, 7, 2, 13, 0, 0),
        reappeared_macs=[],
        delete_results=[FailingMacResult(), FailingStatusResult(), FailingSuccessBoolResult()],
    )

    ArubaMmCleanupGui._append_history_rows(app, summary)

    assert app.history_table.get_children() == ("history-0", "history-1")
    assert app.history_table.rows["history-0"]["values"] == (
        "2026-07-02 13:00:00",
        "aa:bb:cc:00:00:02",
        "삭제 실패",
        "",
    )
    assert app.history_table.rows["history-1"]["values"] == (
        "2026-07-02 13:00:00",
        "aa:bb:cc:00:00:03",
        "삭제 실패",
        "",
    )


def test_summary_updates_simple_dashboard_cards_with_final_values():
    app = make_headless_gui()
    summary = SimpleNamespace(
        queried_count=4,
        target_macs=["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:03"],
        delete_success_count=2,
        delete_failure_count=1,
        remaining_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "10"
    assert app.counter_vars["deleted"].get() == "5"


def test_summary_status_update_failure_does_not_skip_audit_or_history():
    app = make_headless_gui()
    app.status_var = FailingSetVar()
    summary = SimpleNamespace(
        queried_count=1,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path="/tmp/audit.json",
        audit_error="",
        history_error="",
    )

    ArubaMmCleanupGui._handle_summary(app, summary)

    assert "AUDIT: /tmp/audit.json" in app.logs
    assert app.history_summaries == [summary]


def test_summary_unexpected_status_update_failure_does_not_skip_audit_or_history():
    app = make_headless_gui()
    app.status_var = UnexpectedSetFailingVar()
    summary = SimpleNamespace(
        queried_count=1,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path="/tmp/audit.json",
        audit_error="",
        history_error="",
    )

    ArubaMmCleanupGui._handle_summary(app, summary)

    assert "AUDIT: /tmp/audit.json" in app.logs
    assert app.history_summaries == [summary]


def test_summary_button_failure_does_not_skip_audit_or_history():
    app = make_headless_gui()
    app.cancel_button = FailingConfigureButton()
    summary = SimpleNamespace(
        queried_count=1,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path="/tmp/audit.json",
        audit_error="",
        history_error="",
    )

    ArubaMmCleanupGui._handle_summary(app, summary)

    assert "AUDIT: /tmp/audit.json" in app.logs
    assert app.history_summaries == [summary]


def test_summary_unexpected_button_failure_does_not_skip_audit_or_history():
    app = make_headless_gui()
    app.cancel_button = UnexpectedConfigureFailingButton()
    summary = SimpleNamespace(
        queried_count=1,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path="/tmp/audit.json",
        audit_error="",
        history_error="",
    )

    ArubaMmCleanupGui._handle_summary(app, summary)

    assert "AUDIT: /tmp/audit.json" in app.logs
    assert app.history_summaries == [summary]


def test_summary_does_not_double_count_query_progress():
    app = make_headless_gui()
    app._replace_table = lambda *_args, **_kwargs: None
    app._handle_progress(
        "query_done",
        {
            "count": 1,
            "macs": ["aa:bb:cc:00:00:01"],
        },
    )
    summary = SimpleNamespace(
        queried_count=1,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "8"
    assert app.counter_vars["deleted"].get() == "4"


def test_summary_handles_missing_queried_count_as_zero():
    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        delete_success_count=0,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "7"
    assert app.counter_vars["deleted"].get() == "3"
    assert app.status_var.get() == "완료"


def test_summary_handles_missing_status_and_count_fields_as_defaults():
    app = make_headless_gui()
    summary = SimpleNamespace(target_macs=[])

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "7"
    assert app.counter_vars["deleted"].get() == "3"
    assert app.status_var.get() == "완료"
    assert app.reappeared_rows == []
    assert app.logs == []


def test_summary_handles_failing_attribute_access_as_defaults():
    class FailingSummary:
        target_macs = []

        @property
        def error(self):
            raise RuntimeError("bad error")

        @property
        def canceled(self):
            raise RuntimeError("bad canceled")

        @property
        def verification_skipped(self):
            raise RuntimeError("bad verification")

        @property
        def delete_success_count(self):
            raise RuntimeError("bad delete count")

        @property
        def reappeared_count(self):
            raise RuntimeError("bad reappeared count")

        @property
        def reappeared_macs(self):
            raise RuntimeError("bad reappeared macs")

        @property
        def queried_count(self):
            raise RuntimeError("bad queried count")

        @property
        def audit_path(self):
            raise RuntimeError("bad audit path")

        @property
        def audit_error(self):
            raise RuntimeError("bad audit error")

        @property
        def history_error(self):
            raise RuntimeError("bad history error")

    app = make_headless_gui()
    summary = FailingSummary()

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "7"
    assert app.counter_vars["deleted"].get() == "3"
    assert app.status_var.get() == "완료"
    assert app.history_summaries == [summary]


def test_summary_handles_invalid_delete_success_count_as_zero():
    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=0,
        delete_success_count="bad-count",
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["deleted"].get() == "3"
    assert app.status_var.get() == "완료"


def test_summary_handles_count_values_with_failing_string_conversion():
    class BadCount:
        def __str__(self):
            raise RuntimeError("bad count")

    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=BadCount(),
        delete_success_count=BadCount(),
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "7"
    assert app.counter_vars["deleted"].get() == "3"
    assert app.status_var.get() == "완료"


def test_summary_handles_status_values_with_failing_bool_conversion():
    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=0,
        delete_success_count=1,
        reappeared_count=BadBool(),
        verification_skipped=BadBool(),
        error="",
        canceled=BadBool(),
        reappeared_macs=[],
        audit_path="/tmp/audit.json",
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["deleted"].get() == "4"
    assert app.status_var.get() == "완료"
    assert "AUDIT: /tmp/audit.json" in app.logs
    assert app.history_summaries == [summary]


def test_summary_ignores_string_target_macs_without_character_counting():
    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs="aa:bb:cc:00:00:01",
        queried_count=0,
        delete_success_count=0,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "7"
    assert app.status_var.get() == "완료"


def test_summary_uses_queried_count_when_target_macs_length_fails():
    class UnreadableTargetMacs(list):
        def __len__(self):
            raise RuntimeError("bad target macs length")

    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=UnreadableTargetMacs(["aa:bb:cc:00:00:01"]),
        queried_count=2,
        delete_success_count=0,
        reappeared_count=0,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "9"
    assert app.status_var.get() == "완료"
    assert app.history_summaries == [summary]


def test_summary_ignores_string_reappeared_macs_without_character_rows():
    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=0,
        delete_success_count=0,
        reappeared_count=1,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs="aa:bb:cc:00:00:01",
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.status_var.get() == "삭제 MAC 재조회됨"
    assert app.reappeared_rows == []


def test_summary_ignores_unreadable_reappeared_macs_without_stopping_summary():
    class UnreadableReappearedMacs(list):
        def __iter__(self):
            raise RuntimeError("bad reappeared macs")

    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=0,
        delete_success_count=0,
        reappeared_count=1,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=UnreadableReappearedMacs(["aa:bb:cc:00:00:01"]),
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.status_var.get() == "삭제 MAC 재조회됨"
    assert app.reappeared_rows == []
    assert app.history_summaries == [summary]


def test_summary_skips_bad_reappeared_mac_text_without_stopping_highlight():
    class BadText:
        def __str__(self):
            raise RuntimeError("bad reappeared mac")

    app = make_headless_gui()
    summary = SimpleNamespace(
        target_macs=[],
        queried_count=0,
        delete_success_count=0,
        reappeared_count=1,
        verification_skipped=False,
        error="",
        canceled=False,
        reappeared_macs=[BadText(), "aa:bb:cc:00:00:01"],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.status_var.get() == "삭제 MAC 재조회됨"
    assert app.reappeared_rows == [["aa:bb:cc:00:00:01"]]
    assert app.history_summaries == [summary]


@pytest.mark.parametrize(
    ("error", "canceled", "verification_skipped"),
    [("boom", False, False), ("", True, False), ("", False, True)],
)
def test_summary_leaves_confirmed_delete_unknown_without_verification(error, canceled, verification_skipped):
    app = make_headless_gui()
    summary = SimpleNamespace(
        queried_count=4,
        target_macs=["aa:bb:cc:00:00:01"],
        delete_success_count=1,
        reappeared_count=0,
        verification_skipped=verification_skipped,
        error=error,
        canceled=canceled,
        reappeared_macs=[],
        audit_path=None,
        audit_error="",
        history_error="",
    )

    app._handle_summary(summary)

    assert app.counter_vars["queried"].get() == "8"
    assert app.counter_vars["deleted"].get() == "3"


def test_result_mac_column_click_copies_mac_and_hides_notice():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:01"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.copy_notice_frame.place_calls == [{"relx": 0.5, "rely": 0.5, "anchor": "center"}]
    assert app.copy_notice_frame.lift_calls == 1
    assert app.copy_notice_frame.hidden is False
    assert app.scheduled_callbacks[0][0] == 1000

    app.scheduled_callbacks[0][1]()

    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.copy_notice_after_id is None


def test_mac_copy_notice_ignores_hide_timer_schedule_failure():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(tk.TclError("invalid command name"))

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:01"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.copy_notice_frame.hidden is False
    assert app.copy_notice_after_id is None


def test_mac_copy_notice_ignores_unexpected_hide_timer_schedule_failure():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )
    app.after = lambda _ms, _callback: (_ for _ in ()).throw(RuntimeError("after failed"))

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:01"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.copy_notice_frame.hidden is False
    assert app.copy_notice_after_id is None


def test_mac_copy_notice_ignores_unexpected_previous_timer_cancel_failure():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )
    app.copy_notice_after_id = "previous-after"
    app.after_cancel = lambda _after_id: (_ for _ in ()).throw(RuntimeError("after cancel failed"))

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:01"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.copy_notice_frame.hidden is False
    assert app.copy_notice_after_id == "after-1"


def test_mac_copy_notice_ignores_overlay_place_failure():
    app = make_headless_gui()
    app.copy_notice_frame = PlacementFailingOverlayFrame()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:01"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.scheduled_callbacks[0][0] == 1000


def test_mac_copy_ignores_destroyed_table_identify_failure():
    app = make_headless_gui()
    table = IdentifyFailingTreeTable()

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_unexpected_table_identify_failure():
    app = make_headless_gui()
    table = UnexpectedIdentifyFailingTreeTable()

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_unexpected_table_item_failure():
    app = make_headless_gui()
    table = UnexpectedItemFailingTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_malformed_click_event():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, object(), table, "#1")  # type: ignore[arg-type]

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_unexpected_clipboard_failure():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )
    app.clipboard_append = lambda _value: (_ for _ in ()).throw(RuntimeError("clipboard failed"))

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []


def test_show_copy_notice_ignores_destroyed_notice_variables():
    app = make_headless_gui()
    app.copy_notice_title_var = FailingSetVar("")
    app.copy_notice_mac_var = FailingSetVar("")

    ArubaMmCleanupGui._show_copy_notice(app, "aa:bb:cc:00:00:01")

    assert app.copy_notice_frame.hidden is False
    assert app.copy_notice_after_id == "after-1"


def test_show_copy_notice_ignores_unexpected_notice_variable_failures():
    app = make_headless_gui()
    app.copy_notice_title_var = UnexpectedSetFailingVar("")
    app.copy_notice_mac_var = UnexpectedSetFailingVar("")

    ArubaMmCleanupGui._show_copy_notice(app, "aa:bb:cc:00:00:01")

    assert app.copy_notice_frame.hidden is False
    assert app.copy_notice_after_id == "after-1"


def test_show_copy_notice_ignores_unexpected_overlay_place_failure():
    app = make_headless_gui()
    app.copy_notice_frame = UnexpectedPlacementFailingOverlayFrame()

    ArubaMmCleanupGui._show_copy_notice(app, "aa:bb:cc:00:00:01")

    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:01"
    assert app.copy_notice_after_id == "after-1"


def test_hide_copy_notice_clears_state_when_overlay_hide_fails():
    app = make_headless_gui()
    app.copy_notice_frame = HideFailingOverlayFrame()
    app.copy_notice_title_var.set("복사 완료")
    app.copy_notice_mac_var.set("aa:bb:cc:00:00:01")
    app.copy_notice_after_id = "after-1"

    ArubaMmCleanupGui._hide_copy_notice(app)

    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_after_id is None


def test_hide_copy_notice_ignores_destroyed_notice_variables():
    app = make_headless_gui()
    app.copy_notice_title_var = FailingSetVar("복사 완료")
    app.copy_notice_mac_var = FailingSetVar("aa:bb:cc:00:00:01")
    app.copy_notice_after_id = "after-1"
    app.copy_notice_frame.hidden = False

    ArubaMmCleanupGui._hide_copy_notice(app)

    assert app.copy_notice_frame.hidden is True
    assert app.copy_notice_after_id is None


def test_hide_copy_notice_ignores_unexpected_notice_variable_failures():
    app = make_headless_gui()
    app.copy_notice_title_var = UnexpectedSetFailingVar("복사 완료")
    app.copy_notice_mac_var = UnexpectedSetFailingVar("aa:bb:cc:00:00:01")
    app.copy_notice_after_id = "after-1"
    app.copy_notice_frame.hidden = False

    ArubaMmCleanupGui._hide_copy_notice(app)

    assert app.copy_notice_frame.hidden is True
    assert app.copy_notice_after_id is None


def test_hide_copy_notice_ignores_unexpected_overlay_hide_failure():
    app = make_headless_gui()
    app.copy_notice_frame = UnexpectedHideFailingOverlayFrame()
    app.copy_notice_title_var.set("복사 완료")
    app.copy_notice_mac_var.set("aa:bb:cc:00:00:01")
    app.copy_notice_after_id = "after-1"
    app.copy_notice_frame.hidden = False

    ArubaMmCleanupGui._hide_copy_notice(app)

    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_after_id is None


def test_repeated_mac_copy_replaces_center_notice_timer():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")
    table.rows["aa:bb:cc:00:00:01"]["values"] = (
        "aa:bb:cc:00:00:09",
        "삭제 대상",
        "2026-07-02 13:00:00",
        "",
        "",
    )
    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == ["aa:bb:cc:00:00:09"]
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:09"
    assert app.canceled_after_ids == ["after-1"]
    assert len(app.scheduled_callbacks) == 2


def test_history_mac_column_click_copies_second_column_mac():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.click_column = "#2"
    table.insert(
        "",
        "end",
        iid="history-0",
        values=("2026-07-02 13:00:00", "aa:bb:cc:00:00:02", "삭제 완료", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#2")

    assert app.clipboard_values == ["aa:bb:cc:00:00:02"]
    assert app.copy_notice_title_var.get() == "복사 완료"
    assert app.copy_notice_mac_var.get() == "aa:bb:cc:00:00:02"


def test_non_mac_column_click_does_not_copy_mac():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.click_column = "#2"
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []


def test_invalid_mac_column_identifier_does_not_copy_mac():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.click_column = "MAC"
    table.insert(
        "",
        "end",
        iid="aa:bb:cc:00:00:01",
        values=("aa:bb:cc:00:00:01", "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "MAC")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_malformed_row_values():
    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert("", "end", iid="bad-row", values=())
    table.rows["bad-row"]["values"] = None

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []


def test_mac_copy_ignores_unprintable_mac_value():
    class BadText:
        def __str__(self):
            raise RuntimeError("bad mac")

    app = make_headless_gui()
    table = FakeTreeTable()
    table.insert(
        "",
        "end",
        iid="bad-row",
        values=(BadText(), "삭제 대상", "2026-07-02 13:00:00", "", ""),
    )

    ArubaMmCleanupGui._copy_mac_from_table_event(app, FakeClickEvent(), table, "#1")

    assert app.clipboard_values == []
    assert app.copy_notice_title_var.get() == ""
    assert app.copy_notice_mac_var.get() == ""
    assert app.copy_notice_frame.hidden is True
    assert app.scheduled_callbacks == []
