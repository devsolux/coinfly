from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from coinfly.config.models import EngineConfig
from coinfly.control.context_coordinator import ContextCoordinator
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import ContextKey, DomainError, EngineState, ProductTrack, RegionPolicy
from coinfly.execution.recovery import context_fact_sequence_tx
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import (
    PositionResidual,
    RetirementEvidence,
    ScopeWatermark,
    StopCertificate,
)
from coinfly.storage.writer import Writer


def certificate(writer: Writer, context: ContextKey, scopes: tuple[str, ...] = ("paper-book",)) -> StopCertificate:
    now = datetime.now(timezone.utc)
    return StopCertificate(
        proof_id=str(uuid4()), context=context, execution_scope=scopes, issued_at=now, expires_at=now + timedelta(seconds=30),
        fact_sequence=writer.call(lambda connection: context_fact_sequence_tx(connection, context)),
        watermarks=tuple(ScopeWatermark(scope_id=scope, evidence_id="fixture-verified", account_observed_at=now,
                                       account_stream_sequence=0, applied_stream_sequence=0, history_complete=True, account_complete=True) for scope in scopes),
        residual_positions=(), outstanding_order_ids=(), unresolved_create_ids=(), unapplied_fill_count=0,
        all_delegated_positions_covered=True, all_related_orders_terminal=True, dispatch_tasks_fenced=True,
        task_fence_evidence_id="fixture-task-fence",
    )


class FixtureEngine:
    def __init__(self, writer: Writer, context: ContextKey, events: list[str]) -> None:
        self.writer, self.context, self.events = writer, context, events
        self.drain_started = threading.Event()
        self.drain_release: threading.Event | None = None
        self.failure: str | None = None
        self.residual = Decimal(0)
        self.race_fact = False

    def initialize(self, config: EngineConfig, context: ContextKey) -> None:
        self.events.append(f"initialize:{context.epoch}")
        if self.failure == "init":
            raise DomainError("FIXTURE_INIT_FAILURE")

    def quiesce(self) -> None:
        self.events.append(f"quiesce:{self.context.epoch}")

    def drain(self, policy: DrainPolicy) -> None:
        self.events.append(f"drain:{self.context.epoch}")
        self.drain_started.set()
        if self.drain_release is not None and not self.drain_release.wait(5):
            raise DomainError("FIXTURE_DEADLINE")
        if self.failure == "drain":
            raise DomainError("FIXTURE_UNKNOWN_CREATE")

    def reconcile(self) -> StopCertificate:
        self.events.append(f"reconcile:{self.context.epoch}")
        proof = certificate(self.writer, self.context)
        if self.residual:
            return proof.model_copy(update={"residual_positions": (PositionResidual(scope_id="paper-book", instrument="BTC-USD", quantity=self.residual),)})
        return proof

    def retire(self) -> RetirementEvidence:
        self.events.append(f"retire:{self.context.epoch}")
        if self.race_fact:
            self.writer.call(lambda connection: connection.execute(
                "INSERT INTO ex_fact_state VALUES(?,1) ON CONFLICT(context_id) DO UPDATE SET sequence=sequence+1", (self.context.identity,),
            ).rowcount)
        return RetirementEvidence(context=self.context, evidence_id="fixture-retired", mutation_transports_retired=True,
                                  signing_handles_retired=True, all_tasks_joined_or_fenced=True, grants_revoked=True)

    def reset_transient(self) -> None:
        self.events.append(f"reset:{self.context.epoch}")

    def arm(self) -> None:
        self.events.append(f"arm:{self.context.epoch}")


class ContextCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.writer = Writer(Path(self.temp.name) / "worker.sqlite")
        self.interlock = ContextInterlock(self.writer)
        self.events: list[str] = []
        self.engines: list[FixtureEngine] = []
        self.fail_target = False
        def factory(config: EngineConfig, context: ContextKey) -> FixtureEngine:
            engine = FixtureEngine(self.writer, context, self.events)
            if self.fail_target:
                engine.failure = "init"
            self.engines.append(engine)
            return engine
        self.factory = factory
        self.coordinator = ContextCoordinator(self.writer, self.interlock, factory)
        self.config = EngineConfig(authority_id="authority", execution_scope=("paper-book",), region_policy=RegionPolicy.GLOBAL)

    def tearDown(self) -> None:
        for engine in self.engines:
            if engine.drain_release:
                engine.drain_release.set()
        self.coordinator.close()
        self.writer.close()
        self.temp.cleanup()

    def apply(self, config: EngineConfig, request_id: str = "change"):
        status = self.coordinator.status(config.authority_id)
        preview = self.coordinator.preview(config, expected_revision=status.effective_revision, expected_epoch=status.transition_epoch)
        return self.coordinator.apply(preview.preview_id, preview.config_digest, expected_revision=preview.expected_revision,
                                      expected_epoch=preview.expected_epoch, request_id=request_id)

    def activate(self) -> None:
        operation = self.apply(self.config, "initial")
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)
        self.coordinator.arm("authority", expected_revision=1, expected_epoch=1, request_id="arm-initial")

    def test_handover_is_fenced_and_target_waits_for_stop_and_retirement(self) -> None:
        self.activate()
        source = self.engines[0]
        source.drain_release = threading.Event()
        target = self.config.model_copy(update={"product_track": ProductTrack.PERPETUAL})
        operation = self.apply(target)
        self.assertTrue(source.drain_started.wait(2))
        status = self.coordinator.status("authority")
        self.assertEqual(status.effective_config.product_track, ProductTrack.SPOT)
        self.assertFalse(status.armed)
        self.assertEqual(len(self.engines), 1)
        with self.assertRaisesRegex(DomainError, "UNARMED_OR_TRANSITIONING"):
            self.interlock.validate_context(source.context)
        self.interlock.validate_context(source.context, risk_increase=False)
        source.drain_release.set()
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.READY_UNARMED)
        self.assertLess(self.events.index("retire:1"), self.events.index("initialize:2"))
        self.assertLess(self.events.index("reset:1"), self.events.index("initialize:2"))
        self.assertEqual(self.coordinator.status("authority").effective_context.product_track, ProductTrack.PERPETUAL)
        self.assertFalse(self.coordinator.status("authority").armed)
        self.assertEqual(self.coordinator.phase_history(operation.operation_id), tuple(TransitionPhase(value) for value in (
            "REQUESTED", "QUIESCING", "DRAINING", "RECONCILING", "STOPPED_OLD", "RESETTING", "INITIALIZING_NEW", "READY_UNARMED",
        )))

    def test_request_replay_returns_same_operation_but_competing_switch_conflicts(self) -> None:
        self.activate()
        source = self.engines[0]
        source.drain_release = threading.Event()
        preview = self.coordinator.preview(self.config, expected_revision=1, expected_epoch=1)
        args = dict(expected_revision=1, expected_epoch=1, request_id="unique-request")
        first = self.coordinator.apply(preview.preview_id, preview.config_digest, **args)
        second = self.coordinator.apply(preview.preview_id, preview.config_digest, **args)
        self.assertEqual(first.operation_id, second.operation_id)
        with self.assertRaises(DomainError):
            self.coordinator.apply(preview.preview_id, preview.config_digest, expected_revision=1, expected_epoch=1, request_id="different")
        with self.assertRaisesRegex(DomainError, "REQUEST_ID_CONFLICT"):
            self.coordinator.apply(preview.preview_id, "0" * 64, **args)
        self.assertEqual(len(self.engines), 1)
        source.drain_release.set()
        self.coordinator.wait(first.operation_id)

    def test_socket_retirement_precedes_reset_and_cannot_be_reused(self) -> None:
        self.activate()
        source = self.engines[0]
        class Transport:
            def retire(inner):
                self.events.append("close-sockets")
        transport = Transport()
        self.coordinator.transports.register(source.context, transport)
        operation = self.apply(self.config)
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)
        self.assertLess(self.events.index("reconcile:1"), self.events.index("close-sockets"))
        self.assertLess(self.events.index("close-sockets"), self.events.index("reset:1"))
        self.assertLess(self.events.index("close-sockets"), self.events.index("initialize:2"))
        with self.assertRaisesRegex(DomainError, "NETWORK_CONTEXT_RETIRED"):
            self.coordinator.transports.register(source.context, transport)

    def test_failed_socket_close_fences_transition_and_still_closes_other_sockets(self) -> None:
        self.activate()
        source = self.engines[0]
        class Broken:
            def retire(inner):
                raise OSError("test socket close failed")
        class Healthy:
            def retire(inner):
                self.events.append("other-socket-closed")
        self.coordinator.transports.register(source.context, Broken())
        self.coordinator.transports.register(source.context, Healthy())
        operation = self.apply(self.config)
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.BLOCKED_DRAIN)
        self.assertEqual(result.error_code, "NETWORK_RETIREMENT_UNPROVEN")
        self.assertIn("other-socket-closed", self.events)
        self.assertNotIn("initialize:2", self.events)
        self.assertFalse(self.coordinator.status("authority").armed)

    def test_unknown_drain_preserves_source_and_can_only_resume_explicitly(self) -> None:
        self.activate()
        self.engines[0].failure = "drain"
        operation = self.apply(self.config)
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.BLOCKED_DRAIN)
        status = self.coordinator.status("authority")
        self.assertEqual(status.effective_context.epoch, 1)
        self.assertEqual(status.engine_state, EngineState.DRAINING)
        self.assertEqual(status.effective_revision, 1)
        self.assertFalse(status.armed)
        self.assertEqual(len(self.engines), 1)
        self.engines[0].failure = None
        self.coordinator.retry(operation.operation_id)
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)

    def test_dust_is_not_declared_flat(self) -> None:
        self.activate()
        self.engines[0].residual = Decimal("0.00000001")
        operation = self.apply(self.config)
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.error_code, "STOP_PROOF_RESIDUAL_POSITION")
        self.assertNotIn("retire:1", self.events)
        self.assertEqual(len(self.engines), 1)

    def test_fact_racing_retirement_invalidates_certificate(self) -> None:
        self.activate()
        self.engines[0].race_fact = True
        operation = self.apply(self.config)
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.BLOCKED_DRAIN)
        self.assertEqual(result.error_code, "STOP_PROOF_FACTS_CHANGED")
        self.assertEqual(len(self.engines), 1)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_context_retirements").fetchone()[0]), 0)

    def test_failed_target_initialization_leaves_source_stopped(self) -> None:
        self.activate()
        self.fail_target = True
        operation = self.apply(self.config)
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.FAILED_INITIALIZATION)
        status = self.coordinator.status("authority")
        self.assertEqual(status.effective_context.epoch, 1)
        self.assertEqual(status.engine_state, EngineState.STOPPED)
        self.assertFalse(status.armed)
        with self.assertRaises(DomainError):
            self.coordinator.arm("authority", expected_revision=1, expected_epoch=2, request_id="unsafe-arm")

    def test_expired_retired_source_proof_requires_new_read_only_evidence(self) -> None:
        self.activate()
        self.fail_target = True
        operation = self.apply(self.config)
        self.coordinator.wait(operation.operation_id)
        def expire(connection):
            raw = connection.execute("SELECT stop_proof FROM cf_transition_operations WHERE operation_id=?", (operation.operation_id,)).fetchone()[0]
            proof = StopCertificate.model_validate_json(raw)
            old = datetime.now(timezone.utc) - timedelta(minutes=1)
            expired = proof.model_copy(update={"issued_at": old, "expires_at": old + timedelta(seconds=30),
                                               "watermarks": tuple(w.model_copy(update={"account_observed_at": old}) for w in proof.watermarks)})
            connection.execute("UPDATE cf_transition_operations SET stop_proof=? WHERE operation_id=?", (expired.model_dump_json(), operation.operation_id))
        self.writer.call(expire)
        before_reconciles = self.events.count("reconcile:1")
        self.fail_target = False
        self.coordinator.retry(operation.operation_id)
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)
        self.assertEqual(self.events.count("reconcile:1"), before_reconciles + 1)
        self.assertEqual(self.events.count("retire:1"), 1)
        self.assertEqual(self.events.count("drain:1"), 1)

    def test_restart_fences_active_state_without_initializing_or_arming(self) -> None:
        self.activate()
        self.coordinator.close()
        self.coordinator = ContextCoordinator(self.writer, self.interlock, self.factory)
        status = self.coordinator.status("authority")
        self.assertTrue(status.recovery_required)
        self.assertFalse(status.armed)
        self.assertEqual(len(self.engines), 1)
        with self.assertRaisesRegex(DomainError, "RECOVERY_REQUIRED"):
            self.interlock.validate_context(status.effective_context)
        self.coordinator.attach_recovered_engine(self.engines[0])
        operation = self.apply(self.config, "recover")
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)

    def test_first_activation_has_explicit_no_previous_context_path(self) -> None:
        operation = self.apply(self.config, "first")
        self.assertEqual(self.coordinator.wait(operation.operation_id).phase, TransitionPhase.READY_UNARMED)
        history = self.coordinator.phase_history(operation.operation_id)
        self.assertEqual(history, (TransitionPhase.REQUESTED, TransitionPhase.NO_PREVIOUS_CONTEXT,
                                   TransitionPhase.INITIALIZING_NEW, TransitionPhase.READY_UNARMED))
        # No imaginary source engine was quiesced, drained, stopped or reset.
        self.assertEqual([event for event in self.events if not event.startswith("initialize:")], [])
        self.assertEqual(self.coordinator.status("authority").transition_epoch, 1)

    def test_uncoded_factory_exception_keeps_redacted_chain_stage_and_action(self) -> None:
        def broken_factory(config: EngineConfig, context: ContextKey) -> FixtureEngine:
            try:
                raise ModuleNotFoundError("No module named 'coinfly.worker.paper_engine'", name="coinfly.worker.paper_engine")
            except ModuleNotFoundError as inner:
                raise RuntimeError("/private/path/that/must/not/leak") from inner
        coordinator = ContextCoordinator(self.writer, self.interlock, broken_factory)
        self.addCleanup(coordinator.close)
        status = coordinator.status("authority")
        preview = coordinator.preview(self.config, expected_revision=status.effective_revision, expected_epoch=status.transition_epoch)
        operation = coordinator.apply(preview.preview_id, preview.config_digest, expected_revision=0, expected_epoch=0, request_id="broken")
        result = coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(result.error_code, "ENGINE_OPERATION_FAILED")
        assert result.failure is not None
        self.assertEqual(result.failure.stage.value, "ENGINE_FACTORY")
        self.assertEqual(result.failure.category, "IMPORT")
        self.assertEqual([link.type for link in result.failure.chain], ["RuntimeError", "ModuleNotFoundError"])
        self.assertEqual(result.failure.chain[1].module, "coinfly.worker.paper_engine")
        self.assertEqual(result.failure.chain[1].relation, "caused_by")
        self.assertTrue(result.failure.retry_safe)
        self.assertEqual(result.failure.corrective_action.value, "ACTION_RESTART_WORKER")
        self.assertNotIn("/private/path", result.model_dump_json())
        # The durable record survives a fresh coordinator instance (restart).
        reloaded = ContextCoordinator(self.writer, self.interlock, broken_factory)
        self.addCleanup(reloaded.close)
        persisted = reloaded.operation(operation.operation_id)
        assert persisted.failure is not None
        self.assertEqual(persisted.failure.summary, "RuntimeError <- ModuleNotFoundError[coinfly.worker.paper_engine]")
        self.assertEqual(reloaded.status("authority").transition_epoch, 1)

    def test_coded_non_domain_exception_keeps_its_own_code(self) -> None:
        from coinfly.bootstrap.manifest import BootstrapError

        def not_ready(config: EngineConfig, context: ContextKey) -> FixtureEngine:
            raise BootstrapError("DATASET_NOT_READY")
        coordinator = ContextCoordinator(self.writer, self.interlock, not_ready)
        self.addCleanup(coordinator.close)
        preview = coordinator.preview(self.config, expected_revision=0, expected_epoch=0)
        operation = coordinator.apply(preview.preview_id, preview.config_digest, expected_revision=0, expected_epoch=0, request_id="dataset")
        result = coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(result.error_code, "DATASET_NOT_READY")
        assert result.failure is not None
        self.assertEqual(result.failure.category, "CODED")
        self.assertEqual(result.failure.corrective_action.value, "ACTION_VERIFY_DATASET")
        self.assertTrue(result.failure.retry_safe)
        # Retrying the same operation keeps the epoch and does not create a second operation.
        with self.assertRaisesRegex(DomainError, "CONTEXT_TRANSITION_IN_PROGRESS"):
            self.apply(self.config, "competing")
        coordinator.retry(operation.operation_id)
        again = coordinator.wait(operation.operation_id)
        self.assertEqual(again.error_code, "DATASET_NOT_READY")
        self.assertEqual(coordinator.status("authority").transition_epoch, 1)

    def test_failed_initialize_records_stage_and_retry_recovers_without_new_epoch(self) -> None:
        self.fail_target = True
        operation = self.apply(self.config, "first")
        result = self.coordinator.wait(operation.operation_id)
        self.assertEqual(result.phase, TransitionPhase.FAILED_INITIALIZATION)
        assert result.failure is not None
        self.assertEqual(result.failure.stage.value, "ENGINE_INITIALIZE")
        self.assertEqual(result.failure.code, "FIXTURE_INIT_FAILURE")
        self.assertTrue(result.failure.retry_safe)
        self.fail_target = False
        self.coordinator.retry(operation.operation_id)
        recovered = self.coordinator.wait(operation.operation_id)
        self.assertEqual(recovered.phase, TransitionPhase.READY_UNARMED)
        self.assertIsNone(recovered.failure)
        self.assertEqual(self.coordinator.status("authority").transition_epoch, 1)
        self.assertEqual(self.coordinator.status("authority").effective_revision, 1)

    def test_stale_preview_never_changes_effective_revision(self) -> None:
        preview = self.coordinator.preview(self.config, expected_revision=0, expected_epoch=0)
        with self.assertRaisesRegex(DomainError, "PREVIEW_STALE_OR_CHANGED"):
            self.coordinator.apply(preview.preview_id, "0" * 64, expected_revision=0, expected_epoch=0, request_id="bad")
        self.assertEqual(self.coordinator.status("authority").effective_revision, 0)
        self.assertEqual(self.engines, [])
