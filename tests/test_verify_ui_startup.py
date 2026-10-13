"""A frozen GUI must prove readiness and exit cleanly before release succeeds."""

import json
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_ui_startup.py"
READY = {"main_window_ready": True, "event_loop_ticks": 30}


@pytest.fixture
def verify():
    return runpy.run_path(str(SCRIPT))["verify_bundle"]


@pytest.mark.parametrize(("status", "report", "message"), [
    (1, json.dumps(READY), "exited 1"),
    (0, None, "without a valid"),
    (0, "not JSON", "without a valid"),
    (0, "[]", "visible, responsive"),
    (0, '{"main_window_ready":false,"event_loop_ticks":30}', "visible, responsive"),
    (0, '{"main_window_ready":true,"event_loop_ticks":0}', "visible, responsive"),
    (0, '{"main_window_ready":true,"event_loop_ticks":true}', "visible, responsive"),
    (0, json.dumps(READY), None),
])
def test_requires_fresh_readiness_proof_and_clean_exit(verify, monkeypatch, tmp_path, status, report, message):
    app = tmp_path / "coinfly.app"
    executable = app / "Contents/MacOS/coinfly"
    executable.parent.mkdir(parents=True)
    executable.touch()
    directories = []

    def launch(command, **kwargs):
        assert command[:2] == [str(executable), "--smoke-desktop"]
        assert kwargs["timeout"] == 60
        result_path = Path(command[2])
        assert not result_path.exists()
        assert result_path.parent == Path(kwargs["cwd"])
        assert result_path.parent != app.parent
        directories.append(result_path.parent)
        if report is not None:
            result_path.write_text(report)
        return subprocess.CompletedProcess(command, status, "", "fixture crash" if status else "")

    monkeypatch.setattr(subprocess, "run", launch)
    if message:
        with pytest.raises(RuntimeError, match=message):
            verify(app)
    else:
        verify(app)
    assert directories and all(not directory.exists() for directory in directories)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
def test_watchdog_terminates_a_deadlocked_process(verify, tmp_path):
    executable = tmp_path / "stalled-desktop"
    executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'\n")
    executable.chmod(0o700)
    with pytest.raises(RuntimeError, match="process killed") as failure:
        verify(executable, timeout=0.2)
    assert isinstance(failure.value.__cause__, subprocess.TimeoutExpired)


def test_missing_executable_fails_before_launch(verify, monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Started a missing desktop executable")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="not found"):
        verify(tmp_path / "missing.app")


def test_script_returns_failure_to_pipeline(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path / "missing.app")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Packaged executable not found" in result.stderr
