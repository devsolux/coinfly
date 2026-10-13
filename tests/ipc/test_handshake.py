"""IPC handshake: protocol and build identity bound to each connection; graceful worker retirement.

Runs the real worker behind the real Unix-socket server (skipped where the
sandbox denies AF_UNIX).
"""

from __future__ import annotations

import errno
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import TestCase

from coinfly import __application_id__, __version__
from coinfly.ipc.auth import OwnerOnlySandboxPolicy
from coinfly.ipc.protocol import PROTOCOL_VERSION, RPCError
from coinfly.ipc.transport_unix import UnixRPCClient, UnixRPCServer
from coinfly.observability.diagnostics import build_identity, source_fingerprint
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor
from tests.worker.test_automation_lifecycle import TestOnlyBootstrap


class HandshakeTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="cf-hello-")
        self.addCleanup(self.directory.cleanup)
        os.chmod(self.directory.name, 0o700)
        root = Path(self.directory.name)
        self.path = root / "worker.sock"
        self.shutdowns: list[str] = []
        self.worker = WorkerSupervisor(paths=WorkerPaths(root / "worker"), socket_path=self.path, bootstrap=TestOnlyBootstrap(root),
                                       shutdown_request=lambda: self.shutdowns.append("stop"))
        self.addCleanup(self.worker.close)
        server = UnixRPCServer(self.path, self.worker.handle, authorization=OwnerOnlySandboxPolicy(realm_resolver=self.worker._realm_for))
        try:
            self.server = server.start()
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                self.skipTest("Host sandbox denies AF_UNIX socket creation")
            raise
        self.addCleanup(self.server.close)

    def identity(self, **overrides: Any) -> dict[str, Any]:
        return {**dict(build_identity()), **overrides}

    def test_build_identity_is_captured_at_startup_and_immutable(self) -> None:
        build = self.worker.build
        self.assertEqual((build["application_id"], build["version"], build["protocol_version"]), (__application_id__, __version__, PROTOCOL_VERSION))
        self.assertEqual(build["source_fingerprint"], source_fingerprint())
        with self.assertRaises(TypeError):
            build["version"] = "other"  # type: ignore[index]
        snapshot = self.worker.snapshot()
        self.assertEqual(snapshot["build"]["captured_at"], build["captured_at"])

    def test_matching_client_handshakes_and_may_command(self) -> None:
        with UnixRPCClient(self.path, identity=self.identity()) as client:
            client.connect()
            assert client.handshake is not None
            self.assertTrue(client.handshake["build_match"])
            self.assertEqual(client.handshake["protocol_version"], PROTOCOL_VERSION)
            self.assertIsNotNone(client.handshake["connection_id"])
            self.assertIn("state", client.call("automation.status"))
            # A command is admitted on a handshaken connection (it is judged on its own merits).
            self.assertTrue(client.call("automation.pause_entries")["entries_paused"])

    def test_missing_handshake_blocks_commands_but_not_reads(self) -> None:
        with UnixRPCClient(self.path) as client:  # no identity: a read-only tool
            self.assertIn("state", client.call("automation.status"))
            with self.assertRaisesRegex(RPCError, "HANDSHAKE_REQUIRED"):
                client.call("automation.pause_entries")

    def test_protocol_or_application_mismatch_is_refused_at_connect(self) -> None:
        with self.assertRaisesRegex(RPCError, "PROTOCOL_VERSION_MISMATCH"):
            UnixRPCClient(self.path, identity=self.identity(protocol_version=PROTOCOL_VERSION + 1)).connect()
        with self.assertRaisesRegex(RPCError, "APPLICATION_MISMATCH"):
            UnixRPCClient(self.path, identity=self.identity(application_id="other.app")).connect()

    def test_stale_build_can_read_and_retire_but_not_command(self) -> None:
        with UnixRPCClient(self.path, identity=self.identity(source_fingerprint="0" * 64)) as client:
            client.connect()
            assert client.handshake is not None
            self.assertFalse(client.handshake["build_match"])
            self.assertIn("engine_state", client.call("state.snapshot"))
            with self.assertRaisesRegex(RPCError, "WORKER_BUILD_MISMATCH"):
                client.call("automation.pause_entries")
            result = client.call("worker.shutdown")
            self.assertEqual((result["accepted"], result["mode"]), (True, "IMMEDIATE"))
        for _ in range(20):
            if self.shutdowns:
                break
            time.sleep(0.05)
        self.assertEqual(self.shutdowns, ["stop"])


class GracefulRetirementTests(TestCase):
    """A worker managing positions retires only once flat; force is explicit."""

    def test_retirement_waits_for_flat_then_stops(self) -> None:
        from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy

        class Harness(AutomationFixture):
            def runTest(self) -> None:  # pragma: no cover - driven manually
                pass

        harness = Harness()
        harness.setUp()
        try:
            worker = harness.worker
            shutdowns: list[str] = []
            worker._shutdown_request = lambda: shutdowns.append("stop")
            harness.initialize()
            harness.configure(fixture_strategy(["BUY", "HOLD"]))
            harness.enable()
            harness.tick(4)
            harness.feed.books["BTC-USD"] = ("99.80", "99.90")
            harness.tick()
            worker.RETIREMENT_TIMEOUT_SECONDS = 20.0
            result = worker._request_shutdown()
            self.assertEqual(result["mode"], "GRACEFUL")
            self.assertTrue(result["exposure"]["positions"])
            self.assertEqual(shutdowns, [])  # not stopped abruptly
            # The drain needs ticks (the loop is driven manually in tests) and a crossing bid.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not shutdowns:
                harness.feed.books["BTC-USD"] = ("100.00", "100.10")
                try:
                    harness.tick()
                except Exception:
                    pass
                time.sleep(0.1)
            self.assertEqual(shutdowns, ["stop"])
            self.assertIn("completed_at", worker._retirement or {})
            self.assertEqual(worker.automation.status()["state"], "STOPPED")
        finally:
            harness.doCleanups()

    def test_force_retires_at_once_and_reports_exposure(self) -> None:
        from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy

        class Harness(AutomationFixture):
            def runTest(self) -> None:  # pragma: no cover
                pass

        harness = Harness()
        harness.setUp()
        try:
            worker = harness.worker
            shutdowns: list[str] = []
            worker._shutdown_request = lambda: shutdowns.append("stop")
            harness.initialize()
            harness.configure(fixture_strategy(["BUY"]))
            harness.enable()
            harness.tick(4)
            result = worker._request_shutdown(force=True)
            self.assertEqual(result["mode"], "FORCED")
            self.assertEqual(result["exposure"]["open_intents"], 1)
            for _ in range(20):
                if shutdowns:
                    break
                time.sleep(0.05)
            self.assertEqual(shutdowns, ["stop"])
        finally:
            harness.doCleanups()


__all__ = ["threading"]
