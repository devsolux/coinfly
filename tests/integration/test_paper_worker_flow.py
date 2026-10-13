"""Paper integration with explicit fixture weights and a test-only dataset sentinel.

The injected readiness sentinel is NOT a verified MaleCNS dataset, biological
model output, signed bootstrap attestation, or native/live certification. These
tests exercise the production paper lifecycle and financial path in isolation.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase, skipIf

from coinfly.accounting.journal import Journal
from coinfly.bootstrap.installer import VerifiedDataset
from coinfly.bootstrap.manager import BootstrapManager, BootstrapProgress, BootstrapState
from coinfly.config.models import EngineConfig, RiskLimits, default_config
from coinfly.control.slot_service import SlotOperationPhase
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import DomainError, EngineState, Environment, ProductTrack, RegionPolicy
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Submission
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCError, RPCRequest
from coinfly.ipc.transport_unix import UnixRPCClient
from coinfly.simulation.paper_matching import DepthLevel
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.target_coordinator import BoundTarget, TargetCoordinator
from coinfly.worker.paper_engine import LocalPaperEngine
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor

_DATASET_TEST_SENTINEL = object()


class TestOnlyBootstrap(BootstrapManager):
    __test__ = False

    def __init__(self, home: Path) -> None:
        super().__init__(home=home)
        self.progress = BootstrapProgress(BootstrapState.READY, reason="TEST_SENTINEL_NOT_CERTIFIED")

    def verified_dataset(self) -> VerifiedDataset:
        return cast(VerifiedDataset, _DATASET_TEST_SENTINEL)

    async def check(self) -> BootstrapProgress:
        return self.progress


class WorkerFixture(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = WorkerPaths(self.root / "worker")
        self.worker = self.new_worker()

    def new_worker(self, *, verified: bool = True) -> WorkerSupervisor:
        bootstrap = TestOnlyBootstrap(self.root) if verified else BootstrapManager(home=self.root)
        worker = WorkerSupervisor(
            paths=self.paths, socket_path=self.root / "worker.sock", bootstrap=bootstrap
        )
        self.addCleanup(worker.close)
        return worker

    def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        epoch: int | None = None,
    ) -> dict[str, Any]:
        context_epoch = (
            self.worker.coordinator.status(self.worker.authority_id).transition_epoch
            if epoch is None
            else epoch
        )
        request = RPCRequest.parse(
            {
                "jsonrpc": "2.0",
                "id": request_id or method + "-test",
                "method": method,
                "params": params or {},
                "epoch": context_epoch,
                "deadline_ms": int(time.time() * 1000) + 10000,
            }
        )
        result = self.worker.handle(
            request,
            PeerIdentity(
                uid=os.getuid() if hasattr(os, "getuid") else 0,
                gid=os.getgid() if hasattr(os, "getgid") else 0,
                pid=os.getpid(),
            ),
        )
        self.assertIsInstance(result, dict)
        return cast(dict[str, Any], result)

    def initialize(
        self, config: EngineConfig | None = None, *, request_prefix: str = "initial"
    ) -> tuple[LocalPaperEngine, dict[str, Any]]:
        selected = config or default_config()
        status = self.worker.coordinator.status(self.worker.authority_id)
        revisions = {
            "expected_revision": status.effective_revision,
            "expected_epoch": status.transition_epoch,
        }
        preview = self.rpc(
            "context.preview",
            {"config": selected.model_dump(mode="json"), **revisions},
            request_id=request_prefix + "-preview",
        )
        parameters = {
            "preview_id": preview["preview_id"],
            "config_digest": preview["config_digest"],
            "confirmation": True,
            **revisions,
        }
        accepted = self.rpc("context.apply", parameters, request_id=request_prefix + "-apply")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())
        state = self.rpc("state.snapshot")
        self.assertFalse(state["armed"])
        self.assertEqual(state["effective_context"]["environment"], "SANDBOX")
        repeat = self.rpc(
            "context.apply",
            parameters,
            request_id=request_prefix + "-apply",
            epoch=revisions["expected_epoch"],
        )
        self.assertEqual(repeat["operation_id"], accepted["operation_id"])
        engine = self.worker._engines[final.target_context.identity]
        self.assertIsInstance(engine, LocalPaperEngine)
        return cast(LocalPaperEngine, engine), accepted

    def arm(self) -> None:
        state = self.rpc("state.snapshot")
        revisions = {"expected_revision": state["config_revision"], "expected_epoch": state["context_epoch"]}
        result = self.rpc(
            "engine.arm", {**revisions, "confirmation": True}, request_id=f"arm-{state['context_epoch']}"
        )
        self.assertTrue(result["armed"])
        self.assertFalse(result["admissions_fenced"])

    def open_slot(self, instrument: str = "BTC-USD") -> str:
        state = self.rpc("state.snapshot")
        params = {"instrument": instrument, "expected_revision": state["config_revision"]}
        created = self.rpc("slot.create", params, request_id=f"create-btc-{state['context_epoch']}")
        repeated = self.rpc("slot.create", params, request_id=f"create-btc-{state['context_epoch']}")
        self.assertEqual(repeated, created)
        started = self.rpc(
            "slot.start",
            {
                "slot_id": created["slot_id"],
                "expected_revision": created["revision"],
                "expected_epoch": state["context_epoch"],
            },
            request_id=f"start-btc-{state['context_epoch']}",
        )
        self.assertEqual(started["engine_state"], "ACTIVE")
        context = self.worker.coordinator.status(self.worker.authority_id).effective_context
        assert context is not None
        self.assertEqual(len(self.worker.slots.list(context)), 1)
        return str(created["slot_id"])

    @staticmethod
    def market(engine: LocalPaperEngine, instrument: str, bid: str, ask: str) -> None:
        engine.update_market(
            instrument,
            bids=(DepthLevel(price=Decimal(bid), quantity=Decimal("1000")),),
            asks=(DepthLevel(price=Decimal(ask), quantity=Decimal("1000")),),
            now=datetime.now(timezone.utc),
        )

    def buy_fixture(self, engine: LocalPaperEngine) -> str:
        self.market(engine, "BTC-USD", "99.90", "100.10")
        self.market(engine, "ETH-USD", "49.90", "50.10")
        self.market(engine, "SOL-USD", "9.90", "10.10")
        bound = self.target(engine, "0.05", revision=1)
        identifiers = engine.submit_target(bound)
        self.assertEqual(len(identifiers), 1)
        order = IntentStore(self.worker.writer).get(identifiers[0])
        self.assertEqual(order.submission, Submission.ACKNOWLEDGED)
        # The limit rests inside the spread. A subsequent trusted fixture book
        # crosses it; no scripted trade is presented as a neural decision.
        self.market(engine, "BTC-USD", "99.98", "99.99")
        record = IntentStore(self.worker.writer).get(identifiers[0])
        self.assertEqual(record.filled_quantity, Decimal("0.05"))
        self.assertTrue(record.terminal)
        self.assertEqual(record.reservation_remaining, Decimal(0))
        Journal(self.worker.writer).verify()
        return identifiers[0]

    def target(self, engine: LocalPaperEngine, btc_weight: str, *, revision: int) -> BoundTarget:
        now = datetime.now(timezone.utc)
        coordinator = TargetCoordinator(engine.context, ("BTC-USD", "ETH-USD", "SOL-USD"))
        coordinator.set_snapshot("fixture-market-1", now, warm=True)
        proposal = TargetWeights(affected_instruments=("BTC-USD",),
            decision_id=f"fixture-decision-{revision}",
            model_id="integration-fixture-no-neural-claim",
            model_version="1",
            snapshot_id="fixture-market-1",
            allocation_revision=revision,
            expires_at=now + timedelta(seconds=5),
            weights=(
                InstrumentWeight(instrument_id="BTC-USD", weight=Decimal(btc_weight)),
                InstrumentWeight(instrument_id="ETH-USD", weight=Decimal(0)),
                InstrumentWeight(instrument_id="SOL-USD", weight=Decimal(0)),
            ),
        )
        bound = coordinator.bind(proposal, expected_epoch=engine.context.epoch, now=now)
        self.assertIsInstance(bound, BoundTarget)
        return cast(BoundTarget, bound)


class PaperWorkerFlowTests(WorkerFixture):
    def test_slot_edit_drains_original_binding_and_replays_durable_replacement(self) -> None:
        engine, _ = self.initialize()
        self.arm()
        source_id = self.open_slot()
        self.buy_fixture(engine)
        self.market(engine, "BTC-USD", "110.00", "110.02")
        source = self.worker.slots.get(source_id)
        self.assertEqual(engine.matcher.balances["BTC"], Decimal("0.05"))
        journal = Journal(self.worker.writer)
        before_cash = journal.balance(engine.context, engine.scope, "assets:cash", "USD")
        before_risk = engine.admission.drawdown.read(engine.context, engine.scope)
        assert before_risk is not None
        fills_before = len(engine.matcher.fills)
        journals_before = self.worker.writer.call(
            lambda connection: int(connection.execute("SELECT COUNT(*) FROM ex_journals").fetchone()[0])
        )
        parameters = {
            "slot_id": source_id,
            "expected_revision": source.revision,
            "expected_epoch": engine.context.epoch,
            "changes": {"instrument": "ETH-USD"},
        }
        request_id = "edit-btc-to-eth-durable"
        requested = self.rpc("slot.edit", parameters, request_id=request_id)
        self.assertEqual(requested["action"], "EDIT")
        self.assertEqual(requested["slot_id"], source_id)
        self.assertEqual(requested["replacement_instrument"], "ETH-USD")
        self.assertNotEqual(requested["replacement_slot_id"], source_id)
        operation_id = str(requested["operation_id"])
        receipt = self.worker.writer.call(
            lambda connection: dict(
                connection.execute(
                    "SELECT action,slot_id,operation_id FROM cf_slot_receipts WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            )
        )
        self.assertEqual(receipt, {"action": "EDIT", "slot_id": source_id, "operation_id": operation_id})

        completed = self.worker.slots.wait(operation_id, timeout=10)
        self.assertEqual(completed.phase, SlotOperationPhase.COMPLETED, completed.model_dump_json())
        final = self.rpc("operation.get", {"operation_id": operation_id}, request_id="query-slot-edit")
        self.assertEqual(final, completed.model_dump(mode="json"))
        original = self.worker.slots.get(source_id)
        self.assertTrue(original.tombstoned)
        self.assertEqual(original.engine_state, EngineState.STOPPED)
        self.assertFalse(original.scheduler_enabled)
        assert completed.replacement_slot_id is not None
        replacement = self.worker.slots.get(completed.replacement_slot_id)
        self.assertEqual(replacement.instrument, "ETH-USD")
        self.assertEqual(replacement.engine_state, EngineState.STOPPED)
        self.assertFalse(replacement.scheduler_enabled)
        self.assertFalse(replacement.tombstoned)
        self.assertEqual(replacement.context, engine.context)
        self.assertEqual(self.worker.slots.list(engine.context), (replacement,))
        self.assertEqual(engine.matcher.balances["BTC"], Decimal(0))
        self.assertEqual(engine.matcher.balances.get("ETH", Decimal(0)), Decimal(0))
        self.assertFalse(engine.matcher.open_orders)
        self.assertEqual(len(engine.matcher.fills), fills_before + 1)
        sell = engine.matcher.fills[-1]
        expected_cash = before_cash + sell.quantity * sell.price - sell.fee
        self.assertEqual(journal.balance(engine.context, engine.scope, "assets:cash", "USD"), expected_cash)
        self.assertEqual(engine.matcher.balances["USD"], expected_cash)
        after_risk = engine.admission.drawdown.read(engine.context, engine.scope)
        assert after_risk is not None
        matched_result = sell.quantity * (sell.price - engine.matcher.fills[0].price)
        self.assertEqual(
            after_risk.daily_net_result, before_risk.daily_net_result + matched_result - sell.fee
        )
        self.assertEqual(
            self.worker.writer.call(
                lambda connection: int(connection.execute("SELECT COUNT(*) FROM ex_journals").fetchone()[0])
            ),
            journals_before + 1,
        )
        journal.verify()
        closed_report = self.rpc(
            "history.open",
            {"environment": "SANDBOX", "mode": "TRADE_RESULTS"},
            request_id="closed-edit-report",
        )
        expected_net = matched_result - sum((fill.fee for fill in engine.matcher.fills), Decimal(0))
        self.assertEqual(closed_report["total_count"], 1)
        self.assertEqual(Decimal(closed_report["totals"]["net"]), expected_net)
        self.assertEqual(Decimal(closed_report["rows"][0]["closed_net"]), expected_net)

        replay = self.rpc("slot.edit", parameters, request_id=request_id)
        self.assertEqual(replay, final)
        self.assertEqual(len(engine.matcher.fills), fills_before + 1)
        self.assertEqual(engine.matcher.balances["USD"], expected_cash)
        self.assertEqual(len(self.worker.slots.list(engine.context, include_tombstones=True)), 2)
        self.worker.close()
        self.worker = self.new_worker()
        reopened = self.rpc("slot.edit", parameters, request_id=request_id)
        self.assertEqual(reopened, final)
        self.assertEqual(
            Journal(self.worker.writer).balance(engine.context, engine.scope, "assets:cash", "USD"),
            expected_cash,
        )
        self.assertEqual(self.worker.slots.get(completed.replacement_slot_id), replacement)
        reopened_report = self.rpc(
            "history.open",
            {"environment": "SANDBOX", "mode": "TRADE_RESULTS"},
            request_id="closed-edit-report-after-restart",
        )
        self.assertEqual(reopened_report["total_count"], 1)
        self.assertEqual(reopened_report["totals"], closed_report["totals"])
        self.assertEqual(
            self.worker.writer.call(
                lambda connection: int(
                    connection.execute("SELECT COUNT(*) FROM cf_slot_operations").fetchone()[0]
                )
            ),
            1,
        )

    def test_observer_failure_cannot_undo_a_committed_paper_fill(self) -> None:
        engine, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.market(engine, "BTC-USD", "99.90", "100.10")
        self.market(engine, "ETH-USD", "49.90", "50.10")
        self.market(engine, "SOL-USD", "9.90", "10.10")
        identifiers = engine.submit_target(self.target(engine, "0.05", revision=1))
        failures = []

        def broken_observer() -> None:
            failures.append(1)
            raise DomainError("WRITER_BACKPRESSURE")

        engine._on_change = broken_observer
        # The crossing book commits the fill durably; the failing publication
        # observer must neither raise into the caller nor roll back the ledger.
        self.market(engine, "BTC-USD", "99.98", "99.99")
        self.assertGreaterEqual(len(failures), 1)
        record = IntentStore(self.worker.writer).get(identifiers[0])
        self.assertEqual(record.filled_quantity, Decimal("0.05"))
        self.assertTrue(record.terminal)
        Journal(self.worker.writer).verify()

    def test_weights_risk_outbox_fills_then_protected_switch_preserves_money(self) -> None:
        engine, _ = self.initialize()
        self.assertEqual(engine.matcher.balances, {"USD": Decimal(100)})
        with self.assertRaises(DomainError):
            self.rpc(
                "engine.arm", {"expected_revision": 1, "expected_epoch": 1}, request_id="unconfirmed-arm"
            )
        self.arm()
        self.open_slot()
        identifier = self.buy_fixture(engine)
        self.market(engine, "BTC-USD", "110.00", "110.02")
        next_config = default_config().model_copy(update={"region_policy": RegionPolicy.GLOBAL})
        target, operation = self.initialize(next_config, request_prefix="switch")
        self.assertEqual(target.context.epoch, engine.context.epoch + 1)
        self.assertEqual(engine.matcher.balances.get("BTC"), Decimal(0))
        self.assertFalse(engine.matcher.open_orders)
        expected_net = Decimal("0.05") * (Decimal("110") - Decimal("99.99")) - Decimal("0.05") * (
            Decimal("110") + Decimal("99.99")
        ) * Decimal("0.001")
        self.assertEqual(target.matcher.balances["USD"], Decimal(100) + expected_net)
        self.assertEqual(target.matcher.balances, engine.matcher.balances)
        phases = self.worker.coordinator.phase_history(operation["operation_id"])
        self.assertLess(
            phases.index(TransitionPhase.STOPPED_OLD), phases.index(TransitionPhase.INITIALIZING_NEW)
        )
        self.assertFalse(self.rpc("state.snapshot")["armed"])

        def check(connection: sqlite3.Connection) -> tuple[int, int, list[dict[str, Any]]]:
            live_count = connection.execute(
                "SELECT COUNT(*) FROM ex_intents WHERE environment='LIVE'"
            ).fetchone()[0]
            seeds = connection.execute(
                "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'"
            ).fetchone()[0]
            positions = [
                dict(row)
                for row in connection.execute("SELECT quantity,realized_pnl,fees_quote FROM ex_positions")
            ]
            return int(live_count), int(seeds), positions

        live_count, seeds, positions = self.worker.writer.call(check)
        self.assertEqual(live_count, 0)
        self.assertEqual(seeds, 1)
        self.assertEqual(Decimal(positions[0]["quantity"]), Decimal(0))
        self.assertEqual(
            Decimal(positions[0]["realized_pnl"]) - Decimal(positions[0]["fees_quote"]), expected_net
        )
        risk_state = target.admission.drawdown.read(target.context, target.scope)
        self.assertIsNotNone(risk_state)
        assert risk_state is not None
        self.assertEqual(risk_state.daily_net_result, expected_net)
        self.assertEqual(IntentStore(self.worker.writer).get(identifier).filled_quantity, Decimal("0.05"))
        Journal(self.worker.writer).verify()
        history = self.rpc(
            "history.open",
            {"environment": "SANDBOX", "mode": "ALL_EVENTS"},
            request_id="history-after-switch",
        )
        self.assertTrue(history["frozen"])
        self.assertEqual(len(history["rows"]), 10)
        self.assertGreater(history["total_count"], 10)
        closed_report = self.rpc(
            "history.open",
            {"environment": "SANDBOX", "mode": "TRADE_RESULTS"},
            request_id="closed-report-after-switch",
        )
        self.assertEqual(closed_report["total_count"], 1)
        self.assertEqual(Decimal(closed_report["totals"]["profit"]), expected_net)
        self.assertEqual(Decimal(closed_report["totals"]["loss"]), Decimal(0))
        self.assertEqual(Decimal(closed_report["totals"]["net"]), expected_net)
        self.assertEqual(Decimal(closed_report["rows"][0]["closed_net"]), expected_net)

    def test_restart_recovers_exact_nonflat_paper_book_and_never_reseeds_or_rearms(self) -> None:
        engine, _ = self.initialize()
        self.arm()
        self.open_slot()
        identifier = self.buy_fixture(engine)
        expected = engine.matcher.dump_state()
        fact_count = self.worker.writer.call(
            lambda connection: int(connection.execute("SELECT COUNT(*) FROM ex_fills").fetchone()[0])
        )
        cash = Journal(self.worker.writer).balance(engine.context, engine.scope, "assets:cash", "USD")
        self.worker.close()
        self.worker = self.new_worker()
        status = self.worker.coordinator.status(self.worker.authority_id)
        self.assertEqual(status.effective_context, engine.context)
        self.assertFalse(status.armed)
        self.assertTrue(status.admissions_fenced)
        recovered = cast(LocalPaperEngine, self.worker._engines[engine.context.identity])
        restored_state = recovered.matcher.dump_state()
        self.assertEqual(restored_state["books"], [])
        self.assertEqual(
            {key: value for key, value in restored_state.items() if key != "books"},
            {key: value for key, value in expected.items() if key != "books"},
        )
        self.assertEqual(
            Journal(self.worker.writer).balance(engine.context, engine.scope, "assets:cash", "USD"), cash
        )
        self.assertEqual(IntentStore(self.worker.writer).get(identifier).filled_quantity, Decimal("0.05"))
        self.assertEqual(
            self.worker.writer.call(
                lambda connection: int(connection.execute("SELECT COUNT(*) FROM ex_fills").fetchone()[0])
            ),
            fact_count,
        )
        self.assertEqual(
            self.worker.writer.call(
                lambda connection: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'"
                    ).fetchone()[0]
                )
            ),
            1,
        )
        Journal(self.worker.writer).verify()

    def test_spot_perpetual_spot_switches_have_one_effective_unarmed_context(self) -> None:
        original, _ = self.initialize()
        perpetual = default_config().model_copy(
            update={
                "region_policy": RegionPolicy.GLOBAL,
                "product_track": ProductTrack.PERPETUAL,
                "execution_scope": ("paper-perpetual-book",),
            }
        )
        futures, _ = self.initialize(perpetual, request_prefix="perpetual")
        self.assertEqual(futures.context.product_track, ProductTrack.PERPETUAL)
        self.assertIsNotNone(futures.matcher.margin)
        self.assertFalse(self.rpc("state.snapshot")["armed"])
        returned, _ = self.initialize(default_config(), request_prefix="back-to-spot")
        self.assertEqual(returned.context.product_track, ProductTrack.SPOT)
        self.assertEqual(returned.matcher.balances, original.matcher.balances)
        self.assertEqual(returned.matcher.balances["USD"], Decimal(100))
        self.assertEqual(
            self.worker.writer.call(
                lambda connection: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'"
                    ).fetchone()[0]
                )
            ),
            2,
        )

    def test_same_paper_scope_cannot_be_reseeded_into_a_different_track(self) -> None:
        self.initialize()
        invalid = default_config().model_copy(
            update={"region_policy": RegionPolicy.GLOBAL, "product_track": ProductTrack.PERPETUAL}
        )
        with self.assertRaisesRegex(DomainError, "PAPER_TRACK_REQUIRES_SEPARATE_BOOK"):
            self.rpc(
                "context.preview",
                {"config": invalid.model_dump(mode="json"), "expected_revision": 1, "expected_epoch": 1},
            )

    def test_realized_daily_loss_blocks_entries_after_restart_and_context_switch(self) -> None:
        limits = RiskLimits(max_daily_loss=Decimal("0.01"), max_drawdown_fraction=Decimal("0.90"))
        config = default_config().model_copy(update={"limits": limits})
        engine, _ = self.initialize(config)
        self.arm()
        self.open_slot()
        self.buy_fixture(engine)
        self.market(engine, "BTC-USD", "89.99", "90.01")
        closing = engine.submit_target(self.target(engine, "0", revision=2))
        self.assertEqual(len(closing), 1)
        self.market(engine, "BTC-USD", "90.00", "90.01")
        self.assertTrue(IntentStore(self.worker.writer).get(closing[0]).terminal)
        before = engine.admission.drawdown.read(engine.context, engine.scope)
        self.assertIsNotNone(before)
        assert before is not None
        expected_loss = Decimal("0.05") * (Decimal("90") - Decimal("99.99")) - Decimal("0.05") * (
            Decimal("90") + Decimal("99.99")
        ) * Decimal("0.001")
        self.assertEqual(before.daily_net_result, expected_loss)
        with self.assertRaisesRegex(DomainError, "DAILY_LOSS_LIMIT"):
            engine.submit_target(self.target(engine, "0.05", revision=3))
        before = engine.admission.drawdown.read(engine.context, engine.scope)
        self.worker.close()
        self.worker = self.new_worker()
        recovered = cast(LocalPaperEngine, self.worker._engines[engine.context.identity])
        after = recovered.admission.drawdown.read(engine.context, engine.scope)
        self.assertEqual(after, before)
        next_config = config.model_copy(update={"region_policy": RegionPolicy.GLOBAL})
        current, _ = self.initialize(next_config, request_prefix="loss-recovery-switch")
        self.arm()
        self.open_slot()
        self.market(current, "BTC-USD", "90.00", "90.02")
        self.market(current, "ETH-USD", "49.99", "50.01")
        self.market(current, "SOL-USD", "9.99", "10.01")
        with self.assertRaisesRegex(DomainError, "DAILY_LOSS_LIMIT"):
            current.submit_target(self.target(current, "0.05", revision=4))

    def test_missing_dataset_keeps_state_and_history_commands_available(self) -> None:
        from coinfly.bootstrap.manifest import BootstrapError

        self.worker.close()
        self.worker = self.new_worker(verified=False)
        state = self.rpc("state.snapshot")
        self.assertFalse(state["bootstrap"]["ready"])
        preview = self.rpc(
            "context.preview",
            {"config": default_config().model_dump(mode="json"), "expected_revision": 0, "expected_epoch": 0},
        )
        with self.assertRaisesRegex(BootstrapError, "DATASET_NOT_READY"):
            self.rpc(
                "context.apply",
                {
                    "preview_id": preview["preview_id"],
                    "config_digest": preview["config_digest"],
                    "expected_revision": 0,
                    "expected_epoch": 0,
                    "confirmation": True,
                },
                request_id="blocked-apply",
            )
        self.assertIsNone(self.rpc("state.snapshot")["effective_context"])
        history = self.rpc("history.open", {"environment": "SANDBOX", "mode": "ALL_EVENTS"})
        page = self.rpc("history.page", {"snapshot_id": history["snapshot_id"]})
        self.assertTrue(page["frozen"])
        self.assertEqual(page["snapshot_id"], history["snapshot_id"])
        self.rpc("history.close", {"snapshot_id": history["snapshot_id"]})

    @skipIf(os.name == "nt", "Windows named-pipe/native qualification remains an explicit release gate")
    def test_missing_real_dataset_blocks_apply_while_state_and_history_ipc_work(self) -> None:
        self.worker.close()
        self.worker = self.new_worker(verified=False)
        try:
            self.worker.start()
        except PermissionError as error:
            if error.errno != errno.EPERM:
                raise
            self.skipTest(
                "This execution sandbox prohibits creating AF_UNIX sockets; native IPC runtime test requires a permitted host"
            )
        from coinfly.observability.diagnostics import build_identity

        # A commanding client binds itself to the worker's protocol/build identity first.
        with UnixRPCClient(self.root / "worker.sock", identity=dict(build_identity())) as client:
            state = client.call("state.snapshot")
            self.assertFalse(state["bootstrap"]["ready"])
            self.assertIsNone(state["effective_context"])
            preview = client.call(
                "context.preview",
                {
                    "config": default_config().model_dump(mode="json"),
                    "expected_revision": 0,
                    "expected_epoch": 0,
                },
            )
            with self.assertRaisesRegex(RPCError, "DATASET_NOT_READY"):
                client.call(
                    "context.apply",
                    {
                        "preview_id": preview["preview_id"],
                        "config_digest": preview["config_digest"],
                        "expected_revision": 0,
                        "expected_epoch": 0,
                        "confirmation": True,
                    },
                    request_id="blocked-apply",
                )
            after = client.call("state.snapshot")
            self.assertIsNone(after["effective_context"])
            self.assertFalse(after["armed"])
            history = client.call(
                "history.open", {"environment": Environment.SANDBOX.value, "mode": "ALL_EVENTS"}
            )
            self.assertEqual(history["page_size"], 10)
            page = client.call("history.page", {"snapshot_id": history["snapshot_id"]})
            self.assertEqual(page["snapshot_id"], history["snapshot_id"])
            self.assertTrue(page["frozen"])
            client.call("history.close", {"snapshot_id": history["snapshot_id"]})
