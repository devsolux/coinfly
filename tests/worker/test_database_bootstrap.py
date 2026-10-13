"""Real SQLite contention and a responsive diagnostic boundary; no operator data."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from coinfly.bootstrap.manager import BootstrapManager
from coinfly.domain.context import DomainError
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCRequest
from coinfly.ipc.transport_unix import UnixRPCClient
from coinfly.storage.writer import Writer
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.startup import WorkerStartup
from coinfly.worker.supervisor import WorkerSupervisor


def test_sqlite_lock_retry_recovers_without_an_overlapping_writer(tmp_path, caplog):
    path = tmp_path / "ledger.sqlite"
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    retry = threading.Event()
    outcome = []

    def initialize():
        try:
            outcome.append(Writer(path, startup_timeout=2, startup_progress=lambda _: retry.set()))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=initialize)
    thread.start()
    try:
        assert retry.wait(1)
        assert thread.is_alive()
        with pytest.raises(DomainError, match="WRITER_PATH_ALREADY_OWNED"):
            Writer(path)
    finally:
        blocker.rollback()
        blocker.close()
        thread.join(3)
    assert not thread.is_alive()
    assert isinstance(outcome[0], Writer)
    with outcome[0] as writer:
        assert writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]) == 0
    assert "Database locked by another process - Retrying..." in caplog.text


def test_sqlite_lock_has_deadline_and_releases_path(tmp_path):
    path = tmp_path / "ledger.sqlite"
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(DomainError, match="DATABASE_INITIALIZATION_TIMEOUT"):
            Writer(path, startup_timeout=0.25)
        assert time.monotonic() - started < 1
    finally:
        blocker.rollback()
        blocker.close()
    # An interrupted native wait must clean up before ownership can be reused.
    deadline = time.monotonic() + 2
    while True:
        try:
            writer = Writer(path)
            break
        except DomainError as error:
            assert error.code == "WRITER_PATH_ALREADY_OWNED" and time.monotonic() < deadline
            threading.Event().wait(0.01)
    writer.close()


def test_interrupted_constructor_wait_releases_a_healthy_writer_thread(tmp_path, monkeypatch):
    """An interrupt during the readiness wait must not join a parked writer forever."""
    from concurrent.futures import Future

    path = tmp_path / "ledger.sqlite"
    original = Future.result

    def interrupted(self, timeout=None):
        original(self, timeout)  # startup really completed on the writer thread
        raise KeyboardInterrupt

    monkeypatch.setattr(Future, "result", interrupted)
    outcome = []

    def construct():
        try:
            Writer(path, startup_timeout=2)
        except BaseException as error:  # noqa: BLE001 - the interrupt is the expected outcome
            outcome.append(error)

    thread = threading.Thread(target=construct)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "constructor joined a parked writer thread"
    assert isinstance(outcome[0], KeyboardInterrupt)
    monkeypatch.setattr(Future, "result", original)
    assert not any(t.name == "coinfly-sqlite-writer" for t in threading.enumerate())
    with Writer(path) as writer:  # ownership was released, the path is reusable
        assert writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]) == 0


def test_startup_timeout_keeps_diagnostics_responsive_and_discards_late_worker(tmp_path):
    paths = WorkerPaths(tmp_path / "worker")
    paths.prepare()
    blocker = sqlite3.connect(paths.database)
    blocker.execute("BEGIN EXCLUSIVE")
    workers = []

    budgets = []

    def factory(**kwargs):
        # The startup budget is forwarded to the constructor. Simulate a slower
        # constructor stage (as a long recovery would) so the late result path
        # is exercised: the watchdog fires first, the worker completes later.
        budgets.append(kwargs.pop("startup_timeout"))
        worker = WorkerSupervisor(paths=paths, bootstrap=BootstrapManager(home=tmp_path), startup_timeout=5, **kwargs)
        workers.append(worker)
        return worker

    startup = WorkerStartup(socket_path=Path("/tmp/coinfly-test-diagnostic.sock"), timeout=0.4, factory=factory)
    request = RPCRequest.parse({"jsonrpc": "2.0", "id": "snapshot", "method": "state.snapshot", "params": {},
                                "epoch": 0, "deadline_ms": int(time.time() * 1000) + 5000})
    peer = PeerIdentity(0, 0, None)
    try:
        startup.retry()  # Exercise the handler without binding an operator socket.
        for _ in range(10):
            start = time.monotonic()
            snapshot = startup.handle(request, peer)
            assert not snapshot["bootstrap"]["ready"]
            assert time.monotonic() - start < 0.1
            threading.Event().wait(0.05)
        assert snapshot["bootstrap"]["reason"] == "DATABASE_INITIALIZATION_TIMEOUT"
        assert budgets == [0.4]
        with pytest.raises(DomainError, match="BOOTSTRAP_BUSY"):
            startup.retry()
        blocker.rollback()
        startup._thread.join(3)
        assert not startup._thread.is_alive()
        assert startup._worker is None
        assert len(workers) == 1 and workers[0]._closed
        assert not workers[0]._ownership.held
        assert not workers[0].writer._thread.is_alive()
        assert startup.snapshot()["bootstrap"]["reason"] == "DATABASE_INITIALIZATION_TIMEOUT"
    finally:
        blocker.close()
        startup.close()


def test_diagnostic_ipc_remains_available_until_database_lock_is_released(tmp_path):
    # Short AF_UNIX path, with the same private directory checks as production.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="cf-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        paths = WorkerPaths(root / "worker")
        paths.prepare()
        blocker = sqlite3.connect(paths.database)
        blocker.execute("BEGIN EXCLUSIVE")
        startup = WorkerStartup(socket_path=root / "w.sock", factory=lambda **kwargs: WorkerSupervisor(
            paths=paths, bootstrap=BootstrapManager(home=tmp_path), **kwargs))
        try:
            startup.start()
            with UnixRPCClient(root / "w.sock", timeout=1) as client:
                current = client.call("state.snapshot")
                assert current["worker_initializing"] and current["bootstrap"]["component"] == "database"
                with pytest.raises(Exception, match="DATABASE_NOT_READY"):
                    client.call("engine.arm", {"confirmation": True})
                blocker.rollback()
                startup._thread.join(3)
                assert startup._worker is not None
                current = client.call("state.snapshot")
                assert not current.get("worker_initializing")
                # Database readiness deliberately does not await package I/O
                # on the independent dataset installer thread.
                deadline = time.monotonic() + 3
                while current["bootstrap"]["stage"] == "CHECKING" and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = client.call("state.snapshot")
                assert current["bootstrap"]["reason"] == "PUBLISHER_TRUST_ROOT_MISSING"
                assert not current["bootstrap"]["ready"]
        finally:
            blocker.close()
            startup.close()
