"""Production paper path driven by explicit synthetic fixtures only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event
from unittest.mock import patch

from coinfly.domain.context import DomainError
from coinfly.execution.recovery import context_fact_sequence_tx
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.simulation.paper_matching import DepthLevel
from tests.integration.test_paper_worker_flow import WorkerFixture


class WorkerAuditTests(WorkerFixture):
    def test_completed_transitions_release_old_engines_and_future_records(self):
        for index in range(8):
            engine, _ = self.initialize(request_prefix=f"switch-{index}")
            with self.worker.interlock.lock:
                assert set(self.worker._engines) == {engine.context.identity}
                assert not self.worker.coordinator._futures

    def test_terminal_order_replay_does_not_invalidate_fact_sequence(self):
        engine, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.buy_fixture(engine)
        def sequence():
            return self.worker.writer.call(lambda c: context_fact_sequence_tx(c, engine.context))
        before = sequence()
        for _ in range(20):
            self.market(engine, "BTC-USD", "99.98", "99.99")
        assert sequence() == before

    def test_snapshot_and_market_ingestion_do_not_deadlock(self):
        engine, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.buy_fixture(engine)
        def ingest():
            for _ in range(50):
                self.market(engine, "BTC-USD", "99.98", "99.99")
        def snapshot():
            for _ in range(50):
                state = self.worker.snapshot()
                assert state["effective_context"] == engine.context.model_dump(mode="json")
                assert state["balance"]["simulated"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(ingest), pool.submit(snapshot)
            first.result(timeout=10)
            second.result(timeout=10)

    def test_failed_market_match_cannot_leave_uncommitted_memory_state(self):
        engine, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.market(engine, "BTC-USD", "99.90", "100.10")
        self.market(engine, "ETH-USD", "49.90", "50.10")
        self.market(engine, "SOL-USD", "9.90", "10.10")
        engine.submit_target(self.target(engine, "0.05", revision=1))
        before = engine.matcher.dump_state()
        original = engine.matcher.update_book
        def fail_after_matching(*args, **kwargs):
            original(*args, **kwargs)
            raise DomainError("SYNTHETIC_MATCH_INTERRUPTED")
        with patch.object(engine.matcher, "update_book", side_effect=fail_after_matching):
            with self.assertRaisesRegex(DomainError, "SYNTHETIC_MATCH_INTERRUPTED"):
                self.market(engine, "BTC-USD", "99.98", "99.99")
        after = engine.matcher.dump_state()
        assert {k: v for k, v in after.items() if k != "books"} == {
            k: v for k, v in before.items() if k != "books"}
        assert self.worker.interlock.is_provisionally_fenced(engine.context.authority_id)
        assert self.worker.writer.call(lambda c: c.execute("SELECT COUNT(*) FROM ex_fills").fetchone()[0]) == 0

    def test_context_and_slot_drains_share_one_planner(self):
        engine, _ = self.initialize()
        self.arm()
        slot_id = self.open_slot()
        slot = self.worker.slots.get(slot_id)
        entered, release, second_entered = Event(), Event(), Event()
        calls = []
        def plan(policy, selected=None):
            calls.append(selected)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            else:
                second_entered.set()
        with patch.object(engine, "_close_residuals", side_effect=plan):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(engine.drain_slot, slot, DrainPolicy())
                assert entered.wait(5)
                second = pool.submit(engine.drain, DrainPolicy())
                try:
                    assert not second_entered.wait(.1)
                finally:
                    release.set()
                first.result(timeout=5)
                second.result(timeout=5)
        assert len(calls) == 2

    def test_slot_completion_releases_future_and_replays_receipt(self):
        engine, _ = self.initialize()
        self.arm()
        slot_id = self.open_slot()
        slot = self.worker.slots.get(slot_id)
        params = dict(expected_revision=slot.revision, expected_epoch=engine.context.epoch, request_id="stop-audit")
        operation = self.worker.slots.stop(slot_id, **params)
        complete = self.worker.slots.wait(operation.operation_id)
        assert complete.phase.value == "COMPLETED"
        with self.worker.interlock.lock:
            assert not self.worker.slots._futures
        assert self.worker.slots.stop(slot_id, **params).operation_id == operation.operation_id

    def test_invalid_market_input_restores_clock_without_any_fill(self):
        engine, _ = self.initialize()
        before = engine.matcher.dump_state()
        with self.assertRaisesRegex(DomainError, "CROSSED_BOOK"):
            engine.update_market("BTC-USD", bids=(DepthLevel(price=Decimal(101), quantity=Decimal(1)),),
                                 asks=(DepthLevel(price=Decimal(100), quantity=Decimal(1)),),
                                 now=datetime.now(timezone.utc))
        assert engine.matcher.dump_state() == before
