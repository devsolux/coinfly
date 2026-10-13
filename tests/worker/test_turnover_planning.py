"""Exact target-to-order planning: strict turnover, reservations, order TTL, hysteresis.

Every case runs through the real supervisor with the scripted pipeline fixture
(never a model) and the paper venue. The numbers are exact ``Decimal``: a 100
USD paper book, a 5 % per-window limit, so the allowance is 5.0000 USD and an
order of exactly 5.0000 USD is *not* strictly below it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.execution.intent_store import IntentStore
from coinfly.strategy.turnover_ledger import feasible_quantity, window_bounds
from coinfly.strategy.universe import SelectionReason, UniverseSelection
from coinfly.worker.automation import AutomationState
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


def planning_strategy(sides: Sequence[str], *, step: str = "0.04", policy: str = "REJECT", interval: int = 60,
                      ttl: int = 30, expiry: str = "ROLLBACK_UNFILLED_TARGET", **overrides: Any) -> dict[str, Any]:
    definition = fixture_strategy(sides, **overrides)
    definition["execution"] = {"proposal_ttl_seconds": 5, "decision_interval_seconds": 1, "target_step": step,
                               "max_turnover_fraction": "0.05", "accounting_interval_seconds": interval,
                               "turnover_policy": policy, "order_ttl_seconds": ttl, "expiry_policy": expiry}
    return definition


class FeasibleQuantityTests(TestCase):
    def test_equal_notional_is_reduced_by_one_increment(self) -> None:
        # 5.0000 / 100 = 0.05 exactly: 0.05 × 100 is not strictly below 5, so one increment less.
        self.assertEqual(feasible_quantity(Decimal("5.0000"), price=Decimal("100"), multiplier=Decimal(1),
                                           quantity_increment=Decimal("0.00000001"), minimum_quantity=Decimal("0.00000001"),
                                           minimum_notional=Decimal("1")), Decimal("0.04999999"))

    def test_coarse_increment_rounds_down_never_up(self) -> None:
        self.assertEqual(feasible_quantity(Decimal("5"), price=Decimal("3"), multiplier=Decimal(1), quantity_increment=Decimal("0.5"),
                                           minimum_quantity=Decimal("0.5"), minimum_notional=Decimal("1")), Decimal("1.5"))

    def test_below_minimums_is_infeasible(self) -> None:
        self.assertIsNone(feasible_quantity(Decimal("0.5"), price=Decimal("100"), multiplier=Decimal(1),
                                            quantity_increment=Decimal("0.001"), minimum_quantity=Decimal("0.001"),
                                            minimum_notional=Decimal("1")))
        self.assertIsNone(feasible_quantity(Decimal("0"), price=Decimal("100"), multiplier=Decimal(1),
                                            quantity_increment=Decimal("0.001"), minimum_quantity=Decimal("0.001"),
                                            minimum_notional=Decimal("0")))

    def test_windows_are_epoch_aligned_half_open(self) -> None:
        now = datetime(2026, 9, 22, 10, 0, 59, 999999, tzinfo=timezone.utc)
        start, end = window_bounds(now, 60)
        self.assertEqual((start, end), (datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc), datetime(2026, 9, 22, 10, 1, tzinfo=timezone.utc)))
        self.assertEqual(window_bounds(end, 60)[0], end)
        with self.assertRaises(DomainError):
            window_bounds(now, 0)


class TurnoverGuardTests(AutomationFixture):
    def turnover(self, instrument: str = "BTC-USD") -> list[dict[str, Any]]:
        return list(self.decision(instrument).get("provenance", {}).get("turnover", []))

    def start(self, strategy: dict[str, Any], *, headroom_seconds: float = 10.0) -> None:
        self.initialize()
        self.configure(strategy)
        # Accounting windows are epoch-aligned. A test that reasons about "one
        # window" must not straddle a real boundary, so wait for a window with
        # enough time left (the default 60 s interval; a few seconds are used).
        interval = int(strategy["execution"].get("accounting_interval_seconds", 60))
        if interval > headroom_seconds:
            remaining = interval - (time.time() % interval)
            if remaining < headroom_seconds:
                time.sleep(remaining + 0.05)
        self.enable()
        self.tick(4)  # universe, slot, warm-up, first decision

    def test_exactly_five_percent_is_refused_under_reject(self) -> None:
        self.start(planning_strategy(["BUY"], step="0.05"))
        decision = self.decision()
        self.assertEqual((decision["side"], decision["outcome"], decision["outcome_code"]), ("BUY", "RISK_REJECTED", "TURNOVER_LIMIT_EXCEEDED"))
        self.assertEqual(decision["intent_ids"], [])
        [note] = self.turnover()
        self.assertEqual((note["capital"], note["spent"], note["reserved"], note["proposed"], note["allowance"]),
                         ("100.00", "0", "0", "5.0000", "5.0000"))
        self.assertFalse(note["admitted"])
        self.assertEqual(self.worker.automation.status()["execution_health"]["open_intents"], 0)
        # The window and its interval-start capital are persisted, not recomputed.
        rows = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT capital, fraction FROM cf_turnover_windows").fetchall())
        self.assertEqual([tuple(row) for row in rows], [("100.00", "0.05")])
        # A refused order leaves the intended target untouched (no phantom target).
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["target_weight"]), Decimal(0))

    def test_exactly_five_percent_is_constrained_to_the_largest_feasible_quantity(self) -> None:
        self.start(planning_strategy(["BUY"], step="0.05", policy="CONSTRAIN"))
        decision = self.decision()
        self.assertEqual((decision["outcome"], decision["outcome_code"]), ("DISPATCHED", "ADMITTED_CONSTRAINED"))
        intent = IntentStore(self.worker.writer).get(decision["intent_ids"][0])
        self.assertEqual(intent.intent.quantity, Decimal("0.04999999"))
        self.assertLess(intent.intent.quantity * intent.intent.limit_price, Decimal("5"))
        [note] = self.turnover()
        self.assertEqual((note["code"], note["constrained_quantity"]), ("TURNOVER_CONSTRAINED", "0.04999999"))
        # The constrained order fills like any other; the target stays the strategy's 5 % step.
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["actual_quantity"]), Decimal("0.04999999"))

    def test_resting_reservation_and_filled_notional_share_one_window(self) -> None:
        self.start(planning_strategy(["BUY", "BUY", "BUY"]))
        first = self.decision()
        self.assertEqual((first["outcome"], first["outcome_code"]), ("DISPATCHED", "ADMITTED"))
        [note] = self.turnover()
        self.assertEqual((note["proposed"], note["reserved"], note["spent"]), ("4.0000", "0", "0"))
        # The order rests (ask 100.10 above the 100.00 limit). The next BUY steps
        # the target to 0.08, but the planner subtracts the outstanding 0.04, so
        # the proposal is 4.0000 (no compounding) — and the reservation makes it inadmissible.
        time.sleep(1.05)
        self.tick()
        second = self.decision()
        self.assertEqual((second["outcome"], second["outcome_code"]), ("RISK_REJECTED", "TURNOVER_LIMIT_EXCEEDED"))
        [note] = self.turnover()
        self.assertEqual((note["proposed"], note["reserved"], note["spent"], note["allowance"]), ("4.0000", "4.0000", "0", "1.0000"))
        self.assertEqual(self.worker.automation.status()["execution_health"]["open_intents"], 1)
        # Fill the resting order: the reservation becomes spent notional at the fill price.
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        intent = IntentStore(self.worker.writer).get(first["intent_ids"][0])
        self.assertTrue(intent.terminal)
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        third = self.decision()
        self.assertEqual(third["outcome_code"], "TURNOVER_LIMIT_EXCEEDED")
        [note] = self.turnover()
        self.assertEqual((Decimal(note["spent"]), Decimal(note["reserved"]), Decimal(note["proposed"])),
                         (Decimal("3.996"), Decimal(0), Decimal(4)))  # 0.04 × 99.90
        # Nothing was dispatched twice for the same target and no duplicate intent exists.
        count = self.worker.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0])
        self.assertEqual(count, 1)

    def test_partial_fill_counts_spent_plus_remaining_reservation(self) -> None:
        self.start(planning_strategy(["BUY", "BUY"]))
        first = self.decision()
        self.assertEqual(first["outcome"], "DISPATCHED")
        # Only 0.01 displayed at a crossing ask: a partial fill of 0.01 at 99.90.
        self.feed.depths["BTC-USD"] = Decimal("0.01")
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        intent = IntentStore(self.worker.writer).get(first["intent_ids"][0])
        self.assertFalse(intent.terminal)
        self.assertEqual(intent.filled_quantity, Decimal("0.01"))
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        [note] = self.turnover()
        self.assertEqual((Decimal(note["spent"]), Decimal(note["reserved"]), note["admitted"]),
                         (Decimal("0.999"), Decimal(3), False))  # 0.01×99.90 + 0.03×100.00

    def test_a_new_window_starts_from_the_interval_start_equity(self) -> None:
        self.start(planning_strategy(["BUY", "BUY"], interval=1))
        first = self.decision()
        self.assertEqual(first["outcome"], "DISPATCHED")
        [before] = self.turnover()
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        second = self.decision()
        self.assertEqual((second["outcome"], second["outcome_code"]), ("DISPATCHED", "ADMITTED"))
        [after] = self.turnover()
        self.assertGreater(after["window_start"], before["window_start"])
        self.assertEqual(after["spent"], "0")
        self.assertNotEqual(after["capital"], before["capital"])  # equity after the fee-bearing fill, not the stale 100.00

    def test_slots_of_one_strategy_share_the_window(self) -> None:
        self.start(planning_strategy(["BUY", "BUY"], instruments=("BTC-USD", "ETH-USD"), max_active_slots=2))
        btc, eth = self.decision("BTC-USD"), self.decision("ETH-USD")
        self.assertEqual((btc["side"], eth["side"]), ("BUY", "BUY"))
        outcomes = sorted((btc["outcome_code"], eth["outcome_code"]))
        self.assertEqual(outcomes, ["ADMITTED", "TURNOVER_LIMIT_EXCEEDED"])
        rejected = self.turnover("ETH-USD") if eth["outcome_code"] != "ADMITTED" else self.turnover("BTC-USD")
        self.assertEqual((rejected[0]["reserved"], rejected[0]["proposed"]), ("4.0000", "4.0000"))

    def test_strategy_schema_bounds_the_turnover_fraction(self) -> None:
        self.initialize()
        for execution in ({"target_step": "0.05", "max_turnover_fraction": "0.06"},  # above the research limit
                          {"target_step": "0.05", "max_turnover_fraction": "0.04"},  # a step the guard could never admit
                          {"target_step": "0.04", "max_turnover_fraction": "0.05", "accounting_interval_seconds": 0}):
            with self.subTest(execution=execution), self.assertRaises(DomainError) as refused:
                self.configure(fixture_strategy(["HOLD"], execution=execution), request_id=f"bad-{execution}")
            self.assertEqual(refused.exception.code, "INVALID_STRATEGY_DEFINITION")


class OrderExpiryTests(AutomationFixture):
    def orders(self) -> list[tuple[str, str, str]]:
        return [tuple(row) for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT state, outcome_code, filled_quantity FROM cf_automation_orders ORDER BY created_at").fetchall())]

    def start(self, strategy: dict[str, Any]) -> None:
        self.initialize()
        self.configure(strategy)
        self.enable()
        self.tick(4)

    def test_nonmarketable_limit_expires_and_the_target_rolls_back(self) -> None:
        self.start(planning_strategy(["BUY", "HOLD"], ttl=1))
        decision = self.decision()
        self.assertEqual(decision["outcome"], "DISPATCHED")
        self.assertEqual(self.orders(), [("RESTING", "", "0")])
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["target_weight"]), Decimal("0.04"))
        time.sleep(1.05)
        self.tick()
        self.assertEqual(self.orders(), [("EXPIRED", "ORDER_EXPIRED_NONMARKETABLE", "0")])
        status = self.worker.automation.status()
        self.assertEqual(status["expired_orders"][-1]["code"], "ORDER_EXPIRED_NONMARKETABLE")
        self.assertEqual(status["execution_health"]["open_intents"], 0)
        self.assertTrue(IntentStore(self.worker.writer).get(decision["intent_ids"][0]).terminal)
        # The expired order is neither a HOLD nor an execution: the originating
        # decision's durable outcome says so (the fixture's next decision is a
        # HOLD, recorded separately), and the intended target returned to what is
        # held (nothing).
        rows = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT side, outcome, outcome_code FROM cf_automation_decisions ORDER BY decided_at").fetchall())
        self.assertEqual([tuple(row) for row in rows][0], ("BUY", "ORDER_EXPIRED", "ORDER_EXPIRED_NONMARKETABLE"))
        self.assertEqual(Decimal(status["slots"][0]["target_weight"]), Decimal(0))
        self.assertNotEqual(status["state"], AutomationState.FAILED.value)

    def test_crossed_limit_without_depth_is_liquidity_missing(self) -> None:
        self.start(planning_strategy(["BUY", "HOLD"], ttl=1))
        self.assertEqual(self.decision()["outcome"], "DISPATCHED")
        # The ask crosses the 100.00 limit but shows less than one increment: no fill.
        self.feed.depths["BTC-USD"] = Decimal("0.000000001")
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.05)
        self.tick()
        self.assertEqual(self.orders(), [("EXPIRED", "ORDER_EXPIRED_LIQUIDITY_MISSING", "0")])

    def test_partial_fill_expiry_keeps_the_filled_part(self) -> None:
        self.start(planning_strategy(["BUY", "HOLD"], ttl=1))
        self.assertEqual(self.decision()["outcome"], "DISPATCHED")
        self.feed.depths["BTC-USD"] = Decimal("0.01")
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        self.assertEqual(self.orders(), [("EXPIRED", "ORDER_EXPIRED_PARTIAL", "0.01")])
        status = self.worker.automation.status()
        self.assertEqual(Decimal(status["slots"][0]["actual_quantity"]), Decimal("0.01"))
        # Rolled back to the held weight: 0.01 × mark / equity, not the intended 0.04.
        self.assertLess(Decimal(status["slots"][0]["target_weight"]), Decimal("0.011"))
        self.assertGreater(Decimal(status["slots"][0]["target_weight"]), Decimal("0.009"))

    def test_keep_target_policy_leaves_the_intended_target(self) -> None:
        self.start(planning_strategy(["BUY", "HOLD"], ttl=1, expiry="KEEP_TARGET"))
        time.sleep(1.05)
        self.tick()
        self.assertEqual(self.orders(), [("EXPIRED", "ORDER_EXPIRED_NONMARKETABLE", "0")])
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["target_weight"]), Decimal("0.04"))

    def test_filled_orders_settle_without_cancel(self) -> None:
        self.start(planning_strategy(["BUY", "HOLD"], ttl=1))
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.05)
        self.tick()
        self.assertEqual(self.orders(), [("SETTLED", "FILLED", "0.04")])
        self.assertEqual(self.worker.automation.status()["expired_orders"], [])


class SelectionHysteresisTests(AutomationFixture):
    """Ranking-driven departures wait for the grace count; stops retire at once; re-entry waits the cooldown."""

    def selection(self, selected: Sequence[str], *, identity: str) -> UniverseSelection:
        reasons = tuple(SelectionReason(instrument=name, eligible=True, rank=index, codes=("ELIGIBLE",)) for index, name in enumerate(selected))
        return UniverseSelection(strategy_identity=identity, selected=tuple(selected), reasons=reasons,
                                 selected_at=datetime.now(timezone.utc), inputs_digest="0" * 64)

    def reconcile(self, selected: Sequence[str], *, now: datetime | None = None, retire_immediately: bool = False) -> Any:
        automation = self.worker.automation
        context = self.context()
        config = automation.config(context)
        assert config is not None
        status = self.worker.coordinator.status(automation.authority_id)
        scope = status.effective_config.execution_scope[0]  # type: ignore[union-attr]
        return automation.ownership.reconcile(context=context, scope_id=scope, definition=config.strategy,
                                              selection=self.selection(selected, identity=config.strategy.identity),
                                              config_revision=status.effective_revision, now=now or datetime.now(timezone.utc),
                                              may_start=True, start_epoch=context.epoch, retire_immediately=retire_immediately)

    def states(self) -> dict[str, tuple[str, int]]:
        context = self.context()
        return {slot.instrument: (slot.state, slot.absent_count) for slot in self.worker.automation.ownership.owned(context)}

    def test_grace_then_retirement_then_cooldown(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"], instruments=("BTC-USD", "ETH-USD"), max_active_slots=1,
                                        filters={"retirement_grace_selections": 3, "reentry_cooldown_seconds": 300}))
        self.enable()
        self.tick(2)
        self.assertEqual(self.states(), {"BTC-USD": ("ACTIVE", 0)})
        # BTC drops out of the ranking: two rounds of grace, no drain.
        for round_number in (1, 2):
            result = self.reconcile(["ETH-USD"])
            self.assertIn(("BTC-USD", f"RETIREMENT_GRACE:{round_number}/3"), result.blocked)
            self.assertEqual(result.retiring, ())
            self.assertEqual(self.states()["BTC-USD"], ("ACTIVE", round_number))
        # It comes back: the absence counter resets.
        result = self.reconcile(["BTC-USD"])
        self.assertEqual(self.states()["BTC-USD"], ("ACTIVE", 0))
        # Three consecutive absences retire it (a protected drain, then RETIRED).
        for _ in range(2):
            self.reconcile(["ETH-USD"])
        result = self.reconcile(["ETH-USD"])
        self.assertIn("BTC-USD", result.retiring)
        for _ in range(20):
            if self.states()["BTC-USD"][0] == "RETIRED":
                break
            time.sleep(0.05)
            self.reconcile(["ETH-USD"])
        self.assertEqual(self.states()["BTC-USD"][0], "RETIRED")
        # The newcomer waited for the budget the retiring slot held; it is admitted now.
        self.reconcile(["ETH-USD"])
        self.assertEqual(self.states()["ETH-USD"][0], "ACTIVE")
        # Re-selected inside the cooldown: refused with the reason; after it: re-admitted.
        result = self.reconcile(["BTC-USD"])
        self.assertIn(("BTC-USD", "SELECTION_COOLDOWN"), result.blocked)
        later = datetime.now(timezone.utc) + timedelta(seconds=301)
        for _ in range(20):
            if self.states()["ETH-USD"][0] == "RETIRED":
                break
            time.sleep(0.05)
            self.reconcile(["BTC-USD"], now=later, retire_immediately=True)
        result = self.reconcile(["BTC-USD"], now=later)
        self.assertNotIn(("BTC-USD", "SELECTION_COOLDOWN"), result.blocked)
        self.assertEqual(self.states()["BTC-USD"][0], "ACTIVE")

    def test_operator_stop_bypasses_the_grace(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"], filters={"retirement_grace_selections": 5}))
        self.enable()
        self.tick(2)
        self.rpc("automation.stop", request_id="stop")
        for _ in range(20):
            if self.worker.automation.status()["state"] == AutomationState.STOPPED.value:
                break
            time.sleep(0.05)
            self.tick()
        self.assertEqual(self.states()["BTC-USD"][0], "RETIRED")

    def test_operator_exclusions_are_respected_in_selection(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"], instruments=("BTC-USD",), excluded_instruments=["ETH-USD"]))
        self.enable()
        self.tick()
        universe = self.worker.automation.status()["universe"]
        reasons = {reason["instrument"]: reason["codes"] for reason in universe["reasons"]}
        self.assertIn("EXCLUDED_BY_OPERATOR", reasons["ETH-USD"])
        self.assertEqual(universe["selected"], ["BTC-USD"])
        with self.assertRaises(DomainError) as refused:
            self.configure(fixture_strategy(["HOLD"], instruments=("BTC-USD",), excluded_instruments=["BTC-USD"], strategy_id="overlap"), request_id="overlap")
        self.assertEqual(refused.exception.code, "INVALID_STRATEGY_DEFINITION")
