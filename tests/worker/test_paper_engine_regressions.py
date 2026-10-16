"""Paper venue regressions: one monotonic clock, input refusals that do not fence,
and deterministic paper rejections that are proven non-creation (never UNKNOWN).

Everything here runs the production paper engine and ledger; nothing touches a network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from coinfly.domain.context import DomainError
from coinfly.execution.dispatcher import CreateOutcome, Dispatcher
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import IntentKind, IntentRecord, OrderIntent, Resolution, Side, Submission
from coinfly.simulation.paper_matching import DepthLevel
from coinfly.worker.paper_engine import LocalPaperEngine
from tests.integration.test_paper_worker_flow import WorkerFixture
from tests.worker.test_automation_real_feed import RealFeedFixture, strategy


def level(price: str, quantity: str = "1000") -> tuple[DepthLevel, ...]:
    return (DepthLevel(price=Decimal(price), quantity=Decimal(quantity)),)


class PaperVenueClockAutomationTests(RealFeedFixture):
    def test_observation_stamped_during_the_previous_tick_does_not_fail_automation_or_fence(self) -> None:
        """A background sweep stamps a book while a tick is still trading; the next tick polls it.

        Its stamp is older than the paper venue's clock (wall time of that tick's last submit/advance)
        but newer than the instrument's previous book. It used to raise PAPER_TIME_REGRESSION, FAIL the
        automation and fence the authority until restart.
        """
        self.initialize()
        self.rpc("automation.configure", {"strategy": strategy(sides=["BUY"], max_active_slots=1, per_slot_max_exposure_fraction="0.34"),
                                          "approved_capital": "100", "feed": {"kind": "COINBASE_PUBLIC_REST"},
                                          "pipeline_test_mode": True}, request_id="configure-1")
        self.enable()
        self.tick(5)
        engine = self.worker._engines[self.worker.coordinator.status(self.worker.authority_id).effective_context.identity]
        feed = self.feeds[0]
        last_observed = max(item.received_at for item in (feed.latest(name) for name in ("BTC-USD", "ETH-USD", "SOL-USD"))
                            if item is not None)
        engine._advance_pending()  # the venue's own wall-clock event at the end of the tick, after the sweep
        venue_clock = engine.matcher.last_time
        assert venue_clock is not None and last_observed < venue_clock
        stamp = last_observed + (venue_clock - last_observed) / 2
        real_clock, real_book = feed._clock, self.reader.product_book
        feed._clock = lambda: stamp
        self.reader.product_book = lambda product_id, *, limit=10: {
            **real_book(product_id, limit=limit), "time": stamp.isoformat().replace("+00:00", "Z")}
        try:
            feed.sweep_once()
        finally:
            feed._clock, self.reader.product_book = real_clock, real_book
        self.worker.automation.tick(datetime.now(timezone.utc))
        self.assertIsNone(self.worker.automation.status()["failure"])
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
        self.assertFalse(self.worker.interlock.is_provisionally_fenced(engine.context.authority_id))


class PaperEngineFixture(WorkerFixture):
    def prepared(self, *, age: timedelta = timedelta(0)) -> tuple[LocalPaperEngine, str]:
        """An armed paper book with one slot and a book per instrument, observed ``age`` ago."""
        engine, _ = self.initialize()
        self.arm()
        slot_id = self.open_slot()
        stamp = datetime.now(timezone.utc) - age
        for instrument, bid, ask in (("BTC-USD", "99.90", "100.10"), ("ETH-USD", "49.90", "50.10"),
                                     ("SOL-USD", "9.90", "10.10")):
            engine.update_market(instrument, bids=level(bid), asks=level(ask), now=stamp)
        return engine, slot_id

    def fenced(self, engine: LocalPaperEngine) -> bool:
        return bool(self.worker.interlock.is_provisionally_fenced(engine.context.authority_id))


class PaperVenueClockTests(PaperEngineFixture):
    def resting_buy(self, engine: LocalPaperEngine) -> str:
        identifiers = engine.submit_target(self.target(engine, "0.05", revision=1))
        self.assertEqual(len(identifiers), 1)
        return identifiers[0]

    def test_observation_older_than_the_venue_clock_is_accepted_without_fencing(self) -> None:
        engine, _ = self.prepared(age=timedelta(seconds=2))
        intent_id = self.resting_buy(engine)
        clock = engine.matcher.last_time
        assert clock is not None
        # Received while the previous tick was still computing, polled on the next tick.
        observed = clock - timedelta(milliseconds=500)
        fills = engine.update_market("BTC-USD", bids=level("99.98"), asks=level("99.99"), now=observed)
        self.assertEqual(len(fills), 1)
        self.assertFalse(self.fenced(engine))
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
        self.assertGreaterEqual(engine.matcher.last_time, clock)  # type: ignore[operator]
        self.assertEqual(engine._depth["BTC-USD"][2], observed)  # the observation's own stamp stays as data
        record = IntentStore(self.worker.writer).get(intent_id)
        self.assertEqual(record.filled_quantity, Decimal("0.05"))

    def test_wall_clock_operations_never_run_behind_a_feed_stamp(self) -> None:
        engine, _ = self.prepared()
        intent_id = self.resting_buy(engine)
        ahead = datetime.now(timezone.utc) + timedelta(seconds=30)
        engine.update_market("BTC-USD", bids=level("99.00"), asks=level("101.00"), now=ahead)
        result = engine.cancel_intent(intent_id)  # stamped by the engine, not the caller
        self.assertTrue(result["cancelled"])
        self.assertGreaterEqual(engine.matcher.last_time, ahead)  # type: ignore[operator]
        self.assertFalse(self.fenced(engine))
        self.assertFalse(engine.matcher.open_orders)

    def test_older_snapshot_never_replaces_a_newer_book_of_the_same_instrument(self) -> None:
        engine, _ = self.prepared()
        newer = datetime.now(timezone.utc)
        engine.update_market("BTC-USD", bids=level("99.90"), asks=level("100.10"), now=newer)
        fills = engine.update_market("BTC-USD", bids=level("50.00"), asks=level("51.00"),
                                     now=newer - timedelta(seconds=2))
        self.assertEqual(fills, ())
        bids, asks, stamp = engine._depth["BTC-USD"]
        self.assertEqual((bids[0].price, asks[0].price, stamp), (Decimal("99.90"), Decimal("100.10"), newer))
        self.assertFalse(self.fenced(engine))

    def test_naive_feed_stamp_is_refused_without_fencing(self) -> None:
        engine, _ = self.prepared()
        with self.assertRaisesRegex(DomainError, "AWARE_TIMESTAMP_REQUIRED"):
            engine.update_market("BTC-USD", bids=level("99.90"), asks=level("100.10"), now=datetime(2026, 1, 1))
        self.assertFalse(self.fenced(engine))

    def test_malformed_book_is_refused_before_anything_changes_and_does_not_fence(self) -> None:
        engine, _ = self.prepared()
        before = engine.matcher.dump_state()
        for code, bids, asks in (("PAPER_CROSSED_BOOK", level("100.10"), level("99.90")),
                                 ("PAPER_UNKNOWN_INSTRUMENT", level("99.90"), level("100.10"))):
            instrument = "DOGE-USD" if code == "PAPER_UNKNOWN_INSTRUMENT" else "BTC-USD"
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                engine.update_market(instrument, bids=bids, asks=asks, now=datetime.now(timezone.utc))
            self.assertFalse(self.fenced(engine))
            self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
            self.assertEqual(engine.matcher.dump_state(), before)

    def test_accounting_failure_while_applying_a_book_still_fences(self) -> None:
        engine, _ = self.prepared()
        self.resting_buy(engine)
        with patch.object(engine, "_persist_tx", side_effect=RuntimeError("DISK_FAILURE")):
            with self.assertRaises(RuntimeError):
                engine.update_market("BTC-USD", bids=level("99.98"), asks=level("99.99"),
                                     now=datetime.now(timezone.utc))
        self.assertTrue(self.fenced(engine))
        self.assertEqual(engine.matcher.fills, ())  # the matcher fill was rolled back with the failed commit

    def test_balance_snapshot_reports_exact_integer_microseconds(self) -> None:
        engine, _ = self.prepared()
        engine.matcher.latency = timedelta(days=200_000, microseconds=1)  # float seconds cannot hold this
        latency = engine.balance_snapshot()["fill_model"]["latency_microseconds"]
        self.assertEqual(latency, 200_000 * 86_400 * 1_000_000 + 1)


class PaperDeterministicRejectionTests(PaperEngineFixture):
    def admitted(self, engine: LocalPaperEngine, slot_id: str, *, revision: int, quantity: str = "0.05",
                 price: str = "100.00", kind: IntentKind = IntentKind.ENTRY, side: Side = Side.BUY) -> IntentRecord:
        engine.observe_execution_book()
        intent = OrderIntent(context=engine.context, scope_id=engine.scope, profile_version=1, signer_version=0,
                             slot_id=slot_id, allocation_revision=revision, instrument="BTC-USD", base_asset="BTC",
                             quote_asset="USD", side=side, kind=kind, quantity=Decimal(quantity),
                             limit_price=Decimal(price))
        return engine.admission.admit(intent)

    def dispatch(self, engine: LocalPaperEngine, intent_id: str) -> IntentRecord:
        dispatcher = Dispatcher(self.worker.writer, self.worker.interlock, engine, reducer=self.worker.reducer)
        return dispatcher.dispatch(intent_id).result(timeout=5)

    def rejection_evidence(self, intent_id: str) -> list[str]:
        return [row[0] for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT evidence_id FROM ex_events WHERE entity_id=? AND event_kind='ORDER_REJECTED'",
            (intent_id,)).fetchall())]

    def assert_proven_rejection(self, engine: LocalPaperEngine, record: IntentRecord, code: str) -> None:
        self.assertEqual((record.submission, record.resolution), (Submission.REJECTED, Resolution.RESOLVED))
        self.assertTrue(record.terminal)
        self.assertEqual(record.reservation_remaining, Decimal(0))
        self.assertFalse(self.fenced(engine))
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])
        self.assertEqual(engine.matcher.orders, ())
        [evidence] = self.rejection_evidence(record.intent.intent_id)
        self.assertIn(code, evidence)
        unknown = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_events WHERE event_kind='UNKNOWN_SUBMISSION'").fetchone()[0])
        self.assertEqual(unknown, 0)

    def test_matcher_validation_failures_are_proven_non_creation_not_unknown(self) -> None:
        engine, slot_id = self.prepared()
        for revision, (price, quantity, code) in enumerate((("100.005", "0.05", "PAPER_INVALID_INCREMENT"),
                                                             ("100.00", "0.005", "PAPER_BELOW_MINIMUM")), start=1):
            with self.subTest(code=code):
                record = self.admitted(engine, slot_id, revision=revision, quantity=quantity, price=price)
                self.assert_proven_rejection(engine, self.dispatch(engine, record.intent.intent_id), code)

    def test_create_returns_the_rejected_no_creation_outcome_with_the_reason(self) -> None:
        engine, slot_id = self.prepared()
        record = self.admitted(engine, slot_id, revision=1, price="100.005")
        before = engine.matcher.dump_state()
        result = engine.create(record.intent).result(timeout=1)
        self.assertEqual(result.outcome, CreateOutcome.REJECTED_NO_CREATION)
        self.assertIsNone(result.venue_order_id)
        self.assertIn("PAPER_INVALID_INCREMENT", result.evidence_id)
        self.assertEqual(result.context, engine.context)
        self.assertEqual(engine.matcher.dump_state(), before)  # nothing restored, nothing lost (order books kept)

    def test_entry_refused_by_a_quiesced_engine_is_proven_non_creation(self) -> None:
        engine, slot_id = self.prepared()
        record = self.admitted(engine, slot_id, revision=1)
        engine.quiesce()
        self.assert_proven_rejection(engine, self.dispatch(engine, record.intent.intent_id), "PAPER_ENTRIES_FENCED")

    def test_a_conflicting_client_id_is_not_declared_non_creation(self) -> None:
        engine, slot_id = self.prepared()
        record = self.admitted(engine, slot_id, revision=1)
        first = engine.create(record.intent).result(timeout=1)
        self.assertEqual(first.outcome, CreateOutcome.ACCEPTED)
        conflicting = record.intent.model_copy(update={"quantity": Decimal("0.06")})
        with self.assertRaisesRegex(DomainError, "PAPER_CLIENT_ID_CONFLICT"):
            engine.create(conflicting)
        self.assertEqual(len(engine.matcher.orders), 1)

    def test_a_persistence_failure_stays_unknown_and_fences(self) -> None:
        engine, slot_id = self.prepared()
        record = self.admitted(engine, slot_id, revision=1)
        with patch.object(engine, "_persist_tx", side_effect=RuntimeError("DISK_FAILURE")):
            outcome = self.dispatch(engine, record.intent.intent_id)
        self.assertEqual((outcome.submission, outcome.resolution), (Submission.PENDING_SUBMISSION, Resolution.UNKNOWN))
        self.assertTrue(self.fenced(engine))
        self.assertEqual(engine.matcher.orders, ())  # the unsaved order was rolled back, not retried

    def test_accepted_orders_are_unchanged(self) -> None:
        engine, slot_id = self.prepared()
        record = self.admitted(engine, slot_id, revision=1)
        outcome = self.dispatch(engine, record.intent.intent_id)
        self.assertEqual((outcome.submission, outcome.resolution), (Submission.ACKNOWLEDGED, Resolution.RESOLVED))
        self.assertEqual(len(engine.matcher.open_orders), 1)
        self.assertFalse(self.fenced(engine))

