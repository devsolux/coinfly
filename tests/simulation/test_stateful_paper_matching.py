import copy
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from coinfly.config.models import EngineConfig
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack, RunProfile
from coinfly.shadow.runner import MarketFrame, ShadowRunner
from coinfly.simulation.paper_margin import PaperMarginBook
from coinfly.simulation.paper_matching import (
    DepthLevel,
    PaperInstrument,
    PaperMatcher,
    PaperOrderRequest,
    PaperOrderStatus,
    Side,
)

D = Decimal
NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def context(track=ProductTrack.SPOT, environment=Environment.SANDBOX, epoch=1):
    return ContextKey(authority_id="owner", environment=environment, product_track=track,
                      broker_profile_id="paper-local", epoch=epoch)


def instrument(track=ProductTrack.SPOT):
    return PaperInstrument(instrument_id="BTC-USD", base_currency="BTC", quote_currency="USD",
                           product_track=track, quantity_increment=D("0.01"), price_increment=D("0.01"))


def request(client="client-1", *, side=Side.BUY, quantity="1", price="100", reduce_only=False):
    return PaperOrderRequest(client_order_id=client, instrument_id="BTC-USD", side=side, quantity=D(quantity),
                             limit_price=D(price), reduce_only=reduce_only)


def depth(*rows):
    return tuple(DepthLevel(price=D(price), quantity=D(quantity)) for price, quantity in rows)


class PaperMatchingTests(unittest.TestCase):
    def matcher(self, *, latency=timedelta(0), balances=None, fee_rate=D("0.001")):
        return PaperMatcher(context(), (instrument(),), initial_balances=balances or {"USD": D("1000")},
                            latency=latency, fee_rate=fee_rate)

    def test_latency_partial_fill_and_consumed_depth(self):
        matcher = self.matcher(latency=timedelta(seconds=1))
        order = matcher.submit(request(quantity="2"), now=NOW)
        self.assertEqual(matcher.available_balances["USD"], D("799.8"))
        self.assertEqual(matcher.update_book("BTC-USD", bids=depth(("98", "5")), asks=depth(("99", "0.5")), now=NOW), ())
        fills = matcher.advance(now=NOW + timedelta(seconds=1))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, D("0.5"))
        self.assertEqual(fills[0].fee, D("0.0495"))
        self.assertEqual(matcher.order(order.order_id).status, PaperOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(matcher.advance(now=NOW + timedelta(seconds=2)), ())
        self.assertEqual(matcher.balances["BTC"], D("0.5"))
        self.assertEqual(matcher.balances["USD"], D("950.4505"))

    def test_cancel_releases_only_remaining_hold_and_keeps_fills(self):
        matcher = self.matcher()
        order = matcher.submit(request(quantity="2"), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("98", "5")), asks=depth(("99", "0.5")), now=NOW)
        canceled = matcher.cancel(order.order_id, now=NOW)
        self.assertEqual(canceled.status, PaperOrderStatus.CANCELED)
        self.assertEqual(canceled.filled_quantity, D("0.5"))
        self.assertEqual(matcher.available_balances, matcher.balances)
        self.assertEqual(matcher.cancel(order.order_id, now=NOW), canceled)

    def test_multiple_orders_share_depth_and_active_book_cash(self):
        matcher = self.matcher(balances={"USD": D("200")}, fee_rate=D(0))
        first = matcher.submit(request("one"), now=NOW)
        second = matcher.submit(request("two"), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_INSUFFICIENT_FUNDS"):
            matcher.submit(request("three"), now=NOW)
        fills = matcher.update_book("BTC-USD", bids=depth(("99", "3")), asks=depth(("100", "1.5")), now=NOW)
        self.assertEqual(sum((fill.quantity for fill in fills), D(0)), D("1.5"))
        self.assertEqual(matcher.order(first.order_id).status, PaperOrderStatus.FILLED)
        self.assertEqual(matcher.order(second.order_id).filled_quantity, D("0.5"))

    def test_price_limits_depth_levels_and_deterministic_fees(self):
        matcher = self.matcher()
        order = matcher.submit(request(quantity="3", price="101"), now=NOW)
        fills = matcher.update_book("BTC-USD", bids=depth(("99", "10")),
                                    asks=depth(("100", "1"), ("101", "1"), ("102", "10")), now=NOW)
        self.assertEqual([fill.price for fill in fills], [D("100"), D("101")])
        self.assertEqual(matcher.order(order.order_id).remaining_quantity, D("1"))
        self.assertEqual(matcher.balances["USD"], D("798.799"))

    def test_no_short_spot_and_no_live_construction(self):
        matcher = self.matcher()
        with self.assertRaisesRegex(DomainError, "PAPER_INSUFFICIENT_INVENTORY"):
            matcher.submit(request(side=Side.SELL), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_REQUIRES_SANDBOX"):
            PaperMatcher(context(environment=Environment.LIVE), (instrument(),), initial_balances={"USD": D("1000")})

    def test_idempotent_local_client_id_is_not_permission_to_change_payload(self):
        matcher = self.matcher()
        order = matcher.submit(request(), now=NOW)
        self.assertEqual(order, matcher.submit(request(), now=NOW))
        with self.assertRaisesRegex(DomainError, "PAPER_CLIENT_ID_CONFLICT"):
            matcher.submit(request(quantity="2"), now=NOW)

    def test_stale_books_do_not_fill_and_snapshots_keep_financial_state(self):
        matcher = self.matcher()
        order = matcher.submit(request(quantity="2"), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "0.5")), now=NOW)
        snapshot = matcher.dump_state()
        restored = PaperMatcher.restore(context(), (instrument(),), snapshot)
        self.assertEqual(restored.balances, matcher.balances)
        self.assertEqual(restored.orders, matcher.orders)
        self.assertEqual(restored.fills, matcher.fills)
        self.assertEqual(restored.advance(now=NOW + timedelta(seconds=1)), ())
        restored.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "0.5")), now=NOW + timedelta(seconds=2))
        self.assertEqual(restored.order(order.order_id).filled_quantity, D("1"))
        with self.assertRaisesRegex(DomainError, "PAPER_SNAPSHOT_CONTEXT_MISMATCH"):
            PaperMatcher.restore(context(epoch=2), (instrument(),), snapshot)
        corrupted = copy.deepcopy(snapshot)
        corrupted["fills"] = []
        with self.assertRaisesRegex(DomainError, "PAPER_SNAPSHOT_FILL_MISMATCH"):
            PaperMatcher.restore(context(), (instrument(),), corrupted)
        corrupted = copy.deepcopy(snapshot)
        corrupted["balances"] = [["USD", "1000000"], ["BTC", "0.5"]]
        with self.assertRaisesRegex(DomainError, "PAPER_SNAPSHOT_BALANCE_MISMATCH"):
            PaperMatcher.restore(context(), (instrument(),), corrupted)

    def test_book_sequence_and_ambient_decimal_precision_are_checked(self):
        matcher = self.matcher()
        with localcontext() as arithmetic:
            arithmetic.prec = 4
            matcher.submit(request(quantity="0.03", price="100.01"), now=NOW)
            matcher.update_book("BTC-USD", bids=depth(("99", "2")), asks=depth(("100.01", "1")), now=NOW, sequence=4)
        self.assertEqual(matcher.balances["USD"], D("996.9966997"))
        with self.assertRaisesRegex(DomainError, "PAPER_STALE_BOOK_SEQUENCE"):
            matcher.update_book("BTC-USD", bids=depth(("99", "2")), asks=depth(("100.01", "1")), now=NOW, sequence=4)

    def test_flat_rebind_preserves_high_scale_cash_and_namespaces_new_ids(self):
        contract = PaperInstrument(
            instrument_id="BTC-USD", base_currency="BTC", quote_currency="USD", product_track=ProductTrack.SPOT,
            quantity_increment=D("0.000000001"), price_increment=D("0.000000001"),
        )
        matcher = PaperMatcher(context(), (contract,), initial_balances={"USD": D("1000")}, fee_rate=D("0.000000001"))
        buy = matcher.submit(request(quantity="1.000000001", price="100.000000001"), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("100", "2")), asks=depth(("100.000000001", "2")), now=NOW)
        matcher.submit(request("close", side=Side.SELL, quantity="1.000000001", price="100.000000001", reduce_only=True), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("100.000000001", "2")), asks=depth(("100.000000002", "2")), now=NOW)
        exact_cash = D("999.999999799999999797999999998")
        self.assertEqual(matcher.balances["USD"], exact_cash)
        with self.assertRaisesRegex(DomainError, "DECIMAL_PRECISION_LIMIT"):
            PaperMatcher(context(epoch=2), (contract,), initial_balances={"USD": exact_cash})
        source_history = matcher.fills
        rebased = matcher.rebind_flat(context(epoch=2))
        self.assertEqual(rebased.balances["USD"], exact_cash)
        self.assertEqual(rebased.orders, ())
        self.assertEqual(rebased.fills, ())
        self.assertEqual(matcher.fills, source_history)
        with self.assertRaisesRegex(DomainError, "PAPER_CONTEXT_REBOUND"):
            matcher.submit(request("late-source"), now=NOW)
        restored = PaperMatcher.restore(context(epoch=2), (contract,), rebased.dump_state())
        self.assertEqual(restored.balances["USD"], exact_cash)
        new_order = restored.submit(request(quantity="0.1", price="100"), now=NOW)
        self.assertNotEqual(new_order.order_id, buy.order_id)
        restored.update_book("BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")), now=NOW)
        after_trade = PaperMatcher.restore(context(epoch=2), (contract,), restored.dump_state())
        self.assertEqual(after_trade.balances, restored.balances)

    def test_flat_rebind_rejects_wrong_realm_epoch_pending_orders_and_inventory(self):
        matcher = self.matcher()
        for candidate in (context(environment=Environment.LIVE, epoch=2), context(ProductTrack.PERPETUAL, epoch=2),
                          context(epoch=2).model_copy(update={"authority_id": "another-owner"}),
                          context(epoch=2).model_copy(update={"broker_profile_id": "different-profile"})):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(DomainError, "PAPER_REBIND_REALM_MISMATCH"):
                matcher.rebind_flat(candidate)
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_EPOCH_NOT_INCREASING"):
            matcher.rebind_flat(context())
        matcher.submit(request(), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_OPEN_ORDERS"):
            matcher.rebind_flat(context(epoch=2))
        matcher.update_book("BTC-USD", bids=depth(("99", "2")), asks=depth(("100", "2")), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_NONFLAT_INVENTORY"):
            matcher.rebind_flat(context(epoch=2))

    def test_flat_rebind_provenance_is_required_for_derived_seed_restore(self):
        matcher = self.matcher()
        target = context(epoch=2)
        state = matcher.rebind_flat(target).dump_state()
        corrupted = copy.deepcopy(state)
        corrupted["flat_rebind_provenance"] = None
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_PROVENANCE_MISSING"):
            PaperMatcher.restore(target, (instrument(),), corrupted)
        corrupted = copy.deepcopy(state)
        corrupted["initial_balances"] = [["USD", "1000000"]]
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_PROVENANCE_MISMATCH"):
            PaperMatcher.restore(target, (instrument(),), corrupted)


class PaperMarginTests(unittest.TestCase):
    def test_linear_margin_fees_funding_and_reduction_do_not_reverse_position(self):
        margin = PaperMarginBook(D("1000"))
        matcher = PaperMatcher(context(ProductTrack.PERPETUAL), (instrument(ProductTrack.PERPETUAL),),
                               initial_balances={}, margin=margin)
        matcher.submit(request(quantity="2"), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("99", "10")), asks=depth(("100", "10")), now=NOW)
        self.assertEqual(margin.quantity("BTC-USD"), D("2"))
        self.assertEqual(margin.collateral, D("999.8"))
        self.assertTrue(margin.apply_funding("funding-1", "BTC-USD", D("-0.05")))
        self.assertFalse(margin.apply_funding("funding-1", "BTC-USD", D("-0.05")))
        self.assertEqual(margin.collateral, D("999.75"))
        close = matcher.submit(request("close", side=Side.SELL, quantity="2", price="109", reduce_only=True), now=NOW)
        fills = matcher.update_book("BTC-USD", bids=depth(("110", "1")), asks=depth(("111", "5")), now=NOW)
        self.assertEqual(fills[0].realized_pnl, D("10"))
        self.assertEqual(margin.quantity("BTC-USD"), D("1"))
        self.assertEqual(matcher.order(close.order_id).remaining_quantity, D("1"))
        with self.assertRaisesRegex(DomainError, "PAPER_REDUCE_ONLY_VIOLATION"):
            matcher.submit(request("oversized", side=Side.SELL, quantity="2", price="109", reduce_only=True), now=NOW)
        matcher.update_book("BTC-USD", bids=depth(("110", "5")), asks=depth(("111", "5")), now=NOW)
        self.assertEqual(margin.quantity("BTC-USD"), D("0"))
        self.assertEqual(margin.collateral, D("1019.53"))

    def test_pending_orders_reserve_shared_margin(self):
        margin = PaperMarginBook(D("10"))
        matcher = PaperMatcher(context(ProductTrack.PERPETUAL), (instrument(ProductTrack.PERPETUAL),),
                               initial_balances={}, margin=margin, fee_rate=D(0))
        matcher.submit(request(), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_INSUFFICIENT_MARGIN"):
            matcher.submit(request("second"), now=NOW)

    def test_margin_snapshot_and_position_state_restore(self):
        margin = PaperMarginBook(D("1000"))
        margin.apply_fill("BTC-USD", D("1"), D("100"))
        margin.apply_fill("BTC-USD", D("2"), D("101"))
        margin.set_mark("BTC-USD", D("110"))
        self.assertEqual(margin.snapshot().unrealized_pnl.quantize(D("0.01")), D("28.00"))
        restored = PaperMarginBook.restore(margin.dump_state())
        self.assertEqual(restored.snapshot(), margin.snapshot())

    def test_insufficient_margin_rejection_leaves_cash_and_position_unchanged(self):
        margin = PaperMarginBook(D("1"))
        with self.assertRaisesRegex(DomainError, "PAPER_INSUFFICIENT_MARGIN"):
            margin.apply_fill("BTC-USD", D("1"), D("100"), fee=D("0.1"))
        self.assertEqual(margin.collateral, D("1"))
        self.assertEqual(margin.positions, ())

    def test_flat_margin_rebind_retains_exact_collateral(self):
        margin = PaperMarginBook(D("1000"))
        margin.apply_fill("BTC-USD", D("1"), D("100"), fee=D("0.0000000000000000000000001"))
        matcher = PaperMatcher(context(ProductTrack.PERPETUAL), (instrument(ProductTrack.PERPETUAL),),
                               initial_balances={}, margin=margin)
        with self.assertRaisesRegex(DomainError, "PAPER_REBIND_NONFLAT_POSITIONS"):
            matcher.rebind_flat(context(ProductTrack.PERPETUAL, epoch=2))
        margin.apply_fill("BTC-USD", D("-1"), D("100"), fee=D("0.0000000000000000000000001"), reduce_only=True)
        target = context(ProductTrack.PERPETUAL, epoch=2)
        rebased = matcher.rebind_flat(target)
        self.assertEqual(rebased.margin.collateral, D("999.9999999999999999999999998"))
        restored = PaperMatcher.restore(target, (instrument(ProductTrack.PERPETUAL),), rebased.dump_state())
        self.assertEqual(restored.margin.collateral, rebased.margin.collateral)


class ShadowTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_feed_mutates_only_explicit_synthetic_book(self):
        class Feed:
            async def next_frame(self):
                return MarketFrame(instrument_id="BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")), as_of=NOW, sequence=1)

        config = EngineConfig(authority_id="owner", execution_scope=("paper",), run_profile=RunProfile.SHADOW)
        matcher = PaperMatcher(context(), (instrument(),), initial_balances={"USD": D("1000")})
        matcher.submit(request(), now=NOW)
        fills = await ShadowRunner(config, Feed(), matcher).consume_once()
        self.assertEqual(len(fills), 1)
        self.assertEqual(matcher.context.environment, Environment.SANDBOX)
        self.assertFalse(hasattr(ShadowRunner, "sign"))
