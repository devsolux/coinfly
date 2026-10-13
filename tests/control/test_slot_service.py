from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from coinfly.config.models import EngineConfig
from coinfly.control.slot_service import (
    SlotOperation,
    SlotOperationPhase,
    SlotRecord,
    SlotService,
    SlotStopProof,
)
from coinfly.domain.context import (
    ContextKey,
    DomainError,
    EngineState,
    Environment,
    ProductTrack,
    RegionPolicy,
)
from coinfly.execution.recovery import slot_fact_sequence_tx
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import ScopeWatermark, StopCertificate
from coinfly.storage.writer import Writer


class SlotFixtureDriver:
    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self.started: list[str] = []
        self.drain_started = threading.Event()
        self.release: threading.Event | None = None
        self.unknown = False

    def start(self, slot: SlotRecord) -> None:
        self.started.append(slot.slot_id)

    def quiesce(self, slot: SlotRecord) -> None:
        return None

    def drain(self, slot: SlotRecord, policy: DrainPolicy) -> None:
        self.drain_started.set()
        if self.release is not None and not self.release.wait(2):
            raise DomainError("FIXTURE_DRAIN_TIMEOUT")

    def reconcile(self, slot: SlotRecord) -> SlotStopProof:
        now = datetime.now(timezone.utc)
        proof = StopCertificate(
            proof_id=str(uuid4()), context=slot.context, execution_scope=(slot.scope_id,), issued_at=now,
            expires_at=now+timedelta(seconds=30), fact_sequence=self.writer.call(lambda connection: slot_fact_sequence_tx(connection, slot.context, slot.slot_id)),
            watermarks=(ScopeWatermark(scope_id=slot.scope_id, evidence_id="fixture-state", account_observed_at=now,
                                       account_stream_sequence=0, applied_stream_sequence=0, history_complete=True, account_complete=True),),
            residual_positions=(), outstanding_order_ids=(), unresolved_create_ids=("unknown-create",) if self.unknown else (),
            unapplied_fill_count=0, all_delegated_positions_covered=True, all_related_orders_terminal=True,
            dispatch_tasks_fenced=True, task_fence_evidence_id="fixture-retired",
        )
        return SlotStopProof(slot_id=slot.slot_id, certificate=proof)


class SlotServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.writer = Writer(Path(self.temp.name)/"slots.sqlite")
        self.interlock = ContextInterlock(self.writer)
        self.context = ContextKey(authority_id="authority", environment=Environment.SANDBOX, product_track=ProductTrack.SPOT, broker_profile_id="paper-local", epoch=1)
        self.config = EngineConfig(authority_id="authority", execution_scope=("book",), region_policy=RegionPolicy.GLOBAL)
        self._effective(self.context)
        self.driver = SlotFixtureDriver(self.writer)
        self.slots = SlotService(self.writer, self.interlock, self.driver)

    def tearDown(self) -> None:
        if self.driver.release:
            self.driver.release.set()
        self.slots.close()
        self.writer.close()
        self.temp.cleanup()

    def _effective(self, context: ContextKey) -> None:
        config = self.config.model_copy(update={"environment": context.environment, "product_track": context.product_track})
        self.writer.call(lambda connection: connection.execute(
            "INSERT INTO cf_authorities(authority_id,effective_context,effective_config,effective_revision,transition_epoch,engine_state,armed,admissions_fenced) VALUES(?,?,?,?,?,'ACTIVE',1,0) "
            "ON CONFLICT(authority_id) DO UPDATE SET effective_context=excluded.effective_context,effective_config=excluded.effective_config,effective_revision=excluded.effective_revision,transition_epoch=excluded.transition_epoch,engine_state='ACTIVE',armed=1,admissions_fenced=0",
            ("authority", context.model_dump_json(), config.model_dump_json(), context.epoch, context.epoch),
        ).rowcount)

    def create(self, slot_id: str = "slot", context: ContextKey | None = None, instrument: str = "BTC-USD") -> SlotRecord:
        context = context or self.context
        return self.slots.create(slot_id=slot_id, context=context, scope_id="book", instrument=instrument,
                                 expected_config_revision=context.epoch, request_id="create-"+slot_id)

    def test_start_activates_scheduler_without_creating_purchase(self) -> None:
        created = self.create()
        active = self.slots.start(created.slot_id, expected_revision=1, expected_epoch=1, request_id="start")
        self.assertEqual(active.engine_state, EngineState.ACTIVE)
        self.assertTrue(active.scheduler_enabled)
        self.assertEqual(self.driver.started, ["slot"])
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]), 0)
        replay = self.slots.start(created.slot_id, expected_revision=1, expected_epoch=1, request_id="start")
        self.assertEqual(replay.revision, active.revision)
        self.assertEqual(self.driver.started, ["slot"])

    def test_delete_waits_for_stop_proof_and_unknown_retains_visible_slot(self) -> None:
        self.create()
        self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        self.driver.unknown = True
        operation = self.slots.delete("slot", expected_revision=2, expected_epoch=1, request_id="delete")
        blocked = self.slots.wait(operation.operation_id)
        self.assertEqual(blocked.phase, SlotOperationPhase.BLOCKED_DRAIN)
        slot = self.slots.get("slot")
        self.assertFalse(slot.tombstoned)
        self.assertFalse(slot.scheduler_enabled)
        self.assertEqual(slot.engine_state, EngineState.DRAINING)
        again = self.slots.delete("slot", expected_revision=2, expected_epoch=1, request_id="delete-again")
        self.assertEqual(again.operation_id, operation.operation_id)
        self.driver.unknown = False
        self.slots.retry(operation.operation_id)
        self.assertEqual(self.slots.wait(operation.operation_id).phase, SlotOperationPhase.COMPLETED)
        self.assertTrue(self.slots.get("slot").tombstoned)
        self.assertEqual(self.slots.list(self.context), ())
        self.assertEqual(len(self.slots.list(self.context, include_tombstones=True)), 1)

    def test_manual_stop_persists_scheduler_exclusion_across_service_restart(self) -> None:
        self.create()
        self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        operation = self.slots.stop("slot", expected_revision=2, expected_epoch=1, request_id="stop")
        self.slots.wait(operation.operation_id)
        self.slots.close()
        self.slots = SlotService(self.writer, self.interlock, self.driver)
        next_context = self.context.model_copy(update={"epoch": 2})
        self._effective(next_context)
        with self.assertRaisesRegex(DomainError, "INSTRUMENT_MANUALLY_DISABLED"):
            self.create("replacement", next_context)
        self.assertFalse(self.slots.get("slot").scheduler_enabled)

    def test_unstarted_slot_does_not_create_manual_exclusion(self) -> None:
        self.create()
        next_context = self.context.model_copy(update={"epoch": 2})
        self._effective(next_context)
        self.assertEqual(self.create("new-epoch-slot", next_context).engine_state, EngineState.STOPPED)

    def test_manual_exclusion_is_product_and_environment_qualified(self) -> None:
        self.create()
        operation = self.slots.stop("slot", expected_revision=1, expected_epoch=1, request_id="stop")
        self.slots.wait(operation.operation_id)
        perpetual = self.context.model_copy(update={"epoch": 2, "product_track": ProductTrack.PERPETUAL})
        self._effective(perpetual)
        self.assertEqual(self.create("perpetual-slot", perpetual).context.product_track, ProductTrack.PERPETUAL)
        count = self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_slot_exclusions WHERE environment='LIVE'").fetchone()[0])
        self.assertEqual(count, 0)

    def test_start_during_transition_and_stale_edit_fail_before_driver(self) -> None:
        self.create()
        self.writer.call(lambda connection: connection.execute("UPDATE cf_authorities SET active_operation='switch',admissions_fenced=1").rowcount)
        with self.assertRaises(DomainError):
            self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        self.assertEqual(self.driver.started, [])
        with self.assertRaisesRegex(DomainError, "SLOT_REVISION_CONFLICT"):
            self.slots.edit("slot", expected_revision=0, expected_epoch=1, instrument="ETH-USD", request_id="edit")

    def test_late_invalidated_tombstone_reappears_in_visible_slots(self) -> None:
        self.create()
        operation = self.slots.delete("slot", expected_revision=1, expected_epoch=1, request_id="delete")
        self.slots.wait(operation.operation_id)
        self.assertEqual(self.slots.list(self.context), ())
        # The fact reducer atomically clears this proof when an old fill arrives.
        self.writer.call(lambda connection: connection.execute("UPDATE cf_slots SET stop_proof=NULL WHERE slot_id='slot'").rowcount)
        self.assertTrue(self.slots.list(self.context)[0].tombstoned)

    def test_edit_unknown_retains_source_and_retry_creates_one_stopped_replacement(self) -> None:
        self.create()
        self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        self.driver.unknown = True
        self.interlock.provisional_fence("authority", "UNKNOWN_SUBMISSION")
        operation = self.slots.edit("slot", expected_revision=2, expected_epoch=1, instrument="ETH-USD", request_id="edit")
        self.assertIsInstance(operation, SlotOperation)
        blocked = self.slots.wait(operation.operation_id)
        self.assertEqual(blocked.phase, SlotOperationPhase.BLOCKED_DRAIN)
        self.assertEqual(blocked.replacement_instrument, "ETH-USD")
        self.assertNotEqual(blocked.replacement_slot_id, "slot")
        self.assertFalse(self.slots.get("slot").tombstoned)
        self.assertFalse(self.slots.get("slot").scheduler_enabled)
        self.assertEqual([slot.instrument for slot in self.slots.list(self.context)], ["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "SLOT_UNKNOWN"):
            self.slots.get(blocked.replacement_slot_id)
        joined = self.slots.edit("slot", expected_revision=2, expected_epoch=1, instrument="ETH-USD", request_id="edit-again")
        self.assertEqual(joined.operation_id, operation.operation_id)
        self.slots.close()
        self.slots = SlotService(self.writer, self.interlock, self.driver)
        self.driver.unknown = False
        self.interlock.reconcile_provisional_unknown(self.context)
        self.slots.retry(operation.operation_id)
        completed = self.slots.wait(operation.operation_id)
        self.assertEqual(completed.phase, SlotOperationPhase.COMPLETED)
        self.assertEqual(completed.replacement_slot_id, blocked.replacement_slot_id)
        replacement = self.slots.get(completed.replacement_slot_id)
        self.assertEqual(replacement.instrument, "ETH-USD")
        self.assertEqual(replacement.engine_state, EngineState.STOPPED)
        self.assertFalse(replacement.scheduler_enabled)
        self.assertEqual(self.driver.started, ["slot"])
        self.assertTrue(self.slots.get("slot").tombstoned)
        replay = self.slots.edit("slot", expected_revision=2, expected_epoch=1, instrument="ETH-USD", request_id="edit")
        self.assertEqual(replay.operation_id, operation.operation_id)
        repeated = self.slots.edit("slot", expected_revision=2, expected_epoch=1, instrument="ETH-USD", request_id="edit-after-response-loss")
        self.assertEqual(repeated.operation_id, operation.operation_id)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_slots").fetchone()[0]), 2)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]), 0)
        self.slots.start(replacement.slot_id, expected_revision=1, expected_epoch=1, request_id="start-replacement")
        self.assertEqual(self.driver.started, ["slot", replacement.slot_id])

    def test_edit_conflicts_and_pending_target_reservation_precede_any_second_drain(self) -> None:
        self.create()
        self.create("other", instrument="SOL-USD")
        self.driver.unknown = True
        operation = self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="ETH-USD", request_id="edit")
        self.slots.wait(operation.operation_id)
        with self.assertRaisesRegex(DomainError, "REQUEST_ID_CONFLICT"):
            self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="LTC-USD", request_id="edit")
        with self.assertRaisesRegex(DomainError, "SLOT_OPERATION_IN_PROGRESS"):
            self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="LTC-USD", request_id="different-edit")
        with self.assertRaisesRegex(DomainError, "SLOT_REPLACEMENT_TARGET_IN_PROGRESS"):
            self.slots.edit("other", expected_revision=1, expected_epoch=1, instrument="ETH-USD", request_id="second-source")
        with self.assertRaisesRegex(DomainError, "SLOT_REPLACEMENT_TARGET_IN_PROGRESS"):
            self.create("conflicting-create", instrument="ETH-USD")
        self.assertEqual(self.slots.get("other").revision, 1)

    def test_context_switch_racing_edit_rejects_replacement_in_old_context(self) -> None:
        self.create()
        self.driver.release = threading.Event()
        operation = self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="ETH-USD", request_id="edit")
        self.assertTrue(self.driver.drain_started.wait(2))
        with self.interlock.lock:
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_authorities SET active_operation='context-switch',transition_epoch=2,armed=0,admissions_fenced=1 WHERE authority_id='authority'",
            ).rowcount)
        self.driver.release.set()
        blocked = self.slots.wait(operation.operation_id)
        self.assertEqual(blocked.error_code, "REPLACEMENT_CONTEXT_CHANGED")
        self.assertFalse(self.slots.get("slot").tombstoned)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_slots").fetchone()[0]), 1)
        next_context = self.context.model_copy(update={"epoch": 2})
        self._effective(next_context)
        self.writer.call(lambda connection: connection.execute("UPDATE cf_authorities SET active_operation=NULL").rowcount)
        self.slots.retry(operation.operation_id)
        self.assertEqual(self.slots.wait(operation.operation_id).error_code, "STALE_CONTEXT")
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_slots").fetchone()[0]), 1)

    def test_replacement_insert_failure_rolls_back_source_tombstone(self) -> None:
        self.create()
        self.writer.call(lambda connection: connection.execute(
            "CREATE TRIGGER test_replacement_failure BEFORE INSERT ON cf_slots WHEN NEW.instrument='ETH-USD' BEGIN SELECT RAISE(ABORT,'injected failure'); END",
        ).rowcount)
        operation = self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="ETH-USD", request_id="edit")
        self.assertEqual(self.slots.wait(operation.operation_id).phase, SlotOperationPhase.BLOCKED_DRAIN)
        self.assertFalse(self.slots.get("slot").tombstoned)
        self.assertEqual(self.slots.get("slot").engine_state, EngineState.DRAINING)
        self.writer.call(lambda connection: connection.execute("DROP TRIGGER test_replacement_failure").rowcount)
        self.slots.retry(operation.operation_id)
        self.assertEqual(self.slots.wait(operation.operation_id).phase, SlotOperationPhase.COMPLETED)
        self.assertTrue(self.slots.get("slot").tombstoned)
        self.assertEqual(self.slots.get(operation.replacement_slot_id).instrument, "ETH-USD")

    def test_same_instrument_edit_is_receipted_without_revision_or_lifecycle_change(self) -> None:
        created = self.create()
        unchanged = self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="BTC-USD", request_id="same-edit")
        self.assertEqual(unchanged, created)
        self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        replay = self.slots.edit("slot", expected_revision=1, expected_epoch=1, instrument="BTC-USD", request_id="same-edit")
        self.assertEqual(replay.revision, 2)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_slot_operations").fetchone()[0]), 0)
