"""Startup storage barriers use isolated homes; no real user data or network."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from coinfly.cli import main
from coinfly.paths import ApplicationPaths, StorageInitializationError


def test_expanded_home_creates_complete_idempotent_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    paths = ApplicationPaths.for_home(Path("~"))
    assert paths.root == Path("~").expanduser().resolve() / ".devsolux" / "coinfly"
    assert paths.root.is_absolute()
    paths.prepare()
    marker = paths.snapshots / "preserved.txt"
    marker.write_text("existing data")
    paths.prepare()
    assert marker.read_text() == "existing data"
    assert all(path.is_dir() for path in paths.directories)
    assert all((paths.root / name).is_dir() for name in ("datasets", "cache", "logs", "ledger", "snapshots"))
    assert not list(tmp_path.rglob(".coinfly-probe-*"))


@pytest.mark.parametrize("entry", ["desktop", "development", "worker"])
def test_permission_failure_prevents_threads_and_reports_doctor_schema(tmp_path, monkeypatch, capsys, entry):
    from coinfly import development
    from coinfly.worker import supervisor

    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    original = Path.mkdir

    def denied(path, *args, **kwargs):
        if path.name == "logs":
            raise PermissionError("PRIVATE_CANARY account path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    desktop, startup = Mock(), Mock()
    monkeypatch.setattr("coinfly.cli._launch_desktop", desktop)
    monkeypatch.setattr(development, "run_desktop", desktop)
    monkeypatch.setattr("coinfly.worker.startup.WorkerStartup", startup)
    monkeypatch.setattr("sys.argv", ["coinfly-development", "desktop"])
    result = (main(["desktop"]) if entry == "desktop" else
              development.main() if entry == "development" else supervisor._run_native_worker())
    assert result == 2
    desktop.assert_not_called()
    startup.assert_not_called()
    output = capsys.readouterr()
    assert "PRIVATE_CANARY" not in output.err
    report = json.loads(output.err)
    assert report["status"] == "setup_required"
    assert report["environment_blocking_codes"] == ["STORAGE_PERMISSION_DENIED"]
    assert "permissions" in report["checks"][0]["detail"]


def test_storage_is_ready_before_desktop_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    paths = ApplicationPaths.for_home(tmp_path)

    def launch():
        assert all(path.is_dir() for path in paths.directories)
        return 0

    monkeypatch.setattr("coinfly.cli._launch_desktop", launch)
    assert main(["desktop"]) == 0


def test_existing_unwritable_directory_is_probed(tmp_path, monkeypatch):
    paths = ApplicationPaths.for_home(tmp_path)
    paths.prepare()
    probe = Mock(side_effect=PermissionError("synthetic ACL denial"))
    monkeypatch.setattr("coinfly.paths.tempfile.TemporaryFile", probe)
    with pytest.raises(StorageInitializationError, match="STORAGE_PERMISSION_DENIED"):
        paths.prepare()


def test_file_collision_is_actionable_and_does_not_overwrite(tmp_path):
    paths = ApplicationPaths.for_home(tmp_path)
    paths.prepare()
    paths.logs.rmdir()
    paths.logs.write_text("preserve me")
    with pytest.raises(StorageInitializationError, match="STORAGE_PATH_CONFLICT"):
        paths.prepare()
    assert paths.logs.read_text() == "preserve me"


def test_doctor_inspects_storage_without_creating_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    assert main(["doctor"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert "STORAGE_DIRECTORY_MISSING" in report["blocking_codes"]
    assert not (tmp_path / ".devsolux").exists()


def test_doctor_reports_inaccessible_storage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    ApplicationPaths.for_home(tmp_path).prepare()
    monkeypatch.setattr("coinfly.paths.os.access", lambda *_: False)
    assert main(["doctor"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert "STORAGE_PERMISSION_DENIED" in report["blocking_codes"]


@pytest.mark.parametrize("entry", ["main(['desktop'])", "worker_main()"])
def test_storage_failure_is_reported_before_ui_or_database_imports(entry):
    # A fresh interpreter catches eager import regressions hidden by pytest's
    # already-imported modules. This launches neither GUI nor worker processes.
    program = f"""
import sys
from coinfly import cli
from coinfly.paths import StorageInitializationError
assert 'coinfly.ui.app' not in sys.modules
assert 'coinfly.storage.writer' not in sys.modules
def denied():
    raise StorageInitializationError('STORAGE_PERMISSION_DENIED')
cli.initialize_application_storage = denied
assert cli.{entry} == 2
assert 'coinfly.ui.app' not in sys.modules
assert 'coinfly.storage.writer' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", program], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["blocking_codes"] == ["STORAGE_PERMISSION_DENIED"]
