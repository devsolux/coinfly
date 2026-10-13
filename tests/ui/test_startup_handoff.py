import threading

import pytest

from coinfly.cli import _launch_native_desktop, _worker_command
from coinfly.domain.context import DomainError
from coinfly.ui.ipc_client import DesktopClient
from coinfly.ui.render_scheduler import RenderQueue


def test_packaged_desktop_launches_sibling_worker(tmp_path, monkeypatch):
    executable = tmp_path / "coinfly.app/Contents/MacOS/coinfly"
    executable.parent.mkdir(parents=True)
    worker = executable.with_name("coinfly-worker")
    monkeypatch.setattr("coinfly.cli.sys.executable", str(executable))
    monkeypatch.setattr("coinfly.cli.sys.frozen", True, raising=False)
    with pytest.raises(DomainError, match="PACKAGED_WORKER_MISSING"):
        _worker_command()
    worker.touch()
    assert _worker_command() == [str(worker)]


def test_source_desktop_launches_isolated_python_worker(monkeypatch):
    monkeypatch.setattr("coinfly.cli.sys.frozen", False, raising=False)
    monkeypatch.setattr("coinfly.cli.sys.executable", "/fixture/python")
    assert _worker_command() == ["/fixture/python", "-I", "-m", "coinfly.worker"]


def test_launcher_opens_desktop_before_any_worker_probe(monkeypatch):
    monkeypatch.setattr("coinfly.ui.app.load_toolkit", lambda: None)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Worker startup ran before the window")
    monkeypatch.setattr("coinfly.ipc.transport_unix.UnixRPCClient", forbidden)
    monkeypatch.setattr("coinfly.cli.subprocess.Popen", forbidden)
    def run_app(*, socket_path, worker_launcher, credential_store, connection_verifier):
        assert socket_path and callable(worker_launcher)
        assert credential_store is not None and connection_verifier is not None
        assert callable(connection_verifier.verify_connection)
        return 0
    monkeypatch.setattr("coinfly.ui.app.run_app", run_app)
    assert _launch_native_desktop() == 0


def test_slow_worker_launch_never_waits_on_ui_thread():
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    def launch():
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(2)
    client = DesktopClient(RenderQueue(), worker_launcher=launch)
    try:
        client.start()
        assert entered.wait(1)
        client.close()
        assert not release.is_set()
    finally:
        client.close()
        release.set()
        for thread in client._threads:
            thread.join(2)
            assert not thread.is_alive()
