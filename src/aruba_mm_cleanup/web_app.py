"""Loopback-only browser UI with CSRF and two-step deletion approval."""

from __future__ import annotations

import argparse
import secrets
import sys
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .cleanup import MmCleanupRunner
from .hostkeys import HostKeyApprovalRequired, HostKeyObservation
from .models import CleanupPlan
from .parser import normalize_mac
from .web_support import (
    WebRunRequest,
    cleanup_settings_from_request,
    connection_config_from_request,
    parse_run_request,
    smoke_status,
    summary_view,
)

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
MAX_FORM_BYTES = 64 * 1024


class WebAppState:
    def __init__(self, *, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.runner = MmCleanupRunner(persistent_session=True)
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.notice = ""
        self.error = ""
        self.last_summary: dict[str, object] = {}
        self.cumulative_queried_count = 0
        self.cumulative_deleted_count = 0
        self.pending_request: Optional[WebRunRequest] = None
        self.pending_targets: tuple[str, ...] = ()
        self.pending_host_key: Optional[HostKeyObservation] = None

    def clear_pending(self) -> None:
        self.pending_request = None
        self.pending_targets = ()
        self.pending_host_key = None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aruba-mm-cleanup-web",
        description="Aruba MM Cleanup loopback web app.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="loopback web server port")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="default audit output directory")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--smoke", action="store_true", help="verify that the web app executable starts")
    args = parser.parse_args(argv)

    if args.smoke:
        print(smoke_status())
        return 0
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")

    state = WebAppState(output_dir=args.output_dir.expanduser())
    handler_class = _make_handler(state)
    server = ThreadingHTTPServer((DEFAULT_WEB_HOST, args.port), handler_class)
    url = f"http://{DEFAULT_WEB_HOST}:{args.port}/"
    print(f"Aruba MM Cleanup web app (loopback only): {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            state.runner.close_session(reason="web_app_shutdown")
        except Exception:
            pass
        server.server_close()
    return 0


def _make_handler(state: WebAppState):
    class ArubaMmCleanupWebHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not self._valid_local_host_header():
                self.send_error(403, "loopback Host header required")
                return
            if self.path == "/healthz":
                self._send_text("ok")
                return
            if self.path != "/":
                self.send_error(404)
                return
            self._send_html(_render_page(state))

        def do_POST(self) -> None:  # noqa: N802
            if not self._valid_local_host_header():
                self.send_error(403, "loopback Host header required")
                return
            try:
                form = self._read_form()
                self._require_csrf(form)
            except ValueError as exc:
                self._send_error_page(str(exc), status=400)
                return
            if self.path not in {"/preview", "/approve-host-key", "/run", "/disconnect"}:
                self.send_error(404)
                return
            if not state.operation_lock.acquire(blocking=False):
                self._send_error_page("다른 작업이 실행 중입니다.", status=409)
                return
            try:
                if self.path == "/disconnect":
                    try:
                        state.runner.close_session(reason="web_manual")
                    finally:
                        with state.state_lock:
                            state.clear_pending()
                    with state.state_lock:
                        state.notice = "장비 세션 연결을 해제했습니다."
                        state.error = ""
                elif self.path == "/preview":
                    request = parse_run_request(form, default_output_dir=state.output_dir)
                    self._prepare_preview(request)
                elif self.path == "/approve-host-key":
                    self._approve_host_key(form)
                else:
                    self._run_approved_snapshot(form)
                self._send_html(_render_page(state))
            except Exception as exc:
                with state.state_lock:
                    state.notice = ""
                    state.error = str(exc) or exc.__class__.__name__
                self._send_html(_render_page(state), status=400)
            finally:
                state.operation_lock.release()

        def _prepare_preview(self, request: WebRunRequest) -> None:
            with state.state_lock:
                state.clear_pending()
            try:
                query = state.runner.query_users(
                    connection_config_from_request(request),
                    cleanup_settings_from_request(request),
                )
            except HostKeyApprovalRequired as exc:
                with state.state_lock:
                    state.pending_request = request
                    state.pending_targets = ()
                    state.pending_host_key = exc.observation
                    state.notice = "최초 SSH 서버 지문을 별도 경로로 확인한 뒤 승인하세요."
                    state.error = ""
                return
            targets = _unique_normalized_macs(query.macs)
            with state.state_lock:
                state.pending_request = request if targets else None
                state.pending_targets = targets
                state.pending_host_key = None
                state.error = ""
                state.notice = (
                    f"삭제 대상 {len(targets)}개를 조회했습니다. 아래 snapshot을 확인하세요."
                    if targets
                    else "현재 삭제 대상이 없습니다. 삭제 명령은 실행되지 않았습니다."
                )

        def _approve_host_key(self, form: dict[str, list[str]]) -> None:
            with state.state_lock:
                request = state.pending_request
                observation = state.pending_host_key
            if request is None or observation is None:
                raise ValueError("승인 대기 중인 SSH 지문이 없습니다.")
            fingerprint = _form_value(form, "fingerprint")
            if not secrets.compare_digest(fingerprint, observation.fingerprint):
                raise ValueError("SSH 지문 확인 값이 일치하지 않습니다.")
            if _form_value(form, "confirmation") != "TRUST":
                raise ValueError("SSH 지문을 승인하려면 TRUST를 정확히 입력하세요.")
            state.runner.approve_host_key(observation)
            self._prepare_preview(request)

        def _run_approved_snapshot(self, form: dict[str, list[str]]) -> None:
            with state.state_lock:
                request = state.pending_request
                targets = state.pending_targets
            if request is None or not targets:
                raise ValueError("승인 대기 중인 삭제 대상 snapshot이 없습니다.")
            phrase = f"DELETE {len(targets)}"
            if _form_value(form, "confirmation") != phrase:
                raise ValueError(f"삭제를 승인하려면 {phrase}를 정확히 입력하세요.")

            expected = (request.host, request.port, request.username, request.role, targets)

            def approve_exact_snapshot(plan: CleanupPlan) -> bool:
                return (plan.host, plan.port, plan.username, plan.role, plan.target_macs) == expected

            with state.state_lock:
                state.clear_pending()
            summary = state.runner.run_once(
                connection_config_from_request(request),
                cleanup_settings_from_request(request),
                output_dir=request.output_dir.expanduser(),
                approve_targets=approve_exact_snapshot,
            )
            view = summary_view(summary)
            with state.state_lock:
                state.last_summary = view
                state.cumulative_queried_count += _safe_int(view.get("queried_count", 0))
                state.cumulative_deleted_count += _safe_int(view.get("delete_success_count", 0))
                if getattr(summary, "canceled", False):
                    state.notice = "대상 snapshot이 바뀌었거나 승인이 거부되어 삭제하지 않았습니다."
                else:
                    state.notice = "승인된 snapshot 작업이 완료되었습니다."
                state.error = _safe_summary_error(view)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_form(self) -> dict[str, list[str]]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type != "application/x-www-form-urlencoded":
                raise ValueError("지원하지 않는 요청 형식입니다.")
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("요청 크기가 올바르지 않습니다.") from exc
            if length < 0 or length > MAX_FORM_BYTES:
                raise ValueError("요청 크기가 허용 범위를 벗어났습니다.")
            try:
                raw = self.rfile.read(length).decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError("요청 인코딩이 올바르지 않습니다.") from exc
            return parse_qs(raw, keep_blank_values=True, max_num_fields=32)

        def _require_csrf(self, form: dict[str, list[str]]) -> None:
            supplied = _form_value(form, "csrf_token")
            if not supplied or not secrets.compare_digest(supplied, state.csrf_token):
                raise ValueError("CSRF 확인에 실패했습니다. 페이지를 새로고침하세요.")

        def _valid_local_host_header(self) -> bool:
            raw_host = self.headers.get("Host", "")
            try:
                parsed = urlsplit(f"//{raw_host}")
                hostname = (parsed.hostname or "").casefold()
                port = parsed.port
            except ValueError:
                return False
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                return False
            return port is None or port == self.server.server_port

        def _send_error_page(self, message: str, *, status: int) -> None:
            body = (
                "<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\">"
                f"<title>요청 차단</title><p>{escape(message)}</p>"
                "<p><a href=\"/\">돌아가기</a></p></html>"
            )
            self._send_html(body, status=status)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def _send_html(self, body: str, *, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _send_text(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)

    return ArubaMmCleanupWebHandler


def _render_page(state: WebAppState) -> str:
    with state.state_lock:
        summary = dict(state.last_summary)
        notice_text = state.notice
        error_text = state.error
        cumulative_queried = state.cumulative_queried_count
        cumulative_deleted = state.cumulative_deleted_count
        pending_targets = tuple(state.pending_targets)
        pending_host_key = state.pending_host_key
        csrf_token = state.csrf_token
        output_dir = state.output_dir
    notice = f'<div class="notice">{escape(notice_text)}</div>' if notice_text else ""
    error = f'<div class="error">{escape(error_text)}</div>' if error_text else ""
    csrf = escape(csrf_token, quote=True)
    host_key_panel = ""
    if pending_host_key is not None:
        host_key_panel = f"""
        <div class="warning">
          <h3>최초 SSH 지문 승인</h3>
          <p>{escape(pending_host_key.host)}:{pending_host_key.port} / {escape(pending_host_key.key_type)}</p>
          <p class="path">{escape(pending_host_key.fingerprint)}</p>
          <form method="post" action="/approve-host-key">
            <input type="hidden" name="csrf_token" value="{csrf}">
            <input type="hidden" name="fingerprint" value="{escape(pending_host_key.fingerprint, quote=True)}">
            <label>장비 관리자에게 별도 경로로 확인했다면 TRUST 입력</label>
            <input name="confirmation" autocomplete="off" required>
            <button type="submit">지문 저장 후 다시 조회</button>
          </form>
        </div>"""
    preview_panel = ""
    if pending_targets:
        target_items = "".join(f"<li><code>{escape(mac)}</code></li>" for mac in pending_targets)
        phrase = f"DELETE {len(pending_targets)}"
        preview_panel = f"""
        <div class="warning">
          <h3>삭제 대상 미리보기 ({len(pending_targets)}개)</h3>
          <ul>{target_items}</ul>
          <form method="post" action="/run">
            <input type="hidden" name="csrf_token" value="{csrf}">
            <label>정확히 {escape(phrase)} 입력</label>
            <input name="confirmation" autocomplete="off" required>
            <button class="danger" type="submit">이 snapshot 삭제 승인</button>
          </form>
        </div>"""
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aruba MM Session Cleanup</title>
  <style>
    body {{ margin: 0; font-family: Segoe UI, Apple SD Gothic Neo, sans-serif; background: #f4f6f8; color: #1f2933; }}
    header {{ background: #12343b; color: #fff; padding: 18px 28px; }}
    main {{ display: grid; grid-template-columns: minmax(280px, 400px) 1fr; gap: 20px; padding: 20px; }}
    section {{ background: #fff; border: 1px solid #d8dee4; border-radius: 8px; padding: 18px; }}
    label {{ display: block; font-size: 13px; font-weight: 600; margin-top: 12px; }}
    input {{ width: 100%; box-sizing: border-box; padding: 9px 10px; border: 1px solid #b8c2cc; border-radius: 6px; }}
    button {{ margin-top: 14px; padding: 10px 12px; border: 0; border-radius: 6px; background: #126e82; color: #fff; font-weight: 700; cursor: pointer; }}
    button.secondary {{ background: #4b5563; }} button.danger {{ background: #a61b1b; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid #d8dee4; border-radius: 8px; padding: 14px; background: #fbfcfd; }}
    .value {{ font-size: 28px; font-weight: 800; margin-top: 6px; }}
    .notice {{ background: #e8f7ee; border: 1px solid #9bd3ad; padding: 10px; border-radius: 6px; margin-bottom: 12px; }}
    .error {{ background: #fdecec; border: 1px solid #f0a3a3; padding: 10px; border-radius: 6px; margin-bottom: 12px; }}
    .warning {{ background: #fff8e6; border: 1px solid #d6a94a; padding: 12px; border-radius: 6px; margin-top: 16px; }}
    .path {{ word-break: break-all; font-family: Consolas, monospace; font-size: 12px; }}
    @media (max-width: 760px) {{ main {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header><h1>Aruba MM Session Cleanup</h1><p>조회 → 미리보기 → 명시 승인 → 삭제 → 재검증</p></header>
  <main>
    <section>
      <h2>장비 접속 및 조회</h2>
      {notice}{error}
      <form method="post" action="/preview">
        <input type="hidden" name="csrf_token" value="{csrf}">
        <label>MM/WLC 주소</label><input name="host" autocomplete="off" required>
        <label>SSH 포트</label><input name="port" value="22" inputmode="numeric">
        <label>계정</label><input name="username" autocomplete="username" required>
        <label>암호</label><input name="password" type="password" autocomplete="current-password" required>
        <label>Enable 암호</label><input name="enable_password" type="password">
        <label>Role</label><input name="role" value="profiling">
        <label>장비 응답 대기(초)</label><input name="timeout" value="60" inputmode="numeric">
        <label>결과 폴더</label><input name="output_dir" value="{escape(str(output_dir), quote=True)}">
        <button type="submit">삭제 대상 조회</button>
      </form>
      <form method="post" action="/disconnect">
        <input type="hidden" name="csrf_token" value="{csrf}">
        <button class="secondary" type="submit">세션 연결 해제</button>
      </form>
      {host_key_panel}{preview_panel}
    </section>
    <section>
      <h2>작업 결과</h2>
      <div class="cards">{_metric_card("누적 조회 MAC", cumulative_queried)}{_metric_card("누적 삭제 MAC", cumulative_deleted)}</div>
      <h3>최근 실행</h3>
      <p>조회 {escape(str(summary.get("queried_count", 0)))} / 삭제 {escape(str(summary.get("delete_success_count", 0)))} / 실패 {escape(str(summary.get("delete_failure_count", 0)))} / 남은 MAC {escape(str(summary.get("remaining_count", 0)))} / 재조회 {escape(str(summary.get("reappeared_count", 0)))}</p>
      <h3>저장 경로</h3>
      <p class="path">Audit: {escape(str(summary.get("audit_path", "")))}</p>
      <p class="path">History: {escape(str(summary.get("history_path", "")))}</p>
    </section>
  </main>
</body>
</html>"""


def _form_value(form: dict[str, list[str]], name: str) -> str:
    value = form.get(name, [])
    return value[0] if value else ""


def _unique_normalized_macs(macs: object) -> tuple[str, ...]:
    if not isinstance(macs, (list, tuple, set)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in macs:
        normalized = normalize_mac(item) if isinstance(item, str) else None
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _metric_card(label: str, value: object) -> str:
    return f'<div class="card"><div>{escape(label)}</div><div class="value">{escape(str(value))}</div></div>'


def _safe_summary_error(summary: dict[str, object]) -> str:
    for key in ("error", "audit_error", "history_error"):
        value = summary.get(key, "")
        if value:
            return str(value)
    return ""


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
