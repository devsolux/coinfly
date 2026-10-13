"""Failed startup must not strand the authority lock or a non-daemon writer."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coinfly.bootstrap.manager import BootstrapManager
from coinfly.control.slot_service import SlotService
from coinfly.ipc.protocol import RPCError
from coinfly.storage.writer import Writer
from coinfly.worker import supervisor
from coinfly.worker.ownership import AuthorityOwnership
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.startup import WorkerStartup


class SupervisorStartupTests(unittest.TestCase):
    def assert_released(self, paths, writers, owners):
        self.assertEqual(len(writers), 1)
        self.assertFalse(writers[0]._thread.is_alive())
        self.assertEqual(len(owners), 1)
        self.assertFalse(owners[0].held)
        with AuthorityOwnership(owners[0].authority_id, lock_directory=paths.locks):
            with Writer(paths.database) as replacement:
                self.assertEqual(replacement.call(lambda connection: connection.execute(
                    "SELECT 1").fetchone()[0]), 1)

    def exercise_failed_initialization(self, failure_target, *, fail_actor_close=False):
        writers = []
        owners = []

        def create_writer(*args, **kwargs):
            writer = Writer(*args, **kwargs)
            writers.append(writer)
            return writer

        def create_owner(*args, **kwargs):
            owner = AuthorityOwnership(*args, **kwargs)
            owners.append(owner)
            return owner

        close_slot_service = SlotService.close

        def close_actor(actor):
            close_slot_service(actor)
            if fail_actor_close:
                raise RuntimeError("fixture actor cleanup failure")

        with tempfile.TemporaryDirectory(prefix="coinfly-startup-") as directory:
            root = Path(directory)
            paths = WorkerPaths(root / "worker")
            try:
                with (
                    patch.object(supervisor, "Writer", side_effect=create_writer),
                    patch.object(supervisor, "AuthorityOwnership", side_effect=create_owner),
                    patch.object(supervisor, "native_user_home", return_value=root),
                    patch("coinfly.worker.supervisor." + failure_target,
                          side_effect=RuntimeError("fixture startup failure")),
                    patch.object(SlotService, "close", autospec=True, side_effect=close_actor),
                    self.assertRaisesRegex(RuntimeError, "fixture .* failure"),
                ):
                    supervisor.WorkerSupervisor(paths=paths, socket_path=root / "worker.sock")
                self.assert_released(paths, writers, owners)
            finally:
                # A regression must fail the assertion without hanging the suite.
                for writer in writers:
                    writer.close()
                for owner in owners:
                    owner.close()

    def test_each_initialization_stage_failure_releases_acquired_resources(self):
        for failure_target in (
            "ContextInterlock", "FactReducer", "BootstrapManager", "ContextCoordinator",
            "SlotService", "HistoryService", "Reconciler.recover",
            "WorkerSupervisor._recover_context",  # renamed when live recovery joined the paper path
        ):
            with self.subTest(stage=failure_target):
                self.exercise_failed_initialization(failure_target)

    def test_actor_cleanup_error_still_releases_writer_and_authority(self):
        self.exercise_failed_initialization("HistoryService", fail_actor_close=True)

    def test_successful_construction_holds_resources_until_idempotent_close(self):
        with tempfile.TemporaryDirectory(prefix="coinfly-startup-") as directory:
            root = Path(directory)
            worker = supervisor.WorkerSupervisor(
                paths=WorkerPaths(root / "worker"), socket_path=root / "worker.sock",
                bootstrap=BootstrapManager(home=root),
            )
            try:
                self.assertTrue(worker._ownership.held)
                self.assertTrue(worker.writer._thread.is_alive())
            finally:
                worker.close()
                worker.close()
            self.assert_released(worker.paths, [worker.writer], [worker._ownership])

    @unittest.skipIf(sys.platform == "win32", "Windows native transport remains unqualified")
    def test_background_start_failure_closes_constructed_supervisor(self):
        with tempfile.TemporaryDirectory(prefix="coinfly-startup-") as directory:
            root = Path(directory)
            worker = supervisor.WorkerSupervisor(
                paths=WorkerPaths(root / "worker"), socket_path=root / "worker.sock",
                bootstrap=BootstrapManager(home=root),
            )
            startup = WorkerStartup(socket_path=root / "diagnostic.sock", factory=lambda **_: worker)
            try:
                with (
                    patch.object(worker, "start", side_effect=RPCError("IPC_ENDPOINT_EXISTS")),
                ):
                    startup.retry()
                    startup._thread.join(3)
                    self.assertFalse(startup._thread.is_alive())
                    self.assertEqual(startup.snapshot()["bootstrap"]["reason"], "IPC_ENDPOINT_EXISTS")
                self.assert_released(worker.paths, [worker.writer], [worker._ownership])
            finally:
                worker.close()
