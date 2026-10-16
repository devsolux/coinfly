"""Credential-free Coinbase public market data: REST catalog/book and WebSocket level2.

Both feeds are worker-owned, GET/subscribe-only and carry no credential of any
kind. They speak to the documented public endpoints (accessed 2026-09-21):

* REST ``GET https://api.coinbase.com/api/v3/brokerage/market/products`` and
  ``GET .../market/product_book`` — unauthenticated, JSON, numerics as strings.
* WebSocket ``wss://advanced-trade-ws.coinbase.com`` — public channels
  ``level2`` (snapshot then updates with absolute ``new_quantity``) and
  ``heartbeats`` (one per second with ``heartbeat_counter``); every message has
  a ``sequence_num`` — per connection according to the channel references,
  "for each product" according to the overview page (a documented conflict;
  ``coinfly.market.sequence`` detects the contract the stream follows and
  reports it); the server closes a connection that does not subscribe within
  five seconds.

Provenance is part of every record: REST observations are labeled
COINBASE_PUBLIC_REST (polled, no sequence from the venue), WebSocket
observations COINBASE_PUBLIC_WEBSOCKET. Sequence gaps, reconnects and
resynchronizations are counted in the feed health, and a silent connection is
detected by the heartbeat timeout rather than assumed alive.

Network access happens only on the feed's own thread after ``start``; tests
inject a fake reader / client factory and never open a socket.
"""

from __future__ import annotations

import heapq
import json
import random
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import ValidationError

from coinfly.domain.context import DomainError
from coinfly.domain.products import ProductSubtype
from coinfly.market.feed import _ObservationStore
from coinfly.market.observations import (
    DataSource,
    DepthLevelObservation,
    FeedHealth,
    FeedState,
    InstrumentCatalogEntry,
    MarketObservation,
)
from coinfly.market.sequence import SequenceTracker, SequenceVerdict
from coinfly.network.websocket_client import WebSocketClient
from coinfly.venues import decimal_json
from coinfly.venues.coinbase.endpoint_profiles import EndpointProfile, endpoint_profile, require_registered
from coinfly.venues.coinbase.http_reader import pinned_tls_context

PUBLIC_MARKET_PROFILE_ID = "coinbase-public-market"
PUBLIC_WEBSOCKET_URL = "wss://advanced-trade-ws.coinbase.com"
MAX_BOOK_LEVELS = 25
MAX_REST_INSTRUMENTS = 25  # keeps a full sweep well inside the public 10 req/s budget
MAX_WEBSOCKET_INSTRUMENTS = 30
MIN_REQUEST_SPACING_SECONDS = 0.15
CATALOG_TTL = timedelta(minutes=10)
# After a failed catalog read, callers are served the last catalog (or the
# failure) for this long instead of blocking on the network again every tick.
CATALOG_FAILURE_BACKOFF = timedelta(seconds=15)
HEARTBEAT_SILENCE_SECONDS = 10.0  # heartbeats arrive once per second
MAX_BACKOFF_SECONDS = 30.0
# A connection that stayed up this long was healthy (silence past the heartbeat
# timeout already ends a connection), so the next outage starts a fresh backoff.
HEALTHY_CONNECTION_SECONDS = 30.0
# Observations the worker has not polled yet. Past this bound the consumer is
# too far behind for the books to mean anything: the queue is dropped, the books
# invalidated and the connection resynchronized (never silently coalesced).
MAX_PENDING_OBSERVATIONS = 20_000
MAX_CONSECUTIVE_FAILURES = 5
# Longest term accepted as a plain dated (monthly/quarterly) contract when the
# record carries no perpetual_details. Beyond it the record is ambiguous and is
# refused rather than guessed (see classify_product).
DATED_FUTURE_MAX_TERM = timedelta(days=3 * 365)


def parse_venue_timestamp(value: object) -> datetime:
    """RFC 3339 with up to nanoseconds ('2023-02-09T20:32:50.714964855Z') → aware UTC."""
    if not isinstance(value, str) or len(value) > 40:
        raise DomainError("VENUE_TIMESTAMP_INVALID")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        index = 0
        while index < len(tail) and tail[index].isdigit():
            digits += tail[index]
            index += 1
        text = f"{head}.{(digits + '000000')[:6]}{tail[index:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError("VENUE_TIMESTAMP_INVALID") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}") from None
    if not number.is_finite():
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}")
    return number


def _positive(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


def _informative(value: Decimal | None) -> Decimal | None:
    """An optional, informational number (price, volume): dropped when it exceeds the
    exact-decimal bounds (38 digits, 18 places) instead of excluding the product."""
    if value is None:
        return None
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -18 or len(value.as_tuple().digits) > 38:
        return None
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return _decimal(value, field="optional")
    except DomainError:
        return None


def _present(value: object) -> bool:
    """A venue field that carries information: not null, not an empty string/collection."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return bool(value)
    return True


def _funding_marker(details: Mapping[str, Any]) -> bool:
    """Whether a futures record carries a funding schedule (the perpetual-style marker).

    Observed in the public catalog on 2026-09-22 (118 FCM futures): *every*
    futures record — monthly contracts included — carries a
    ``perpetual_details`` object with its open interest, and its
    ``funding_rate`` is empty even on the perpetual-style ones. The funding
    schedule itself is reported on ``future_product_details``
    (``funding_interval`` "3600s", ``funding_rate``, ``funding_time``) and only
    on the perpetual-style contracts. So a non-empty ``perpetual_details`` is
    NOT evidence; a funding field (at either level) is.
    """
    perpetual = details.get("perpetual_details")
    nested = perpetual if isinstance(perpetual, Mapping) else {}
    return any(_present(details.get(key)) for key in ("funding_interval", "funding_rate", "funding_time")) or \
        any(_present(nested.get(key)) for key in ("funding_rate", "funding_time"))


def classify_product(product: Mapping[str, Any], *, now: datetime) -> tuple[ProductSubtype, datetime | None]:
    """Map a venue product record onto the contract model from its documented fields only.

    Documented fields (2026-09-21, re-checked against the live public catalog
    2026-09-22): ``product_type`` SPOT|FUTURE,
    ``future_product_details.contract_expiry_type`` PERPETUAL|EXPIRING,
    ``future_product_details.contract_expiry`` and the funding schedule of the
    US "perpetual-style" futures — expiring contracts with a multi-year term
    that pay funding (``funding_interval`` / ``funding_rate`` /
    ``funding_time``, see ``_funding_marker``). That schedule is the only
    authoritative marker of the perpetual-style subtype in the record:

    * PERPETUAL → PERPETUAL_NO_EXPIRY;
    * EXPIRING with a funding schedule → PERPETUAL_STYLE_FUTURE;
    * EXPIRING without one and an expiry inside ``DATED_FUTURE_MAX_TERM`` →
      DATED_FUTURE (monthly/quarterly contracts, which also carry an open
      interest block but no funding);
    * EXPIRING without one and an expiry beyond that term is ambiguous — a
      long-dated contract or a perpetual-style one whose details this record
      lacks — and is refused (``VENUE_PRODUCT_SUBTYPE_UNVERIFIED``): the catalog
      excludes it and reports the exclusion. Elapsed term alone never selects a
      subtype, because funding, settlement and reversal rules follow from it.
    """
    product_type = str(product.get("product_type", "")).upper()
    if product_type == "SPOT":
        return ProductSubtype.SPOT, None
    if product_type != "FUTURE":
        raise DomainError("VENUE_PRODUCT_TYPE_UNKNOWN")
    details = product.get("future_product_details") or {}
    if not isinstance(details, Mapping):
        raise DomainError("VENUE_PRODUCT_INVALID")
    expiry_type = str(details.get("contract_expiry_type", "")).upper()
    expiry_raw = details.get("contract_expiry")
    expiry = parse_venue_timestamp(expiry_raw) if isinstance(expiry_raw, str) and expiry_raw else None
    if expiry_type == "PERPETUAL":
        return ProductSubtype.PERPETUAL_NO_EXPIRY, None
    if expiry_type != "EXPIRING" or expiry is None:
        raise DomainError("VENUE_PRODUCT_INVALID")
    if _funding_marker(details):
        return ProductSubtype.PERPETUAL_STYLE_FUTURE, expiry
    if expiry - now > DATED_FUTURE_MAX_TERM:
        raise DomainError("VENUE_PRODUCT_SUBTYPE_UNVERIFIED",
                          f"VENUE_PRODUCT_SUBTYPE_UNVERIFIED: {product.get('product_id')} expires {expiry.date().isoformat()} "
                          "without a funding schedule; term alone does not select a subtype")
    return ProductSubtype.DATED_FUTURE, expiry


_CURRENCY_CODE = re.compile(r"^[A-Z0-9]{1,20}$")


def _base_currency(product: Mapping[str, Any]) -> str:
    """The base asset code. FCM futures leave ``base_currency_id`` empty and name
    the underlying in ``future_product_details.contract_root_unit`` (BTC, ETH, or
    an index code such as CDEGLD); ``base_display_symbol`` is the last fallback."""
    base = product.get("base_currency_id")
    if isinstance(base, str) and base.strip():
        return base.strip().upper()
    details = product.get("future_product_details")
    if isinstance(details, Mapping):
        root = details.get("contract_root_unit")
        if isinstance(root, str) and root.strip():
            return root.strip().upper()
    display = product.get("base_display_symbol")
    if isinstance(display, str) and display.strip():
        return display.strip().upper()
    raise DomainError("VENUE_PRODUCT_INVALID", "VENUE_PRODUCT_INVALID: base asset missing")


def _venue_status(product: Mapping[str, Any], subtype: ProductSubtype) -> str:
    """Normalized tradability status.

    Spot records carry ``status`` ("online", "delisted", ...). FCM futures
    records carry an EMPTY status and express tradability through
    ``is_disabled``/``view_only``/``trading_disabled`` and the FCM trading
    session (``fcm_trading_session_details.is_session_open``). An empty futures
    status is therefore mapped explicitly — never assumed tradable: "online"
    only while the session is reported open, "session_closed" when it is
    reported closed, "session_unknown" when the record says neither.
    """
    status = product.get("status")
    text = status.strip().lower()[:40] if isinstance(status, str) else ""
    if text or subtype is ProductSubtype.SPOT:
        return text or "unknown"
    session = product.get("fcm_trading_session_details")
    state = session.get("is_session_open") if isinstance(session, Mapping) else None
    if state is True:
        return "online"
    return "session_closed" if state is False else "session_unknown"


def catalog_entry_from_product(product: Mapping[str, Any], *, now: datetime,
                               source: DataSource = DataSource.COINBASE_PUBLIC_REST) -> InstrumentCatalogEntry:
    """One catalog entry, or DomainError — a malformed record never escapes as another exception type."""
    subtype, expiry = classify_product(product, now=now)
    instrument = product.get("product_id")
    quote = product.get("quote_currency_id")
    if not isinstance(instrument, str) or not isinstance(quote, str):
        raise DomainError("VENUE_PRODUCT_INVALID")
    base = _base_currency(product)
    if not _CURRENCY_CODE.match(base) or not _CURRENCY_CODE.match(quote.upper()):
        raise DomainError("VENUE_PRODUCT_INVALID", f"VENUE_PRODUCT_INVALID: asset code {base}/{quote}")
    multiplier = Decimal(1)
    details = product.get("future_product_details")
    if isinstance(details, Mapping):
        size = _optional_decimal(details.get("contract_size"))
        if size is not None and size > 0:
            multiplier = size
    price_increment = _optional_decimal(product.get("price_increment"))
    try:
        return InstrumentCatalogEntry(
            instrument_id=instrument, base_currency=base, quote_currency=quote.upper(), subtype=subtype,
            status=_venue_status(product, subtype),
            trading_disabled=bool(product.get("trading_disabled", False)) or bool(product.get("is_disabled", False))
            or bool(product.get("view_only", False)),
            cancel_only=bool(product.get("cancel_only", False)), limit_only=bool(product.get("limit_only", False)),
            post_only=bool(product.get("post_only", False)),
            base_increment=_decimal(product.get("base_increment"), field="base_increment"),
            quote_increment=_decimal(product.get("quote_increment"), field="quote_increment"),
            price_increment=_positive(price_increment),
            base_min_size=_optional_decimal(product.get("base_min_size")) or Decimal(0),
            quote_min_size=_optional_decimal(product.get("quote_min_size")) or Decimal(0),
            contract_multiplier=multiplier, volume_24h=_informative(_optional_decimal(product.get("volume_24h"))),
            quote_volume_24h=_informative(_optional_decimal(product.get("approximate_quote_24h_volume"))),
            reference_price=_informative(_positive(_optional_decimal(product.get("price")))),
            expiry=expiry, observed_at=now, source=source,
        )
    except ValidationError as error:
        fields = ",".join(sorted({str(item["loc"][0]) for item in error.errors() if item.get("loc")}))[:120]
        raise DomainError("VENUE_PRODUCT_INVALID", f"VENUE_PRODUCT_INVALID: {instrument} {fields}") from None


def _levels(raw: object, *, descending: bool) -> tuple[DepthLevelObservation, ...]:
    if not isinstance(raw, list):
        raise DomainError("VENUE_BOOK_INVALID")
    levels: list[DepthLevelObservation] = []
    for item in raw[:MAX_BOOK_LEVELS]:
        if not isinstance(item, Mapping):
            raise DomainError("VENUE_BOOK_INVALID")
        price = _decimal(item.get("price"), field="price")
        size = _decimal(item.get("size"), field="size")
        if price <= 0 or size <= 0:
            continue
        levels.append(DepthLevelObservation(price=price, quantity=size))
    levels.sort(key=lambda level: level.price, reverse=descending)
    deduplicated: list[DepthLevelObservation] = []
    for level in levels:
        if deduplicated and deduplicated[-1].price == level.price:
            continue
        deduplicated.append(level)
    return tuple(deduplicated)


class PublicMarketReader:
    """GET-only HTTPS reader for the public market profile: pinned CA, no redirects, no credential."""

    _QUERY_ALLOWLIST: Mapping[str, frozenset[str]] = {
        "/products": frozenset({"product_type", "limit", "offset", "get_all_products"}),
        "/product_book": frozenset({"product_id", "limit"}),
    }

    def __init__(self, endpoint: EndpointProfile | None = None, *, timeout: float = 10.0,
                 max_response_bytes: int = 8 * 1024 * 1024) -> None:
        endpoint = endpoint or endpoint_profile(PUBLIC_MARKET_PROFILE_ID)
        require_registered(endpoint)
        if endpoint.authenticated:
            raise DomainError("PUBLIC_READER_REQUIRES_UNAUTHENTICATED_ENDPOINT")
        if not 0 < timeout <= 30 or not 1024 <= max_response_bytes <= 16 * 1024 * 1024:
            raise DomainError("HTTP_BUDGET_INVALID")
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_bytes = max_response_bytes
        self._tls = pinned_tls_context()
        self._lock = threading.Lock()
        self._retired = False
        self._connections: set[HTTPSConnection] = set()

    def retire(self) -> None:
        with self._lock:
            self._retired = True
            connections = tuple(self._connections)
        for connection in connections:
            try:
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            except Exception:
                pass

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        params = dict(query or {})
        allowed = self._QUERY_ALLOWLIST.get(path, frozenset())
        if not set(params).issubset(allowed):
            raise DomainError("HTTP_QUERY_FORBIDDEN")
        if any(not isinstance(value, str) or not value or len(value) > 64 or not value.replace("-", "").replace("_", "").isalnum()
               for value in params.values()):
            raise DomainError("HTTP_QUERY_INVALID")
        address = self._endpoint.validate("GET", path, purpose=None)
        parsed = urlsplit(address)
        if parsed.hostname is None:
            raise DomainError("ENDPOINT_PROFILE_INVALID")
        target = parsed.path + ("?" + urlencode(sorted(params.items())) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "coinfly/1.0.0"}
        with self._lock:
            if self._retired:
                raise DomainError("NETWORK_CONTEXT_RETIRED")
            connection = HTTPSConnection(parsed.hostname, timeout=self._timeout, context=self._tls)
            self._connections.add(connection)
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise DomainError("HTTP_REDIRECT_FORBIDDEN")
            if response.status == 429:
                raise DomainError("HTTP_RATE_LIMITED")
            if response.status != 200:
                raise DomainError("HTTP_READ_FAILED")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise DomainError("HTTP_ENCODING_UNSUPPORTED")
            body = response.read(self._max_bytes + 1)
            if len(body) > self._max_bytes:
                raise DomainError("HTTP_RESPONSE_TOO_LARGE")
            data = decimal_json.loads(body)
            if not isinstance(data, dict):
                raise DomainError("HTTP_RESPONSE_INVALID")
            return data
        except DomainError:
            raise
        except Exception:
            raise DomainError("HTTP_READ_FAILED") from None
        finally:
            connection.close()
            with self._lock:
                self._connections.discard(connection)

    def products(self, *, product_type: str = "SPOT") -> list[Mapping[str, Any]]:
        if product_type not in ("SPOT", "FUTURE"):
            raise DomainError("HTTP_QUERY_INVALID")
        document = self.get("/products", query={"product_type": product_type})
        products = document.get("products")
        if not isinstance(products, list):
            raise DomainError("HTTP_RESPONSE_INVALID")
        return [item for item in products if isinstance(item, Mapping)]

    def product_book(self, product_id: str, *, limit: int = 10) -> Mapping[str, Any]:
        if not 1 <= limit <= MAX_BOOK_LEVELS:
            raise DomainError("HTTP_QUERY_INVALID")
        document = self.get("/product_book", query={"product_id": product_id, "limit": str(limit)})
        pricebook = document.get("pricebook")
        if not isinstance(pricebook, Mapping):
            raise DomainError("HTTP_RESPONSE_INVALID")
        return pricebook


class _CatalogCache:
    """Products list with a TTL; a stale catalog is served with its original observed_at."""

    def __init__(self, reader: PublicMarketReader, *, quote_currencies: Sequence[str], subtype: ProductSubtype,
                 clock: Callable[[], datetime]) -> None:
        self._reader = reader
        self._quotes = tuple(quote.upper() for quote in quote_currencies)
        self._subtype = subtype
        self._clock = clock
        self._entries: tuple[InstrumentCatalogEntry, ...] = ()
        self._fetched_at: datetime | None = None
        self._excluded: dict[str, int] = {}
        self._unverified: tuple[str, ...] = ()
        self._other_subtypes: dict[str, int] = {}
        self._last_error: str | None = None
        self._failed_at: datetime | None = None
        self._lock = threading.Lock()

    def refresh(self, *, force: bool = False) -> tuple[InstrumentCatalogEntry, ...]:
        now = self._clock()
        with self._lock:
            if not force and self._fetched_at is not None and now - self._fetched_at < CATALOG_TTL:
                return self._entries
            if not force and self._failed_at is not None and timedelta(0) <= now - self._failed_at < CATALOG_FAILURE_BACKOFF:
                if self._entries:
                    return self._entries
                raise DomainError(self._last_error or "HTTP_READ_FAILED")
        product_type = "SPOT" if self._subtype is ProductSubtype.SPOT else "FUTURE"
        try:
            products = self._reader.products(product_type=product_type)
        except DomainError as error:
            with self._lock:
                self._last_error, self._failed_at = error.code, now
            raise
        entries: list[InstrumentCatalogEntry] = []
        excluded: dict[str, int] = {}
        unverified: list[str] = []
        other_subtypes: dict[str, int] = {}
        for product in products:
            try:
                entry = catalog_entry_from_product(product, now=now)
            except ValidationError:
                excluded["VENUE_PRODUCT_INVALID"] = excluded.get("VENUE_PRODUCT_INVALID", 0) + 1
                continue
            except DomainError as error:
                # A malformed, unknown or ambiguous product never poisons the
                # catalog, and never enters it: the exclusion is counted by code
                # and ambiguous subtypes are named so the omission is visible.
                excluded[error.code] = excluded.get(error.code, 0) + 1
                if error.code == "VENUE_PRODUCT_SUBTYPE_UNVERIFIED" and isinstance(product.get("product_id"), str):
                    unverified.append(str(product["product_id"]))
                continue
            if entry.subtype is not self._subtype:
                # Counted so an empty catalog for this contract family is
                # explained (e.g. "PERPETUAL_STYLE_FUTURE 30, DATED_FUTURE 88").
                other_subtypes[entry.subtype.value] = other_subtypes.get(entry.subtype.value, 0) + 1
            elif entry.quote_currency in self._quotes:
                entries.append(entry)
        entries.sort(key=lambda entry: entry.instrument_id)
        with self._lock:
            self._entries, self._fetched_at = tuple(entries), now
            self._excluded, self._unverified = excluded, tuple(sorted(unverified))[:50]
            self._other_subtypes, self._last_error, self._failed_at = other_subtypes, None, None
            return self._entries

    def current(self) -> tuple[InstrumentCatalogEntry, ...]:
        with self._lock:
            return self._entries

    def report(self) -> dict[str, Any]:
        """What the last refresh excluded, by code; ambiguous subtypes by product id."""
        with self._lock:
            report: dict[str, Any] = {"fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
                                      "entries": len(self._entries), "subtype": self._subtype.value,
                                      "excluded": dict(self._excluded), "subtype_unverified": list(self._unverified),
                                      "other_subtypes": dict(self._other_subtypes)}
            if self._last_error is not None:
                report["last_error"] = self._last_error
            return report


class CoinbasePublicRestFeed:
    """Polls the public product book for each subscribed instrument on its own thread."""

    source = DataSource.COINBASE_PUBLIC_REST
    # Subscription capacity the automation plans against (one sweep polls each
    # subscribed book once, spaced by MIN_REQUEST_SPACING_SECONDS).
    max_instruments = MAX_REST_INSTRUMENTS

    def __init__(self, *, quote_currencies: Sequence[str] = ("USD",), reader: PublicMarketReader | None = None,
                 subtype: ProductSubtype = ProductSubtype.SPOT, poll_interval_seconds: float = 2.0,
                 book_levels: int = 10, clock: Callable[[], datetime] | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        if not 0.5 <= poll_interval_seconds <= 60 or not 1 <= book_levels <= MAX_BOOK_LEVELS:
            raise DomainError("FEED_BUDGET_INVALID")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._reader = reader or PublicMarketReader()
        self._catalog = _CatalogCache(self._reader, quote_currencies=quote_currencies, subtype=subtype, clock=self._clock)
        self._store = _ObservationStore(self.source)
        self._interval = poll_interval_seconds
        self._levels = book_levels
        self._sleep = sleeper
        self._pending: list[MarketObservation] = []
        self._pending_lock = threading.Lock()
        self._sequence: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failures = 0

    def start(self, instruments: Sequence[str]) -> None:
        wanted = tuple(dict.fromkeys(instruments))
        if len(wanted) > MAX_REST_INSTRUMENTS:
            raise DomainError("FEED_INSTRUMENT_LIMIT", f"FEED_INSTRUMENT_LIMIT: REST polling covers at most {MAX_REST_INSTRUMENTS} instruments")
        known = {entry.instrument_id for entry in self._catalog.refresh()}
        if any(name not in known for name in wanted):
            raise DomainError("FEED_INSTRUMENT_UNKNOWN")
        self._store.subscribe(wanted)
        self._store.set_state(FeedState.CONNECTING, "first sweep pending")
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="coinfly-public-rest-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._store.stop()
        self._reader.retire()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval + 15)
        self._thread = None

    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]:
        try:
            return self._catalog.refresh()
        except DomainError:
            return self._catalog.current()

    def latest(self, instrument: str) -> MarketObservation | None:
        return self._store.latest(instrument)

    def health(self) -> FeedHealth:
        return self._store.health().model_copy(update={"catalog": self._catalog.report()})

    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        with self._pending_lock:
            produced, self._pending = tuple(self._pending), []
        return produced

    def sweep_once(self) -> int:
        """One synchronous pass over the subscribed instruments (used by the thread and by tests)."""
        produced = 0
        for instrument in self._store.subscribed:
            if self._stop.is_set():
                break
            started = time.monotonic()
            try:
                pricebook = self._reader.product_book(instrument, limit=self._levels)
                observation = self._observation(instrument, pricebook)
            except DomainError as error:
                self._failures += 1
                state = FeedState.DISCONNECTED if self._failures >= MAX_CONSECUTIVE_FAILURES else FeedState.DEGRADED
                self._store.set_state(state, f"{error.code} on {instrument}")
                if error.code == "HTTP_RATE_LIMITED":
                    self._sleep(min(MAX_BACKOFF_SECONDS, self._interval * 2))
                self._space_requests(started)  # a failing endpoint is not retried back-to-back either
                continue
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._store.note_reconnect()
            self._failures = 0
            if self._store.record(observation):
                with self._pending_lock:
                    self._pending.append(observation)
                produced += 1
            self._store.set_state(FeedState.CONNECTED, f"polled every {self._interval:g}s")
            self._space_requests(started)
        return produced

    def _space_requests(self, started: float) -> None:
        elapsed = time.monotonic() - started
        if elapsed < MIN_REQUEST_SPACING_SECONDS:
            self._sleep(MIN_REQUEST_SPACING_SECONDS - elapsed)

    def _observation(self, instrument: str, pricebook: Mapping[str, Any]) -> MarketObservation:
        if pricebook.get("product_id") != instrument:
            raise DomainError("VENUE_BOOK_INVALID")
        now = self._clock()
        observed = parse_venue_timestamp(pricebook.get("time")) if pricebook.get("time") else now
        sequence = self._sequence.get(instrument, 0) + 1
        try:
            observation = MarketObservation(
                instrument=instrument, bids=_levels(pricebook.get("bids", []), descending=True),
                asks=_levels(pricebook.get("asks", []), descending=False), observed_at=observed,
                received_at=max(now, observed), sequence=sequence, source=self.source,
                lineage=f"GET /market/product_book poll {sequence}",
            )
        except ValidationError:
            # A crossed or otherwise invalid snapshot is one failed poll of one
            # instrument, never an exception that aborts the whole sweep.
            raise DomainError("VENUE_BOOK_INVALID") from None
        self._sequence[instrument] = sequence
        return observation

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception as error:  # never let the feed thread die silently
                self._store.set_state(FeedState.DEGRADED, f"sweep failed: {type(error).__name__}")
            if self._stop.wait(self._interval):
                break


class _Level2Book:
    """Absolute-quantity level2 book for one instrument (snapshot then updates)."""

    __slots__ = ("bids", "asks", "synchronized")

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.synchronized = False

    def apply(self, event_type: str, updates: Sequence[Mapping[str, Any]]) -> None:
        if event_type == "snapshot":
            self.bids.clear()
            self.asks.clear()
            self.synchronized = True
        elif event_type != "update":
            raise DomainError("VENUE_BOOK_INVALID")
        elif not self.synchronized:
            raise DomainError("VENUE_BOOK_UNSYNCHRONIZED")
        for update in updates:
            side = update.get("side")
            price = _decimal(update.get("price_level"), field="price_level")
            quantity = _decimal(update.get("new_quantity"), field="new_quantity")
            book = self.bids if side == "bid" else self.asks if side == "offer" else None
            if book is None:
                raise DomainError("VENUE_BOOK_INVALID")
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def top(self, depth: int) -> tuple[tuple[DepthLevelObservation, ...], tuple[DepthLevelObservation, ...]]:
        # O(n log depth) per update instead of sorting the whole book: a liquid
        # level2 book holds thousands of levels and changes many times a second.
        bids = tuple(DepthLevelObservation(price=price, quantity=quantity)
                     for price, quantity in heapq.nlargest(depth, self.bids.items()))
        asks = tuple(DepthLevelObservation(price=price, quantity=quantity)
                     for price, quantity in heapq.nsmallest(depth, self.asks.items()))
        return bids, asks


class WebSocketLike(Protocol):
    def connect(self) -> None: ...
    def send_text(self, text: str) -> None: ...
    def receive(self) -> str | None: ...
    def close(self) -> None: ...


class CoinbasePublicWebSocketFeed:
    """Public level2 + heartbeats over the documented WebSocket, with gap detection and resync."""

    source = DataSource.COINBASE_PUBLIC_WEBSOCKET
    # level2 streams every book change; a bounded subscription keeps the parse
    # and book-maintenance work (and the observation queue) inside one tick.
    max_instruments = MAX_WEBSOCKET_INSTRUMENTS

    def __init__(self, *, quote_currencies: Sequence[str] = ("USD",), reader: PublicMarketReader | None = None,
                 subtype: ProductSubtype = ProductSubtype.SPOT, url: str = PUBLIC_WEBSOCKET_URL,
                 client_factory: Callable[[], WebSocketLike] | None = None, depth: int = 10,
                 clock: Callable[[], datetime] | None = None, sleeper: Callable[[float], None] = time.sleep,
                 max_reconnects: int | None = None) -> None:
        if not 1 <= depth <= MAX_BOOK_LEVELS:
            raise DomainError("FEED_BUDGET_INVALID")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._reader = reader or PublicMarketReader()
        self._catalog = _CatalogCache(self._reader, quote_currencies=quote_currencies, subtype=subtype, clock=self._clock)
        self._store = _ObservationStore(self.source)
        self._url = url
        self._client_factory = client_factory or (lambda: WebSocketClient(url, timeout=HEARTBEAT_SILENCE_SECONDS))
        self._depth = depth
        self._sleep = sleeper
        self._max_reconnects = max_reconnects
        self._pending: list[MarketObservation] = []
        self._pending_lock = threading.Lock()
        self._books: dict[str, _Level2Book] = {}
        # Per-instrument observation counters: the store judges continuity per
        # instrument, so one shared counter would look like gaps on every
        # multiplexed stream. Venue sequence gaps come from the SequenceTracker.
        self._counters: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client: WebSocketLike | None = None
        self._client_lock = threading.Lock()
        self._sequence = SequenceTracker()
        self._last_heartbeat_counter: int | None = None
        self._heartbeat_gap = False  # set by a missed heartbeat, cleared by the next consecutive one
        self._monotonic = time.monotonic
        self._reconnects = 0

    # --- MarketFeed -------------------------------------------------------------------
    def start(self, instruments: Sequence[str]) -> None:
        wanted = tuple(dict.fromkeys(instruments))
        if not wanted:
            raise DomainError("FEED_INSTRUMENT_UNKNOWN")
        if len(wanted) > MAX_WEBSOCKET_INSTRUMENTS:
            raise DomainError("FEED_INSTRUMENT_LIMIT",
                              f"FEED_INSTRUMENT_LIMIT: the level2 subscription covers at most {MAX_WEBSOCKET_INSTRUMENTS} instruments")
        known = {entry.instrument_id for entry in self._catalog.refresh()}
        if any(name not in known for name in wanted):
            raise DomainError("FEED_INSTRUMENT_UNKNOWN")
        self._store.subscribe(wanted)
        self._store.set_state(FeedState.CONNECTING, "connecting")
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="coinfly-public-ws-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._store.stop()
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._reader.retire()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=HEARTBEAT_SILENCE_SECONDS + 15)
        self._thread = None

    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]:
        try:
            return self._catalog.refresh()
        except DomainError:
            return self._catalog.current()

    def latest(self, instrument: str) -> MarketObservation | None:
        return self._store.latest(instrument)

    def health(self) -> FeedHealth:
        health = self._store.health()
        # The venue's own sequence anomalies, not the store's per-instrument counter continuity.
        return health.model_copy(update={"catalog": self._catalog.report(), "sequence_gaps": int(health.sequence.get("gaps", 0))})

    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        with self._pending_lock:
            produced, self._pending = tuple(self._pending), []
        return produced

    # --- connection loop --------------------------------------------------------------
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = self._monotonic()
            try:
                self.run_connection()
            except DomainError as error:
                self._connection_lost(error.code)
            except Exception as error:
                self._connection_lost(f"connection failed: {type(error).__name__}")
            if self._stop.is_set():
                break
            if self._monotonic() - started >= HEALTHY_CONNECTION_SECONDS:
                backoff = 1.0  # run_connection only returns on stop(); a long session ends by an exception
            self._reconnects += 1
            if self._max_reconnects is not None and self._reconnects > self._max_reconnects:
                self._store.set_state(FeedState.DISCONNECTED, "reconnect budget exhausted")
                break
            self._store.note_reconnect()
            self._sleep(min(MAX_BACKOFF_SECONDS, backoff + random.uniform(0, backoff / 4)))
            backoff = min(MAX_BACKOFF_SECONDS, backoff * 2)

    def _connection_lost(self, detail: str) -> None:
        # The books belong to the dead connection: nothing may decide on them while the feed reconnects.
        self._store.invalidate(detail)
        self._store.set_state(FeedState.DISCONNECTED, detail)

    def run_connection(self) -> None:
        """Connect, subscribe, consume until the peer closes, a gap forces a resync, or stop()."""
        client = self._client_factory()
        with self._client_lock:
            self._client = client
        self._sequence.new_connection()
        self._store.set_sequence(self._sequence.describe())
        self._last_heartbeat_counter = None
        self._heartbeat_gap = False
        for book in self._books.values():
            book.synchronized = False  # a new connection delivers new snapshots; old books are history
        try:
            client.connect()
            subscribed = list(self._store.subscribed)
            client.send_text(json.dumps({"type": "subscribe", "channel": "heartbeats"}, separators=(",", ":")))
            client.send_text(json.dumps({"type": "subscribe", "channel": "level2", "product_ids": subscribed}, separators=(",", ":")))
            self._store.set_state(FeedState.CONNECTING, "subscribed; awaiting snapshot")
            while not self._stop.is_set():
                text = client.receive()
                if text is None:
                    raise DomainError("WEBSOCKET_PEER_CLOSED")
                self.handle_message(text)
        finally:
            with self._client_lock:
                self._client = None
            try:
                client.close()
            except Exception:
                pass

    def handle_message(self, text: str) -> None:
        """Parse one server message; raises WEBSOCKET_SEQUENCE_GAP to force a resync by reconnecting."""
        if len(text) > 4 * 1024 * 1024:
            raise DomainError("WEBSOCKET_FRAME_TOO_LARGE")
        try:
            message = decimal_json.loads(text)
        except DomainError:
            raise DomainError("WEBSOCKET_MESSAGE_INVALID") from None
        if not isinstance(message, Mapping):
            raise DomainError("WEBSOCKET_MESSAGE_INVALID")
        if message.get("type") == "error":
            raise DomainError("WEBSOCKET_SUBSCRIBE_REJECTED")
        channel = message.get("channel")
        events = message.get("events")
        if not isinstance(events, list):
            events = []
        sequence = message.get("sequence_num")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            product = next((event.get("product_id") for event in events if isinstance(event, Mapping) and isinstance(event.get("product_id"), str)), None)
            verdict = self._sequence.observe(sequence, channel=str(channel), product=product)
            self._store.set_sequence(self._sequence.describe())
            if verdict in (SequenceVerdict.DUPLICATE, SequenceVerdict.OUT_OF_ORDER):
                return  # never applied to a book; counted in the health block
            if verdict in (SequenceVerdict.GAP, SequenceVerdict.RESET):
                self._store.note_resync()
                self._store.invalidate(f"sequence {verdict.value.lower()} at {sequence} ({self._sequence.contract.value}); resynchronizing")
                raise DomainError("WEBSOCKET_SEQUENCE_GAP")
        received = self._clock()
        if channel == "heartbeats":
            for event in events:
                counter = event.get("heartbeat_counter") if isinstance(event, Mapping) else None
                try:
                    value = int(counter) if counter is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is not None:
                    last = self._last_heartbeat_counter
                    if last is not None and value > last + 1:
                        self._heartbeat_gap = True
                        self._store.set_state(FeedState.DEGRADED, f"heartbeat gap {last}->{value}")
                    elif self._heartbeat_gap and last is not None and value == last + 1:
                        self._heartbeat_gap = False  # heartbeats are consecutive again
                        self._publish_state()
                    self._last_heartbeat_counter = value
            return
        if channel == "subscriptions":
            return
        if channel != "l2_data":
            return  # other public channels are not subscribed; ignore defensively
        observed = parse_venue_timestamp(message.get("timestamp")) if message.get("timestamp") else received
        for event in events:
            if not isinstance(event, Mapping):
                raise DomainError("VENUE_BOOK_INVALID")
            instrument = event.get("product_id")
            if not isinstance(instrument, str) or instrument not in self._store.subscribed:
                continue
            updates = event.get("updates") or []
            if not isinstance(updates, list):
                raise DomainError("VENUE_BOOK_INVALID")
            book = self._books.setdefault(instrument, _Level2Book())
            book.apply(str(event.get("type", "")), updates)
            bids, asks = book.top(self._depth)
            if not bids or not asks or bids[0].price >= asks[0].price:
                continue  # one-sided or transiently crossed while updates are applied
            counter = self._counters.get(instrument, 0) + 1
            self._counters[instrument] = counter
            observation = MarketObservation(
                instrument=instrument, bids=bids, asks=asks, observed_at=observed, received_at=max(received, observed),
                sequence=counter, source=self.source,
                lineage=f"level2 {event.get('type')} seq {sequence} gen {self._sequence.generation}",
            )
            if self._store.record(observation):
                with self._pending_lock:
                    self._pending.append(observation)
                    overflow = len(self._pending) > MAX_PENDING_OBSERVATIONS
                    if overflow:
                        self._pending = []
                if overflow:
                    self._store.note_overflow()
                    self._store.note_resync()
                    self._store.invalidate("observation queue overflow; books invalidated, resynchronizing")
                    raise DomainError("WEBSOCKET_QUEUE_OVERFLOW")
        self._publish_state()

    def _publish_state(self) -> None:
        """CONNECTED only while every subscribed book has its snapshot and the heartbeats are consecutive."""
        if self._heartbeat_gap:
            return  # stays DEGRADED until the heartbeats recover; a book update proves nothing about them
        waiting = [name for name in self._store.subscribed if not (name in self._books and self._books[name].synchronized)]
        if waiting:
            self._store.set_state(FeedState.CONNECTING, f"awaiting level2 snapshot: {','.join(waiting[:5])} ({len(waiting)} pending)")
        else:
            self._store.set_state(FeedState.CONNECTED, "level2 synchronized")
