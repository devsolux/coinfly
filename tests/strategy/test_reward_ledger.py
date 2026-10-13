"""Reward windows from the real paper ledger: contiguity, executions, credits, FIFO delivery.

The fills come from the scripted pipeline fixture through the real paper
venue; the reward ledger is then driven with explicit equity samples so every
window boundary is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from coinfly.domain.context import DomainError
from coinfly.research.reward import evaluate_reward
from coinfly.strategy.reward_ledger import RewardLedger
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


class RewardLedgerTests(AutomationFixture):
    def ledger(self, *, interval: float = 1.0, horizon: float = 5.0, started: datetime | None = None) -> RewardLedger:
        context = self.context()
        engine = self.worker._engines[context.identity]
        return RewardLedger(self.worker.writer, context=context, session_id="test-session", interval_seconds=interval, currency="USD",
                            fee_rate=Decimal(engine.matcher.fee_rate), credit_horizon_seconds=horizon,
                            started_at=started or datetime.now(timezone.utc))

    def equity(self) -> Decimal:
        context = self.context()
        return Decimal(self.worker._engines[context.identity].observe_execution_book().funded_equity)

    def rows(self) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT window_start, window_end, capital, equity_end, pnl, state FROM cf_reward_windows ORDER BY window_start").fetchall())]

    def test_windows_are_contiguous_in_time_and_equity_and_carry_over_gaps(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"]))
        self.enable()
        self.tick(2)
        start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        ledger = self.ledger(started=start)
        self.assertEqual(ledger.sample(start, Decimal("100")), 0)
        self.assertEqual(ledger.sample(start + timedelta(milliseconds=400), Decimal("100.5")), 0)
        # First sample past the end closes the window with the last sample AT OR BEFORE the end.
        self.assertEqual(ledger.sample(start + timedelta(seconds=1, milliseconds=100), Decimal("101")), 1)
        rows = self.rows()
        self.assertEqual(rows[0][2:], ("100", "100.5", "0.5", "CLOSED"))
        self.assertEqual(rows[1][2], "100.5")  # capital of the next window is the previous equity_end
        # A gap of several intervals: skipped windows close with carried equity and zero P&L.
        self.assertEqual(ledger.sample(start + timedelta(seconds=4, milliseconds=50), Decimal("99")), 3)
        rows = self.rows()
        self.assertEqual([(row[2], row[4]) for row in rows[1:4]], [("100.5", "0.5"), ("101", "0"), ("101", "0")])
        self.assertEqual(rows[4][2], "101")
        # Delivery is strictly in order and never skips.
        first = ledger.next_pending(start + timedelta(seconds=5))
        assert first is not None
        self.assertEqual(first.window.start_at, start)
        self.assertEqual(first.window.pnl, Decimal("0.5"))
        ledger.mark_fed(first, decision_id="d1", now=start + timedelta(seconds=5))
        second = ledger.next_pending(start + timedelta(seconds=5))
        assert second is not None
        self.assertEqual(second.window.start_at, start + timedelta(seconds=1))
        self.assertEqual(second.window.capital, Decimal("100.5"))
        self.assertEqual(second.window.capital, first.window.capital + first.window.pnl)
        backlog = ledger.backlog(start + timedelta(seconds=5))
        self.assertEqual((backlog["pending_windows"], backlog["windows_fed"], backlog["windows_closed"]), (3, 1, 4))
        # A window is not available before its closing sample.
        self.assertIsNone(self.ledger(started=start).next_pending(start))
        with self.assertRaises(DomainError):
            ledger.sample(start, Decimal("1"))  # time regression is refused

    def test_executions_and_credits_come_from_the_ledger_and_evaluate_exactly(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY", "HOLD"]))
        self.enable()
        started = datetime.now(timezone.utc) - timedelta(seconds=1)
        ledger = self.ledger(started=started, interval=10.0)
        ledger.sample(started, Decimal("100"))
        self.tick(4)  # the BUY rests at 100.00
        buy = self.decision()
        self.assertEqual(buy["outcome"], "DISPATCHED")
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()  # fills at 99.90 with a booked fee
        equity = self.equity()
        ledger.sample(datetime.now(timezone.utc), equity)  # the post-tick sample inside the window
        closing = started + timedelta(seconds=10, milliseconds=1)
        ledger.sample(closing, equity)
        pending = ledger.next_pending(closing + timedelta(seconds=1))
        assert pending is not None
        window = pending.window
        self.assertEqual(window.basis, "net_execution")
        self.assertEqual(window.capital, Decimal("100"))
        self.assertEqual(window.pnl, equity - Decimal("100"))
        [cost] = window.executions
        self.assertEqual((cost.side, cost.quantity, cost.fill_price, cost.reference_price), ("BUY", Decimal("0.04"), Decimal("99.90"), Decimal("100.00")))
        self.assertIsNotNone(cost.actual_fee)
        self.assertGreater(cost.actual_fee or Decimal(0), Decimal(0))
        self.assertTrue(cost.reference_at <= cost.reference_available_at <= cost.decision_at <= cost.submitted_at <= cost.filled_at <= cost.available_at)
        self.assertEqual(cost.fees.evidence_id, f"PAPER_SIMULATED_FEE_RATE:{cost.fees.rate}")
        # The originating decision earns the whole exposure credit at its exact decision time.
        [credit] = window.credits
        self.assertEqual(credit.fraction, Decimal(1))
        decided_at = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT decided_at FROM cf_automation_orders WHERE intent_id=?", (buy["intent_ids"][0],)).fetchone()[0])
        self.assertEqual(credit.decision_at, datetime.fromisoformat(decided_at))
        # Net execution: booked fee and price improvement are reported, P&L is the ledger's.
        result = evaluate_reward(window)
        self.assertEqual(result.net_pnl, window.pnl)
        self.assertEqual(result.fees, cost.actual_fee)
        self.assertEqual(result.execution_shortfall, Decimal("0.04") * (Decimal("99.90") - Decimal("100.00")))
        self.assertEqual(result.estimated_fee_count, 0)
        self.assertFalse(pending.horizon_exceeded)
        self.assertEqual(pending.credits_truncated, 0)

    def test_drain_fills_have_no_decision_credit(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY", "HOLD"]))
        self.enable()
        started = datetime.now(timezone.utc) - timedelta(seconds=1)
        ledger = self.ledger(started=started, interval=10.0)
        ledger.sample(started, Decimal("100"))
        self.tick(4)
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.rpc("automation.stop", request_id="stop")
        for _ in range(30):
            if self.worker.automation.status()["state"] == "STOPPED":
                break
            self.feed.books["BTC-USD"] = ("100.00", "100.10")
            self.tick()
        self.assertEqual(self.worker.automation.status()["execution_health"]["fills"], 2)
        ledger.sample(datetime.now(timezone.utc), self.equity())
        closing = started + timedelta(seconds=10, milliseconds=1)
        ledger.sample(closing, self.equity())
        pending = ledger.next_pending(closing + timedelta(seconds=1))
        assert pending is not None
        sides = sorted(cost.side for cost in pending.window.executions)
        self.assertEqual(sides, ["BUY", "SELL"])
        drain = next(cost for cost in pending.window.executions if cost.side == "SELL")
        self.assertEqual(drain.reference_price, drain.fill_price)  # no decision-time reference: zero shortfall, honestly
        # The position is flat: only the BUY decision's executed notional carries credit; the drain earns none.
        self.assertEqual(len(pending.window.credits), 1)

    def test_horizon_exceeded_is_counted_not_dropped(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"]))
        self.enable()
        self.tick(2)
        start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        ledger = self.ledger(started=start, horizon=2.0)
        ledger.sample(start, Decimal("100"))
        ledger.sample(start + timedelta(seconds=1, milliseconds=1), Decimal("100"))
        pending = ledger.next_pending(start + timedelta(seconds=10))
        assert pending is not None
        self.assertTrue(pending.horizon_exceeded)
        self.assertAlmostEqual(pending.delay_seconds, 9.0)
        ledger.mark_fed(pending, decision_id="late", now=start + timedelta(seconds=10))
        self.assertEqual(ledger.backlog(start + timedelta(seconds=10))["horizon_exceeded"], 1)
        row = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT state, fed_decision_id, horizon_exceeded FROM cf_reward_windows WHERE window_start=?", (start.isoformat(),)).fetchone())
        self.assertEqual(tuple(row), ("FED", "late", 1))

    def test_insolvent_paper_account_fails_the_session_explicitly(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"]))
        self.enable()
        self.tick(2)
        start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        ledger = self.ledger(started=start)
        ledger.sample(start, Decimal("100"))
        with self.assertRaises(DomainError) as refused:
            ledger.sample(start + timedelta(seconds=2), Decimal("0"))
        self.assertEqual(refused.exception.code, "REWARD_CAPITAL_INVALID")
