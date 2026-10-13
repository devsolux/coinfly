"""Global safety commands against real durable slots and an explicit no-trade fixture."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from coinfly.bootstrap.manager import BootstrapManager
from coinfly.config.models import default_config
from coinfly.control.slot_service import (
    SlotOperation,
    SlotOperationPhase,
    SlotRecord,
    SlotService,
    SlotStopProof,
)
from coinfly.domain.context import ContextKey, DomainError, EngineState
from coinfly.execution.recovery import slot_fact_sequence_tx
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCRequest
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import ScopeWatermark, StopCertificate
from coinfly.storage.writer import Writer
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor


class FixtureSlotDriver:
    """No broker, model, dataset or real account is represented by this fixture."""

    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self.started: list[str] = []
        self.drained: list[str] = []
        self.unknown: set[str] = set()
        self.entered: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}

    def start(self, slot: SlotRecord) -> None:
        self.started.append(slot.slot_id)

    def quiesce(self, slot: SlotRecord) -> None:
        return None

    def drain(self, slot: SlotRecord, policy: DrainPolicy) -> None:
        self.drained.append(slot.slot_id)
        self.entered.setdefault(slot.slot_id, threading.Event()).set()
        gate = self.release.get(slot.slot_id)
        if gate is not None and not gate.wait(3):
            raise DomainError("FIXTURE_DRAIN_TIMEOUT")

    def reconcile(self, slot: SlotRecord) -> SlotStopProof:
        now = datetime.now(timezone.utc)
        proof = StopCertificate(
            proof_id=str(uuid4()), context=slot.context, execution_scope=(slot.scope_id,),
            issued_at=now, expires_at=now+timedelta(seconds=30),
            fact_sequence=self.writer.call(lambda connection: slot_fact_sequence_tx(connection, slot.context, slot.slot_id)),
            watermarks=(ScopeWatermark(scope_id=slot.scope_id, evidence_id="fixture-account-evidence", account_observed_at=now,
                                      account_stream_sequence=0, applied_stream_sequence=0, history_complete=True, account_complete=True),),
            residual_positions=(), outstanding_order_ids=(), unresolved_create_ids=("fixture-unknown",) if slot.slot_id in self.unknown else (),
            unapplied_fill_count=0, all_delegated_positions_covered=True, all_related_orders_terminal=True,
            dispatch_tasks_fenced=True, task_fence_evidence_id="fixture-fenced",
        )
        return SlotStopProof(slot_id=slot.slot_id, certificate=proof)


class SafetyStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.worker = WorkerSupervisor(paths=WorkerPaths(root / "worker"), socket_path=root / "worker.sock",
                                       bootstrap=BootstrapManager(home=root))
        self.config = default_config()
        self.context = ContextKey(authority_id=self.config.authority_id, environment=self.config.environment,
                                  product_track=self.config.product_track, broker_profile_id=self.config.broker_profile_id, epoch=1)
        # Seed only worker authority state. No verified dataset or executable
        # financial engine is claimed; the test exercises operator orchestration.
        self.worker.writer.call(lambda connection: connection.execute(
            "INSERT INTO cf_authorities(authority_id,effective_context,effective_config,effective_revision,transition_epoch,engine_state,armed,admissions_fenced) VALUES(?,?,?,?,?,'ACTIVE',1,0)",
            (self.config.authority_id, self.context.model_dump_json(), self.config.model_dump_json(), 1, 1),
        ).rowcount)
        self.worker.slots.close()
        self.driver = FixtureSlotDriver(self.worker.writer)
        self.worker.slots = SlotService(self.worker.writer, self.worker.interlock, self.driver, on_change=self.worker._publish)

    def tearDown(self) -> None:
        for gate in self.driver.release.values():
            gate.set()
        self.worker.close()
        self.temp.cleanup()

    def active(self, slot_id: str, instrument: str) -> SlotRecord:
        self.worker.slots.create(slot_id=slot_id, context=self.context, scope_id=self.config.execution_scope[0], instrument=instrument,
                                 expected_config_revision=1, request_id="create-"+slot_id)
        return self.worker.slots.start(slot_id, expected_revision=1, expected_epoch=1, request_id="start-"+slot_id)

    def stop(self, request_id: str = "global-stop") -> dict[str, Any]:
        request = RPCRequest.parse({"jsonrpc": "2.0", "id": request_id, "method": "safety.stop", "params": {},
                                    "epoch": 1, "deadline_ms": int(time.time() * 1000)+10000})
        return self.worker.handle(request, PeerIdentity(uid=0, gid=0, pid=os.getpid()))

    def assert_paused_without_creates(self) -> None:
        status = self.worker.coordinator.status(self.config.authority_id)
        self.assertFalse(status.armed)
        self.assertTrue(status.admissions_fenced)
        count = self.worker.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0])
        self.assertEqual(count, 0)

    def check_pending_operation(self, action: str) -> None:
        source = self.active("a-source", "BTC-USD")
        other = self.active("b-independent", "SOL-USD")
        self.driver.release[source.slot_id] = threading.Event()
        self.driver.entered[source.slot_id] = threading.Event()
        if action == "EDIT":
            operation = self.worker.slots.edit(source.slot_id, expected_revision=source.revision, expected_epoch=1,
                                               instrument="ETH-USD", request_id="edit-source")
            self.assertIsInstance(operation, SlotOperation)
        else:
            operation = self.worker.slots.delete(source.slot_id, expected_revision=source.revision, expected_epoch=1, request_id="delete-source")
        self.assertTrue(self.driver.entered[source.slot_id].wait(2))
        result = self.stop()
        by_slot = {value["slot_id"]: value for value in result["operations"]}
        self.assertEqual(result["errors"], [])
        self.assertEqual(by_slot[source.slot_id]["operation_id"], operation.operation_id)
        self.assertEqual(by_slot[source.slot_id]["action"], action)
        self.assertEqual(by_slot[other.slot_id]["action"], "STOP")
        self.assertEqual(self.worker.slots.wait(by_slot[other.slot_id]["operation_id"]).phase, SlotOperationPhase.COMPLETED)
        self.driver.release[source.slot_id].set()
        complete = self.worker.slots.wait(operation.operation_id)
        self.assertEqual(complete.phase, SlotOperationPhase.COMPLETED)
        self.assertTrue(self.worker.slots.get(source.slot_id).tombstoned)
        if action == "EDIT":
            replacement = self.worker.slots.get(complete.replacement_slot_id)
            self.assertEqual(replacement.engine_state, EngineState.STOPPED)
            self.assertFalse(replacement.scheduler_enabled)
            self.assertNotIn(replacement.slot_id, self.driver.started)
        self.assert_paused_without_creates()

    def test_global_stop_joins_pending_edit_and_stops_other_slots(self) -> None:
        self.check_pending_operation("EDIT")

    def test_global_stop_joins_pending_delete_and_stops_other_slots(self) -> None:
        self.check_pending_operation("DELETE")

    def test_blocked_unknown_source_is_retried_without_hiding_remaining_slots(self) -> None:
        source = self.active("a-source", "BTC-USD")
        other = self.active("b-independent", "SOL-USD")
        self.driver.unknown.add(source.slot_id)
        pending = self.worker.slots.stop(source.slot_id, expected_revision=source.revision, expected_epoch=1, request_id="stop-source")
        self.assertEqual(self.worker.slots.wait(pending.operation_id).phase, SlotOperationPhase.BLOCKED_DRAIN)
        result = self.stop()
        by_slot = {value["slot_id"]: value for value in result["operations"]}
        self.assertEqual(by_slot[source.slot_id]["operation_id"], pending.operation_id)
        self.assertEqual(self.worker.slots.wait(pending.operation_id).phase, SlotOperationPhase.BLOCKED_DRAIN)
        self.assertEqual(self.worker.slots.wait(by_slot[other.slot_id]["operation_id"]).phase, SlotOperationPhase.COMPLETED)
        self.assertEqual(self.worker.slots.get(source.slot_id).engine_state, EngineState.DRAINING)
        self.assertFalse(self.worker.slots.get(source.slot_id).tombstoned)
        self.assertIn(source.slot_id, [slot.slot_id for slot in self.worker.slots.list(self.context)])
        self.assert_paused_without_creates()

    def test_failed_slot_command_is_recorded_and_does_not_abort_other_stops(self) -> None:
        source = self.active("a-failed", "BTC-USD")
        other = self.active("b-independent", "SOL-USD")
        actual_stop = self.worker.slots.stop
        def injected_stop(slot_id: str, **kwargs: Any) -> SlotOperation:
            if slot_id == source.slot_id:
                raise DomainError("INJECTED_COMMAND_REJECTION")
            return actual_stop(slot_id, **kwargs)
        with patch.object(self.worker.slots, "stop", side_effect=injected_stop):
            result = self.stop()
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["slot_id"], source.slot_id)
        self.assertEqual(result["errors"][0]["code"], "INJECTED_COMMAND_REJECTION")
        self.assertTrue(result["errors"][0]["recorded"])
        self.assertEqual(result["operations"][0]["slot_id"], other.slot_id)
        self.assertEqual(self.worker.slots.wait(result["operations"][0]["operation_id"]).phase, SlotOperationPhase.COMPLETED)
        self.assertEqual(self.worker.slots.get(source.slot_id).engine_state, EngineState.ACTIVE)
        alarms = self.worker.writer.call(lambda connection: [dict(row) for row in connection.execute(
            "SELECT entity_id,reason,acknowledged FROM ex_alarms WHERE reason='INJECTED_COMMAND_REJECTION'",
        )])
        self.assertEqual(alarms, [{"entity_id": source.slot_id, "reason": "INJECTED_COMMAND_REJECTION", "acknowledged": 0}])
        self.assert_paused_without_creates()

    def test_invalidated_tombstone_reports_recovery_error_and_other_slots_stop(self) -> None:
        source = self.active("a-deleted", "BTC-USD")
        other = self.active("b-independent", "SOL-USD")
        deleted = self.worker.slots.delete(source.slot_id, expected_revision=source.revision, expected_epoch=1, request_id="delete-source")
        self.worker.slots.wait(deleted.operation_id)
        # The reducer uses this state when a newly discovered old fill invalidates
        # a tombstoned slot. Global Stop must keep its recovery need visible.
        self.worker.writer.call(lambda connection: connection.execute("UPDATE cf_slots SET stop_proof=NULL WHERE slot_id=?", (source.slot_id,)).rowcount)
        result = self.stop()
        self.assertEqual(result["errors"][0]["code"], "TOMBSTONED_SLOT_REQUIRES_RECOVERY")
        self.assertTrue(result["errors"][0]["tombstoned"])
        self.assertTrue(result["errors"][0]["recorded"])
        operation = next(value for value in result["operations"] if value["slot_id"] == other.slot_id)
        self.assertEqual(self.worker.slots.wait(operation["operation_id"]).phase, SlotOperationPhase.COMPLETED)
        self.assert_paused_without_creates()


class WorkerPlatformGateTests(unittest.TestCase):
    def test_windows_enters_the_native_worker_launcher(self) -> None:
        from coinfly.worker import supervisor
        with patch.object(supervisor.sys, "platform", "win32"), patch.object(supervisor, "_run_native_worker", return_value=0) as runner:
            self.assertEqual(supervisor.main(), 0)
        runner.assert_called_once_with()
