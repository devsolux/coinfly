"""WebSocket sequence contract: per connection or per product, multiplexing, duplicates, order, resets, overflow.

The venue documents both a per-connection and a per-product ``sequence_num``
(a documented conflict). ``SequenceTracker`` follows the evidence; the feed
applies only accepted messages, ignores duplicates/out-of-order ones, and
resynchronizes (reconnect → fresh snapshot, new generation) on a gap, a reset
or a consumer-queue overflow. Scripted sockets; no network.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.market import coinbase_public
from coinfly.market.observations import FeedState
from coinfly.market.sequence import (
    EVIDENCE_TO_LOCK,
    OUT_OF_ORDER_TOLERANCE,
    SequenceContract,
    SequenceTracker,
    SequenceVerdict,
)
from tests.market.test_coinbase_public import NOW, WebSocketFeedTests, heartbeat, l2

A, H = SequenceVerdict.ACCEPT, "heartbeats"


class TrackerTests(TestCase):
    def test_per_connection_stream_multiplexing_products_and_heartbeats_locks_the_connection_contract(self) -> None:
        tracker = SequenceTracker()
        tracker.new_connection()
        products = ["BTC-USD", "ETH-USD", None]
        for sequence in range(EVIDENCE_TO_LOCK + 4):
            product = products[sequence % 3]
            self.assertIs(tracker.observe(sequence, channel="l2_data" if product else H, product=product), A)
        self.assertIs(tracker.contract, SequenceContract.PER_CONNECTION)
        # A missed message under the connection contract is a gap even if the product's own count looks plausible.
        self.assertIs(tracker.observe(EVIDENCE_TO_LOCK + 6, channel="l2_data", product="BTC-USD"), SequenceVerdict.GAP)
        self.assertEqual(tracker.describe()["gaps"], 1)

    def test_per_product_stream_locks_the_product_contract_and_detects_product_gaps(self) -> None:
        tracker = SequenceTracker()
        tracker.new_connection()
        # Independent counters per product interleaved on one connection.
        for step in range(EVIDENCE_TO_LOCK + 2):
            self.assertIs(tracker.observe(step, channel="l2_data", product="BTC-USD"), A)
            self.assertIs(tracker.observe(step, channel="l2_data", product="ETH-USD"), A)
        self.assertIs(tracker.contract, SequenceContract.PER_PRODUCT)
        self.assertIs(tracker.observe(EVIDENCE_TO_LOCK + 2, channel="l2_data", product="BTC-USD"), A)
        self.assertIs(tracker.observe(EVIDENCE_TO_LOCK + 4, channel="l2_data", product="ETH-USD"), SequenceVerdict.GAP)

    def test_duplicates_out_of_order_and_resets(self) -> None:
        tracker = SequenceTracker()
        tracker.new_connection()
        for sequence in range(5):
            tracker.observe(sequence, channel="l2_data", product="BTC-USD")
        self.assertIs(tracker.observe(4, channel="l2_data", product="BTC-USD"), SequenceVerdict.DUPLICATE)
        self.assertIs(tracker.observe(2, channel="l2_data", product="BTC-USD"), SequenceVerdict.OUT_OF_ORDER)
        self.assertIs(tracker.observe(5, channel="l2_data", product="BTC-USD"), A)  # the stream continues normally
        for _ in range(OUT_OF_ORDER_TOLERANCE):
            self.assertIs(tracker.observe(1, channel="l2_data", product="BTC-USD"), SequenceVerdict.OUT_OF_ORDER)
        self.assertIs(tracker.observe(1, channel="l2_data", product="BTC-USD"), SequenceVerdict.GAP)  # a burst is a broken stream
        self.assertIs(tracker.observe(0, channel="l2_data", product="BTC-USD"), SequenceVerdict.RESET)
        description = tracker.describe()
        # The burst's last message is both out of order and the gap verdict.
        self.assertEqual((description["duplicates"], description["out_of_order"], description["gaps"], description["resets"]),
                         (1, OUT_OF_ORDER_TOLERANCE + 2, 1, 1))
        # A new connection is a new generation: counters of the contract detection start over.
        self.assertEqual(tracker.new_connection(), 2)
        self.assertIs(tracker.contract, SequenceContract.UNDETERMINED)
        self.assertIs(tracker.observe(0, channel="l2_data", product="BTC-USD"), A)

    def test_undetermined_stream_treats_anomalies_under_both_contracts_as_gaps(self) -> None:
        tracker = SequenceTracker()
        tracker.new_connection()
        tracker.observe(0, channel="l2_data", product="BTC-USD")
        tracker.observe(1, channel="l2_data", product="BTC-USD")
        self.assertIs(tracker.observe(3, channel="l2_data", product="BTC-USD"), SequenceVerdict.GAP)

    def test_rejects_invalid_sequences(self) -> None:
        tracker = SequenceTracker()
        tracker.new_connection()
        with self.assertRaises(ValueError):
            tracker.observe(-1, channel="l2_data", product="BTC-USD")


class FeedSequenceTests(WebSocketFeedTests):
    def snapshot(self, sequence: int, product: str, bid: str, ask: str) -> str:
        return l2(sequence, "snapshot", [("bid", bid, "1"), ("offer", ask, "1")], product_id=product)

    def test_multiplexed_products_keep_separate_books_and_duplicates_are_not_applied(self) -> None:
        feed = self.make_feed([
            heartbeat(0, 1),
            self.snapshot(1, "BTC-USD", "99.90", "100.10"),
            self.snapshot(2, "ETH-USD", "49.90", "50.10"),
            l2(3, "update", [("bid", "99.95", "2")]),
            l2(3, "update", [("bid", "99.99", "5")]),  # duplicate sequence: ignored, never applied
            l2(1, "update", [("bid", "49.99", "5")], product_id="ETH-USD"),  # older than ETH's snapshot: ignored
            heartbeat(4, 2),
            None,
        ])
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD", "ETH-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_PEER_CLOSED"):
            feed.run_connection()
        btc, eth = feed.latest("BTC-USD"), feed.latest("ETH-USD")
        assert btc is not None and eth is not None
        self.assertEqual(str(btc.bids[0].price), "99.95")
        self.assertEqual(str(eth.bids[0].price), "49.90")
        health = feed.health()
        self.assertEqual((health.sequence["duplicates"], health.sequence["out_of_order"], health.sequence["gaps"]), (1, 1, 0))
        self.assertEqual(health.sequence["generation"], 1)
        self.assertEqual(set(health.instrument_observed_at), {"BTC-USD", "ETH-USD"})
        self.assertIsNotNone(health.instrument_age("BTC-USD", NOW + timedelta(seconds=2)))
        self.assertEqual(health.resynchronizations, 0)

    def test_gap_invalidates_books_and_the_reconnection_is_a_new_generation_with_fresh_snapshots(self) -> None:
        feed = self.make_feed(
            [heartbeat(0, 1), self.snapshot(1, "BTC-USD", "99.90", "100.10"), l2(7, "update", [("bid", "99.95", "1")])],
            [heartbeat(0, 5), self.snapshot(1, "BTC-USD", "99.70", "100.30"), None],
        )
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_SEQUENCE_GAP"):
            feed.run_connection()
        self.assertIsNone(feed.latest("BTC-USD"))  # invalidated: no stale book survives a gap
        health = feed.health()
        self.assertIs(health.state, FeedState.DEGRADED)
        self.assertEqual(health.resynchronizations, 1)
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_PEER_CLOSED"):
            feed.run_connection()
        latest = feed.latest("BTC-USD")
        assert latest is not None
        self.assertEqual(str(latest.bids[0].price), "99.70")
        self.assertIn("gen 2", latest.lineage)
        self.assertEqual(feed.health().sequence["generation"], 2)

    def test_sequence_reset_forces_resync(self) -> None:
        feed = self.make_feed([heartbeat(3, 1), self.snapshot(4, "BTC-USD", "99.90", "100.10"), heartbeat(0, 2)])
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_SEQUENCE_GAP"):
            feed.run_connection()
        self.assertEqual(feed.health().sequence["resets"], 1)

    def test_update_after_reconnect_without_a_new_snapshot_is_refused(self) -> None:
        feed = self.make_feed([self.snapshot(0, "BTC-USD", "99.90", "100.10"), None],
                              [l2(0, "update", [("bid", "99.95", "1")])])
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_PEER_CLOSED"):
            feed.run_connection()
        with self.assertRaisesRegex(DomainError, "VENUE_BOOK_UNSYNCHRONIZED"):
            feed.run_connection()  # the old book is history; a delta before the new snapshot is a protocol error

    def test_consumer_queue_overflow_invalidates_and_resynchronizes(self) -> None:
        original = coinbase_public.MAX_PENDING_OBSERVATIONS
        coinbase_public.MAX_PENDING_OBSERVATIONS = 3
        self.addCleanup(setattr, coinbase_public, "MAX_PENDING_OBSERVATIONS", original)
        messages = [self.snapshot(0, "BTC-USD", "99.90", "100.10")]
        messages += [l2(index, "update", [("bid", f"99.{90 + index}", "1")]) for index in range(1, 6)]
        feed = self.make_feed(messages)
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_QUEUE_OVERFLOW"):
            feed.run_connection()
        health = feed.health()
        self.assertEqual((health.overflows, health.resynchronizations), (1, 1))
        self.assertIs(health.state, FeedState.DEGRADED)
        self.assertEqual(feed.poll(NOW), ())  # the stale queue was dropped, not coalesced
        self.assertIsNone(feed.latest("BTC-USD"))
