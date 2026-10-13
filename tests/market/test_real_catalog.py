"""The public catalog parsed from records captured from the live endpoint on 2026-09-22.

The synthetic fixtures in ``test_coinbase_public`` encode the documented
shapes; these records are what the endpoint actually returned, and they broke
three of those assumptions at once: single-letter asset codes (``S``), FCM
futures with an empty ``status`` and ``base_currency_id``, and a
``perpetual_details`` block on every futures record (monthly contracts
included) while the funding schedule appears only on perpetual-style ones. A
single invalid record used to raise a pydantic ValidationError out of the
catalog refresh, which failed the automation loop at SETUP for every feed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.domain.products import ProductSubtype
from coinfly.market.coinbase_public import (
    CoinbasePublicRestFeed,
    _CatalogCache,
    catalog_entry_from_product,
    classify_product,
)
from coinfly.market.observations import FeedState

CAPTURE = json.loads((Path(__file__).parent / "captures" / "coinbase_public_catalog_2026-09-22.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 9, 22, 19, 25, tzinfo=timezone.utc)


class CapturedReader:
    """Serves the captured records; optionally fails the next ``products`` calls."""

    def __init__(self) -> None:
        self.product_failures: list[str] = []
        self.books: dict[str, Mapping[str, Any]] = {name: value["pricebook"] for name, value in CAPTURE["product_book"].items()}
        self.calls: list[tuple[str, str]] = []

    def products(self, *, product_type: str = "SPOT") -> list[Mapping[str, Any]]:
        self.calls.append(("products", product_type))
        if self.product_failures:
            raise DomainError(self.product_failures.pop(0))
        return list(CAPTURE["spot" if product_type == "SPOT" else "future"]["products"])

    def product_book(self, product_id: str, *, limit: int = 10) -> Mapping[str, Any]:
        self.calls.append(("product_book", product_id))
        book = self.books.get(product_id)
        if book is None:
            raise DomainError("HTTP_READ_FAILED")
        return book

    def retire(self) -> None:
        pass


def captured(product_id: str) -> dict[str, Any]:
    for section in ("spot", "future"):
        for product in CAPTURE[section]["products"]:
            if product["product_id"] == product_id:
                return dict(product)
    raise KeyError(product_id)


class CapturedSpotCatalogTests(TestCase):
    def test_single_letter_assets_and_delisted_products_parse(self) -> None:
        cache = _CatalogCache(CapturedReader(), quote_currencies=("USD",), subtype=ProductSubtype.SPOT, clock=lambda: NOW)
        entries = {entry.instrument_id: entry for entry in cache.refresh()}
        self.assertEqual(sorted(entries), ["BTC-USD", "DAI-USD", "ETH-USD", "S-USD", "SOL-USD"])  # BTC-USDC: other quote
        self.assertEqual(entries["S-USD"].base_currency, "S")
        self.assertTrue(entries["S-USD"].tradable)
        self.assertFalse(entries["DAI-USD"].tradable)  # delisted, trading disabled
        btc = entries["BTC-USD"]
        self.assertEqual((btc.tick_size, btc.quote_volume_24h, btc.reference_price),
                         (Decimal("0.01"), Decimal("910398177.3"), Decimal("86485.75")))
        # Base volume is in coins: S trades 42M coins but a thousandth of BTC's dollar volume.
        self.assertGreater(entries["S-USD"].volume_24h, btc.volume_24h)  # type: ignore[operator]
        self.assertGreater(btc.ranking_volume, entries["S-USD"].ranking_volume)
        self.assertEqual(cache.report()["excluded"], {})

    def test_an_invalid_record_is_excluded_never_raised(self) -> None:
        reader = CapturedReader()
        broken = captured("BTC-USD") | {"product_id": "BAD-USD", "base_currency_id": "bad asset!"}
        huge = captured("ETH-USD") | {"product_id": "HUGE-USD", "base_increment": "0.0000000000000000001"}  # 19 places
        original = reader.products
        reader.products = lambda *, product_type="SPOT": [*original(product_type=product_type), broken, huge]  # type: ignore[method-assign]
        cache = _CatalogCache(reader, quote_currencies=("USD",), subtype=ProductSubtype.SPOT, clock=lambda: NOW)
        names = [entry.instrument_id for entry in cache.refresh()]
        self.assertNotIn("BAD-USD", names)
        self.assertNotIn("HUGE-USD", names)
        self.assertIn("BTC-USD", names)
        self.assertEqual(cache.report()["excluded"], {"VENUE_PRODUCT_INVALID": 2})
        with self.assertRaisesRegex(DomainError, "VENUE_PRODUCT_INVALID"):
            catalog_entry_from_product(huge, now=NOW)
        # An over-precise informational number (price, volume) is dropped, the product kept.
        noisy = catalog_entry_from_product(captured("S-USD") | {"approximate_quote_24h_volume": "1.1234567890123456789012"}, now=NOW)
        self.assertIsNone(noisy.quote_volume_24h)
        self.assertEqual(noisy.base_currency, "S")

    def test_catalog_failure_is_reported_backed_off_and_recovers(self) -> None:
        reader = CapturedReader()
        reader.product_failures = ["HTTP_READ_FAILED"]
        clock = [NOW]
        feed = CoinbasePublicRestFeed(reader=reader, clock=lambda: clock[0], sleeper=lambda _: None)  # type: ignore[arg-type]
        self.assertEqual(feed.catalog(), ())
        self.assertEqual(feed.health().catalog["last_error"], "HTTP_READ_FAILED")
        # Inside the back-off nobody blocks on the network again (the automation calls this every tick).
        for _ in range(5):
            self.assertEqual(feed.catalog(), ())
        with self.assertRaisesRegex(DomainError, "HTTP_READ_FAILED"):
            feed.start(["BTC-USD"])
        self.assertEqual(reader.calls.count(("products", "SPOT")), 1)
        clock[0] = NOW + timedelta(seconds=16)
        self.assertEqual(len(feed.catalog()), 5)
        self.assertNotIn("last_error", feed.health().catalog)
        self.assertEqual(reader.calls.count(("products", "SPOT")), 2)


class CapturedFuturesCatalogTests(TestCase):
    def test_funding_schedule_not_open_interest_marks_perpetual_style(self) -> None:
        expected = {
            "BIP-20DEC30-CDE": ProductSubtype.PERPETUAL_STYLE_FUTURE, "ETP-20DEC30-CDE": ProductSubtype.PERPETUAL_STYLE_FUTURE,
            "SLP-20DEC30-CDE": ProductSubtype.PERPETUAL_STYLE_FUTURE, "TEK-19DEC30-CDE": ProductSubtype.PERPETUAL_STYLE_FUTURE,
            # Monthly contracts carry perpetual_details (open interest) too, but no funding.
            "BIT-25SEP26-CDE": ProductSubtype.DATED_FUTURE, "BIT-30OCT26-CDE": ProductSubtype.DATED_FUTURE,
            "BIT-24DEC26-CDE": ProductSubtype.DATED_FUTURE, "GOL-25NOV26-CDE": ProductSubtype.DATED_FUTURE,
        }
        for product_id, subtype in expected.items():
            with self.subTest(product_id):
                self.assertIs(classify_product(captured(product_id), now=NOW)[0], subtype)

    def test_futures_entries_take_base_status_tick_and_contract_size_from_the_contract(self) -> None:
        cache = _CatalogCache(CapturedReader(), quote_currencies=("USD",), subtype=ProductSubtype.PERPETUAL_STYLE_FUTURE,
                              clock=lambda: NOW)
        entries = {entry.instrument_id: entry for entry in cache.refresh()}
        self.assertEqual(sorted(entries), ["BIP-20DEC30-CDE", "ETP-20DEC30-CDE", "SLP-20DEC30-CDE", "TEK-19DEC30-CDE"])
        bip = entries["BIP-20DEC30-CDE"]
        self.assertEqual((bip.base_currency, bip.contract_multiplier, bip.tick_size, bip.base_increment, bip.status),
                         ("BTC", Decimal("0.01"), Decimal("5"), Decimal("1"), "online"))
        self.assertTrue(bip.tradable)
        self.assertEqual(entries["TEK-19DEC30-CDE"].base_currency, "CDETEK")
        report = cache.report()
        self.assertEqual(report["other_subtypes"], {"DATED_FUTURE": 4})
        self.assertEqual(report["excluded"], {})

    def test_view_only_and_closed_sessions_are_not_tradable(self) -> None:
        view_only = catalog_entry_from_product(captured("BIT-30OCT26-CDE"), now=NOW)
        self.assertEqual(view_only.status, "online")
        self.assertTrue(view_only.trading_disabled)
        self.assertFalse(view_only.tradable)
        closed = catalog_entry_from_product(captured("BIT-24DEC26-CDE"), now=NOW)
        self.assertEqual(closed.status, "session_closed")
        self.assertFalse(closed.tradable)
        open_monthly = catalog_entry_from_product(captured("BIT-25SEP26-CDE"), now=NOW)
        self.assertTrue(open_monthly.tradable)
        # No session information at all: never assumed tradable.
        silent = catalog_entry_from_product(captured("BIP-20DEC30-CDE") | {"fcm_trading_session_details": None}, now=NOW)
        self.assertEqual(silent.status, "session_unknown")
        self.assertFalse(silent.tradable)


class CapturedBookTests(TestCase):
    def test_contract_book_and_a_crossed_book_do_not_abort_the_sweep(self) -> None:
        reader = CapturedReader()
        reader.books["ETP-20DEC30-CDE"] = {"product_id": "ETP-20DEC30-CDE", "time": "2026-09-22T19:24:29Z",
                                           "bids": [{"price": "2761", "size": "3"}], "asks": [{"price": "2760.5", "size": "2"}]}
        feed = CoinbasePublicRestFeed(reader=reader, subtype=ProductSubtype.PERPETUAL_STYLE_FUTURE,  # type: ignore[arg-type]
                                      clock=lambda: NOW, sleeper=lambda _: None)
        feed._thread = object()  # type: ignore[assignment]  # no polling thread in this test
        feed.start(["ETP-20DEC30-CDE", "BIP-20DEC30-CDE"])
        self.assertEqual(feed.sweep_once(), 1)  # the crossed ETP book is one failed poll, BIP still polled
        health = feed.health()
        self.assertIs(health.state, FeedState.CONNECTED)
        observation = feed.latest("BIP-20DEC30-CDE")
        assert observation is not None
        self.assertEqual((observation.bids[0].price, observation.asks[0].price), (Decimal("86535"), Decimal("86550")))
        self.assertIsNone(feed.latest("ETP-20DEC30-CDE"))
        feed._thread = None  # type: ignore[assignment]
        feed.stop()


class CapturedWebSocketTests(TestCase):
    """Nine consecutive messages of the public level2 + heartbeats stream (2026-09-22, snapshot trimmed to six levels a side)."""

    def test_captured_stream_builds_the_contract_book_without_gaps(self) -> None:
        from coinfly.market.coinbase_public import CoinbasePublicWebSocketFeed

        lines = (Path(__file__).parent / "captures" / "coinbase_ws_level2_2026-09-22.jsonl").read_text("utf-8").splitlines()
        feed = CoinbasePublicWebSocketFeed(reader=CapturedReader(), subtype=ProductSubtype.PERPETUAL_STYLE_FUTURE,  # type: ignore[arg-type]
                                           clock=lambda: datetime(2026, 9, 22, 19, 42, 1, 100000, tzinfo=timezone.utc),
                                           sleeper=lambda _: None)
        feed._thread = object()  # type: ignore[assignment]  # no connection thread: messages are fed directly
        feed.start(["BIP-20DEC30-CDE"])
        feed._sequence.new_connection()
        for line in lines:
            feed.handle_message(line)
        latest = feed.latest("BIP-20DEC30-CDE")
        assert latest is not None
        self.assertEqual((latest.bids[0].price, latest.bids[0].quantity), (Decimal("86425"), Decimal("8")))
        self.assertEqual((latest.asks[0].price, latest.asks[0].quantity), (Decimal("86435"), Decimal("9")))
        self.assertEqual(len(feed.poll(NOW)), 6)  # one observation per applied l2 event
        health = feed.health()
        self.assertIs(health.state, FeedState.CONNECTED)
        self.assertEqual((health.sequence["gaps"], health.sequence["duplicates"], health.sequence["messages"]), (0, 0, 9))
        feed._thread = None  # type: ignore[assignment]
        feed.stop()
