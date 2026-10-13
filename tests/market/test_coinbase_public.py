"""Public Coinbase market data feeds against synthetic transports (no socket is opened).

Message shapes follow the Advanced Trade documentation accessed 2026-09-21
(REST ``/market/products`` and ``/market/product_book``; WebSocket ``level2``
snapshot/update events with absolute ``new_quantity`` and per-connection
``sequence_num``; ``heartbeats`` with ``heartbeat_counter``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import TestCase

from coinfly.config.models import EngineConfig, default_config
from coinfly.domain.context import DomainError, Environment, RunProfile
from coinfly.domain.products import ProductSubtype
from coinfly.market.coinbase_public import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REST_INSTRUMENTS,
    CoinbasePublicRestFeed,
    CoinbasePublicWebSocketFeed,
    PublicMarketReader,
    catalog_entry_from_product,
    classify_product,
    parse_venue_timestamp,
)
from coinfly.market.observations import DataSource, FeedState
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def product(product_id: str = "BTC-USD", **overrides: Any) -> dict[str, Any]:
    base, _, quote = product_id.partition("-")
    document: dict[str, Any] = {
        "product_id": product_id, "price": "100.00", "volume_24h": "12345.6", "base_increment": "0.00000001",
        "quote_increment": "0.01", "quote_min_size": "1", "base_min_size": "0.00000001", "status": "online",
        "cancel_only": False, "limit_only": False, "post_only": False, "trading_disabled": False,
        "product_type": "SPOT", "quote_currency_id": quote, "base_currency_id": base, "product_venue": "CBE",
    }
    document.update(overrides)
    return document


class FakeReader:
    """Stands in for PublicMarketReader; records calls, serves scripted responses."""

    def __init__(self, products: list[dict[str, Any]], books: dict[str, dict[str, Any]] | None = None) -> None:
        self._products = products
        self.books = books or {}
        self.failures: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.retired = False

    def products(self, *, product_type: str = "SPOT") -> list[Mapping[str, Any]]:
        self.calls.append(("products", product_type))
        return [item for item in self._products if item["product_type"] == product_type]

    def product_book(self, product_id: str, *, limit: int = 10) -> Mapping[str, Any]:
        self.calls.append(("product_book", product_id))
        pending = self.failures.get(product_id)
        if pending:
            raise DomainError(pending.pop(0))
        book = self.books.get(product_id)
        if book is None:
            raise DomainError("HTTP_READ_FAILED")
        return book

    def retire(self) -> None:
        self.retired = True


def pricebook(product_id: str, bid: str, ask: str, *, time: str = "2026-09-21T12:00:00.123456789Z") -> dict[str, Any]:
    return {"product_id": product_id, "bids": [{"price": bid, "size": "2"}, {"price": str(Decimal(bid) - Decimal("0.01")), "size": "3"}],
            "asks": [{"price": ask, "size": "1.5"}], "time": time}


class ParsingTests(TestCase):
    def test_timestamps_with_nanoseconds(self) -> None:
        parsed = parse_venue_timestamp("2023-02-09T20:32:50.714964855Z")
        self.assertEqual(parsed, datetime(2023, 2, 9, 20, 32, 50, 714964, tzinfo=timezone.utc))
        self.assertEqual(parse_venue_timestamp("2026-09-21T12:00:00Z"), NOW)
        for bad in ("yesterday", 12, "2026-13-40T00:00:00Z", ""):
            with self.assertRaisesRegex(DomainError, "VENUE_TIMESTAMP_INVALID"):
                parse_venue_timestamp(bad)

    def test_product_classification_follows_the_documented_contract_model(self) -> None:
        self.assertEqual(classify_product(product(), now=NOW), (ProductSubtype.SPOT, None))
        perpetual = product("BTC-PERP-INTX", product_type="FUTURE", product_venue="INTX",
                            future_product_details={"contract_expiry_type": "PERPETUAL"})
        self.assertEqual(classify_product(perpetual, now=NOW), (ProductSubtype.PERPETUAL_NO_EXPIRY, None))
        style = product("BIP-20DEC30-CDE", product_type="FUTURE", product_venue="FCM",
                        future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2030-12-20T16:00:00Z",
                                                "perpetual_details": {"funding_rate": "0.0001"}})
        self.assertEqual(classify_product(style, now=NOW), (ProductSubtype.PERPETUAL_STYLE_FUTURE, datetime(2030, 12, 20, 16, tzinfo=timezone.utc)))
        dated = product("BIT-26DEC26-CDE", product_type="FUTURE", product_venue="FCM",
                        future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2026-12-26T16:00:00Z"})
        self.assertEqual(classify_product(dated, now=NOW), (ProductSubtype.DATED_FUTURE, datetime(2026, 12, 26, 16, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(DomainError, "VENUE_PRODUCT_TYPE_UNKNOWN"):
            classify_product(product(product_type="OPTION"), now=NOW)
        with self.assertRaisesRegex(DomainError, "VENUE_PRODUCT_INVALID"):
            classify_product(product(product_type="FUTURE", future_product_details={"contract_expiry_type": "EXPIRING"}), now=NOW)
        # A long term without perpetual_details is ambiguous: refused, never guessed perpetual-style.
        long_dated = product("BIT-20DEC30-CDE", product_type="FUTURE", product_venue="FCM",
                             future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2030-12-20T16:00:00Z"})
        with self.assertRaisesRegex(DomainError, "VENUE_PRODUCT_SUBTYPE_UNVERIFIED"):
            classify_product(long_dated, now=NOW)
        empty_details = product("BIT-20DEC30-CDE", product_type="FUTURE", product_venue="FCM",
                                future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2030-12-20T16:00:00Z",
                                                        "perpetual_details": {}})
        with self.assertRaisesRegex(DomainError, "VENUE_PRODUCT_SUBTYPE_UNVERIFIED"):
            classify_product(empty_details, now=NOW)

    def test_catalog_reports_ambiguous_products_instead_of_guessing(self) -> None:
        from coinfly.market.coinbase_public import _CatalogCache

        reader = FakeReader([
            product("BIP-20DEC30-CDE", product_type="FUTURE", product_venue="FCM", quote_currency_id="USD",
                    future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2030-12-20T16:00:00Z",
                                            "perpetual_details": {"funding_rate": "0.0001"}}),
            product("BIT-20DEC30-CDE", product_type="FUTURE", product_venue="FCM", quote_currency_id="USD",
                    future_product_details={"contract_expiry_type": "EXPIRING", "contract_expiry": "2030-12-20T16:00:00Z"}),
            product("BAD-FUT", product_type="FUTURE", product_venue="FCM", quote_currency_id="USD",
                    future_product_details={"contract_expiry_type": "EXPIRING"}),
        ])
        cache = _CatalogCache(reader, quote_currencies=("USD",), subtype=ProductSubtype.PERPETUAL_STYLE_FUTURE, clock=lambda: NOW)
        self.assertEqual([entry.instrument_id for entry in cache.refresh()], ["BIP-20DEC30-CDE"])
        report = cache.report()
        self.assertEqual(report["entries"], 1)
        self.assertEqual(report["excluded"], {"VENUE_PRODUCT_SUBTYPE_UNVERIFIED": 1, "VENUE_PRODUCT_INVALID": 1})
        self.assertEqual(report["subtype_unverified"], ["BIT-20DEC30-CDE"])

    def test_catalog_entry_uses_exact_strings_and_status_flags(self) -> None:
        entry = catalog_entry_from_product(product(cancel_only=True, volume_24h="0.5"), now=NOW)
        self.assertEqual((entry.base_currency, entry.quote_currency, entry.subtype), ("BTC", "USD", ProductSubtype.SPOT))
        self.assertEqual(entry.base_increment, Decimal("0.00000001"))
        self.assertEqual(entry.minimum_notional, Decimal("1"))
        self.assertEqual(entry.volume_24h, Decimal("0.5"))
        self.assertFalse(entry.tradable)  # cancel_only
        self.assertIs(entry.source, DataSource.COINBASE_PUBLIC_REST)
        with self.assertRaisesRegex(DomainError, "VENUE_NUMBER_INVALID"):
            catalog_entry_from_product(product(base_increment=1e-8), now=NOW)  # floats are refused


class ReaderBoundaryTests(TestCase):
    def test_profile_and_query_allowlist(self) -> None:
        reader = PublicMarketReader()
        with self.assertRaisesRegex(DomainError, "HTTP_QUERY_FORBIDDEN"):
            reader.get("/products", query={"cursor": "x"})
        with self.assertRaisesRegex(DomainError, "HTTP_QUERY_INVALID"):
            reader.get("/product_book", query={"product_id": "BTC-USD?x=1", "limit": "5"})
        with self.assertRaisesRegex(DomainError, "HTTP_QUERY_INVALID"):
            reader.products(product_type="OPTION")
        with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
            reader.get("/accounts")
        with self.assertRaisesRegex(DomainError, "PUBLIC_READER_REQUIRES_UNAUTHENTICATED_ENDPOINT"):
            PublicMarketReader(endpoint_profile("coinbase-advanced-spot"))
        profile = endpoint_profile("coinbase-public-market")
        self.assertFalse(profile.authenticated)
        self.assertEqual(profile.validate("GET", "/product_book", purpose=None),
                         "https://api.coinbase.com/api/v3/brokerage/market/product_book")
        with self.assertRaisesRegex(DomainError, "UNAUTHENTICATED_ENDPOINT_FORBIDS_CREDENTIALS"):
            from coinfly.security.credential_realms import CredentialPurpose

            profile.validate("GET", "/products", purpose=CredentialPurpose.OBSERVATION)

    def test_live_configuration_cannot_point_at_the_public_profile(self) -> None:
        base = default_config().model_dump()
        with self.assertRaisesRegex(ValueError, "public-data endpoint"):
            EngineConfig.model_validate({**base, "environment": Environment.LIVE, "run_profile": RunProfile.STANDARD,
                                         "credential_binding_id": "a" * 32, "endpoint_profile_id": "coinbase-public-market"})
        with self.assertRaisesRegex(ValueError, "SANDBOX endpoint does not match"):
            EngineConfig.model_validate({**base, "endpoint_profile_id": "coinbase-public-market"})


class RestFeedTests(TestCase):
    def setUp(self) -> None:
        self.reader = FakeReader([product("BTC-USD"), product("ETH-USD"), product("BTC-EUR"),
                                  product("BTC-PERP-INTX", product_type="FUTURE", future_product_details={"contract_expiry_type": "PERPETUAL"})],
                                 {"BTC-USD": pricebook("BTC-USD", "99.90", "100.10"), "ETH-USD": pricebook("ETH-USD", "49.90", "50.10")})
        self.clock = NOW
        self.sleeps: list[float] = []
        self.feed = CoinbasePublicRestFeed(quote_currencies=("USD",), reader=self.reader, clock=lambda: self.clock,  # type: ignore[arg-type]
                                           sleeper=self.sleeps.append)

    def test_catalog_filters_subtype_and_quote_and_is_cached(self) -> None:
        names = [entry.instrument_id for entry in self.feed.catalog()]
        self.assertEqual(names, ["BTC-USD", "ETH-USD"])
        self.feed.catalog()
        self.assertEqual(self.reader.calls.count(("products", "SPOT")), 1)
        self.clock = NOW + timedelta(minutes=11)
        self.feed.catalog()
        self.assertEqual(self.reader.calls.count(("products", "SPOT")), 2)

    def test_sweep_produces_labeled_observations_and_reports_failures(self) -> None:
        with self.assertRaisesRegex(DomainError, "FEED_INSTRUMENT_UNKNOWN"):
            self.feed.start(["DOGE-USD"])
        with self.assertRaisesRegex(DomainError, "FEED_INSTRUMENT_LIMIT"):
            self.feed.start([f"X{index}-USD" for index in range(MAX_REST_INSTRUMENTS + 1)])
        self.feed._thread = object()  # type: ignore[assignment]  # keep the polling thread out of the test
        self.feed.start(["BTC-USD", "ETH-USD"])
        self.assertIs(self.feed.health().state, FeedState.CONNECTING)
        self.assertEqual(self.feed.sweep_once(), 2)
        produced = self.feed.poll(NOW)
        self.assertEqual([item.instrument for item in produced], ["BTC-USD", "ETH-USD"])
        self.assertEqual(self.feed.poll(NOW), ())  # drained
        first = produced[0]
        self.assertIs(first.source, DataSource.COINBASE_PUBLIC_REST)
        self.assertEqual(first.observed_at, datetime(2026, 9, 21, 12, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual([level.price for level in first.bids], [Decimal("99.90"), Decimal("99.89")])
        self.assertEqual(first.asks[0].quantity, Decimal("1.5"))
        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.lineage, "GET /market/product_book poll 1")
        self.assertIs(self.feed.health().state, FeedState.CONNECTED)
        self.assertTrue(self.feed.health().as_dict()["real_market_data"])
        self.assertTrue(self.sleeps)  # request spacing honoured
        # Repeated failures degrade, then disconnect; recovery counts a reconnect.
        self.reader.failures["ETH-USD"] = ["HTTP_READ_FAILED"]
        self.feed.sweep_once()
        health = self.feed.health()
        self.assertIs(health.state, FeedState.DEGRADED)  # one instrument failed in this sweep
        self.assertEqual(health.detail, "HTTP_READ_FAILED on ETH-USD")
        # Every request failing for several sweeps = disconnected (rate limit backs off).
        self.reader.failures["BTC-USD"] = ["HTTP_RATE_LIMITED"] * MAX_CONSECUTIVE_FAILURES
        self.reader.failures["ETH-USD"] = ["HTTP_READ_FAILED"] * MAX_CONSECUTIVE_FAILURES
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            self.feed.sweep_once()
        health = self.feed.health()
        self.assertIs(health.state, FeedState.DISCONNECTED)
        self.assertIn("HTTP_READ_FAILED", health.detail)
        self.assertIn(4.0, self.sleeps)  # rate-limit back-off (2 x poll interval)
        self.assertEqual(self.feed.sweep_once(), 2)
        self.assertEqual(self.feed.health().reconnects, 1)
        latest = self.feed.latest("BTC-USD")
        assert latest is not None
        self.assertGreater(latest.sequence, 1)
        self.feed._thread = None  # type: ignore[assignment]
        self.feed.stop()
        self.assertTrue(self.reader.retired)
        self.assertIsNone(self.feed.latest("BTC-USD"))


class FakeSocket:
    """Scripted server side of a WebSocket connection."""

    def __init__(self, messages: list[str | None]) -> None:
        self.messages = messages
        self.sent: list[str] = []
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def receive(self) -> str | None:
        if not self.messages:
            raise DomainError("WEBSOCKET_SILENT_TIMEOUT")
        item = self.messages.pop(0)
        if isinstance(item, str) and item.startswith("RAISE:"):
            raise DomainError(item[6:])
        return item

    def close(self) -> None:
        self.closed = True


def l2(sequence: int, event_type: str, updates: list[tuple[str, str, str]], *, product_id: str = "BTC-USD",
       timestamp: str = "2026-09-21T12:00:01.000000000Z") -> str:
    return json.dumps({"channel": "l2_data", "client_id": "", "timestamp": timestamp, "sequence_num": sequence,
                       "events": [{"type": event_type, "product_id": product_id,
                                   "updates": [{"side": side, "event_time": timestamp, "price_level": price, "new_quantity": quantity}
                                               for side, price, quantity in updates]}]})


def heartbeat(sequence: int, counter: int) -> str:
    return json.dumps({"channel": "heartbeats", "timestamp": "2026-09-21T12:00:00Z", "sequence_num": sequence,
                       "events": [{"current_time": "2026-09-21 12:00:00.000000+00", "heartbeat_counter": str(counter)}]})


class WebSocketFeedTests(TestCase):
    def setUp(self) -> None:
        self.reader = FakeReader([product("BTC-USD"), product("ETH-USD")])
        self.sockets: list[FakeSocket] = []
        self.scripts: list[list[str | None]] = []
        self.sleeps: list[float] = []

    def make_feed(self, *scripts: list[str | None], max_reconnects: int | None = None) -> CoinbasePublicWebSocketFeed:
        self.scripts = list(scripts)

        def factory() -> FakeSocket:
            socket = FakeSocket(self.scripts.pop(0) if self.scripts else [])
            self.sockets.append(socket)
            return socket

        return CoinbasePublicWebSocketFeed(quote_currencies=("USD",), reader=self.reader, client_factory=factory,  # type: ignore[arg-type]
                                           clock=lambda: NOW + timedelta(seconds=1), sleeper=self.sleeps.append, max_reconnects=max_reconnects)

    def test_snapshot_then_updates_build_observations_with_absolute_quantities(self) -> None:
        feed = self.make_feed([
            json.dumps({"channel": "subscriptions", "sequence_num": 0, "events": [{"subscriptions": {"level2": ["BTC-USD"]}}]}),
            heartbeat(1, 10),
            l2(2, "snapshot", [("bid", "99.90", "1"), ("bid", "99.80", "2"), ("offer", "100.10", "1.5"), ("offer", "100.20", "1")]),
            l2(3, "update", [("bid", "99.90", "0"), ("offer", "100.05", "0.7")]),
            heartbeat(4, 11),
            None,
        ])
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_PEER_CLOSED"):
            feed.run_connection()
        socket = self.sockets[0]
        self.assertTrue(socket.connected and socket.closed)
        self.assertEqual([json.loads(item) for item in socket.sent],
                         [{"type": "subscribe", "channel": "heartbeats"}, {"type": "subscribe", "channel": "level2", "product_ids": ["BTC-USD"]}])
        produced = feed.poll(NOW)
        self.assertEqual(len(produced), 2)
        snapshot, update = produced
        self.assertIs(snapshot.source, DataSource.COINBASE_PUBLIC_WEBSOCKET)
        self.assertEqual([(level.price, level.quantity) for level in snapshot.bids], [(Decimal("99.90"), Decimal("1")), (Decimal("99.80"), Decimal("2"))])
        self.assertEqual(snapshot.observed_at, datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc))
        self.assertEqual([level.price for level in update.bids], [Decimal("99.80")])  # 99.90 removed (quantity 0)
        self.assertEqual([level.price for level in update.asks], [Decimal("100.05"), Decimal("100.10"), Decimal("100.20")])
        self.assertEqual((snapshot.sequence, update.sequence), (1, 2))
        self.assertEqual(update.lineage, "level2 update seq 3 gen 1")
        health = feed.health()
        self.assertEqual((health.sequence_gaps, health.resynchronizations), (0, 0))

    def test_sequence_gap_forces_resync_and_reconnects_are_counted(self) -> None:
        feed = self.make_feed(
            [heartbeat(0, 1), l2(1, "snapshot", [("bid", "99.90", "1"), ("offer", "100.10", "1")]), l2(5, "update", [("bid", "99.95", "1")])],
            [heartbeat(0, 1), l2(1, "snapshot", [("bid", "99.90", "1"), ("offer", "100.10", "1")]), "RAISE:WEBSOCKET_SILENT_TIMEOUT"],
            [json.dumps({"type": "error", "message": "rejected"})],
        )
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        feed._max_reconnects = 2
        feed._run()
        self.assertEqual(len(self.sockets), 3)
        health = feed.health()
        self.assertEqual(health.resynchronizations, 1)
        self.assertEqual(health.reconnects, 2)  # the third failure exhausts the budget without another attempt
        self.assertIs(health.state, FeedState.DISCONNECTED)
        self.assertEqual(health.detail, "reconnect budget exhausted")
        self.assertEqual(len(self.sleeps), 2)  # exponential backoff between attempts
        self.assertLess(self.sleeps[0], self.sleeps[1])
        # The update after a gap was never applied: only the two snapshots reached the store.
        self.assertEqual([item.lineage for item in feed.poll(NOW)], ["level2 snapshot seq 1 gen 1", "level2 snapshot seq 1 gen 2"])

    def test_update_before_snapshot_and_malformed_messages_are_protocol_errors(self) -> None:
        feed = self.make_feed([])
        feed._thread = object()  # type: ignore[assignment]
        feed.start(["BTC-USD"])
        with self.assertRaisesRegex(DomainError, "VENUE_BOOK_UNSYNCHRONIZED"):
            feed.handle_message(l2(0, "update", [("bid", "1", "1")]))
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_MESSAGE_INVALID"):
            feed.handle_message("not json")
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_SUBSCRIBE_REJECTED"):
            feed.handle_message(json.dumps({"type": "error", "message": "authentication failure"}))
        feed.handle_message(l2(1, "snapshot", [("bid", "99.90", "1"), ("offer", "100.10", "1")], product_id="ETH-USD"))
        self.assertEqual(feed.poll(NOW), ())  # not subscribed: ignored, never recorded
        feed._thread = None  # type: ignore[assignment]
        feed.stop()
        self.assertTrue(self.reader.retired)
