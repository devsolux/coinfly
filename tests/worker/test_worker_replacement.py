"""Replacing a development worker that runs older sources.

The development worker keeps running after the desktop closes (it owns the
paper book). After the checkout changed, the next desktop attached to that
worker and every command was refused with WORKER_BUILD_MISMATCH; ``dev.sh
worker`` could not bind the endpoint (IPC_ENDPOINT_ACTIVE); a desktop-driven
restart raced the predecessor's authority lock and stayed BLOCKED on
AUTHORITY_ALREADY_OWNED, and the blocked startup refused ``worker.hello`` so
the desktop could not even see why. These tests pin the replacement path.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from coinfly.config.models import default_config
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.local import LocalRPCClient, LocalRPCServer
from coinfly.ipc.protocol import PROTOCOL_VERSION, RPCRequest
from coinfly.observability import diagnostics
from coinfly.observability.diagnostics import build_identity
from coinfly.worker import startup as startup_module
from coinfly.worker.ownership import AuthorityOwnership
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.startup import WorkerStartup
from coinfly.worker.supervisor import WorkerSupervisor

from .test_automation_lifecycle import TestOnlyBootstrap


def request(method: str, params: dict[str, Any] | None = None) -> RPCRequest:
    return RPCRequest.parse({"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}, "epoch": 0,
                             "deadline_ms": int(time.time() * 1000) + 10000})


def hello_params(fingerprint: str | None = None) -> dict[str, Any]:
    identity = dict(build_identity())
    if fingerprint is not None:
        identity["source_fingerprint"] = fingerprint
    return identity


class ReplacementFixture(TestCase):
    def setUp(self) -> None:
        # Short, private path: Unix socket paths are limited to ~104 bytes on macOS.
        self.root = Path(tempfile.mkdtemp(prefix="cf-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.socket = self.root / "ipc" / "worker.sock"
        self.socket.parent.mkdir(mode=0o700)
        self.paths = WorkerPaths(self.root / "worker")

    def factory(self, **kwargs: Any) -> WorkerSupervisor:
        return WorkerSupervisor(paths=self.paths, socket_path=self.socket, bootstrap=TestOnlyBootstrap(self.root), **kwargs)

    def wait_until(self, predicate: Any, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition not reached in time")
            time.sleep(0.05)


class InitializingStartupTests(ReplacementFixture):
    def test_hello_and_build_identity_are_served_while_initializing(self) -> None:
        release = threading.Event()

        def slow_factory(**kwargs: Any) -> WorkerSupervisor:
            release.wait(10)
            return self.factory(**kwargs)

        startup = WorkerStartup(socket_path=self.socket, factory=slow_factory, timeout=30).start()
        self.addCleanup(startup.close)
        self.addCleanup(release.set)
        peer = PeerIdentity(uid=os.getuid(), gid=os.getgid(), pid=os.getpid())
        hello = startup.handle(request("worker.hello", hello_params()), peer)
        self.assertEqual((hello["protocol_version"], hello["build_match"], hello["initializing"]), (PROTOCOL_VERSION, True, True))
        stale = startup.handle(request("worker.hello", hello_params("0" * 64)), peer)
        self.assertFalse(stale["build_match"])
        snapshot = startup.handle(request("state.snapshot"), peer)
        self.assertEqual(snapshot["build"]["source_fingerprint"], build_identity()["source_fingerprint"])
        self.assertEqual(snapshot["runtime"]["pid"], os.getpid())
        # A desktop connection (which always performs the handshake) now gets the diagnostic snapshot.
        with LocalRPCClient(self.socket, timeout=5, identity=dict(build_identity())) as client:
            self.assertTrue(client.handshake["initializing"])  # type: ignore[index]
            self.assertTrue(client.call("state.snapshot")["worker_initializing"])
        release.set()
        self.wait_until(lambda: startup._worker is not None)

    def test_successor_waits_for_the_predecessor_authority(self) -> None:
        self.paths.prepare()
        predecessor = AuthorityOwnership(default_config().authority_id, lock_directory=self.paths.locks)
        predecessor.acquire()
        self.addCleanup(predecessor.close)
        with patch.object(startup_module, "AUTHORITY_HANDOVER_INTERVAL", 0.2):
            startup = WorkerStartup(socket_path=self.socket, factory=self.factory, timeout=10).start()
            self.addCleanup(startup.close)
            self.wait_until(lambda: startup.snapshot()["bootstrap"]["reason"] == "AUTHORITY_HANDOVER_PENDING")
            self.assertEqual(startup.snapshot()["bootstrap"]["stage"], "RETRYING")
            time.sleep(0.5)
            self.assertIsNone(startup._worker)  # still waiting, never BLOCKED
            predecessor.close()  # the old process finished closing its ledger
            self.wait_until(lambda: startup._worker is not None)


class StaleWorkerRetirementTests(ReplacementFixture):
    def run_worker_process(self) -> tuple[WorkerStartup, threading.Thread]:
        """What ``coinfly.development.run_worker`` does, on a thread instead of a process."""
        startup = WorkerStartup(socket_path=self.socket, factory=self.factory, timeout=10).start()

        def serve() -> None:
            while not startup.shutdown_requested.is_set():
                time.sleep(0.05)
            startup.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.wait_until(lambda: startup._worker is not None)
        return startup, thread

    def test_worker_from_older_sources_is_retired_current_one_is_kept(self) -> None:
        from coinfly.development import probe_worker, retire_worker

        startup, thread = self.run_worker_process()
        self.addCleanup(startup.close)
        self.assertTrue(probe_worker(self.socket)["build_match"])  # type: ignore[index]
        self.assertEqual(retire_worker(self.socket, timeout=5), "current")
        self.assertFalse(startup.shutdown_requested.is_set())
        # The checkout changed after the worker started: the launcher's identity differs.
        changed = dict(build_identity(), source_fingerprint="f" * 64)
        with patch.object(diagnostics, "build_identity", return_value=changed):
            self.assertFalse(probe_worker(self.socket)["build_match"])  # type: ignore[index]
            self.assertEqual(retire_worker(self.socket, timeout=10), "retired")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.socket.exists())
        from coinfly.development import retire_worker as again

        self.assertEqual(again(self.socket, timeout=1), "absent")

    def test_enabled_automation_retires_gracefully_once_flat(self) -> None:
        """An enabled (here: FAILED) automation makes retirement graceful; with nothing held it completes."""
        stopped = threading.Event()
        worker = self.factory(shutdown_request=stopped.set)
        self.addCleanup(worker.close)
        worker.automation.loop_enabled = False
        enabled = {"enabled": True, "state": "FAILED"}
        with patch.object(worker.automation, "status", return_value=enabled), \
                patch.object(worker.automation, "stop_and_reconcile", return_value={"state": "STOPPED"}) as stop:
            reply = worker._request_shutdown()
            self.assertEqual(reply["mode"], "GRACEFUL")
            stop.assert_called_once()
            self.assertTrue(stopped.wait(5))
        self.assertIn("completed_at", worker._retirement or {})


class EndpointCleanupTests(ReplacementFixture):
    def test_closing_server_never_removes_a_successor_endpoint(self) -> None:
        first = LocalRPCServer(self.socket, lambda request, peer: {"server": "first"}, cleanup_stale=True)
        first.start()
        # The old process stopped listening but has not removed its endpoint yet.
        first._stopped.set()
        first._listener.close()
        assert first._thread is not None
        first._thread.join(2)
        second = LocalRPCServer(self.socket, lambda request, peer: {"server": "second"}, cleanup_stale=True)
        second.start()  # replaces the refused endpoint
        self.addCleanup(second.close)
        item = self.socket.lstat()
        first._inode = (item.st_dev, item.st_ino)  # a filesystem may reuse the inode number
        first.close()
        self.assertTrue(self.socket.exists())
        with LocalRPCClient(self.socket, timeout=2) as client:
            self.assertEqual(client.call("state.snapshot"), {"server": "second"})
