"""LiveSpotEngine behind the common execution interface, against a fake transport.

No socket, no credential, no real order. The tests pin: arming is gated by the
mandate and the transport; the planner dispatches through the same admission
actor, dispatcher and reducer as the paper venue; acceptance needs positive
identity; UNKNOWN stays UNKNOWN until venue history resolves it; sync rebuilds
balances, order states and fills from the documented routes; a stale sync
refuses planning; drains cancel and close through the same path; the
interface is asserted, not duck-typed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import TestCase

from coinfly.config.models import EngineConfig, RiskLimits
from coinfly.control.slot_service import SlotService
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack, RunProfile
from coinfly.execution.engine import ExecutionEngine, execution_engine_gaps, require_execution_engine
from coinfly.execution.fact_reducer import FactReducer
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Resolution, Submission
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.simulation.paper_matching import DepthLevel, PaperInstrument
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.target_coordinator import BoundTarget
from coinfly.venues.coinbase.live_spot_engine import LiveSpotEngine
from coinfly.worker.paper_engine import LocalPaperEngine
from tests.execution.helpers import LedgerCase
from tests.risk.test_risk_admission import NoTradeSlotDriver

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
PORTFOLIO = "portfolio-live-1"


class FakeTransport:
    """Documented shapes only; records every call; never a socket."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.create_response: Mapping[str, Any] | Exception = {"status": 200, "body": {"success": True, "success_response": {"order_id": "venue-1"}}}
        self.cancel_results: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = [
            {"currency": "USD", "available_balance": {"value": "1000.00", "currency": "USD"}, "hold": {"value": "0", "currency": "USD"}},
            {"currency": "BTC", "available_balance": {"value": "0", "currency": "BTC"}, "hold": {"value": "0", "currency": "BTC"}},
        ]
        for index, account in enumerate(self.accounts):
            account.update(uuid=f"account-{index}", retail_portfolio_id=PORTFOLIO, active=True, ready=True)
        self.orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []

    def retire(self) -> None:
        pass  # No sockets or signing handles in this explicitly isolated fixture.

    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.posts.append((path, json.loads(json.dumps(body))))
        if path == "/orders":
            if isinstance(self.create_response, Exception):
                raise self.create_response
            return self.create_response
        if path == "/orders/batch_cancel":
            return {"status": 200, "body": {"results": self.cancel_results}}
        raise AssertionError(path)

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        self.gets.append((path, dict(query or {})))
        if path == "/accounts":
            return {"accounts": self.accounts, "has_next": False, "cursor": ""}
        if path.endswith("/batch"):
            selected = self.orders
            if query and query.get("order_status"):
                selected = [order for order in selected if order.get("status") == query["order_status"]]
            return {"orders": [{"retail_portfolio_id": PORTFOLIO, **order} for order in selected], "has_next": False}
        if path.endswith("/fills"):
            return {"fills": self.fills, "cursor": ""}
        raise AssertionError(path)


def live_config() -> EngineConfig:
    return EngineConfig(authority_id="owner", environment=Environment.LIVE, product_track=ProductTrack.SPOT, run_profile=RunProfile.STANDARD,
                        execution_scope=(PORTFOLIO,), broker_profile_id="coinbase-advanced", endpoint_profile_id="coinbase-advanced-spot-execution",
                        credential_binding_id="0" * 32, limits=RiskLimits(max_order_notional=Decimal("500"), max_gross_exposure=Decimal("1"),
                                                                           max_daily_loss=Decimal("100"), max_drawdown_fraction=Decimal("0.5")))


def live_context() -> ContextKey:
    return ContextKey(authority_id="owner", environment=Environment.LIVE, product_track=ProductTrack.SPOT, broker_profile_id="coinbase-advanced", epoch=1)


BTC = PaperInstrument(instrument_id="BTC-USD", base_currency="BTC", quote_currency="USD", product_track=ProductTrack.SPOT,
                      quantity_increment=Decimal("0.00000001"), price_increment=Decimal("0.01"), minimum_notional=Decimal("1"))


class LiveSpotEngineTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.transport = FakeTransport()
        self.mandate_ok = True
        self.interlock = ContextInterlock(self.writer)
        self.reducer = FactReducer(self.writer)
        # The interlock and the admission actor judge freshness on the wall clock: so does the engine here.
        self.engine = LiveSpotEngine(live_config(), live_context(), self.writer, self.reducer, self.interlock,
                                     transport_factory=lambda: self.transport, mandate_check=lambda config, now: self.mandate_ok,
                                     instruments=(BTC,))
        self.engine.initialize(live_config(), live_context())
        from coinfly.operations.live_arming import MANDATE_STATEMENT, LiveMandate, LiveMandateStore
        cfg = live_config()
        now = datetime.now(timezone.utc)
        LiveMandateStore(self.writer).record(LiveMandate(authority_id=cfg.authority_id, approver="isolated test",
            statement=MANDATE_STATEMENT, product_track=cfg.product_track, product_subtype=cfg.effective_subtype,
            portfolio_id=PORTFOLIO, allowed_instruments=("BTC-USD",), max_order_notional=cfg.limits.max_order_notional,
            max_gross_exposure=cfg.limits.max_gross_exposure, max_daily_loss=cfg.limits.max_daily_loss,
            approved_at=now, expires_at=now + timedelta(hours=1)), now=now)
        # The armed authority row and an active slot, as the coordinator and slot service would leave them.
        self.writer.call(lambda connection: connection.execute(
            "INSERT INTO cf_authorities(authority_id,effective_context,effective_config,effective_revision,transition_epoch,engine_state,armed,admissions_fenced) "
            "VALUES(?,?,?,?,?,'ACTIVE',1,0)", ("owner", live_context().model_dump_json(), live_config().model_dump_json(), 1, 1)).rowcount)
        self.slots = SlotService(self.writer, self.interlock, NoTradeSlotDriver())
        self.slots.create(slot_id="slot-btc", context=live_context(), scope_id=PORTFOLIO, instrument="BTC-USD", expected_config_revision=1, request_id="create")
        self.slots.start("slot-btc", expected_revision=1, expected_epoch=1, request_id="start")
        for book_id in (PORTFOLIO, "authority"):
            self.engine.admission.drawdown.record(live_context(), book_id, equity=Decimal(1000), evidence_id=f"initial:{book_id}",
                                                  observed_at=datetime.now(timezone.utc) - timedelta(seconds=2))

    def tearDown(self) -> None:
        try:
            self.engine.close_background()
        finally:
            super().tearDown()

    @property
    def clock_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def mark(self, bid: str = "99.90", ask: str = "100.10") -> None:
        self.engine.update_market("BTC-USD", bids=(DepthLevel(price=Decimal(bid), quantity=Decimal("10")),),
                                 asks=(DepthLevel(price=Decimal(ask), quantity=Decimal("10")),), now=self.clock_now)

    def target(self, weight: str, *, revision: int = 1) -> BoundTarget:
        now = self.clock_now
        proposal = TargetWeights(affected_instruments=("BTC-USD",), decision_id=f"decision-{revision}", model_id="fixed-decoder", model_version="1", snapshot_id="snap",
                                 allocation_revision=revision, expires_at=now + timedelta(seconds=5),
                                 weights=(InstrumentWeight(instrument_id="BTC-USD", weight=Decimal(weight)),))
        return BoundTarget(context=live_context(), proposal=proposal, accepted_at=now)

    def test_interface_is_asserted_for_both_engines(self) -> None:
        self.assertEqual(execution_engine_gaps(self.engine), ())
        self.assertIsInstance(self.engine, ExecutionEngine)
        self.assertEqual(execution_engine_gaps(LocalPaperEngine), ())  # the paper venue implements the same interface
        with self.assertRaises(DomainError) as refused:
            require_execution_engine(object())
        self.assertEqual(refused.exception.code, "EXECUTION_ENGINE_INTERFACE_INCOMPLETE")

    def test_arming_needs_mandate_and_transport_and_syncs_first(self) -> None:
        self.mandate_ok = False
        with self.assertRaises(DomainError) as refused:
            self.engine.arm()
        self.assertEqual(refused.exception.code, "LIVE_MANDATE_REQUIRED")
        with self.assertRaises(DomainError) as unsynced:
            self.engine.observe_execution_book()
        self.assertEqual(unsynced.exception.code, "LIVE_ACCOUNT_NOT_SYNCED")
        self.mandate_ok = True
        self.engine.arm()
        self.assertEqual([path for path, _ in self.transport.gets][:1], ["/accounts"])
        self.mark()
        book = self.engine.observe_execution_book()
        self.assertEqual(book.funded_equity, Decimal("1000.00"))
        self.assertTrue(book.authoritative_account_evidence)
        snapshot = self.engine.balance_snapshot()
        self.assertEqual((snapshot["environment"], snapshot["simulated"], snapshot["valuation_provenance"]), ("LIVE", False, "COINBASE_ACCOUNTS_ROUTE"))

    def test_target_dispatches_once_through_the_common_path_with_positive_identity(self) -> None:
        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"), cause={"decision_id": "d1", "controller_profile": "fixture-sequence-v1", "pipeline_fixture": True})
        self.assertEqual(len(ids), 1)
        [(path, body)] = self.transport.posts
        self.assertEqual((path, body["client_order_id"], body["side"]), ("/orders", ids[0], "BUY"))
        record = IntentStore(self.writer).get(ids[0])
        self.assertEqual((record.submission, record.venue_order_id), (Submission.ACKNOWLEDGED, "venue-1"))
        self.assertEqual(record.intent.quantity, Decimal("0.4"))  # 4 % of 1000 at 100.00
        # The same target again re-plans against the outstanding order: nothing is duplicated.
        self.engine.sync(self.clock_now)
        self.assertEqual(self.engine.submit_target(self.target("0.04", revision=2)), ())
        self.assertEqual(len(self.transport.posts), 1)

    def test_unknown_create_stays_unknown_until_history_resolves_it(self) -> None:
        self.engine.arm()
        self.mark()
        self.transport.create_response = DomainError("HTTP_WRITE_OUTCOME_UNKNOWN")
        ids = self.engine.submit_target(self.target("0.04"))
        record = IntentStore(self.writer).get(ids[0])
        self.assertEqual(record.resolution, Resolution.UNKNOWN)
        self.assertFalse(record.terminal)
        # Planning while an UNKNOWN is outstanding: the outstanding quantity is reserved, nothing new is sent.
        report = self.engine.sync(self.clock_now)
        self.assertEqual(report["unknown_open"], 1)  # history did not show it: still unknown, never "rejected"
        self.assertEqual(self.engine.submit_target(self.target("0.04", revision=2)), ())
        # The venue's history names the client id: positive identity resolves it.
        self.transport.orders = [{"order_id": "venue-9", "client_order_id": ids[0], "status": "OPEN", "filled_size": "0"}]
        report = self.engine.sync(self.clock_now)
        self.assertEqual(report["unknown_resolved"], 1)
        record = IntentStore(self.writer).get(ids[0])
        self.assertEqual((record.submission, record.venue_order_id), (Submission.ACKNOWLEDGED, "venue-9"))

    def test_sync_applies_fills_and_terminal_states_from_venue_history(self) -> None:
        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"))
        self.transport.orders = [{"order_id": "venue-1", "client_order_id": ids[0], "status": "FILLED", "filled_size": "0.4"}]
        self.transport.fills = [{"entry_id": "e1", "trade_id": "t1", "order_id": "venue-1", "trade_time": "2026-09-22T12:00:01Z", "price": "100.00",
                                 "size": "0.4", "commission": "0.24", "side": "BUY", "size_in_quote": False}]
        self.transport.accounts[0]["available_balance"]["value"] = "959.76"
        self.transport.accounts[1]["available_balance"]["value"] = "0.4"
        report = self.engine.sync(self.clock_now)
        self.assertEqual((report["fills_applied"], report["orders_terminal"]), (1, 1))
        record = IntentStore(self.writer).get(ids[0])
        self.assertTrue(record.terminal)
        self.assertEqual(record.filled_quantity, Decimal("0.4"))
        position = self.writer.call(lambda connection: connection.execute("SELECT quantity FROM ex_positions WHERE instrument='BTC-USD'").fetchone()[0])
        self.assertEqual(Decimal(position), Decimal("0.4"))
        self.mark()
        book = self.engine.observe_execution_book()
        self.assertEqual(book.funded_equity, Decimal("959.76") + Decimal("0.4") * Decimal("100.00"))
        # A second sync applies nothing twice.
        report = self.engine.sync(self.clock_now)
        self.assertEqual((report["fills_applied"], report["orders_terminal"]), (0, 0))

    def test_terminal_status_waits_for_the_fills_the_venue_reports(self) -> None:
        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"))
        # History already says FILLED for 0.4 but the fills route lags: the intent stays open.
        self.transport.orders = [{"order_id": "venue-1", "client_order_id": ids[0], "status": "FILLED", "filled_size": "0.4"}]
        report = self.engine.sync(self.clock_now)
        self.assertEqual(report["orders_terminal"], 0)
        self.assertFalse(IntentStore(self.writer).get(ids[0]).terminal)
        # A cancellation after a partial fill is booked once the partial fill is in the ledger.
        self.transport.orders = [{"order_id": "venue-1", "client_order_id": ids[0], "status": "CANCELLED", "filled_size": "0.1"}]
        self.transport.fills = [{"entry_id": "e1", "trade_id": "t1", "order_id": "venue-1", "price": "100.00", "size": "0.1", "commission": "0",
                                 "side": "BUY", "size_in_quote": False}]
        report = self.engine.sync(self.clock_now)
        self.assertEqual((report["fills_applied"], report["orders_terminal"]), (1, 1))
        record = IntentStore(self.writer).get(ids[0])
        self.assertTrue(record.terminal)
        self.assertEqual(record.filled_quantity, Decimal("0.1"))

    def test_stale_sync_refuses_planning(self) -> None:
        import time

        from coinfly.venues.coinbase import live_spot_engine as module

        original = module.SYNC_MAX_AGE
        module.SYNC_MAX_AGE = timedelta(milliseconds=200)
        self.addCleanup(setattr, module, "SYNC_MAX_AGE", original)
        self.engine.arm()
        time.sleep(0.3)
        self.mark()
        with self.assertRaises(DomainError) as refused:
            self.engine.submit_target(self.target("0.04"))
        self.assertEqual(refused.exception.code, "LIVE_ACCOUNT_SYNC_STALE")
        self.assertEqual(self.transport.posts, [])

    def test_cancel_reports_the_venue_outcome_only(self) -> None:
        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"))
        self.transport.cancel_results = [{"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER", "order_id": "venue-1"}]
        self.assertEqual(self.engine.cancel_intent(ids[0])["reason"], "UNKNOWN_ORDER")
        self.assertFalse(IntentStore(self.writer).get(ids[0]).terminal)
        self.transport.cancel_results = [{"success": True, "order_id": "venue-1"}]
        result = self.engine.cancel_intent(ids[0])
        self.assertFalse(result["cancelled"])
        self.assertTrue(result["cancel_requested"])
        self.assertFalse(IntentStore(self.writer).get(ids[0]).terminal)
        self.transport.orders = [{"order_id": "venue-1", "client_order_id": ids[0], "status": "CANCELLED", "filled_size": "0"}]
        self.engine.sync(self.clock_now)
        self.assertTrue(IntentStore(self.writer).get(ids[0]).terminal)

    def test_drain_cancels_open_orders_and_closes_residuals_through_the_gateway(self) -> None:
        from coinfly.risk.drain_policy import DrainPolicy

        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"))
        # The order filled meanwhile (history says so) and the position must be closed.
        self.transport.orders = [{"order_id": "venue-1", "client_order_id": ids[0], "status": "FILLED", "filled_size": "0.4"}]
        self.transport.fills = [{"entry_id": "e1", "trade_id": "t1", "order_id": "venue-1", "price": "100.00", "size": "0.4", "commission": "0",
                                 "side": "BUY", "size_in_quote": False}]
        # The account routes show the bought BTC; the exit reserves it from the venue's own balance evidence.
        self.transport.accounts[0]["available_balance"]["value"] = "960.00"
        self.transport.accounts[1]["available_balance"]["value"] = "0.4"
        report = self.engine.sync(self.clock_now)
        self.assertEqual((report["fills_applied"], report["orders_terminal"]), (1, 1))
        # Now the drain: the exit SELL is accepted and, per history, filled.
        self.transport.create_response = {"status": 200, "body": {"success": True, "success_response": {"order_id": "venue-exit"}}}
        exit_fill = {"entry_id": "e2", "trade_id": "t2", "order_id": "venue-exit", "price": "99.80", "size": "0.4", "commission": "0",
                     "side": "SELL", "size_in_quote": False}
        transport = self.transport

        original_get = transport.get

        def get(path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
            exit_ids = [body["client_order_id"] for p, body in transport.posts if p == "/orders" and body["side"] == "SELL"]
            if exit_ids:
                transport.orders = [transport.orders[0], {"order_id": "venue-exit", "client_order_id": exit_ids[0], "status": "FILLED", "filled_size": "0.4"}]
                transport.fills = [transport.fills[0], exit_fill]
            return original_get(path, query=query)
        transport.get = get  # type: ignore[method-assign]
        self.engine.drain(DrainPolicy())
        sells = [body for path, body in transport.posts if path == "/orders" and body["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["order_configuration"]["limit_limit_gtc"]["base_size"], "0.4")
        position = self.writer.call(lambda connection: connection.execute("SELECT quantity FROM ex_positions WHERE instrument='BTC-USD'").fetchone()[0])
        self.assertEqual(Decimal(position), Decimal(0))
        certificate = self.engine.reconcile()
        self.assertTrue(certificate.all_related_orders_terminal)
        causes = self.writer.call(lambda connection: [tuple(row) for row in connection.execute("SELECT cause FROM cf_intent_causes ORDER BY recorded_at").fetchall()])
        self.assertIn(("PROTECTIVE_DRAIN",), causes)

    def test_restarted_engine_drains_again_without_a_semantic_intent_conflict(self) -> None:
        from coinfly.risk.drain_policy import DrainPolicy

        self.engine.arm()
        self.mark()
        ids = self.engine.submit_target(self.target("0.04"))
        transport = self.transport
        buy = {"order_id": "venue-1", "client_order_id": ids[0], "status": "FILLED", "filled_size": "0.4"}
        buy_fill = {"entry_id": "e1", "trade_id": "t1", "order_id": "venue-1", "price": "100.00", "size": "0.4", "commission": "0",
                    "side": "BUY", "size_in_quote": False}
        transport.orders, transport.fills = [buy], [buy_fill]
        transport.accounts[0]["available_balance"]["value"] = "960.00"
        transport.accounts[1]["available_balance"]["value"] = "0.4"
        self.engine.sync(self.clock_now)
        transport.create_response = {"status": 200, "body": {"success": True, "success_response": {"order_id": "venue-exit-1"}}}
        original_get = transport.get

        def sells() -> list[dict[str, Any]]:
            return [body for path, body in transport.posts if path == "/orders" and body["side"] == "SELL"]

        def get(path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
            orders, fills = [buy], [buy_fill]
            cancelled = any(path == "/orders/batch_cancel" for path, _ in transport.posts)
            for index, body in enumerate(sells()):
                status = ("CANCELLED" if cancelled else "OPEN") if index == 0 else "FILLED"
                orders.append({"order_id": f"venue-exit-{index + 1}", "client_order_id": body["client_order_id"], "status": status,
                               "filled_size": "0.4" if status == "FILLED" else "0"})
                if status == "FILLED":
                    fills.append({"entry_id": f"x{index}", "trade_id": f"tx{index}", "order_id": f"venue-exit-{index + 1}", "price": "90.00",
                                  "size": "0.4", "commission": "0", "side": "SELL", "size_in_quote": False})
            transport.orders, transport.fills = orders, fills
            return original_get(path, query=query)
        transport.get = get  # type: ignore[method-assign]
        # The first drain sends its exit, the venue leaves it resting: the drain reports it is still awaiting liquidity.
        with self.assertRaises(DomainError) as awaiting:
            self.engine.drain(DrainPolicy())
        self.assertEqual(awaiting.exception.code, "DRAIN_AWAITING_LIQUIDITY")
        self.assertEqual(len(sells()), 1)
        # Worker restart: a fresh engine on the same ledger sees a different book. Its exit must not collide
        # with the persisted first exit (same slot, instrument and kind; different price).
        restarted = LiveSpotEngine(live_config(), live_context(), self.writer, self.reducer, self.interlock,
                                   transport_factory=lambda: transport, mandate_check=lambda config, now: True, instruments=(BTC,))
        self.addCleanup(restarted.close_background)
        restarted.initialize(live_config(), live_context())
        restarted.recover_existing()
        restarted.update_market("BTC-USD", bids=(DepthLevel(price=Decimal("90.00"), quantity=Decimal("10")),),
                                asks=(DepthLevel(price=Decimal("90.20"), quantity=Decimal("10")),), now=self.clock_now)
        transport.cancel_results = [{"success": True, "order_id": "venue-exit-1"}]
        transport.create_response = {"status": 200, "body": {"success": True, "success_response": {"order_id": "venue-exit-2"}}}
        restarted.drain(DrainPolicy())
        self.assertEqual(len(sells()), 2)
        self.assertNotEqual(sells()[0]["client_order_id"], sells()[1]["client_order_id"])
        position = self.writer.call(lambda connection: connection.execute("SELECT quantity FROM ex_positions WHERE instrument='BTC-USD'").fetchone()[0])
        self.assertEqual(Decimal(position), Decimal(0))

    def test_order_sent_while_sync_walks_the_venue_is_not_external_activity(self) -> None:
        self.engine.arm()
        self.mark()
        transport = self.transport
        original_get = transport.get
        raced: list[tuple[str, ...]] = []

        def get(path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
            if path.endswith("/batch") and not raced:
                # Another thread persists and sends an intent after sync began and before its walk reads the venue.
                raced.append(self.engine.submit_target(self.target("0.04")))
                transport.orders = [{"order_id": "venue-1", "client_order_id": raced[0][0], "status": "OPEN", "filled_size": "0"}]
            return original_get(path, query=query)
        transport.get = get  # type: ignore[method-assign]
        report = self.engine.sync(self.clock_now)
        self.assertEqual(len(raced[0]), 1)
        self.assertEqual(report["external_open_orders"], 0)
        self.assertFalse(self.engine._external_activity)

    def test_genuinely_unknown_open_order_is_still_external_activity_during_a_race(self) -> None:
        self.engine.arm()
        self.mark()
        transport = self.transport
        original_get = transport.get
        raced: list[tuple[str, ...]] = []

        def get(path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
            if path.endswith("/batch") and not raced:
                raced.append(self.engine.submit_target(self.target("0.04")))
                transport.orders = [{"order_id": "venue-1", "client_order_id": raced[0][0], "status": "OPEN", "filled_size": "0"},
                                    {"order_id": "foreign", "client_order_id": "not-ours", "status": "OPEN", "filled_size": "0"}]
            return original_get(path, query=query)
        transport.get = get  # type: ignore[method-assign]
        report = self.engine.sync(self.clock_now)
        self.assertEqual(report["external_open_orders"], 1)
        self.assertTrue(self.engine._external_activity)

    def test_live_engine_is_spot_only_and_requires_a_live_context(self) -> None:
        with self.assertRaises(DomainError) as refused:
            LiveSpotEngine(live_config().model_copy(update={"product_track": ProductTrack.PERPETUAL}), live_context(), self.writer, self.reducer,
                           self.interlock, transport_factory=lambda: self.transport, mandate_check=lambda c, n: True)
        self.assertEqual(refused.exception.code, "LIVE_ENGINE_SPOT_ONLY")


class MandateKindTests(TestCase):
    def test_qualification_mandate_is_bounded_and_breaks_the_circularity(self) -> None:
        from coinfly.operations.live_arming import (
            QUALIFICATION_STATEMENT,
            LiveMandate,
            live_arming_prerequisites,
        )
        from coinfly.venues.coinbase.adapter_factory import default_local_qualifications

        approved = NOW
        mandate = LiveMandate(authority_id="owner", approver="Baris", statement=QUALIFICATION_STATEMENT, kind="QUALIFICATION",
                              product_track=ProductTrack.SPOT, product_subtype=live_config().effective_subtype, portfolio_id=PORTFOLIO,
                              allowed_instruments=("BTC-USD",), max_order_notional=Decimal("10"), max_gross_exposure=Decimal("1"),
                              max_daily_loss=Decimal("5"), approved_at=approved, expires_at=approved + timedelta(hours=2))
        # The admission actor enforces the configuration's limits: a wider configuration is not a bounded run.
        self.assertEqual(mandate.check(live_config(), now=approved + timedelta(minutes=1)), ("LIVE_MANDATE_QUALIFICATION_CONFIG_WIDER",))
        bounded = live_config().model_copy(update={"limits": RiskLimits(max_order_notional=Decimal("10"), max_gross_exposure=Decimal("1"),
                                                                         max_daily_loss=Decimal("5"), max_drawdown_fraction=Decimal("0.5"))})
        self.assertEqual(mandate.check(bounded, now=approved + timedelta(minutes=1)), ())
        with self.assertRaises(ValueError):
            LiveMandate(**{**mandate.model_dump(), "allowed_instruments": ("BTC-USD", "ETH-USD")})
        with self.assertRaises(ValueError):
            LiveMandate(**{**mandate.model_dump(), "max_order_notional": Decimal("26")})
        with self.assertRaises(ValueError):
            LiveMandate(**{**mandate.model_dump(), "expires_at": approved + timedelta(days=2)})
        items = live_arming_prerequisites(live_config(), now=approved + timedelta(minutes=1), evidence=None, local=default_local_qualifications(),
                                          credential_bound=False, dataset_ready=(True, "DATASET_READY"), kernel_ready=(True, "NATIVE_KERNEL_VERIFIED"),
                                          mandate=mandate)
        by_code = {item.code: item for item in items}
        self.assertTrue(by_code["LIVE_ADAPTER_QUALIFICATION_RUN"].satisfied)  # the run under this mandate produces the record
        self.assertIn("Qualification mode", by_code["LIVE_ADAPTER_QUALIFICATION_RUN"].detail)
        # Every other blocker is still listed, together, not one at a time.
        missing = [item.code for item in items if not item.satisfied]
        for code in ("NATIVE_SIGNING_UNCERTIFIED", "REGION_UNVERIFIED", "LIVE_GATEWAY_UNCERTIFIED", "EXECUTION_CREDENTIAL_BOUND", "ACCOUNT_EVIDENCE_MISSING"):
            self.assertIn(code, missing)
        trading = LiveMandate(**{**mandate.model_dump(), "kind": "TRADING", "statement": QUALIFICATION_STATEMENT})
        self.assertIn("LIVE_MANDATE_STATEMENT_MISMATCH", trading.check(live_config(), now=approved + timedelta(minutes=1)))
