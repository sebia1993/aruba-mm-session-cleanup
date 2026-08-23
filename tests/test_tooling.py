import configparser
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aruba_mm_cleanup.cli import main as cli_main
from tools.verify_release_package import (
    _find_latest_zip,
    _read_zip_names,
    _smoke_gui,
    _smoke_web,
)
from tools.verify_release_package import (
    main as verifier_main,
)

RELEASE_ZIP_REQUIRED_FILES = [
    "README_START_HERE_KO.txt",
    "gui/ArubaMMCleanupGUI.exe",
    "gui/USER_GUIDE_KO.md",
    "gui/config/mock_scenarios/profiling_users.txt",
    "web/ArubaMMCleanupWeb.exe",
    "web/start_webapp.cmd",
    "web/config/mock_scenarios/profiling_users.txt",
]


@pytest.fixture(autouse=True)
def _secure_cli_password_prompts(monkeypatch):
    monkeypatch.setattr(
        "aruba_mm_cleanup.cli.getpass.getpass",
        lambda prompt: "" if prompt.startswith("Enable") else "secret",
    )


def write_release_zip(zip_path, extra_names=()):
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name in RELEASE_ZIP_REQUIRED_FILES:
            archive.writestr(name, "sample")
        for name in extra_names:
            archive.writestr(name, "sample")


def test_release_zip_verifier_checks_required_files(tmp_path):
    repo_root = Path(__file__).parents[1]
    verifier = repo_root / "tools" / "verify_release_package.py"
    zip_path = tmp_path / "aruba-mm-session-cleanup_v0.2.0_windows.zip"
    write_release_zip(zip_path)

    completed = subprocess.run(
        [sys.executable, str(verifier), "--zip", str(zip_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_release_zip_verifier_ignores_disappearing_zip_candidates(tmp_path, monkeypatch):
    stale_zip = tmp_path / "stale.zip"
    latest_zip = tmp_path / "latest.zip"
    stale_zip.write_text("stale", encoding="utf-8")
    latest_zip.write_text("latest", encoding="utf-8")
    original_stat = Path.stat

    def flaky_stat(path, *args, **kwargs):
        if path == stale_zip:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert _find_latest_zip(tmp_path) == latest_zip


def test_release_zip_verifier_reports_inaccessible_dist_directory(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    original_glob = Path.glob

    def inaccessible_glob(path, pattern):
        if path == dist_dir and pattern == "*.zip":
            raise PermissionError("dist access denied")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", inaccessible_glob)

    with pytest.raises(SystemExit) as exc_info:
        _find_latest_zip(dist_dir)

    assert "Release ZIP directory is not accessible" in str(exc_info.value)
    assert str(dist_dir) in str(exc_info.value)


def test_release_zip_verifier_reports_inaccessible_zip_path(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    original_exists = Path.exists

    def inaccessible_exists(path):
        if path == zip_path:
            raise PermissionError("access denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", inaccessible_exists)

    with pytest.raises(SystemExit) as exc_info:
        verifier_main(["--zip", str(zip_path)])

    assert "Release ZIP is not accessible" in str(exc_info.value)


def test_release_zip_verifier_reports_zip_open_permission_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    zip_path.write_text("placeholder", encoding="utf-8")
    original_zip_file = zipfile.ZipFile

    def locked_zip_file(path, *args, **kwargs):
        if path == zip_path:
            raise PermissionError("locked by scanner")
        return original_zip_file(path, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", locked_zip_file)

    with pytest.raises(SystemExit) as exc_info:
        _read_zip_names(zip_path)

    assert "Release ZIP could not be read" in str(exc_info.value)
    assert "locked by scanner" in str(exc_info.value)


def test_release_zip_verifier_reports_duplicate_zip_entries(tmp_path):
    zip_path = tmp_path / "release.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("ArubaMMCleanupGUI.exe", "first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("ArubaMMCleanupGUI.exe", "second")

    with pytest.raises(SystemExit) as exc_info:
        _read_zip_names(zip_path)

    assert "Release ZIP contains duplicate entries" in str(exc_info.value)
    assert "ArubaMMCleanupGUI.exe" in str(exc_info.value)


def test_release_zip_verifier_reports_zip_inspection_runtime_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("ArubaMMCleanupGUI.exe", "sample")

    def failing_testzip(self):
        raise RuntimeError("encrypted zip entry")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", failing_testzip)

    with pytest.raises(SystemExit) as exc_info:
        _read_zip_names(zip_path)

    assert "Release ZIP could not be inspected" in str(exc_info.value)
    assert "encrypted zip entry" in str(exc_info.value)


def test_release_zip_verifier_does_not_accept_directory_as_required_file(tmp_path):
    zip_path = tmp_path / "release.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name in RELEASE_ZIP_REQUIRED_FILES:
            archive.writestr(f"{name}/" if name == "gui/ArubaMMCleanupGUI.exe" else name, "sample")

    with pytest.raises(SystemExit) as exc_info:
        verifier_main(["--zip", str(zip_path)])

    assert "Release ZIP is missing required files" in str(exc_info.value)
    assert "ArubaMMCleanupGUI.exe" in str(exc_info.value)


def test_release_zip_verifier_reports_unsafe_zip_entry_paths(tmp_path):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path, extra_names=["../outside.txt"])

    with pytest.raises(SystemExit) as exc_info:
        verifier_main(["--zip", str(zip_path)])

    assert "Release ZIP contains unsafe paths" in str(exc_info.value)
    assert "../outside.txt" in str(exc_info.value)


def test_release_zip_verifier_rejects_cli_files(tmp_path):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path, extra_names=["ArubaMMCleanupCLI.exe"])

    with pytest.raises(SystemExit) as exc_info:
        verifier_main(["--zip", str(zip_path)])

    assert "must not include CLI files" in str(exc_info.value)
    assert "ArubaMMCleanupCLI.exe" in str(exc_info.value)


def test_release_zip_verifier_reports_sha256_mismatch(tmp_path):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    with pytest.raises(SystemExit) as exc_info:
        verifier_main(["--zip", str(zip_path), "--expected-sha256", "0" * 64])

    assert "SHA256 mismatch" in str(exc_info.value)


def test_release_zip_verifier_accepts_matching_sha256(tmp_path):
    import hashlib

    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)
    expected = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    assert verifier_main(["--zip", str(zip_path), "--expected-sha256", expected]) == 0


def test_release_zip_verifier_reports_web_smoke_timeout(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["cmd", "/c", "start_webapp.cmd", "--smoke"], 60)

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", timeout_run)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke command timed out" in str(exc_info.value)


def test_web_smoke_rejects_unsafe_zip_paths_before_extract(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path, extra_names=["../outside.txt"])

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="webapp smoke ok", stderr=""),
    )

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Release ZIP contains unsafe paths" in str(exc_info.value)
    assert "../outside.txt" in str(exc_info.value)


def test_web_smoke_rechecks_unsafe_zip_paths_during_extract(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path, extra_names=["../outside.txt"])

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr("tools.verify_release_package._read_zip_names", lambda _zip_path: {"web/start_webapp.cmd"})

    def fail_if_extractall_runs(self, _path):
        raise AssertionError("extractall should not run for unsafe paths")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", fail_if_extractall_runs)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke ZIP contains unsafe paths" in str(exc_info.value)
    assert "../outside.txt" in str(exc_info.value)


def test_release_zip_verifier_reports_web_smoke_launch_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def failing_run(*_args, **_kwargs):
        raise OSError("launch denied")

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", failing_run)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke command could not start" in str(exc_info.value)


def test_release_zip_verifier_reports_gui_smoke_launch_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def failing_run(*_args, **_kwargs):
        raise OSError("launch denied")

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", failing_run)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_gui(zip_path, require=True)

    assert "GUI smoke command could not start" in str(exc_info.value)


def test_release_zip_verifier_reports_gui_smoke_timeout(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["ArubaMMCleanupGUI.exe"], 60)

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", timeout_run)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_gui(zip_path, require=True)

    assert "GUI smoke command timed out" in str(exc_info.value)


def test_release_zip_verifier_reports_web_smoke_temp_directory_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.tempfile.TemporaryDirectory",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("temp denied")),
    )

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke temporary directory could not be created" in str(exc_info.value)
    assert "temp denied" in str(exc_info.value)


def test_release_zip_verifier_reports_gui_smoke_temp_directory_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.tempfile.TemporaryDirectory",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("temp denied")),
    )

    with pytest.raises(SystemExit) as exc_info:
        _smoke_gui(zip_path, require=True)

    assert "GUI smoke temporary directory could not be created" in str(exc_info.value)
    assert "temp denied" in str(exc_info.value)


def test_release_zip_verifier_reports_temp_directory_runtime_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.tempfile.TemporaryDirectory",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temp runtime failure")),
    )

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke temporary directory could not be created" in str(exc_info.value)
    assert "temp runtime failure" in str(exc_info.value)


def test_release_zip_verifier_ignores_web_smoke_temp_cleanup_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    smoke_dir = tmp_path / "smoke"
    smoke_dir.mkdir()

    class CleanupFailingTempDir:
        name = str(smoke_dir)

        def cleanup(self):
            raise PermissionError("cleanup denied")

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.tempfile.TemporaryDirectory",
        lambda *args, **kwargs: CleanupFailingTempDir(),
    )
    monkeypatch.setattr(
        "tools.verify_release_package.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="webapp smoke ok", stderr=""),
    )

    _smoke_web(zip_path, require=True)


def test_release_zip_verifier_ignores_temp_cleanup_runtime_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    smoke_dir = tmp_path / "smoke"
    smoke_dir.mkdir()

    class CleanupRuntimeFailingTempDir:
        name = str(smoke_dir)

        def cleanup(self):
            raise RuntimeError("cleanup runtime failure")

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.tempfile.TemporaryDirectory",
        lambda *args, **kwargs: CleanupRuntimeFailingTempDir(),
    )
    monkeypatch.setattr(
        "tools.verify_release_package.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="webapp smoke ok", stderr=""),
    )

    _smoke_web(zip_path, require=True)


def test_release_zip_verifier_runs_gui_smoke_with_smoke_environment(tmp_path, monkeypatch):
    release_dir = tmp_path / "release package"
    release_dir.mkdir()
    zip_path = release_dir / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", fake_run)

    _smoke_gui(zip_path, require=True)

    assert Path(captured["args"][0]).name == "ArubaMMCleanupGUI.exe"
    assert Path(captured["args"][0]).parent.name == "gui"
    assert captured["env"]["ARUBA_MM_CLEANUP_GUI_SMOKE"] == "1"


def test_release_zip_verifier_runs_web_smoke_script(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    captured = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="webapp smoke ok", stderr="")

    monkeypatch.setattr("tools.verify_release_package.subprocess.run", fake_run)

    _smoke_web(zip_path, require=True)

    assert captured["args"][:2] == ["cmd", "/c"]
    assert Path(captured["args"][2]).name == "start_webapp.cmd"
    assert captured["args"][3] == "--smoke"


def test_release_zip_verifier_reports_web_smoke_missing_marker(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "tools.verify_release_package.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout="different", stderr=""),
    )

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke output did not include expected marker" in str(exc_info.value)


def test_release_zip_verifier_reports_web_smoke_extract_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def failing_extractall(self, _path):
        raise OSError("extract denied")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", failing_extractall)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_web(zip_path, require=True)

    assert "Web app smoke ZIP extraction failed" in str(exc_info.value)


def test_release_zip_verifier_reports_gui_smoke_extract_failure(tmp_path, monkeypatch):
    zip_path = tmp_path / "release.zip"
    write_release_zip(zip_path)

    monkeypatch.setattr("tools.verify_release_package.platform.system", lambda: "Windows")

    def failing_extractall(self, _path):
        raise RuntimeError("encrypted zip")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", failing_extractall)

    with pytest.raises(SystemExit) as exc_info:
        _smoke_gui(zip_path, require=True)

    assert "GUI smoke ZIP extraction failed" in str(exc_info.value)


def test_cli_help_distinguishes_timeout_from_delete_delay():
    completed = subprocess.run(
        [sys.executable, "-m", "aruba_mm_cleanup.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "--timeout" in output
    assert "device response timeout seconds" in output
    assert "--delay" in output
    assert "countdown seconds between query and delete" in output
    assert "--password" not in output
    assert "--enable-password" not in output


def test_cli_rejects_out_of_range_port_before_connecting():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aruba_mm_cleanup.cli",
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--port",
            "0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "--port must be between 1 and 65535" in output


def test_cli_rejects_non_positive_timeout_before_connecting():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aruba_mm_cleanup.cli",
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--timeout",
            "0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "--timeout must be at least 1" in output


def test_cli_uses_actual_one_second_timeout(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, _config, settings, **_kwargs):
            captured["timeout"] = settings.timeout
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--timeout",
            "1",
        ]
    )

    assert result == 0
    assert captured["timeout"] == 1


def test_cli_rejects_negative_delay_before_connecting():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aruba_mm_cleanup.cli",
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--delay",
            "-1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "--delay must be at least 0" in output


def test_cli_uses_actual_zero_delete_delay(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, _config, settings, **_kwargs):
            captured["delete_delay_seconds"] = settings.delete_delay_seconds
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--delay",
            "0",
        ]
    )

    assert result == 0
    assert captured["delete_delay_seconds"] == 0


def test_cli_rejects_empty_host_before_connecting(monkeypatch, capsys):
    def fail_runner():
        raise AssertionError("runner should not be created for empty host")

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", fail_runner)

    try:
        cli_main(
            [
                "--host",
                " ",
                "--username",
                "admin",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("CLI should reject empty host")

    assert "MM 주소" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("option", "value", "expected_error"),
    [
        ("--host", "192.0.2.10", "MM 주소"),
        ("--username", "admin", "계정"),
        ("--role", "profiling", "Role"),
    ],
)
def test_cli_rejects_unstrippable_text_args_before_connecting(
    option,
    value,
    expected_error,
    monkeypatch,
    capsys,
):
    class BadStripArg(str):
        def strip(self, *_args, **_kwargs):
            raise RuntimeError("bad strip")

    def fail_runner():
        raise AssertionError("runner should not be created for invalid text args")

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", fail_runner)
    args = [
        "--host",
        "192.0.2.10",
        "--username",
        "admin",
        "--role",
        "profiling",
    ]
    args[args.index(option) + 1] = BadStripArg(value)

    try:
        cli_main(args)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("CLI should reject unstrippable text args")

    assert expected_error in capsys.readouterr().err


def test_cli_rejects_empty_username_before_connecting(monkeypatch, capsys):
    def fail_runner():
        raise AssertionError("runner should not be created for empty username")

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", fail_runner)

    try:
        cli_main(
            [
                "--host",
                "192.0.2.10",
                "--username",
                " ",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("CLI should reject empty username")

    assert "계정" in capsys.readouterr().err


def test_cli_rejects_role_control_characters_before_connecting():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aruba_mm_cleanup.cli",
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--role",
            "profiling\nshow version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "Role" in output


def test_cli_treats_missing_confirmation_input_as_cancel(monkeypatch, capsys):
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    class FakeRunner:
        def run_once(self, *_args, **kwargs):
            approved = kwargs["approve_targets"](
                SimpleNamespace(role="profiling", target_macs=("02:00:00:00:00:01",))
            )
            return SimpleNamespace(
                queried_count=1,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=1,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
                canceled=not approved,
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    assert result == 1
    assert "Canceled before deletion approval." in capsys.readouterr().out


def test_cli_treats_missing_password_input_as_cancel(monkeypatch, capsys):
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("aruba_mm_cleanup.cli.getpass.getpass", raise_eof)

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    assert result == 1
    assert "Canceled before password input." in capsys.readouterr().out


def test_cli_reports_history_save_warning(monkeypatch, capsys, tmp_path):
    class FakeRunner:
        def run_once(self, *_args, **_kwargs):
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=tmp_path / "cleanup_summary.json",
                audit_error="",
                history_error="history write failed",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "History warning: history write failed" in output


def test_cli_handles_malformed_summary_without_attribute_error(monkeypatch, capsys):
    class MalformedSummary:
        queried_count = 0
        remaining_count = 0
        audit_path = None

        @property
        def delete_success_count(self):
            raise RuntimeError("bad delete success count")

        @property
        def delete_failure_count(self):
            raise RuntimeError("bad delete failure count")

        @property
        def reappeared_count(self):
            raise RuntimeError("bad reappeared count")

        @property
        def audit_error(self):
            raise RuntimeError("bad audit error")

        @property
        def history_error(self):
            raise RuntimeError("bad history error")

        @property
        def error(self):
            raise RuntimeError("bad summary error")

    class FakeRunner:
        def run_once(self, *_args, **_kwargs):
            return MalformedSummary()

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "Queried: 0" in output
    assert "Deleted: 0" in output
    assert "Failed: 0" in output
    assert "Remaining: 0" in output
    assert "Reappeared: 0" in output


def test_cli_reports_unexpected_runner_failure(monkeypatch, capsys):
    class FakeRunner:
        def run_once(self, *_args, **_kwargs):
            raise RuntimeError("runner exploded")

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "Run error: runner exploded" in output


def test_cli_handles_unprintable_summary_values(monkeypatch, capsys):
    class BadText:
        def __str__(self):
            raise RuntimeError("bad text")

        def __repr__(self):
            raise RuntimeError("bad repr")

    class FakeRunner:
        def run_once(self, *_args, **_kwargs):
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=BadText(),
                audit_error=BadText(),
                history_error=BadText(),
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Audit:" in output


def test_cli_handles_unreadable_summary_truthiness(monkeypatch, capsys):
    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

        def __str__(self):
            return "bad-bool"

    class FakeRunner:
        def run_once(self, *_args, **_kwargs):
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=BadBool(),
                remaining_count=0,
                reappeared_count=BadBool(),
                audit_path=None,
                audit_error=BadBool(),
                history_error=BadBool(),
                error=BadBool(),
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "Failed: bad-bool" in output


def test_cli_handles_malformed_progress_payload(monkeypatch, capsys):
    class BadPayload:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("bad payload")

    class BadText:
        def __str__(self):
            raise RuntimeError("bad progress text")

        def __repr__(self):
            raise RuntimeError("bad progress repr")

    class FakeRunner:
        def run_once(self, *_args, **kwargs):
            progress_callback = kwargs["progress_callback"]
            progress_callback("countdown", BadPayload())
            progress_callback("query_done", BadText())
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Delete countdown:" in output
    assert "query_done:" in output
    assert "Queried: 0" in output


def test_cli_expands_user_home_output_dir(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, *_args, **kwargs):
            captured["output_dir"] = kwargs["output_dir"]
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--output-dir",
            "~/aruba-cli-output",
        ]
    )

    assert result == 0
    assert captured["output_dir"] == Path.home() / "aruba-cli-output"


def test_cli_strips_host_before_connecting(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, config, *_args, **_kwargs):
            captured["host"] = config.host
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            " 192.0.2.10 ",
            "--username",
            "admin",
        ]
    )

    assert result == 0
    assert captured["host"] == "192.0.2.10"


def test_cli_strips_username_before_connecting(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, config, *_args, **_kwargs):
            captured["username"] = config.username
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            " admin ",
        ]
    )

    assert result == 0
    assert captured["username"] == "admin"


def test_cli_strips_role_before_running(monkeypatch):
    captured = {}

    class FakeRunner:
        def run_once(self, _config, settings, **_kwargs):
            captured["role"] = settings.role
            return SimpleNamespace(
                queried_count=0,
                delete_success_count=0,
                delete_failure_count=0,
                remaining_count=0,
                reappeared_count=0,
                audit_path=None,
                audit_error="",
                history_error="",
                error="",
            )

    monkeypatch.setattr("aruba_mm_cleanup.cli.MmCleanupRunner", lambda **_kwargs: FakeRunner())

    result = cli_main(
        [
            "--host",
            "192.0.2.10",
            "--username",
            "admin",
            "--role",
            " profiling ",
        ]
    )

    assert result == 0
    assert captured["role"] == "profiling"


def test_windows_build_and_docs_reference_gui_web_release_contract():
    repo_root = Path(__file__).parents[1]
    build_script = (repo_root / "build_windows_gui_exe.ps1").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    release_notes = (repo_root / "RELEASE_NOTES.md").read_text(encoding="utf-8")

    for text in (build_script, readme, release_notes):
        assert "ArubaMMCleanupGUI" in text
        assert "ArubaMMCleanupWeb" in text
        assert "ArubaMMCleanupCLI.exe" not in text
    assert "aruba-mm-session-cleanup_v0.2.0_windows.zip" in readme
    assert "aruba-mm-session-cleanup_v0.2.0_windows.zip.sha256" in readme
    assert "aruba-mm-session-cleanup_v0.2.0_sbom.cdx.json" in readme
    assert "python .\\tools\\verify_release_package.py --dist .\\dist --smoke-gui --smoke-web" in readme
    assert "README_START_HERE_KO.txt" in readme
    assert "web\\start_webapp.cmd" in readme
    assert "Source code (zip)" in readme
    assert "--require-hashes" in build_script
    assert '-c ".\\constraints.txt"' in build_script
    assert "-m pip check" in build_script
    assert "pip install failed with exit code $LASTEXITCODE" in build_script
    assert "pip check failed with exit code $LASTEXITCODE" in build_script
    assert "version lookup failed with exit code $LASTEXITCODE" in build_script


def test_github_actions_release_contract():
    repo_root = Path(__file__).parents[1]
    pr_workflow = (repo_root / ".github" / "workflows" / "pr-validation.yml").read_text(encoding="utf-8")
    release_workflow = (repo_root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "pull_request:" in pr_workflow
    assert "branches: [main]" in pr_workflow
    assert "gh release" not in pr_workflow
    assert "push:" in release_workflow
    assert "workflow_dispatch:" in release_workflow
    assert 'ref: ${{ inputs.tag || github.ref }}' in release_workflow
    assert '"v[0-9]*.[0-9]*.[0-9]*"' in release_workflow
    assert '"!v*-*"' in release_workflow
    assert "-notmatch '^v\\d+\\.\\d+\\.\\d+$'" in release_workflow
    assert 'if ($tag -ne "v$version")' in release_workflow
    assert "must be an existing annotated tag" in release_workflow
    assert "does not match tag" in release_workflow
    assert "already exists; refusing to overwrite it" in release_workflow
    assert 'aruba-mm-session-cleanup_${tag}_windows.zip' in release_workflow
    assert 'aruba-mm-session-cleanup_${tag}_sbom.cdx.json' in release_workflow
    assert "Get-FileHash -Algorithm SHA256" in release_workflow
    assert ".sha256" in release_workflow
    assert "cyclonedx-py environment" in release_workflow
    assert "(Get-Command python -ErrorAction Stop).Source" in release_workflow
    assert 'throw "CycloneDX SBOM generation failed."' in release_workflow
    assert 'throw "CycloneDX SBOM file is missing or empty."' in release_workflow
    assert 'throw "CycloneDX SBOM JSON validation failed."' in release_workflow
    assert "pip_audit" in release_workflow
    assert "bandit" in release_workflow
    assert "gh release create" in release_workflow
    assert release_workflow.count("#${{ steps.metadata.outputs.asset_name }}") == 1
    assert release_workflow.count("#${{ steps.metadata.outputs.checksum_name }}") == 1
    assert release_workflow.count("#${{ steps.metadata.outputs.sbom_name }}") == 1
    assert '--title "Aruba MM Session Cleanup ${{ steps.metadata.outputs.tag }}"' in release_workflow
    assert "--draft=false" in release_workflow
    assert '--notes-file ".\\RELEASE_NOTES.md"' in release_workflow
    assert "--cleanup-tag" not in release_workflow
    assert "--smoke-gui --smoke-web --require-gui-smoke --require-web-smoke" in release_workflow
    assert "--smoke-cli" not in release_workflow
    assert "ArubaMMCleanupCLI.exe" not in release_workflow
    assert "세부 커밋 및 변경 파일" not in release_workflow
    assert "### 원본 커밋 목록" not in release_workflow
    assert "### 변경 파일" not in release_workflow


def test_package_metadata_versions_and_dependencies_do_not_drift():
    repo_root = Path(__file__).parents[1]
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    init_py = (repo_root / "src" / "aruba_mm_cleanup" / "__init__.py").read_text(encoding="utf-8")
    setup_cfg = configparser.ConfigParser()
    setup_cfg.read(repo_root / "setup.cfg", encoding="utf-8")
    constraints = (repo_root / "constraints.txt").read_text(encoding="utf-8")

    pyproject_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1)
    init_version = re.search(r'^__version__ = "([^"]+)"$', init_py, re.MULTILINE).group(1)

    assert pyproject_version == setup_cfg["metadata"]["version"] == init_version
    assert '"netmiko>=4.3.0"' in pyproject
    assert "netmiko>=4.3.0" in setup_cfg["options"]["install_requires"]
    assert '"pyinstaller>=6.0"' in pyproject
    assert "pyinstaller>=6.0" in setup_cfg["options.extras_require"]["dev"]
    assert "aruba-mm-cleanup-web" in pyproject
    assert "aruba-mm-cleanup-web" in setup_cfg["options.entry_points"]["console_scripts"]
    assert "netmiko==4.6.0" in constraints
    assert "pyinstaller==6.21.0" in constraints
    assert "pytest==8.4.2" in constraints
