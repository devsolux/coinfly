"""Synthetic paper settlement per declared subtype: funding at boundaries, expiry at the mark.

Every event is labeled synthetic and journaled; nothing here is venue evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from coinfly.accounting.journal import Journal
from coinfly.domain.products import ProductSubtype
from coinfly.simulation.paper_matching import PaperInstrument
from coinfly.worker.paper_engine import PAPER_FUNDING_RATE_PER_INTERVAL, _interval_start
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

    def test_dated_future_is_cash_settled_at_expiry(self) -> None:
        engine = self.open_long()
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        contract = engine.instrument_contract("BTC-USD")
        dated = PaperInstrument(**{**contract.model_dump(), "subtype": ProductSubtype.DATED_FUTURE, "expiry": expiry})
        # Re-declare the paper instrument as a dated future for this test (definitions never change in production).
        matcher = engine.matcher
        matcher.instruments = tuple(dated if item.instrument_id == "BTC-USD" else item for item in matcher.instruments)
        matcher._instruments["BTC-USD"] = dated
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
