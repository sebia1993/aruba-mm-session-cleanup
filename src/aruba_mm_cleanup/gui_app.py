"""Tkinter GUI for Windows operators."""

from __future__ import annotations

import heapq
import json
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

from .cleanup import MmCleanupRunner, build_query_command
from .hostkeys import HostKeyObservation
from .models import CleanupPlan, CleanupSettings, MmConnectionConfig
from .parser import normalize_mac
from .validation import validate_host, validate_username

APP_TITLE = "Aruba MM Cleanup Dashboard"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "ArubaMMCleanup" / "outputs"
DEFAULT_ROLE = "profiling"
DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 1
MAX_HISTORY_ROWS = 500
MAX_LOG_LINES = 1000
HISTORY_FILE_NAME = "deletion_history.jsonl"
SHUTDOWN_GRACE_MS = 250
TYPE_NA_MESSAGE = "Type=N/A: 관리자 직접 장비 지정 필요"

BG = "#f4f4f4"
PANEL = "#ffffff"
TEXT = "#171a20"
BODY = "#393c41"
MUTED = "#5c5e62"
ACCENT = "#3e6ae1"
DANGER = "#b42318"
DANGER_ACTIVE = "#8f1d14"
DANGER_SOFT = "#fff4f2"
LINE = "#eeeeee"
FIELD_BG = "#fafafa"
CARD_BG = "#ffffff"
DISABLED = "#8e8e8e"
SIDEBAR_BG = "#ffffff"
SECONDARY_BG = "#f4f4f4"
SECONDARY_ACTIVE = "#eeeeee"
LOG_BG = "#171a20"
LOG_TEXT = "#f4f4f4"


class _ApprovalRequest:
    def __init__(self, kind: str, payload: object) -> None:
        self.kind = kind
        self.payload = payload
        self.approved = False
        self.done = threading.Event()


class ArubaMmCleanupGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1160x760")
        self.minsize(980, 660)
        self.configure(bg=BG)

        self.event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.scheduler_worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.scheduler_stop_event = threading.Event()
        self.is_running = False
        self.scheduler_running = False
        self.closing = False
        self.runner = MmCleanupRunner(
            persistent_session=True,
            host_key_approval_callback=self._confirm_host_key_from_worker,
        )
        self.runner_lock = threading.Lock()
        self.session_close_worker: Optional[threading.Thread] = None
        self.session_close_lock = threading.Lock()
        self.history_row_counter = 0
        self.settings_frame: Optional[tk.Frame] = None
        self.loaded_history_dir: Optional[Path] = None
        self._drain_after_id: Optional[str] = None
        self.copy_notice_after_id: Optional[str] = None
        self.copy_notice_frame: Optional[tk.Frame] = None
        self.cumulative_queried_count = 0
        self.cumulative_deleted_count = 0
        self.current_run_queried_count = 0
        self.current_run_query_counted = False
        self.current_run_delete_counted = False

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.enable_password_var = tk.StringVar()
        self.role_var = tk.StringVar(value=DEFAULT_ROLE)
        self.timeout_var = tk.StringVar(value="60")
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_SECONDS))
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.status_var = tk.StringVar(value="대기 중")
        self.timer_value_var = tk.StringVar(value="-")
        self.timer_state_var = tk.StringVar(value="대기")
        self.copy_notice_title_var = tk.StringVar(value="")
        self.copy_notice_mac_var = tk.StringVar(value="")
        self.counter_vars = {
            "queried": tk.StringVar(value="0"),
            "deleted": tk.StringVar(value="0"),
        }

        self._build_styles()
        self._build_layout()
        try:
            self._load_history_from_output_dir(DEFAULT_OUTPUT_DIR)
        except Exception as exc:
            error = _safe_text(exc) or exc.__class__.__name__
            self._log(f"WARNING: 이력 로드 실패 - {error}")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_after_id = self.after(150, self._drain_events)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            rowheight=30,
            font=("Segoe UI", 10),
            fieldbackground=PANEL,
            background=PANEL,
            foreground=BODY,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI Semibold", 10),
            foreground=TEXT,
            background=SECONDARY_BG,
            borderwidth=0,
            relief="flat",
        )
        style.map("Treeview", background=[("selected", SECONDARY_ACTIVE)], foreground=[("selected", TEXT)])

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = tk.Frame(self, bg=SIDEBAR_BG, width=220, highlightbackground=LINE, highlightthickness=1)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        tk.Label(
            sidebar,
            text="Aruba MM",
            bg=SIDEBAR_BG,
            fg=TEXT,
            justify="left",
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="w", padx=20, pady=(24, 8))
        tk.Label(
            sidebar,
            text="Cleanup Dashboard",
            bg=SIDEBAR_BG,
            fg=MUTED,
            justify="left",
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=20)
        self.manual_button = self._sidebar_button(sidebar, "1회 실행", self.start_manual_run)
        self.manual_button.pack(fill="x", padx=14, pady=(28, 8))
        self.schedule_button = self._sidebar_button(sidebar, "주기 실행 시작", self.start_scheduler, variant="secondary")
        self.schedule_button.pack(fill="x", padx=14, pady=8)
        self.stop_schedule_button = self._sidebar_button(
            sidebar,
            "주기 실행 정지",
            self.stop_scheduler,
            state="disabled",
            variant="secondary",
        )
        self.stop_schedule_button.pack(fill="x", padx=14, pady=8)
        self.disconnect_button = self._sidebar_button(
            sidebar,
            "세션 연결 해제",
            self.disconnect_session,
            variant="secondary",
        )
        self.disconnect_button.pack(fill="x", padx=14, pady=8)

        main = tk.Frame(self, bg=BG)
        main.grid(row=0, column=1, sticky="nsew", padx=24, pady=24)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=0)
        main.grid_rowconfigure(3, weight=1)

        self._build_header(main)
        self._build_settings(main)
        self._build_cards(main)
        self._build_results(main)
        self._build_log(main)
        self._build_copy_notice_overlay()

    def _sidebar_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        state: str = "normal",
        *,
        variant: str = "primary",
    ) -> tk.Button:
        if variant == "primary":
            background = ACCENT
            foreground = "#ffffff"
            active_background = "#3457b1"
            active_foreground = "#ffffff"
        else:
            background = SECONDARY_BG
            foreground = TEXT
            active_background = SECONDARY_ACTIVE
            active_foreground = TEXT
        return tk.Button(
            parent,
            text=text,
            command=command,
            state=state,
            bg=background,
            fg=foreground,
            disabledforeground=DISABLED,
            activebackground=active_background,
            activeforeground=active_foreground,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Segoe UI Semibold", 10),
            padx=12,
            pady=10,
            cursor="hand2",
        )

    def _build_header(self, parent: tk.Widget) -> None:
        frame = self._panel(parent)
        frame.grid(row=0, column=0, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        tk.Label(frame, text=APP_TITLE, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 17)).grid(
            row=0, column=0, sticky="w", padx=18, pady=(16, 2)
        )
        tk.Label(
            frame,
            textvariable=self.status_var,
            bg=PANEL,
            fg=ACCENT,
            font=("Segoe UI Semibold", 11),
        ).grid(row=0, column=1, sticky="e", padx=18, pady=(16, 2))
        tk.Label(
            frame,
            text="조회 snapshot의 MAC을 미리 보여주고 매 실행마다 명시 승인을 받은 뒤에만 삭제합니다.",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=18, pady=(0, 16))

    def _build_settings(self, parent: tk.Widget) -> None:
        frame = self._panel(parent)
        self.settings_frame = frame
        frame.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        for column in range(6):
            frame.grid_columnconfigure(column, weight=1)

        self._entry(frame, "MM IP/Host", self.host_var, 0, 0)
        self._entry(frame, "Port", self.port_var, 0, 1, width=7)
        self._entry(frame, "계정", self.username_var, 0, 2)
        self._entry(frame, "암호", self.password_var, 0, 3, show="*")
        self._entry(frame, "Enable 암호", self.enable_password_var, 0, 4, show="*")
        self._entry(frame, "Role", self.role_var, 0, 5)
        self._entry(frame, "장비 응답 대기(초)", self.timeout_var, 2, 0, width=12)
        self._entry(frame, "주기(초)", self.interval_var, 2, 1, width=8)
        tk.Label(frame, text="결과 폴더", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(
            row=2, column=2, sticky="w", padx=12, pady=(10, 2)
        )
        tk.Entry(
            frame,
            textvariable=self.output_dir_var,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            bg=FIELD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 10),
        ).grid(
            row=3, column=2, columnspan=3, sticky="ew", padx=12, pady=(0, 14)
        )
        tk.Button(
            frame,
            text="폴더 선택",
            command=self.browse_output_dir,
            bg=SECONDARY_BG,
            fg=TEXT,
            activebackground=SECONDARY_ACTIVE,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Segoe UI Semibold", 10),
        ).grid(row=3, column=5, sticky="ew", padx=12, pady=(0, 14))

    def _entry(
        self,
        parent: tk.Widget,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        *,
        show: str = "",
        width: int = 16,
    ) -> None:
        tk.Label(parent, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(
            row=row, column=column, sticky="w", padx=12, pady=(12, 2)
        )
        tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            width=width,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            bg=FIELD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 10),
        ).grid(
            row=row + 1, column=column, sticky="ew", padx=12, pady=(0, 12)
        )

    def _build_cards(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=BG)
        frame.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        for column in range(3):
            frame.grid_columnconfigure(column, weight=1, uniform="cards")
        self._card(frame, "누적 조회 MAC", self.counter_vars["queried"], 0, TEXT)
        self._card(frame, "누적 삭제 완료", self.counter_vars["deleted"], 1, TEXT)
        self._timer_card(frame, 2)

    def _card(self, parent: tk.Widget, title: str, variable: tk.StringVar, column: int, color: str) -> None:
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        tk.Label(card, text=title, bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(12, 0))
        tk.Label(card, textvariable=variable, bg=CARD_BG, fg=color, font=("Segoe UI Semibold", 24)).pack(
            anchor="w", padx=16, pady=(0, 12)
        )

    def _timer_card(self, parent: tk.Widget, column: int) -> None:
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=LINE, highlightthickness=1)
        card.grid(row=0, column=column, sticky="ew", padx=(8, 0))
        tk.Label(card, text="작업 상태", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9)).pack(
            anchor="w", padx=16, pady=(12, 0)
        )
        tk.Label(card, textvariable=self.timer_value_var, bg=CARD_BG, fg=ACCENT, font=("Segoe UI Semibold", 20)).pack(
            anchor="w", padx=16, pady=(0, 0)
        )
        tk.Label(card, textvariable=self.timer_state_var, bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9)).pack(
            anchor="w", padx=16, pady=(0, 10)
        )

    def _build_results(self, parent: tk.Widget) -> None:
        frame = self._panel(parent)
        frame.grid(row=3, column=0, sticky="nsew", pady=(16, 0))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=3)
        frame.grid_rowconfigure(3, weight=1)
        top = tk.Frame(frame, bg=PANEL)
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        tk.Label(top, text="삭제 대상 및 결과", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 12)).pack(side="left")
        columns = ("mac", "status", "queried_at", "deleted_at", "error")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=9)
        headings = {
            "mac": "MAC",
            "status": "상태",
            "queried_at": "조회시각",
            "deleted_at": "삭제시각",
            "error": "메시지",
        }
        widths = {"mac": 150, "status": 120, "queried_at": 150, "deleted_at": 150, "error": 360}
        for key in columns:
            self.table.heading(key, text=headings[key])
            self.table.column(key, width=widths[key], anchor="w")
        self.table.tag_configure("reappeared", foreground=DANGER)
        self.table.bind("<ButtonRelease-1>", lambda event: self._copy_mac_from_table_event(event, self.table, "#1"))
        self.table.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))

        history_top = tk.Frame(frame, bg=PANEL)
        history_top.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 10))
        tk.Label(history_top, text="최근 삭제 이력", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 12)).pack(
            side="left"
        )
        self._action_button(
            history_top,
            "이력 전체 지우기",
            self.clear_history,
            variant="danger_outline",
        ).pack(side="right")
        history_columns = ("run_at", "mac", "result", "error")
        self.history_table = ttk.Treeview(frame, columns=history_columns, show="headings", height=4)
        history_headings = {
            "run_at": "실행시각",
            "mac": "MAC",
            "result": "결과",
            "error": "오류",
        }
        history_widths = {"run_at": 150, "mac": 150, "result": 120, "error": 500}
        for key in history_columns:
            self.history_table.heading(key, text=history_headings[key])
            self.history_table.column(key, width=history_widths[key], anchor="w")
        self.history_table.tag_configure("reappeared", foreground=DANGER)
        self.history_table.bind(
            "<ButtonRelease-1>",
            lambda event: self._copy_mac_from_table_event(event, self.history_table, "#2"),
        )
        self.history_table.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 14))

    def _build_log(self, parent: tk.Widget) -> None:
        frame = self._panel(parent)
        frame.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        frame.grid_columnconfigure(0, weight=1)
        button_row = tk.Frame(frame, bg=PANEL)
        button_row.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))
        self.cancel_button = self._action_button(
            button_row,
            "이번 삭제 취소",
            self.cancel_current_delete,
            state="disabled",
            variant="danger",
        )
        self.cancel_button.pack(side="left")
        self._action_button(
            button_row,
            "로그 지우기",
            self.clear_log,
            variant="secondary",
        ).pack(side="right")
        self.log_text = tk.Text(
            frame,
            height=7,
            bg=LOG_BG,
            fg=LOG_TEXT,
            insertbackground=LOG_TEXT,
            relief="flat",
            font=("Consolas", 10),
            wrap="word",
        )
        self.log_text.grid(row=1, column=0, sticky="ew", padx=16, pady=12)

    def _panel(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)

    def _build_copy_notice_overlay(self) -> None:
        frame = tk.Frame(self, bg=TEXT, highlightbackground=ACCENT, highlightthickness=1)
        tk.Label(
            frame,
            textvariable=self.copy_notice_title_var,
            bg=TEXT,
            fg="#ffffff",
            font=("Segoe UI Semibold", 13),
        ).pack(anchor="center", padx=34, pady=(18, 4))
        tk.Label(
            frame,
            textvariable=self.copy_notice_mac_var,
            bg=TEXT,
            fg="#ffffff",
            font=("Consolas", 12),
        ).pack(anchor="center", padx=34, pady=(0, 18))
        self.copy_notice_frame = frame

    def _action_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        *,
        state: str = "normal",
        variant: str = "secondary",
    ) -> tk.Button:
        if variant == "danger":
            background = DANGER
            foreground = "#ffffff"
            active_background = DANGER_ACTIVE
            active_foreground = "#ffffff"
            disabled_foreground = "#f7d6d2"
            highlight_thickness = 0
            highlight_background = background
        elif variant == "danger_outline":
            background = PANEL
            foreground = DANGER
            active_background = DANGER_SOFT
            active_foreground = DANGER
            disabled_foreground = DISABLED
            highlight_thickness = 1
            highlight_background = DANGER
        else:
            background = SECONDARY_BG
            foreground = TEXT
            active_background = SECONDARY_ACTIVE
            active_foreground = TEXT
            disabled_foreground = DISABLED
            highlight_thickness = 0
            highlight_background = background
        return tk.Button(
            parent,
            text=text,
            command=command,
            state=state,
            bg=background,
            fg=foreground,
            disabledforeground=disabled_foreground,
            activebackground=active_background,
            activeforeground=active_foreground,
            relief="flat",
            bd=0,
            highlightthickness=highlight_thickness,
            highlightbackground=highlight_background,
            highlightcolor=highlight_background,
            font=("Segoe UI Semibold", 10),
            padx=16,
            pady=9,
            cursor="hand2",
        )

    def browse_output_dir(self) -> None:
        try:
            initial_dir = self.output_dir_var.get() or str(DEFAULT_OUTPUT_DIR)
            selected = filedialog.askdirectory(initialdir=initial_dir)
        except Exception:
            return
        if selected:
            try:
                self.output_dir_var.set(selected)
            except Exception:
                return
            try:
                self._load_history_from_output_dir(Path(selected), force=True)
            except Exception as exc:
                error = _safe_text(exc) or exc.__class__.__name__
                self._log(f"WARNING: 이력 로드 실패 - {error}")

    def start_manual_run(self) -> None:
        if self.closing or self.is_running:
            return
        if self.scheduler_running:
            self._log("주기 실행 중에는 1회 실행을 시작할 수 없습니다.")
            return
        try:
            config, settings, output_dir = self._read_inputs()
        except ValueError as exc:
            try:
                messagebox.showerror("입력 오류", _safe_text(exc) or exc.__class__.__name__)
            except Exception:
                pass
            return
        try:
            self._load_history_from_output_dir(output_dir)
        except Exception as exc:
            error = _safe_text(exc) or exc.__class__.__name__
            self._log(f"WARNING: 이력 로드 실패 - {error}")
        self.cancel_event.clear()
        self.scheduler_stop_event.clear()
        self._set_running(True)
        try:
            self.worker = threading.Thread(
                target=self._run_once_worker,
                args=(config, settings, output_dir),
                daemon=True,
            )
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self._set_running(False)
            error = _safe_text(exc) or exc.__class__.__name__
            self._log(f"WARNING: 작업 스레드 시작 실패 - {error}")

    def start_scheduler(self) -> None:
        if self.closing or self.scheduler_running:
            return
        if self.is_running:
            self._log("실행 중에는 주기 실행을 시작할 수 없습니다.")
            return
        try:
            config, settings, output_dir = self._read_inputs()
            interval = self._read_interval()
        except ValueError as exc:
            try:
                messagebox.showerror("입력 오류", _safe_text(exc) or exc.__class__.__name__)
            except Exception:
                pass
            return
        try:
            self._load_history_from_output_dir(output_dir)
        except Exception as exc:
            error = _safe_text(exc) or exc.__class__.__name__
            self._log(f"WARNING: 이력 로드 실패 - {error}")
        self.scheduler_stop_event.clear()
        self.scheduler_running = True
        try:
            self.manual_button.configure(state="disabled")
        except Exception:
            pass
        try:
            self.schedule_button.configure(state="disabled")
        except Exception:
            pass
        try:
            self.stop_schedule_button.configure(state="normal")
        except Exception:
            pass
        self._sync_settings_visibility()
        self._log(f"주기 실행 시작: {interval}초 간격")
        try:
            self.scheduler_worker = threading.Thread(
                target=self._scheduler_loop,
                args=(config, settings, output_dir, interval),
                daemon=True,
            )
            self.scheduler_worker.start()
        except Exception as exc:
            self.scheduler_worker = None
            self.scheduler_running = False
            self.scheduler_stop_event.set()
            try:
                self.manual_button.configure(state="normal")
            except Exception:
                pass
            try:
                self.schedule_button.configure(state="normal")
            except Exception:
                pass
            try:
                self.stop_schedule_button.configure(state="disabled")
            except Exception:
                pass
            self._set_timer("-", "대기")
            self._sync_settings_visibility()
            error = _safe_text(exc) or exc.__class__.__name__
            self._log(f"WARNING: 주기 실행 스레드 시작 실패 - {error}")

    def stop_scheduler(self) -> None:
        self.scheduler_stop_event.set()
        self.cancel_event.set()
        self.scheduler_running = False
        try:
            self.manual_button.configure(state="disabled" if self.is_running else "normal")
        except Exception:
            pass
        try:
            self.schedule_button.configure(state="disabled" if self.is_running else "normal")
        except Exception:
            pass
        try:
            self.stop_schedule_button.configure(state="disabled")
        except Exception:
            pass
        self._set_timer("-", "대기")
        self._sync_settings_visibility()
        self._log("주기 실행 정지 요청")

    def cancel_current_delete(self) -> None:
        self.cancel_event.set()
        self._log("이번 삭제 취소 요청")

    def disconnect_session(self) -> None:
        if self.closing:
            return
        if self.is_running:
            self._log("실행 중에는 세션 연결 해제를 건너뜁니다.")
            return
        self._start_session_close(reason="manual", enqueue_progress=True)
        try:
            self.status_var.set("세션 연결 해제")
        except Exception:
            pass
        self._log("SESSION DISCONNECT REQUEST")

    def on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.scheduler_stop_event.set()
        self.cancel_event.set()
        if self._drain_after_id is not None:
            try:
                self.after_cancel(self._drain_after_id)
            except Exception:
                pass
            self._drain_after_id = None
        if self.copy_notice_after_id is not None:
            try:
                self.after_cancel(self.copy_notice_after_id)
            except Exception:
                pass
            self.copy_notice_after_id = None
        self._start_session_close(reason="app_close", enqueue_progress=False)
        try:
            self.after(SHUTDOWN_GRACE_MS, self._destroy_window)
        except Exception:
            self._destroy_window()

    def _scheduler_loop(
        self,
        config: MmConnectionConfig,
        settings: CleanupSettings,
        output_dir: Path,
        interval: int,
    ) -> None:
        interval_seconds = _safe_interval_seconds(interval)
        stop_scheduler = False
        try:
            while not self.scheduler_stop_event.is_set() and not stop_scheduler:
                self.cancel_event.clear()
                self._enqueue_event("running", True)
                try:
                    if self._run_summary(config, settings, output_dir) is False:
                        stop_scheduler = True
                except Exception as exc:
                    error = _safe_text(exc) or exc.__class__.__name__
                    self._enqueue_event("progress", ("run_error", {"error": error}))
                    stop_scheduler = True
                finally:
                    self._enqueue_event("running", False)
                if stop_scheduler:
                    break
                for remaining in range(interval_seconds, 0, -1):
                    if self.scheduler_stop_event.is_set():
                        break
                    self._enqueue_event("next_run", remaining)
                    try:
                        if self.scheduler_stop_event.wait(1):
                            break
                    except Exception:
                        stop_scheduler = True
                        break
        finally:
            self._enqueue_event("scheduler_stopped", None)

    def _run_once_worker(self, config: MmConnectionConfig, settings: CleanupSettings, output_dir: Path) -> None:
        try:
            self._run_summary(config, settings, output_dir)
        except Exception as exc:
            error = _safe_text(exc) or exc.__class__.__name__
            self._enqueue_event("progress", ("run_error", {"error": error}))
        finally:
            self._enqueue_event("running", False)

    def _run_summary(self, config: MmConnectionConfig, settings: CleanupSettings, output_dir: Path) -> bool:
        def progress(event: str, payload: dict[str, object]) -> None:
            self._enqueue_event("progress", (event, payload))

        try:
            with self.runner_lock:
                summary = self.runner.run_once(
                    config,
                    settings,
                    output_dir=output_dir,
                    progress_callback=progress,
                    should_cancel=self._should_cancel_run,
                    approve_targets=self._confirm_targets_from_worker,
                )
            self._enqueue_event("summary", summary)
            return True
        except Exception as exc:
            error = _safe_text(exc) or exc.__class__.__name__
            self._enqueue_event("progress", ("run_error", {"error": error}))
            return False

    def _confirm_host_key_from_worker(self, observation: HostKeyObservation) -> bool:
        return self._request_approval("host_key", observation)

    def _confirm_targets_from_worker(self, plan: CleanupPlan) -> bool:
        return self._request_approval("targets", plan)

    def _request_approval(self, kind: str, payload: object) -> bool:
        request = _ApprovalRequest(kind, payload)
        if not self._enqueue_event("approval_request", request):
            return False
        deadline = time.monotonic() + 300
        while not request.done.wait(0.1):
            if self.closing or self.cancel_event.is_set() or self.scheduler_stop_event.is_set():
                return False
            if time.monotonic() >= deadline:
                return False
        return request.approved is True

    def _close_runner_session(self, *, reason: str, enqueue_progress: bool) -> None:
        progress = None
        if enqueue_progress:
            def enqueue_runner_progress(event: str, payload: dict[str, object]) -> None:
                self._enqueue_event("progress", (event, payload))

            progress = enqueue_runner_progress
        with self.runner_lock:
            try:
                self.runner.close_session(progress_callback=progress, reason=reason)
            except Exception as exc:
                if enqueue_progress:
                    error = _safe_text(exc) or exc.__class__.__name__
                    self._enqueue_event(
                        "progress",
                        (
                            "warning",
                            {"message": f"session close failed: {error}", "reason": reason},
                        ),
                    )

    def _start_session_close(self, *, reason: str, enqueue_progress: bool) -> None:
        with self.session_close_lock:
            if self.session_close_worker is not None:
                try:
                    if self.session_close_worker.is_alive():
                        return
                except Exception:
                    self.session_close_worker = None
            try:
                self.session_close_worker = threading.Thread(
                    target=self._close_runner_session,
                    kwargs={"reason": reason, "enqueue_progress": enqueue_progress},
                    daemon=True,
                )
                self.session_close_worker.start()
            except Exception as exc:
                self.session_close_worker = None
                error = _safe_text(exc) or exc.__class__.__name__
                warning = f"세션 종료 스레드 시작 실패 - {error}"
                if enqueue_progress:
                    self._enqueue_event("progress", ("warning", {"message": warning, "reason": reason}))
                else:
                    self._log(f"WARNING: {warning}")

    def _should_cancel_run(self) -> bool:
        return self.cancel_event.is_set() or self.scheduler_stop_event.is_set() or self.closing

    def _enqueue_event(self, event: str, payload: object) -> bool:
        if self.closing:
            return False
        try:
            self.event_queue.put((event, payload))
        except Exception:
            return False
        return True

    def _read_inputs(self) -> tuple[MmConnectionConfig, CleanupSettings, Path]:
        try:
            host = self.host_var.get().strip()
            username = self.username_var.get().strip()
            password = self.password_var.get()
            port_text = self.port_var.get().strip() or "22"
            timeout_text = self.timeout_var.get().strip() or "60"
            role = self.role_var.get().strip() or DEFAULT_ROLE
            enable_password = self.enable_password_var.get()
            output_dir_text = self.output_dir_var.get().strip()
        except (AttributeError, tk.TclError) as exc:
            raise ValueError("입력값을 읽을 수 없습니다.") from exc
        except Exception as exc:
            raise ValueError("입력값을 읽을 수 없습니다.") from exc
        try:
            host = validate_host(host)
            username = validate_username(username)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not password:
            raise ValueError("암호를 입력하세요.")
        try:
            port = int(port_text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Port는 숫자로 입력하세요.") from exc
        if port < 1 or port > 65535:
            raise ValueError("Port는 1부터 65535 사이 숫자로 입력하세요.")
        try:
            timeout = int(timeout_text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("장비 응답 대기(초)는 숫자로 입력하세요.") from exc
        if timeout < 1:
            raise ValueError("장비 응답 대기(초)는 1 이상 숫자로 입력하세요.")
        if timeout > 600:
            raise ValueError("장비 응답 대기(초)는 600 이하 숫자로 입력하세요.")
        try:
            build_query_command(role)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        config = MmConnectionConfig(
            host=host,
            username=username,
            password=password,
            port=port,
            enable_password=enable_password,
        )
        settings = CleanupSettings(
            role=role,
            timeout=timeout,
            delete_delay_seconds=0,
        )
        output_dir = Path(output_dir_text or DEFAULT_OUTPUT_DIR).expanduser()
        return config, settings, output_dir

    def _read_interval(self) -> int:
        try:
            interval = int(self.interval_var.get().strip() or str(DEFAULT_INTERVAL_SECONDS))
        except (AttributeError, tk.TclError) as exc:
            raise ValueError("주기(초)는 1 이상 숫자로 입력하세요.") from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("주기(초)는 1 이상 숫자로 입력하세요.") from exc
        except Exception as exc:
            raise ValueError("주기(초)는 1 이상 숫자로 입력하세요.") from exc
        if interval < MIN_INTERVAL_SECONDS:
            raise ValueError("주기(초)는 1 이상 숫자로 입력하세요.")
        return interval

    def _drain_events(self) -> None:
        if self.closing:
            return
        try:
            while True:
                queue_item = self.event_queue.get_nowait()
                try:
                    event, payload = queue_item
                except Exception as exc:
                    try:
                        error = _safe_text(exc) or exc.__class__.__name__
                        self._log(f"WARNING: 이벤트 형식 오류 - {error}")
                    except Exception:
                        pass
                    continue
                try:
                    if event == "running":
                        self._set_running(_safe_bool(payload))
                    elif event == "progress":
                        try:
                            progress_event, progress_payload = payload
                        except (TypeError, ValueError) as exc:
                            try:
                                error = _safe_text(exc) or exc.__class__.__name__
                                self._log(f"WARNING: 진행 이벤트 형식 오류 - {error}")
                            except Exception:
                                pass
                            continue
                        if not isinstance(progress_payload, dict):
                            progress_payload = {}
                        progress_event_name = _safe_text(progress_event) or progress_event.__class__.__name__
                        self._handle_progress(progress_event_name, progress_payload)
                    elif event == "summary":
                        self._handle_summary(payload)
                    elif event == "approval_request":
                        self._handle_approval_request(payload)
                    elif event == "next_run":
                        remaining = "None" if payload is None else (_safe_text(payload) or payload.__class__.__name__)
                        self._set_timer(f"{remaining}s", "다음 실행")
                    elif event == "scheduler_stopped":
                        self.scheduler_running = False
                        try:
                            self.manual_button.configure(state="disabled" if self.is_running else "normal")
                        except Exception:
                            pass
                        try:
                            self.schedule_button.configure(state="disabled" if self.is_running else "normal")
                        except Exception:
                            pass
                        try:
                            self.stop_schedule_button.configure(state="disabled")
                        except Exception:
                            pass
                        self._set_timer("-", "대기")
                        self._sync_settings_visibility()
                except Exception as exc:
                    try:
                        error = _safe_text(exc) or exc.__class__.__name__
                        self._log(f"WARNING: 이벤트 처리 실패({event}) - {error}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        except Exception as exc:
            try:
                error = _safe_text(exc) or exc.__class__.__name__
                self._log(f"WARNING: 이벤트 큐 처리 실패 - {error}")
            except Exception:
                pass
        if not self.closing:
            try:
                self._drain_after_id = self.after(150, self._drain_events)
            except Exception:
                self._drain_after_id = None

    def _handle_approval_request(self, payload: object) -> None:
        if not isinstance(payload, _ApprovalRequest):
            return
        approved = False
        try:
            if payload.kind == "host_key" and isinstance(payload.payload, HostKeyObservation):
                observation = payload.payload
                approved = messagebox.askyesno(
                    "최초 SSH 지문 승인",
                    "다음 장비 지문을 앱 known_hosts에 저장하시겠습니까?\n\n"
                    f"장비: {observation.host}:{observation.port}\n"
                    f"키 유형: {observation.key_type}\n"
                    f"지문: {observation.fingerprint}\n\n"
                    "장비 관리자에게 별도 경로로 지문을 확인한 뒤 승인하세요.",
                    parent=self,
                )
            elif payload.kind == "targets" and isinstance(payload.payload, CleanupPlan):
                plan = payload.payload
                target_lines = "\n".join(f"- {mac}" for mac in plan.target_macs)
                phrase = f"DELETE {len(plan.target_macs)}"
                answer = simpledialog.askstring(
                    "삭제 대상 최종 승인",
                    f"Role: {plan.role}\n대상: {len(plan.target_macs)}개\n\n{target_lines}\n\n"
                    f"정확히 '{phrase}'를 입력하면 이 snapshot만 삭제합니다.",
                    parent=self,
                )
                approved = answer == phrase
        except Exception:
            approved = False
        finally:
            payload.approved = approved
            payload.done.set()

    def _handle_progress(self, event: str, payload: dict[str, object]) -> None:
        if event == "connect_start":
            try:
                self.status_var.set("MM 접속 중")
            except Exception:
                pass
            raw_host = payload.get("host")
            host = "None" if raw_host is None else (_safe_text(raw_host) or raw_host.__class__.__name__)
            self._log(f"CONNECT: {host}")
        elif event == "connect_done":
            try:
                self.status_var.set("MM 세션 연결됨")
            except Exception:
                pass
            raw_host = payload.get("host")
            host = "None" if raw_host is None else (_safe_text(raw_host) or raw_host.__class__.__name__)
            self._log(f"CONNECT OK: {host}")
        elif event == "session_reconnect_start":
            try:
                self.status_var.set("MM 세션 재접속 중")
            except Exception:
                pass
            raw_command = payload.get("command")
            raw_error = payload.get("error")
            command = "None" if raw_command is None else (_safe_text(raw_command) or raw_command.__class__.__name__)
            error = "None" if raw_error is None else (_safe_text(raw_error) or raw_error.__class__.__name__)
            self._log(f"RECONNECT: {command} | {error}")
        elif event == "session_disconnected":
            try:
                self.status_var.set("세션 연결 해제")
            except Exception:
                pass
            raw_reason = payload.get("reason")
            reason = "None" if raw_reason is None else (_safe_text(raw_reason) or raw_reason.__class__.__name__)
            self._log(f"DISCONNECT: {reason}")
        elif event == "warning":
            raw_message = payload.get("message")
            message = (
                "None" if raw_message is None else (_safe_text(raw_message) or raw_message.__class__.__name__)
            )
            self._log(f"WARNING: {message}")
        elif event == "query_start":
            try:
                self.status_var.set("global-user-table 조회 중")
            except Exception:
                pass
            self._set_timer("실행 중", "조회 처리")
            raw_command = payload.get("command")
            command = "None" if raw_command is None else (_safe_text(raw_command) or raw_command.__class__.__name__)
            self._log(f"QUERY: {command}")
        elif event == "query_done":
            raw_macs = payload.get("macs")
            raw_type_na_macs = payload.get("type_na_macs")
            macs = []
            if isinstance(raw_macs, (list, tuple, set)):
                try:
                    macs = list(raw_macs)
                except Exception:
                    macs = []
            type_na_macs = []
            if isinstance(raw_type_na_macs, (list, tuple, set)):
                try:
                    type_na_macs_iter = iter(raw_type_na_macs)
                except Exception:
                    type_na_macs_iter = iter(())
                while True:
                    try:
                        mac = next(type_na_macs_iter)
                    except StopIteration:
                        break
                    except Exception:
                        break
                    try:
                        type_na_macs.append(str(mac))
                    except Exception:
                        continue
            self._count_current_query(len(_unique_display_macs(macs)))
            self._replace_table(macs, "삭제 대상", type_na_macs=type_na_macs)
            raw_count = payload.get("count", 0)
            count = _safe_text(raw_count) or raw_count.__class__.__name__
            self._log(f"QUERY DONE: {count} MAC(s)")
            for mac in _unique_display_macs(type_na_macs):
                self._log(f"TYPE N/A: {mac} - 관리자 직접 장비 지정 필요")
        elif event == "countdown":
            try:
                remaining = int(payload.get("remaining", 0))
            except (TypeError, ValueError, RuntimeError, OverflowError):
                remaining = 0
            self._set_timer(f"{remaining}s", "삭제 시작 대기" if remaining > 0 else "삭제 시작")
            try:
                self.status_var.set(f"{remaining}초 후 삭제 시작" if remaining > 0 else "삭제 시작")
            except Exception:
                pass
            try:
                self.cancel_button.configure(state="normal" if remaining > 0 else "disabled")
            except Exception:
                pass
        elif event == "delete_start":
            try:
                self.status_var.set("MAC 삭제 중")
            except Exception:
                pass
            self._set_timer("실행 중", "삭제 처리")
            raw_mac = payload.get("mac")
            mac = "None" if raw_mac is None else (_safe_text(raw_mac) or raw_mac.__class__.__name__)
            self._set_row_status(mac, "삭제 중", "")
            self._log(f"DELETE START: {mac}")
        elif event == "delete_done":
            raw_mac = payload.get("mac")
            mac = "None" if raw_mac is None else (_safe_text(raw_mac) or raw_mac.__class__.__name__)
            self._set_row_status(mac, "삭제 완료", "")
            self._log(f"DELETE OK: {mac}")
        elif event == "delete_error":
            raw_mac = payload.get("mac")
            raw_error = payload.get("error")
            mac = "None" if raw_mac is None else (_safe_text(raw_mac) or raw_mac.__class__.__name__)
            error = "" if raw_error is None else (_safe_text(raw_error) or raw_error.__class__.__name__)
            self._set_row_status(mac, "삭제 실패", error)
            self._log(f"DELETE ERROR: {mac} | {error}")
        elif event == "delete_unknown":
            raw_mac = payload.get("mac")
            raw_error = payload.get("error")
            mac = "None" if raw_mac is None else (_safe_text(raw_mac) or raw_mac.__class__.__name__)
            error = "" if raw_error is None else (_safe_text(raw_error) or raw_error.__class__.__name__)
            self._set_row_status(mac, "확인 필요", error)
            self._log(f"DELETE UNKNOWN: {mac} | {error}")
        elif event == "reappeared_macs":
            raw_macs = payload.get("macs")
            macs = []
            if isinstance(raw_macs, (list, tuple, set)):
                for mac in raw_macs:
                    try:
                        macs.append(str(mac))
                    except Exception:
                        continue
            try:
                self.status_var.set("삭제 MAC 재조회됨")
            except Exception:
                pass
            self._mark_reappeared_rows(macs)
            for mac in macs:
                self._log(f"REAPPEARED: {mac}")
        elif event == "delete_canceled":
            try:
                self.status_var.set("이번 삭제 취소됨")
            except Exception:
                pass
            self._set_timer("-", "대기")
            try:
                self.cancel_button.configure(state="disabled")
            except Exception:
                pass
            self._set_all_pending_status("취소됨")
            raw_count = payload.get("count")
            count = "None" if raw_count is None else (_safe_text(raw_count) or raw_count.__class__.__name__)
            self._log(f"CANCELED: {count} pending MAC(s)")
        elif event == "run_error":
            try:
                self.status_var.set("실패")
            except Exception:
                pass
            self._set_timer("-", "대기")
            try:
                self.cancel_button.configure(state="disabled")
            except Exception:
                pass
            raw_error = payload.get("error")
            error = "None" if raw_error is None else (_safe_text(raw_error) or raw_error.__class__.__name__)
            self._log(f"ERROR: {error}")

    def _handle_summary(self, summary) -> None:
        def summary_value(name: str, default: object) -> object:
            try:
                return getattr(summary, name, default)
            except Exception:
                return default

        self._ensure_cumulative_counters()
        error = _safe_text(summary_value("error", ""))
        canceled = _safe_bool(summary_value("canceled", False))
        verification_skipped = _safe_bool(summary_value("verification_skipped", False))
        delete_success_count = summary_value("delete_success_count", 0)
        reappeared_count = _safe_int(summary_value("reappeared_count", 0))
        raw_reappeared_macs = summary_value("reappeared_macs", [])
        if raw_reappeared_macs is None:
            raw_reappeared_macs = []
        reappeared_macs = []
        if isinstance(raw_reappeared_macs, (list, tuple, set)):
            try:
                reappeared_macs_iter = iter(raw_reappeared_macs)
            except Exception:
                reappeared_macs_iter = iter(())
            while True:
                try:
                    mac = next(reappeared_macs_iter)
                except StopIteration:
                    break
                except Exception:
                    break
                try:
                    reappeared_macs.append(str(mac))
                except Exception:
                    continue
        audit_path = _safe_text(summary_value("audit_path", None))
        audit_error = _safe_text(summary_value("audit_error", ""))
        history_error = _safe_text(summary_value("history_error", ""))
        target_macs = summary_value("target_macs", [])
        if target_macs is None:
            target_macs = []
        target_count = _safe_int(summary_value("queried_count", 0))
        if isinstance(target_macs, (list, tuple, set)):
            try:
                if target_macs:
                    target_count = len(target_macs)
            except Exception:
                pass
        if not self.current_run_query_counted:
            self._count_current_query(target_count)
        if not self.current_run_delete_counted:
            if not (error or canceled or verification_skipped):
                self.cumulative_deleted_count += _safe_int(delete_success_count)
            self.current_run_delete_counted = True
            self._sync_counter_vars()
        self._set_timer("-", "대기")
        try:
            self.cancel_button.configure(state="disabled")
        except Exception:
            pass
        try:
            if error:
                self.status_var.set("실패")
            elif canceled:
                self.status_var.set("취소됨")
            elif reappeared_count:
                self.status_var.set("삭제 MAC 재조회됨")
            else:
                self.status_var.set("완료")
        except Exception:
            pass
        if reappeared_macs:
            self._mark_reappeared_rows(reappeared_macs)
        if audit_path:
            self._log(f"AUDIT: {audit_path}")
        if audit_error:
            self._log(f"AUDIT WARNING: {audit_error}")
        if history_error:
            self._log(f"HISTORY WARNING: {history_error}")
        self._append_history_rows(summary)

    def _replace_table(self, macs: list[str], status: str, *, type_na_macs: Optional[list[str]] = None) -> None:
        try:
            self.table.delete(*self.table.get_children())
        except Exception:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        type_na_set = set(_unique_display_macs([] if type_na_macs is None else type_na_macs))
        for mac in _unique_display_macs(macs):
            message = TYPE_NA_MESSAGE if mac in type_na_set else ""
            try:
                self.table.insert("", "end", iid=mac, values=(mac, status, now, "", message))
            except Exception:
                return

    def _set_row_status(self, mac: str, status: str, error: str) -> None:
        try:
            if not mac or not self.table.exists(mac):
                return
            values = list(self.table.item(mac, "values"))
        except Exception:
            return
        if len(values) < 5:
            return
        values[1] = status
        if status in {"삭제 완료", "삭제 실패", "확인 필요", "재조회됨"}:
            values[3] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            existing_message = str(values[4] or "")
        except Exception:
            existing_message = ""
        values[4] = _merge_status_message(existing_message, error)
        try:
            self.table.item(mac, values=values)
            self.table.item(mac, tags=("reappeared",) if status == "재조회됨" else ())
        except Exception:
            return

    def _mark_reappeared_rows(self, macs: list[str]) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        error = "삭제 성공 후 검증 조회에서 다시 발견"
        for mac in macs:
            try:
                exists = self.table.exists(mac)
            except Exception:
                continue
            if exists:
                self._set_row_status(mac, "재조회됨", error)
            else:
                try:
                    self.table.insert(
                        "",
                        "end",
                        iid=mac,
                        values=(mac, "재조회됨", now, now, error),
                        tags=("reappeared",),
                    )
                except Exception:
                    continue

    def _set_all_pending_status(self, status: str) -> None:
        try:
            item_ids = self.table.get_children()
        except Exception:
            return
        try:
            item_ids_iter = iter(item_ids)
        except Exception:
            return
        for item_id in item_ids_iter:
            try:
                values = list(self.table.item(item_id, "values"))
            except TypeError:
                continue
            except Exception:
                continue
            if len(values) < 2:
                continue
            try:
                should_update = values[1] in {"삭제 대상", "삭제 중"}
            except Exception:
                continue
            if should_update:
                values[1] = status
                try:
                    self.table.item(item_id, values=values)
                except Exception:
                    continue

    def _set_running(self, running: bool) -> None:
        self.is_running = running
        try:
            self.manual_button.configure(state="disabled" if running or self.scheduler_running else "normal")
        except Exception:
            pass
        try:
            self.schedule_button.configure(state="disabled" if running or self.scheduler_running else "normal")
        except Exception:
            pass
        if running:
            self._reset_run_counters()
            self._set_timer("실행 중", "조회/삭제 처리")
            try:
                self.cancel_button.configure(state="disabled")
            except Exception:
                pass
        elif not self.scheduler_running:
            self._set_timer("-", "대기")
        self._sync_settings_visibility()

    def _set_timer(self, value: str, state: str) -> None:
        try:
            self.timer_value_var.set(value)
            self.timer_state_var.set(state)
        except Exception:
            return

    def _reset_run_counters(self) -> None:
        self._ensure_cumulative_counters()
        self.current_run_queried_count = 0
        self.current_run_query_counted = False
        self.current_run_delete_counted = False
        self._sync_counter_vars()

    def _count_current_query(self, count: int) -> None:
        self._ensure_cumulative_counters()
        self.current_run_queried_count = max(_safe_int(count), 0)
        if not self.current_run_query_counted:
            self.cumulative_queried_count += self.current_run_queried_count
            self.current_run_query_counted = True
        self._sync_counter_vars()

    def _sync_counter_vars(self) -> None:
        self._ensure_cumulative_counters()
        try:
            self.counter_vars["queried"].set(str(self.cumulative_queried_count))
            self.counter_vars["deleted"].set(str(self.cumulative_deleted_count))
        except Exception:
            return

    def _ensure_cumulative_counters(self) -> None:
        counter_vars = self.__dict__.get("counter_vars", {})
        if "cumulative_queried_count" not in self.__dict__:
            try:
                queried_value = counter_vars["queried"].get()
            except Exception:
                queried_value = 0
            self.cumulative_queried_count = _safe_int(queried_value)
        if "cumulative_deleted_count" not in self.__dict__:
            try:
                deleted_value = counter_vars["deleted"].get()
            except Exception:
                deleted_value = 0
            self.cumulative_deleted_count = _safe_int(deleted_value)
        if "current_run_queried_count" not in self.__dict__:
            self.current_run_queried_count = 0
        if "current_run_query_counted" not in self.__dict__:
            self.current_run_query_counted = False
        if "current_run_delete_counted" not in self.__dict__:
            self.current_run_delete_counted = False

    def _sync_settings_visibility(self) -> None:
        if self.settings_frame is None:
            return
        try:
            if self.is_running or self.scheduler_running:
                self.settings_frame.grid_remove()
            else:
                self.settings_frame.grid()
        except Exception:
            return

    def _append_history_rows(self, summary) -> None:
        def safe_get(obj: object, name: str, default: object) -> object:
            try:
                return getattr(obj, name, default)
            except Exception:
                return default

        def safe_bool(value: object) -> bool:
            try:
                return bool(value)
            except Exception:
                return False

        delete_results = safe_get(summary, "delete_results", None)
        if not isinstance(delete_results, (list, tuple)):
            return
        try:
            has_delete_results = bool(delete_results)
        except Exception:
            return
        if not has_delete_results:
            return
        started_at = safe_get(summary, "started_at", None)
        try:
            run_at = (
                started_at.strftime("%Y-%m-%d %H:%M:%S")
                if callable(getattr(started_at, "strftime", None))
                else ""
            )
        except Exception:
            run_at = ""
        raw_reappeared_macs = safe_get(summary, "reappeared_macs", [])
        if raw_reappeared_macs is None:
            raw_reappeared_macs = []
        reappeared_macs = set()
        if isinstance(raw_reappeared_macs, (list, tuple, set)):
            try:
                reappeared_macs = set(
                    _unique_display_macs([mac for mac in raw_reappeared_macs if isinstance(mac, str)])
                )
            except Exception:
                reappeared_macs = set()
        try:
            delete_results_iter = iter(delete_results)
        except Exception:
            return
        while True:
            try:
                item = next(delete_results_iter)
            except StopIteration:
                break
            except Exception:
                break
            mac = safe_get(item, "mac", "")
            if not isinstance(mac, str) or not mac:
                continue
            status = safe_get(item, "status", "")
            result, error, tags = self._history_row_display(
                {
                    "mac": mac,
                    "result": "",
                    "status": status,
                    "success": safe_bool(safe_get(item, "success", False)),
                    "error": safe_get(item, "error", ""),
                    "reappeared": mac in reappeared_macs or status == "reappeared",
                }
            )
            self._insert_history_row(run_at, mac, result, error, tags=tags)
        self._cap_history_rows()

    def _cap_history_rows(self) -> None:
        try:
            children = self.history_table.get_children()
        except Exception:
            return
        try:
            overflow = len(children) - MAX_HISTORY_ROWS
        except Exception:
            return
        if overflow > 0:
            try:
                self.history_table.delete(*children[:overflow])
            except Exception:
                return

    def _log(self, message: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"{time.strftime('%H:%M:%S')} {message}\n")
            self._cap_log_lines()
            self.log_text.see("end")
        except Exception:
            pass
        finally:
            try:
                self.log_text.configure(state="disabled")
            except Exception:
                pass

    def _cap_log_lines(self) -> None:
        try:
            line_count = int(self.log_text.index("end-1c").split(".")[0])
        except Exception:
            return
        overflow = line_count - MAX_LOG_LINES
        if overflow > 0:
            try:
                self.log_text.delete("1.0", f"{overflow + 1}.0")
            except Exception:
                return

    def clear_log(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            try:
                self.log_text.configure(state="disabled")
            except Exception:
                pass

    def clear_history(self) -> None:
        try:
            self.history_table.delete(*self.history_table.get_children())
        except Exception:
            return
        self.history_row_counter = 0

    def _load_history_from_output_dir(self, output_dir: Path, *, force: bool = False) -> None:
        def safe_text(value: object) -> str:
            try:
                return str(value)
            except Exception:
                return ""

        def safe_get(mapping: dict[str, object], key: str, default: object) -> object:
            try:
                return mapping.get(key, default)
            except Exception:
                return default

        try:
            output_dir = output_dir.expanduser()
        except Exception:
            return
        if not force and self.loaded_history_dir == output_dir:
            return
        if not hasattr(self, "history_table"):
            return
        try:
            records = self._read_history_records(output_dir)
        except Exception:
            return
        try:
            recent_records = records[-MAX_HISTORY_ROWS:]
        except Exception:
            return
        try:
            self.history_table.delete(*self.history_table.get_children())
        except Exception:
            return
        self.loaded_history_dir = output_dir
        self.history_row_counter = 0
        for record in recent_records:
            run_at = safe_text(safe_get(record, "run_at", ""))[:19].replace("T", " ")
            mac = safe_text(safe_get(record, "mac", ""))
            if not mac:
                continue
            result, error, tags = self._history_row_display(record)
            self._insert_history_row(run_at, mac, result, error, tags=tags)

    def _read_history_records(self, output_dir: Path) -> list[dict[str, object]]:
        def safe_text(value: object) -> str:
            try:
                return str(value)
            except Exception:
                return ""

        def safe_get(mapping: dict[str, object], key: str, default: object) -> object:
            try:
                return mapping.get(key, default)
            except Exception:
                return default

        jsonl_path = output_dir / HISTORY_FILE_NAME
        try:
            has_jsonl_history = jsonl_path.exists()
        except Exception:
            has_jsonl_history = False
        if has_jsonl_history:
            records: deque[dict[str, object]] = deque(maxlen=MAX_HISTORY_ROWS)
            try:
                with jsonl_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, RecursionError, RuntimeError, UnicodeError):
                            continue
                        mac = safe_get(record, "mac", "") if isinstance(record, dict) else ""
                        if isinstance(record, dict) and isinstance(mac, str) and mac:
                            records.append(record)
            except (OSError, UnicodeError, RuntimeError):
                return self._read_audit_history_records(output_dir)
            return list(records)
        return self._read_audit_history_records(output_dir)

    def _read_audit_history_records(self, output_dir: Path) -> list[dict[str, object]]:
        def safe_text(value: object) -> str:
            try:
                return str(value)
            except Exception:
                return ""

        def safe_get(mapping: dict[str, object], key: str, default: object) -> object:
            try:
                return mapping.get(key, default)
            except Exception:
                return default

        records: deque[dict[str, object]] = deque(maxlen=MAX_HISTORY_ROWS)
        audit_paths = _recent_audit_paths(output_dir)
        for audit_path in audit_paths:
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, RuntimeError):
                continue
            if not isinstance(audit, dict):
                continue
            run_at = safe_text(safe_get(audit, "started_at", ""))
            reappeared_macs = safe_get(audit, "reappeared_macs", [])
            if reappeared_macs is None:
                reappeared_macs = []
            if not isinstance(reappeared_macs, (list, tuple, set)):
                reappeared_macs = []
            try:
                reappeared = {mac for mac in reappeared_macs if isinstance(mac, str)}
            except Exception:
                reappeared = set()
            delete_results = safe_get(audit, "delete_results", [])
            if delete_results is None:
                delete_results = []
            if not isinstance(delete_results, (list, tuple)):
                delete_results = []
            try:
                delete_results_iter = iter(delete_results)
            except Exception:
                continue
            while True:
                try:
                    item = next(delete_results_iter)
                except StopIteration:
                    break
                except Exception:
                    break
                if not isinstance(item, dict):
                    continue
                mac = safe_get(item, "mac", "")
                if not isinstance(mac, str) or not mac:
                    continue
                record = dict(item)
                record["run_at"] = run_at
                record["reappeared"] = mac in reappeared or safe_get(item, "status", "") == "reappeared"
                records.append(record)
        return list(records)

    def _history_row_display(self, record: dict[str, object]) -> tuple[str, str, tuple[str, ...]]:
        def safe_text(value: object) -> str:
            try:
                if not value:
                    return ""
                return str(value)
            except Exception:
                return ""

        def safe_bool(value: object) -> bool:
            try:
                return bool(value)
            except Exception:
                return False

        def safe_get(key: str, default: object = "") -> object:
            try:
                return record.get(key, default)
            except Exception:
                return default

        status = safe_text(safe_get("status"))
        result = safe_text(safe_get("result"))
        error = safe_text(safe_get("error"))
        reappeared = safe_bool(safe_get("reappeared", False)) or status == "reappeared"
        if reappeared:
            return "재조회됨", error or "삭제 성공 후 검증 조회에서 다시 발견", ("reappeared",)
        if result:
            return result, error, ()
        if status == "unknown":
            return "확인 필요", error, ()
        if safe_bool(safe_get("success", False)):
            return "삭제 완료", error, ()
        return "삭제 실패", error, ()

    def _insert_history_row(
        self,
        run_at: str,
        mac: str,
        result: str,
        error: str,
        *,
        tags: tuple[str, ...] = (),
    ) -> None:
        row_id = f"history-{self.history_row_counter}"
        try:
            self.history_table.insert("", "end", iid=row_id, values=(run_at, mac, result, error), tags=tags)
        except Exception:
            return
        self.history_row_counter += 1

    def _copy_mac_from_table_event(self, event: tk.Event, table: ttk.Treeview, mac_column: str) -> None:
        try:
            clicked_column = table.identify_column(event.x)
            row_id = table.identify_row(event.y)
        except Exception:
            return
        if clicked_column != mac_column:
            return
        if not row_id:
            return
        try:
            values = list(table.item(row_id, "values"))
        except Exception:
            return
        try:
            column_index = int(mac_column.removeprefix("#")) - 1
        except (AttributeError, ValueError):
            return
        if column_index < 0 or column_index >= len(values):
            return
        try:
            mac = str(values[column_index]).strip()
        except Exception:
            return
        if not mac:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(mac)
        except Exception:
            return
        self._show_copy_notice(mac)

    def _show_copy_notice(self, mac: str) -> None:
        if self.copy_notice_after_id is not None:
            try:
                self.after_cancel(self.copy_notice_after_id)
            except Exception:
                pass
        try:
            self.copy_notice_title_var.set("복사 완료")
        except Exception:
            pass
        try:
            self.copy_notice_mac_var.set(mac)
        except Exception:
            pass
        if self.copy_notice_frame is not None:
            try:
                self.copy_notice_frame.place(relx=0.5, rely=0.5, anchor="center")
                self.copy_notice_frame.lift()
            except Exception:
                pass
        try:
            self.copy_notice_after_id = self.after(1000, self._hide_copy_notice)
        except Exception:
            self.copy_notice_after_id = None

    def _hide_copy_notice(self) -> None:
        try:
            self.copy_notice_title_var.set("")
        except Exception:
            pass
        try:
            self.copy_notice_mac_var.set("")
        except Exception:
            pass
        if self.copy_notice_frame is not None:
            try:
                self.copy_notice_frame.place_forget()
            except Exception:
                pass
        self.copy_notice_after_id = None

    def _destroy_window(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass


def _unique_display_macs(macs: object) -> list[str]:
    try:
        macs_iter = iter(macs)  # type: ignore[arg-type]
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
        if not isinstance(mac, str):
            continue
        try:
            normalized = normalize_mac(mac) or mac.strip().casefold()
        except Exception:
            continue
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _recent_audit_paths(output_dir: Path) -> list[Path]:
    try:
        paths = heapq.nlargest(
            MAX_HISTORY_ROWS,
            output_dir.glob("*/cleanup_summary.json"),
            key=_audit_path_sort_key,
        )
    except Exception:
        return []
    try:
        return sorted(paths, key=_audit_path_sort_key)
    except Exception:
        return []


def _audit_path_sort_key(path: Path) -> str:
    try:
        return str(path)
    except Exception:
        return ""


def _merge_status_message(existing: str, update: str) -> str:
    has_type_na_message = TYPE_NA_MESSAGE in existing
    try:
        update = update.strip()
    except Exception:
        update = _safe_text(update)
        try:
            update = update.strip()
        except Exception:
            update = ""
    if has_type_na_message and update:
        return f"{TYPE_NA_MESSAGE} | {update}"
    if has_type_na_message:
        return TYPE_NA_MESSAGE
    return update


def _safe_int(value: object) -> int:
    try:
        return int(str(value))
    except Exception:
        return 0


def _safe_interval_seconds(value: object) -> int:
    try:
        interval = int(str(value))
    except Exception:
        return MIN_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, interval)


def _safe_bool(value: object) -> bool:
    try:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y"}
        return bool(value)
    except Exception:
        return False


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def main() -> int:
    if os.environ.get("ARUBA_MM_CLEANUP_GUI_SMOKE") == "1":
        app = ArubaMmCleanupGui()
        app.update_idletasks()
        app.closing = True
        if app._drain_after_id is not None:
            try:
                app.after_cancel(app._drain_after_id)
            except Exception:
                pass
        app._destroy_window()
        return 0
    app = ArubaMmCleanupGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
