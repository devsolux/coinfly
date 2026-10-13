"""Regression: the reproduced FAILED_INITIALIZATION / ENGINE_OPERATION_FAILED path.

The desktop showed a paper context that never initialized while every
underlying code was collapsed into ENGINE_OPERATION_FAILED. These tests drive
the real supervisor through preview -> apply -> operation with a dataset
sentinel that turns unusable between acceptance and engine construction, then
assert the failing stage, the preserved code, the redacted chain, retry safety
and the successful explicit retry without a new epoch or a reseeded book.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase

from coinfly.bootstrap.installer import VerifiedDataset
from coinfly.bootstrap.manager import BootstrapManager, BootstrapProgress, BootstrapState
from coinfly.bootstrap.manifest import BootstrapError
from coinfly.config.models import default_config
from coinfly.control.transition_state import TransitionPhase
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCRequest
from coinfly.observability.diagnostics import redacted_chain, source_fingerprint
from coinfly.ui.state import DesktopState
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor

_SENTINEL = object()


class FlakyBootstrap(BootstrapManager):
    """READY for the RPC handler's check, unusable when the engine factory asks."""

    __test__ = False

    def __init__(self, home: Path) -> None:
        super().__init__(home=home)
        self.progress = BootstrapProgress(BootstrapState.READY, reason="TEST_SENTINEL_NOT_CERTIFIED")
        self.calls = 0
        self.fail_from_call: int | None = None
        self.failure: BaseException | None = None

    def verified_dataset(self) -> VerifiedDataset:
        self.calls += 1
        if self.fail_from_call is not None and self.calls >= self.fail_from_call and self.failure is not None:
            raise self.failure
        return cast(VerifiedDataset, _SENTINEL)

    async def check(self) -> BootstrapProgress:
        return self.progress


class InitializationDiagnosticsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-diag-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bootstrap = FlakyBootstrap(self.root)
        self.shutdowns: list[str] = []
        self.worker = WorkerSupervisor(paths=WorkerPaths(self.root / "worker"), socket_path=self.root / "w.sock",
                                       bootstrap=self.bootstrap, shutdown_request=lambda: self.shutdowns.append("stop"))
        self.addCleanup(self.worker.close)

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        epoch = self.worker.coordinator.status(self.worker.authority_id).transition_epoch
        request = RPCRequest.parse({"jsonrpc": "2.0", "id": request_id or method, "method": method, "params": params or {},
                                    "epoch": epoch, "deadline_ms": int(time.time() * 1000) + 10000})
        result = self.worker.handle(request, PeerIdentity(uid=os.getuid(), gid=os.getgid(), pid=os.getpid()))
        return cast(dict[str, Any], result)

    def apply(self, request_id: str) -> dict[str, Any]:
        config = default_config()
        status = self.worker.coordinator.status(self.worker.authority_id)
        revisions = {"expected_revision": status.effective_revision, "expected_epoch": status.transition_epoch}
        preview = self.rpc("context.preview", {"config": config.model_dump(mode="json"), **revisions}, request_id=request_id + "-p")
        return self.rpc("context.apply", {"preview_id": preview["preview_id"], "config_digest": preview["config_digest"],
                                          "confirmation": True, **revisions}, request_id=request_id)

    def test_dataset_invalid_inside_factory_keeps_code_stage_and_action(self) -> None:
        # apply() itself checks readiness once; the factory asks again.
        self.bootstrap.fail_from_call = 2
        self.bootstrap.failure = BootstrapError("LOCAL_DATASET_READINESS_INVALID")
        accepted = self.apply("first")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(final.error_code, "LOCAL_DATASET_READINESS_INVALID")
        assert final.failure is not None
        self.assertEqual(final.failure.stage.value, "ENGINE_FACTORY")
        self.assertEqual(final.failure.corrective_action.value, "ACTION_RESTART_WORKER")
        self.assertTrue(final.failure.retry_safe)
        snapshot = self.rpc("state.snapshot")
        self.assertIsNone(snapshot["effective_context"])
        self.assertEqual(snapshot["engine_state"], "STOPPED")
        self.assertEqual(snapshot["transition"]["failure"]["code"], "LOCAL_DATASET_READINESS_INVALID")
        self.assertEqual(snapshot["balance"]["coverage"], "UNKNOWN_UNIVERSE")
        self.assertIsNone(snapshot["balance"]["total"])
        self.assertEqual(snapshot["balance"]["label"], "NOT INITIALIZED — NO PAPER BOOK")
        state = DesktopState(snapshot, True)
        self.assertFalse(state.switching)
        self.assertTrue(state.transition_retryable)
        failure = state.transition_failure
        assert failure is not None
        self.assertEqual(failure["code"], "LOCAL_DATASET_READINESS_INVALID")
        self.assertEqual(failure["stage"], "ENGINE_FACTORY")
        self.assertEqual(failure["summary"], "BootstrapError[LOCAL_DATASET_READINESS_INVALID]")
        # Recovery: the prerequisite is corrected, the SAME operation is retried.
        self.bootstrap.fail_from_call = None
        with self.assertRaises(Exception):
            self.apply("second")  # a competing apply conflicts with the pending operation
        self.rpc("operation.retry", {"operation_id": accepted["operation_id"]}, request_id="retry")
        recovered = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(recovered.phase, TransitionPhase.READY_UNARMED)
        self.assertIsNone(recovered.failure)
        snapshot = self.rpc("state.snapshot")
        self.assertEqual(snapshot["context_epoch"], 1)
        self.assertEqual(snapshot["config_revision"], 1)
        self.assertEqual(snapshot["effective_context"]["epoch"], 1)
        self.assertEqual(snapshot["balance"]["coverage"], "COMPLETE_VERIFIED")
        self.assertEqual(snapshot["balance"]["total"], "100")
        self.assertFalse(DesktopState(snapshot, True).switching)
        self.assertTrue(DesktopState(snapshot, True).normal_controls_enabled)
        # Exactly one paper funding event exists even after the failed attempt.
        seeds = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0])
        self.assertEqual(seeds, 1)

    def test_uncoded_exception_after_initialize_is_recorded_and_retry_does_not_reseed(self) -> None:
        # The factory succeeds; the readiness check that LocalPaperEngine.initialize
        # performs raises something without a code (a plain RuntimeError).
        self.bootstrap.fail_from_call = 3
        self.bootstrap.failure = RuntimeError("/Users/someone/private/path")
        accepted = self.apply("first")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(final.error_code, "ENGINE_OPERATION_FAILED")
        assert final.failure is not None
        self.assertEqual(final.failure.stage.value, "ENGINE_INITIALIZE")
        self.assertEqual([link.type for link in final.failure.chain], ["RuntimeError"])
        self.assertNotIn("/Users/", final.model_dump_json())
        self.bootstrap.fail_from_call = None
        self.rpc("operation.retry", {"operation_id": accepted["operation_id"]}, request_id="retry")
        recovered = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(recovered.phase, TransitionPhase.READY_UNARMED)
        seeds = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0])
        self.assertEqual(seeds, 1)
        self.assertEqual(self.rpc("state.snapshot")["context_epoch"], 1)

    def test_restart_with_failed_initialization_recovers_by_explicit_retry_only(self) -> None:
        self.bootstrap.fail_from_call = 2
        self.bootstrap.failure = BootstrapError("DATASET_NOT_READY")
        accepted = self.apply("first")
        self.assertEqual(self.worker.coordinator.wait(accepted["operation_id"], timeout=10).phase,
                         TransitionPhase.FAILED_INITIALIZATION)
        self.worker.close()
        healthy = FlakyBootstrap(self.root)
        restarted = WorkerSupervisor(paths=WorkerPaths(self.root / "worker"), socket_path=self.root / "w2.sock",
                                     bootstrap=healthy)
        self.addCleanup(restarted.close)
        self.worker = restarted
        snapshot = self.rpc("state.snapshot")
        # The failed operation and its record survive; nothing auto-resumed.
        self.assertEqual(snapshot["transition"]["phase"], "FAILED_INITIALIZATION")
        self.assertEqual(snapshot["transition"]["error_code"], "DATASET_NOT_READY")
        self.assertIsNone(snapshot["effective_context"])
        self.assertTrue(snapshot["admissions_fenced"])
        self.assertEqual(snapshot["context_epoch"], 1)
        self.rpc("operation.retry", {"operation_id": accepted["operation_id"]}, request_id="retry-after-restart")
        recovered = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(recovered.phase, TransitionPhase.READY_UNARMED)
        snapshot = self.rpc("state.snapshot")
        self.assertEqual(snapshot["context_epoch"], 1)
        self.assertFalse(snapshot["recovery_required"])
        self.assertEqual(snapshot["balance"]["total"], "100")

    def test_snapshot_carries_runtime_identity_for_stale_worker_detection(self) -> None:
        snapshot = self.rpc("state.snapshot")
        self.assertEqual(snapshot["runtime"]["source_fingerprint"], source_fingerprint())
        self.assertEqual(snapshot["runtime"]["pid"], os.getpid())
        self.assertFalse(DesktopState(snapshot, True, source_fingerprint()).worker_source_mismatch)
        self.assertTrue(DesktopState(snapshot, True, "0" * 64).worker_source_mismatch)
        self.assertFalse(DesktopState(snapshot, False, "0" * 64).worker_source_mismatch)

    def test_worker_shutdown_fences_entries_and_asks_launcher_to_stop(self) -> None:
        result = self.rpc("worker.shutdown", request_id="shutdown")
        self.assertTrue(result["accepted"])
        deadline = time.monotonic() + 2
        while not self.shutdowns and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.shutdowns, ["stop"])
        self.assertTrue(self.rpc("state.snapshot")["admissions_fenced"])

    def test_redacted_chain_never_reads_exception_messages(self) -> None:
        class Loud(Exception):
            def __str__(self) -> str:  # pragma: no cover - must never be called
                raise AssertionError("message rendered")
        try:
            try:
                raise OSError(13, "Permission denied", "/private/file")
            except OSError as inner:
                raise Loud("secret") from inner
        except Loud as error:
            chain = redacted_chain(error)
        self.assertEqual([link.type.rsplit(".", 1)[-1] for link in chain], ["Loud", "PermissionError"])
        self.assertEqual(chain[1].errno, "EACCES")
        self.assertEqual(chain[1].relation, "caused_by")
