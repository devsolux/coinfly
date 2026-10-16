"""Public feed resilience: reconnect policy, stale books after a loss, health honesty, request spacing.

Regression tests for the audit of ``coinfly.market.coinbase_public``. Scripted
sockets and a scripted reader; no network. Each test names the failure it
reproduces (a healthy session never resetting the backoff, books surviving a
peer close, false sequence gaps on multiplexed products, an optimistic
CONNECTED state, unspaced failed polls).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from unittest import TestCase, mock

from coinfly.market.coinbase_public import (
    MAX_BACKOFF_SECONDS,
    MIN_REQUEST_SPACING_SECONDS,
    CoinbasePublicRestFeed,
    CoinbasePublicWebSocketFeed,
)
from coinfly.market.observations import FeedState
from tests.market.test_coinbase_public import NOW, FakeReader, FakeSocket, heartbeat, l2, product

SNAPSHOT = [("bid", "99.90", "1"), ("offer", "100.10", "1")]
ETH_SNAPSHOT = [("bid", "49.90", "1"), ("offer", "50.10", "1")]


class ScriptedSocket(FakeSocket):
    """A FakeSocket whose script ends with an arbitrary exception instead of the silence timeout."""

    def __init__(self, messages: list[str | None], error: Exception) -> None:
        super().__init__(messages)
        self.error = error

    def receive(self) -> str | None:
        if not self.messages:
            raise self.error
        return super().receive()


class LongSessionSocket(FakeSocket):
    """A connection that stays up for two minutes of (fake) monotonic time before the peer goes silent."""

    def __init__(self, ticks: list[float]) -> None:
        super().__init__([])
        self.ticks = ticks

    def receive(self) -> str | None:
        self.ticks[0] += 120.0
        return super().receive()


class WebSocketFeedCase(TestCase):
    def setUp(self) -> None:
        self.reader = FakeReader([product("BTC-USD"), product("ETH-USD")])
        self.sleeps: list[float] = []

    def make_feed(self, factory: Callable[[], FakeSocket], *, instruments: tuple[str, ...] = ("BTC-USD",),
                  max_reconnects: int | None = None) -> CoinbasePublicWebSocketFeed:
        feed = CoinbasePublicWebSocketFeed(quote_currencies=("USD",), reader=self.reader, client_factory=factory,  # type: ignore[arg-type]
                                           clock=lambda: NOW + timedelta(seconds=1), sleeper=self.sleeps.append,
                                           max_reconnects=max_reconnects)
        feed._thread = object()  # type: ignore[assignment]  # keep the connection thread out of the test
        feed.start(list(instruments))
        return feed

    def scripted(self, *scripts: list[str | None]) -> Callable[[], FakeSocket]:
        remaining = list(scripts)
        return lambda: FakeSocket(remaining.pop(0) if remaining else [])


class ReconnectBackoffTests(WebSocketFeedCase):
    def test_backoff_restarts_after_a_healthy_session(self) -> None:
        ticks = [0.0]
        sockets: list[FakeSocket] = [FakeSocket([]), FakeSocket([]), LongSessionSocket(ticks), FakeSocket([])]
        feed = self.make_feed(lambda: sockets.pop(0), max_reconnects=3)
        feed._monotonic = lambda: ticks[0]
        with mock.patch("coinfly.market.coinbase_public.random.uniform", return_value=0.0):
            feed._run()
        # Two quick failures double the wait; a session that held for minutes was healthy, so the next wait starts over.
        self.assertEqual(self.sleeps, [1.0, 2.0, 1.0])

    def test_jitter_never_pushes_a_wait_past_the_cap(self) -> None:
        feed = self.make_feed(self.scripted(), max_reconnects=8)
        with mock.patch("coinfly.market.coinbase_public.random.uniform", side_effect=lambda _low, high: high):
            feed._run()
        self.assertEqual(len(self.sleeps), 8)
        self.assertLessEqual(max(self.sleeps), MAX_BACKOFF_SECONDS)
        self.assertEqual(self.sleeps[-1], MAX_BACKOFF_SECONDS)


class ConnectionLossTests(WebSocketFeedCase):
    def test_every_way_of_losing_the_connection_drops_the_books(self) -> None:
        endings: dict[str, Callable[[], FakeSocket]] = {
            "peer closed": lambda: FakeSocket([heartbeat(0, 1), l2(1, "snapshot", SNAPSHOT), None]),
            "silent timeout": lambda: FakeSocket([heartbeat(0, 1), l2(1, "snapshot", SNAPSHOT), "RAISE:WEBSOCKET_SILENT_TIMEOUT"]),
            "protocol error": lambda: FakeSocket([heartbeat(0, 1), l2(1, "snapshot", SNAPSHOT), "RAISE:WEBSOCKET_PROTOCOL_ERROR"]),
            "transport error": lambda: ScriptedSocket([heartbeat(0, 1), l2(1, "snapshot", SNAPSHOT)], ConnectionResetError("reset")),
        }
        for name, socket_factory in endings.items():
            with self.subTest(ending=name):
                feed = self.make_feed(socket_factory, max_reconnects=0)
                feed._run()
                # Pre-disconnect books are history: nothing may decide on them while the feed reconnects.
                self.assertIsNone(feed.latest("BTC-USD"))
                health = feed.health()
                self.assertEqual(health.instrument_observed_at, {})
                self.assertIs(health.state, FeedState.DISCONNECTED)


class HealthHonestyTests(WebSocketFeedCase):
    def test_gapless_multiplexed_stream_reports_no_sequence_gaps(self) -> None:
        feed = self.make_feed(self.scripted([
            heartbeat(0, 1),
            l2(1, "snapshot", SNAPSHOT),
            l2(2, "snapshot", ETH_SNAPSHOT, product_id="ETH-USD"),
            l2(3, "update", [("bid", "99.95", "1")]),
            l2(4, "update", [("bid", "49.95", "1")], product_id="ETH-USD"),
            l2(5, "update", [("bid", "99.96", "1")]),
            l2(6, "update", [("bid", "49.96", "1")], product_id="ETH-USD"),
            None,
        ]), instruments=("BTC-USD", "ETH-USD"))
        feed._max_reconnects = 0
        feed._run()
        self.assertEqual(feed.health().sequence_gaps, 0)
        produced = feed.poll(NOW)
        for instrument in ("BTC-USD", "ETH-USD"):
            # Each instrument's own observation counter is contiguous; there is no shared counter to look like a gap.
            self.assertEqual([item.sequence for item in produced if item.instrument == instrument], [1, 2, 3])

    def test_a_real_sequence_gap_is_what_sequence_gaps_reports(self) -> None:
        feed = self.make_feed(self.scripted([heartbeat(0, 1), l2(1, "snapshot", SNAPSHOT), l2(5, "update", [("bid", "99.95", "1")])]),
                              max_reconnects=0)
        feed._run()
        health = feed.health()
        self.assertEqual((health.sequence_gaps, health.resynchronizations), (1, 1))

    def test_connected_waits_for_every_subscribed_book(self) -> None:
        feed = self.make_feed(self.scripted(), instruments=("BTC-USD", "ETH-USD"))
        feed._sequence.new_connection()
        feed.handle_message(heartbeat(0, 1))
        feed.handle_message(l2(1, "snapshot", SNAPSHOT))
        self.assertIs(feed.health().state, FeedState.CONNECTING)  # ETH has not delivered its snapshot yet
        self.assertIn("ETH-USD", feed.health().detail)
        feed.handle_message(l2(2, "snapshot", ETH_SNAPSHOT, product_id="ETH-USD"))
        self.assertIs(feed.health().state, FeedState.CONNECTED)

    def test_heartbeat_gap_stays_degraded_until_heartbeats_recover(self) -> None:
        feed = self.make_feed(self.scripted())
        feed._sequence.new_connection()
        feed.handle_message(heartbeat(0, 10))
        feed.handle_message(l2(1, "snapshot", SNAPSHOT))
        self.assertIs(feed.health().state, FeedState.CONNECTED)
        feed.handle_message(heartbeat(2, 14))  # counters 11..13 never arrived
        self.assertIs(feed.health().state, FeedState.DEGRADED)
        feed.handle_message(l2(3, "update", [("bid", "99.95", "1")]))
        self.assertIs(feed.health().state, FeedState.DEGRADED)  # a book update does not prove the heartbeats are back
        feed.handle_message(heartbeat(4, 15))
        self.assertIs(feed.health().state, FeedState.CONNECTED)


class RestSpacingTests(TestCase):
    def test_failed_polls_are_spaced_like_successful_ones(self) -> None:
        sleeps: list[float] = []
        reader = FakeReader([product("BTC-USD"), product("ETH-USD")])  # no books: every poll fails
        feed = CoinbasePublicRestFeed(reader=reader, clock=lambda: NOW, sleeper=sleeps.append)  # type: ignore[arg-type]
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD", "ETH-USD"])
        self.assertEqual(feed.sweep_once(), 0)
        self.assertEqual(len(sleeps), 2)  # one spacing wait per failed request, not back-to-back
        self.assertTrue(all(0 < wait <= MIN_REQUEST_SPACING_SECONDS for wait in sleeps))
