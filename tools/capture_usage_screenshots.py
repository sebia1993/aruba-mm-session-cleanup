"""Capture real cleanup widgets with synthetic events; never approve or execute deletion."""

from __future__ import annotations

import argparse
import sys
import tempfile
import tkinter as tk
from datetime import datetime
from pathlib import Path

from capture_windows import block_network, capture_window, prepare_capture_desktop, write_manifest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    prepare_capture_desktop()
    block_network()
    from aruba_mm_cleanup import gui_app as gui
    from aruba_mm_cleanup.models import CleanupPlan, CleanupRunSummary, DeleteResult

    with tempfile.TemporaryDirectory(prefix="cleanup-docs-") as directory:
        gui.DEFAULT_OUTPUT_DIR = Path(directory)
        app = gui.ArubaMmCleanupGui()
        try:
            app.maxsize(1920, 1400)
            app.geometry("1400x1000+0+0")
            app.update()
            assert app.winfo_width() >= 1300 and app.winfo_height() >= 950, app.geometry()
            app.host_var.set("192.0.2.20")
            app.username_var.set("netops-demo")
            app.password_var.set("documentation-only")
            app.output_dir_var.set(r"C:\DocumentationDemo\Cleanup")
            app.status_var.set("문서용 합성 입력 · 장비 접속 없음")
            capture_window(app, output / "01-settings.png")
            macs = ["02:00:00:00:00:11", "02:00:00:00:00:22"]
            app._handle_progress("query_done", {"macs": macs, "count": 2})
            app.status_var.set("합성 조회 결과 · 최종 승인 전")
            app._log("[DEMO] 합성 이벤트: 실제 조회/삭제 명령을 전송하지 않았습니다.")
            capture_window(app, output / "02-targets.png")
            plan = CleanupPlan(
                plan_id="documentation-only",
                created_at=datetime(2026, 9, 8, 9),
                host="192.0.2.20",
                port=22,
                username="netops-demo",
                role="profiling",
                query_command="show global-user-table list role profiling",
                queried_count=2,
                target_macs=tuple(macs),
            )
            request = gui._ApprovalRequest("targets", plan)
            captured = []

            def capture_approval() -> None:
                for child in app.winfo_children():
                    if isinstance(child, tk.Toplevel):
                        try:
                            capture_window(child, output / "03-approval.png")
                            captured.append(True)
                        finally:
                            child.cancel()  # Actual dialog cancel; never enter DELETE N.
                        return
                raise RuntimeError("Actual approval dialog was not found")

            app.after(350, capture_approval)
            app.after(
                10000, lambda: [child.destroy() for child in app.winfo_children() if isinstance(child, tk.Toplevel)]
            )
            app._handle_approval_request(request)
            assert captured and request.done.is_set() and not request.approved
            # Separate synthetic result illustration after cancelling the capture dialog.
            app._handle_progress("delete_done", {"mac": macs[0]})
            app._handle_progress("delete_unknown", {"mac": macs[1], "error": "합성 예시: 응답 불명확, 재조회 필요"})
            summary = CleanupRunSummary(
                started_at=datetime(2026, 9, 8, 9),
                role="profiling",
                queried_count=2,
                delete_success_count=1,
                delete_failure_count=1,
                remaining_count=1,
                target_macs=macs,
                delete_results=[
                    DeleteResult(macs[0], True, "synthetic-only", status="verified_deleted", verified_absent=True),
                    DeleteResult(macs[1], False, "synthetic-only", error="재조회 필요", status="unknown"),
                ],
            )
            app._handle_summary(summary)
            app.status_var.set("합성 검증 결과 · 확인된 삭제 1 / 확인 필요 1")
            capture_window(app, output / "04-verification.png")
            write_manifest(
                output,
                app=gui.APP_TITLE,
                version="0.2.0",
                method="Actual Tk window / PrintWindow; synthetic query/result event injection; actual approval dialog captured then cancelled; no cleanup runner execution",
            )
        finally:
            app.destroy()


if __name__ == "__main__":
    main()
