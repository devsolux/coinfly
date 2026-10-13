"""Credential-free local feeds: labels, determinism, ordering, retirement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.market.feed import ReplayFeed, _ObservationStore, record_observation_line, stale_after
from coinfly.market.fixtures import PROFILES, fixture_feed
from coinfly.market.observations import DataSource, DepthLevelObservation, FeedState, MarketObservation

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def observation(instrument: str, sequence: int, *, bid: str = "99.90", ask: str = "100.10", at: datetime = NOW,
                source: DataSource = DataSource.SYNTHETIC_FIXTURE) -> MarketObservation:
    return MarketObservation(instrument=instrument, bids=(DepthLevelObservation(price=Decimal(bid), quantity=Decimal("1")),),
                             asks=(DepthLevelObservation(price=Decimal(ask), quantity=Decimal("1")),), observed_at=at, received_at=at,
                             sequence=sequence, source=source, lineage="test")


class FixtureFeedTests(TestCase):
    def test_profiles_are_deterministic_and_labeled_as_fixtures(self) -> None:
        first, second = fixture_feed("random-walk-3"), fixture_feed("random-walk-3")
        for feed in (first, second):
            feed.start(["BTC-USD", "ETH-USD", "SOL-USD"])
        series_a = [feed_observation.mid for _ in range(25) for feed_observation in first.poll(NOW)]
        series_b = [feed_observation.mid for _ in range(25) for feed_observation in second.poll(NOW)]
        self.assertEqual(series_a, series_b)
        self.assertGreater(len(set(series_a)), 10)  # it moves
        health = first.health()
        self.assertIs(health.source, DataSource.SYNTHETIC_FIXTURE)
        self.assertEqual(health.as_dict()["label"], "SYNTHETIC FIXTURE — NOT MARKET DATA")
        self.assertFalse(health.as_dict()["real_market_data"])
        latest = first.latest("BTC-USD")
        assert latest is not None
        self.assertTrue(latest.two_sided)
        self.assertEqual(latest.sequence, 25)
        self.assertEqual({entry.quote_currency for entry in first.catalog()}, {"USD"})
        self.assertEqual(set(PROFILES), {"random-walk-3", "trend-up-1", "flat-1"})

    def test_unknown_instrument_and_profile_are_refused(self) -> None:
        with self.assertRaisesRegex(DomainError, "FIXTURE_PROFILE_UNKNOWN"):
            fixture_feed("does-not-exist")
        feed = fixture_feed("flat-1")
        with self.assertRaisesRegex(DomainError, "FEED_INSTRUMENT_UNKNOWN"):
            feed.start(["DOGE-USD"])

    def test_stop_revokes_later_reads(self) -> None:
        feed = fixture_feed("flat-1")
        feed.start(["BTC-USD"])
        feed.poll(NOW)
        self.assertIsNotNone(feed.latest("BTC-USD"))
        feed.stop()
        self.assertIsNone(feed.latest("BTC-USD"))
        self.assertIs(feed.health().state, FeedState.STOPPED)
        with self.assertRaisesRegex(DomainError, "FEED_STOPPED"):
            feed.start(["BTC-USD"])


class ObservationStoreTests(TestCase):
    def test_regressions_ignored_and_gaps_counted(self) -> None:
        store = _ObservationStore(DataSource.SYNTHETIC_FIXTURE)
        self.assertTrue(store.record(observation("BTC-USD", 1)))
        self.assertFalse(store.record(observation("BTC-USD", 1)))  # duplicate
        self.assertFalse(store.record(observation("BTC-USD", 0)))  # regression
        self.assertTrue(store.record(observation("BTC-USD", 5)))  # gap 2..4
        self.assertEqual(store.health().sequence_gaps, 1)
        latest = store.latest("BTC-USD")
        assert latest is not None
        self.assertEqual(latest.sequence, 5)

    def test_bounded_instruments(self) -> None:
        store = _ObservationStore(DataSource.SYNTHETIC_FIXTURE)
        for index in range(200):
            store.record(observation(f"A{index}-USD", 1))
        with self.assertRaisesRegex(DomainError, "FEED_INSTRUMENT_LIMIT"):
            store.record(observation("ZZZ-USD", 1))


class ReplayFeedTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-replay-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recording.jsonl"

    def write(self, *observations: MarketObservation) -> None:
        self.path.write_text("\n".join(record_observation_line(item) for item in observations) + "\n", encoding="utf-8")

    def test_replay_relabels_source_and_releases_by_time(self) -> None:
        self.write(observation("BTC-USD", 1, source=DataSource.COINBASE_PUBLIC_WEBSOCKET),
                   observation("BTC-USD", 2, at=NOW + timedelta(seconds=1), source=DataSource.COINBASE_PUBLIC_WEBSOCKET),
                   observation("ETH-USD", 3, at=NOW + timedelta(seconds=2), bid="49.9", ask="50.1", source=DataSource.COINBASE_PUBLIC_WEBSOCKET))
        feed = ReplayFeed(self.path)
        feed.start(["BTC-USD"])
        self.assertEqual(feed.remaining, 3)
        released = feed.poll(NOW)
        self.assertEqual([item.sequence for item in released], [1])
        self.assertIs(released[0].source, DataSource.REPLAY_FILE)
        self.assertTrue(released[0].lineage.startswith("replay of COINBASE_PUBLIC_WEBSOCKET"))
        self.assertIs(feed.health().state, FeedState.CONNECTED)
        released = feed.poll(NOW + timedelta(seconds=5))
        self.assertEqual([item.sequence for item in released], [2])  # ETH is not subscribed
        self.assertTrue(feed.exhausted)
        self.assertIs(feed.health().state, FeedState.DEGRADED)
        self.assertEqual(feed.health().as_dict()["label"], "REPLAYED RECORDING — NOT LIVE")
        self.assertEqual(feed.catalog()[0].source, DataSource.REPLAY_FILE)

    def test_non_chronological_or_invalid_lines_are_refused(self) -> None:
        self.write(observation("BTC-USD", 2, at=NOW + timedelta(seconds=1)), observation("BTC-USD", 3, at=NOW))
        with self.assertRaisesRegex(DomainError, "REPLAY_NOT_CHRONOLOGICAL"):
            ReplayFeed(self.path)
        self.path.write_text('{"not": "an observation"}\n', encoding="utf-8")
        with self.assertRaisesRegex(DomainError, "REPLAY_LINE_INVALID"):
            ReplayFeed(self.path)
        with self.assertRaisesRegex(DomainError, "REPLAY_FILE_INVALID"):
            ReplayFeed(self.path.with_name("missing.jsonl"))


class StalenessTests(TestCase):
    def test_stale_after(self) -> None:
        item = observation("BTC-USD", 1)
        self.assertFalse(stale_after(item, NOW + timedelta(seconds=4), timedelta(seconds=5)))
        self.assertTrue(stale_after(item, NOW + timedelta(seconds=6), timedelta(seconds=5)))
        self.assertTrue(stale_after(item, NOW - timedelta(seconds=1), timedelta(seconds=5)))  # future data is never fresh
        self.assertTrue(stale_after(None, NOW, timedelta(seconds=5)))
