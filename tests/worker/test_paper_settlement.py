"""Synthetic paper settlement per declared subtype: funding at boundaries, expiry at the mark.

Every event is labeled synthetic and journaled; nothing here is venue evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from coinfly.accounting.journal import Journal
from coinfly.domain.context import DomainError
from coinfly.domain.products import ProductSubtype
from coinfly.execution.intent_causes import cause_summary_tx
from coinfly.execution.recovery import assert_context_closed_tx
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.simulation.paper_matching import DepthLevel, PaperInstrument
from coinfly.worker.paper_engine import PAPER_FUNDING_RATE_PER_INTERVAL, LocalPaperEngine, _interval_start
from tests.worker.test_automation_lifecycle import DerivativesFixture, derivatives_strategy


class PaperSettlementTests(DerivativesFixture):
    def open_long(self) -> Any:
        self.initialize()
        self.configure(derivatives_strategy(["BUY", "HOLD"]))
        self.enable()
        self.tick(4)
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.assertEqual(self.position(), Decimal("0.04"))
        return self.worker._engines[self.context().identity]

    def journal_kinds(self) -> list[str]:
        return [row[0] for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT event_kind FROM ex_journals ORDER BY rowid").fetchall())]

    def test_funding_is_charged_when_the_clock_crosses_an_interval_boundary_only(self) -> None:
        engine = self.open_long()
        margin = engine.matcher.margin
        assert margin is not None
        collateral = margin.collateral
        now = datetime.now(timezone.utc)
        self.assertEqual(engine.settle(now + timedelta(seconds=1)), ())  # inside the current 8 h interval: nothing
        boundary = _interval_start(now, timedelta(hours=8)) + timedelta(hours=8)
        events = engine.settle(boundary + timedelta(seconds=1))
        self.assertEqual(len(events), 1)
        [event] = events
        self.assertEqual((event["kind"], event["synthetic"], event["instrument"]), ("PAPER_FUNDING", True, "BTC-USD"))
        mark = (Decimal("99.80") + Decimal("99.90")) / 2
        expected = -(PAPER_FUNDING_RATE_PER_INTERVAL * Decimal("0.04") * mark)
        self.assertEqual(Decimal(event["payment"]), expected)  # the long pays at the declared synthetic rate
        self.assertEqual(margin.collateral, collateral + expected)
        # Idempotent per boundary; a later call inside the same interval charges nothing.
        self.assertEqual(engine.settle(boundary + timedelta(seconds=2)), ())
        # Two boundaries crossed at once are two events.
        events = engine.settle(boundary + timedelta(hours=16, seconds=1))
        self.assertEqual([item["interval_start"] for item in events],
                         [(boundary + timedelta(hours=16)).isoformat(), (boundary + timedelta(hours=8)).isoformat()])
        self.assertEqual(self.journal_kinds().count("PAPER_FUNDING"), 3)
        Journal(self.worker.writer).verify()
        status = self.worker.automation.status()
        self.assertEqual(self.rpc("state.snapshot")["balance"]["fill_model"]["queue_position"], "NOT_MODELED")
        self.assertIn("settlements", status)

    @staticmethod
    def redeclare_as_dated(engine: Any, expiry: datetime) -> None:
        """Re-declare the paper instrument as a dated future (definitions never change in production)."""
        contract = engine.instrument_contract("BTC-USD")
        dated = PaperInstrument(**{**contract.model_dump(), "subtype": ProductSubtype.DATED_FUTURE, "expiry": expiry})
        matcher = engine.matcher
        matcher.instruments = tuple(dated if item.instrument_id == "BTC-USD" else item for item in matcher.instruments)
        matcher._instruments["BTC-USD"] = dated

    def durable_collateral(self) -> Decimal:
        state = self.worker.writer.call(lambda connection: connection.execute("SELECT state_json FROM wp_paper_books").fetchone()[0])
        return Decimal(json.loads(state)["margin"]["collateral"])

    def ledger_rows(self, sql: str, *parameters: Any) -> list[Any]:
        return [tuple(row) for row in self.worker.writer.call(lambda connection: connection.execute(sql, parameters).fetchall())]

    def test_dated_future_is_cash_settled_at_expiry(self) -> None:
        engine = self.open_long()
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        self.redeclare_as_dated(engine, expiry)
        self.assertEqual(engine.settle(expiry - timedelta(seconds=1)), ())
        events = engine.settle(expiry + timedelta(seconds=1))
        kinds = [item["kind"] for item in events]
        self.assertIn("PAPER_EXPIRY_SETTLEMENT", kinds)
        settlement = next(item for item in events if item["kind"] == "PAPER_EXPIRY_SETTLEMENT")
        self.assertEqual(Decimal(settlement["quantity"]), Decimal("0.04"))
        margin = engine.matcher.margin
        assert margin is not None
        self.assertEqual([item.quantity for item in margin.positions], [])
        self.assertEqual(engine.settle(expiry + timedelta(seconds=2)), ())
        Journal(self.worker.writer).verify()

    def settled_at_expiry(self) -> tuple[Any, datetime, dict[str, Any]]:
        engine = self.open_long()
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        self.redeclare_as_dated(engine, expiry)
        engine.settle(expiry - timedelta(seconds=1))
        before = engine.admission.drawdown.read(engine.context, engine.scope)
        assert before is not None
        self.before_daily_result = before.daily_net_result
        [event] = engine.settle(expiry + timedelta(seconds=1))
        self.assertEqual(event["kind"], "PAPER_EXPIRY_SETTLEMENT")
        return engine, expiry, event

    def test_expiry_closes_the_ledger_position_not_just_the_margin_book(self) -> None:
        engine, _, event = self.settled_at_expiry()
        self.assertEqual(self.position(), Decimal(0))
        self.assertEqual([Decimal(row[0]) for row in self.ledger_rows("SELECT quantity_remaining FROM ex_open_lots")
                          if Decimal(row[0]) != 0], [])
        # The stop proof is satisfiable: no residual position, no unattributed fill, no open intent.
        self.worker.writer.call(lambda connection: assert_context_closed_tx(connection, engine.context))
        self.assertFalse(self.worker.interlock.is_provisionally_fenced(engine.context.authority_id))
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
        # The carrier is labeled as a venue settlement, never as a decision or an unlabeled trade.
        summary = self.worker.writer.call(lambda connection: cause_summary_tx(connection, engine.context.identity))
        self.assertEqual(summary["VENUE_SETTLEMENT"], 1)
        self.assertEqual(summary["UNLABELED"], 0)

    def test_expiry_is_booked_as_one_synthetic_fee_free_fill_with_exact_realized_result(self) -> None:
        engine, _, event = self.settled_at_expiry()
        [(trade_id, intent_id, side, quantity, price, fee, fill_id)] = self.ledger_rows(
            "SELECT f.trade_id,f.intent_id,json_extract(f.payload_json,'$.side'),json_extract(f.payload_json,'$.quantity'),"
            "json_extract(f.payload_json,'$.price'),json_extract(f.payload_json,'$.fee'),f.fill_id "
            "FROM ex_fills f WHERE f.trade_id LIKE '%paper-expiry%'")
        self.assertIsNotNone(intent_id)  # attributed: never a suspense / UNATTRIBUTED_FILL entry
        self.assertEqual((side, Decimal(quantity), Decimal(fee)), ("SELL", Decimal("0.04"), Decimal(0)))
        self.assertEqual(Decimal(price), Decimal(event["mark"]))
        [(realized,)] = self.ledger_rows("SELECT realized_pnl FROM ex_lot_movements WHERE fill_id=?", fill_id)
        self.assertEqual(Decimal(realized), Decimal(event["realized"]))
        self.assertNotEqual(Decimal(realized), 0)
        # The synthetic carrier intent is an exit that is already terminal with nothing reserved.
        [(kind, terminal, remaining)] = self.ledger_rows(
            "SELECT json_extract(i.payload_json,'$.kind'),o.terminal,r.remaining_amount FROM ex_intents i "
            "JOIN ex_outbox o USING(intent_id) JOIN ex_reservations r USING(intent_id) WHERE i.intent_id=?", intent_id)
        self.assertEqual((kind, terminal, Decimal(remaining)), ("EXIT", 1, Decimal(0)))
        # One fill only, and a replay settles nothing further.
        self.assertEqual(engine.settle(datetime.now(timezone.utc) + timedelta(days=2)), ())
        self.assertEqual(len(self.ledger_rows("SELECT 1 FROM ex_fills WHERE trade_id LIKE '%paper-expiry%'")), 1)

    def test_expiry_result_reaches_the_ledger_cash_and_the_risk_books_once(self) -> None:
        engine, _, event = self.settled_at_expiry()
        margin = engine.matcher.margin
        assert margin is not None
        journal = Journal(self.worker.writer)
        # The ledger cash account tracks the margin book's collateral exactly (seed + fills + fees + this settlement).
        self.assertEqual(journal.balance(engine.context, engine.scope, "assets:cash", "USD"), margin.collateral)
        self.assertEqual(margin.collateral, self.durable_collateral())
        # The realized result is booked once: the old synthetic-settlement account is not used for it as well.
        self.assertEqual(journal.balance(engine.context, engine.scope, "pnl:synthetic_settlement", "USD"), Decimal(0))
        self.assertEqual(journal.balance(engine.context, engine.scope, "income:realized_derivative", "USD"),
                         -sum((Decimal(row[0]) for row in self.ledger_rows("SELECT realized_pnl FROM ex_lot_movements")), Decimal(0)))
        risk = engine.admission.drawdown.read(engine.context, engine.scope)
        assert risk is not None
        self.assertEqual(risk.daily_net_result, self.before_daily_result + Decimal(event["realized"]))
        self.assertEqual(self.ledger_rows("SELECT event_kind FROM ex_events WHERE event_kind='PAPER_EXPIRY_SETTLEMENT'"),
                         [("PAPER_EXPIRY_SETTLEMENT",)])
        journal.verify()

    def test_expired_book_recovers_without_a_ledger_conflict(self) -> None:
        engine, _, _ = self.settled_at_expiry()
        reopened = LocalPaperEngine(engine.config, engine.context, self.worker.writer, self.worker.reducer,
                                    self.worker.interlock, ready=lambda: None)
        # The test re-declared BTC-USD as dated in memory only; a real catalog definition arrives via ensure_instruments.
        reopened._default_instruments = engine.matcher.instruments
        # An acknowledged intent without a matcher order is a ledger conflict, except the ledger-only expiry close.
        reopened.recover_existing()
        self.assertEqual(reopened.matcher.margin.positions, ())  # type: ignore[union-attr]
        self.assertEqual(reopened.matcher.margin.collateral, engine.matcher.margin.collateral)  # type: ignore[union-attr]
        self.assertEqual(self.ledger_rows("SELECT COUNT(*) FROM ex_authority_fences WHERE active=1"), [(0,)])
        self.worker.writer.call(lambda connection: assert_context_closed_tx(connection, engine.context))

    def test_failed_funding_commit_rolls_the_margin_book_and_event_ids_back(self) -> None:
        engine = self.open_long()
        collateral = engine.matcher.margin.collateral  # type: ignore[union-attr]
        now = datetime.now(timezone.utc)
        boundary = _interval_start(now, timedelta(hours=8)) + timedelta(hours=8)
        self.assertEqual(engine.settle(now + timedelta(seconds=1)), ())
        with patch.object(engine, "_persist_tx", side_effect=DomainError("SIMULATED_COMMIT_FAILURE")):
            with self.assertRaisesRegex(DomainError, "SIMULATED_COMMIT_FAILURE"):
                engine.settle(boundary + timedelta(seconds=1))
        self.assertEqual(engine.matcher.margin.collateral, collateral)  # type: ignore[union-attr]
        self.assertEqual(self.durable_collateral(), collateral)
        self.assertEqual(self.journal_kinds().count("PAPER_FUNDING"), 0)
        # The retry charges the boundary exactly once, in memory and durably.
        [event] = engine.settle(boundary + timedelta(seconds=2))
        self.assertEqual(event["kind"], "PAPER_FUNDING")
        charged = collateral + Decimal(event["payment"])
        self.assertEqual(engine.matcher.margin.collateral, charged)  # type: ignore[union-attr]
        self.assertEqual(self.durable_collateral(), charged)
        self.assertEqual(self.journal_kinds().count("PAPER_FUNDING"), 1)
        self.assertEqual(engine.settle(boundary + timedelta(seconds=3)), ())

    def test_failed_expiry_commit_leaves_margin_and_ledger_open_until_the_retry(self) -> None:
        engine = self.open_long()
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        self.redeclare_as_dated(engine, expiry)
        engine.settle(expiry - timedelta(seconds=1))
        with patch.object(engine, "_persist_tx", side_effect=DomainError("SIMULATED_COMMIT_FAILURE")):
            with self.assertRaisesRegex(DomainError, "SIMULATED_COMMIT_FAILURE"):
                engine.settle(expiry + timedelta(seconds=1))
        self.assertEqual([item.quantity for item in engine.matcher.margin.positions], [Decimal("0.04")])  # type: ignore[union-attr]
        self.assertEqual(self.position(), Decimal("0.04"))
        self.assertEqual(self.ledger_rows("SELECT 1 FROM ex_fills WHERE trade_id LIKE '%paper-expiry%'"), [])
        [event] = engine.settle(expiry + timedelta(seconds=2))
        self.assertEqual(event["kind"], "PAPER_EXPIRY_SETTLEMENT")
        self.assertEqual(engine.matcher.margin.positions, ())  # type: ignore[union-attr]
        self.assertEqual(self.position(), Decimal(0))
        self.worker.writer.call(lambda connection: assert_context_closed_tx(connection, engine.context))

    def test_drain_refused_by_the_matcher_fails_fast_once_and_does_not_fence(self) -> None:
        """A deterministic paper refusal is a proven non-creation: the drain reports it instead of re-planning 1000 times."""
        engine = self.open_long()
        margin = engine.matcher.margin
        assert margin is not None
        # Diverge the matcher from the ledger (what the unsettled expiry used to do): flat margin book, long ledger.
        margin.apply_fill("BTC-USD", Decimal("-0.04"), Decimal("99.85"), multiplier=Decimal(1), fee=Decimal(0), reduce_only=True)
        engine.update_market("BTC-USD", bids=(DepthLevel(price=Decimal("99.80"), quantity=Decimal("5")),),
                             asks=(DepthLevel(price=Decimal("99.90"), quantity=Decimal("5")),), now=datetime.now(timezone.utc))
        self.assertEqual(self.position(), Decimal("0.04"))
        with self.assertRaisesRegex(DomainError, "DRAIN_ORDER_REJECTED"):
            engine.drain(DrainPolicy())
        self.assertEqual(self.ledger_rows("SELECT o.submission,o.resolution FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
                                          "WHERE json_extract(i.payload_json,'$.kind')='EXIT'"), [("REJECTED", "RESOLVED")])
        # Nothing was created, so the drain consumed none of the snapshot's displayed liquidity.
        self.assertNotIn("BTC-USD", engine._drain_used)
        self.assertFalse(self.worker.interlock.is_provisionally_fenced(engine.context.authority_id))
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
