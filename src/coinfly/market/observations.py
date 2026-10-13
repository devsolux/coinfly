"""Immutable market observations with provenance, freshness and sequence lineage.

An observation is what a feed *received*, never what a strategy would like to be
true. Its ``source`` label is part of the record: a synthetic fixture, a replayed
file, the public Coinbase REST book and the public Coinbase WebSocket book are
four different things and are shown as such.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from coinfly.domain.context import DomainError, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal
from coinfly.domain.products import ProductSubtype


class DataSource(StrEnum):
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    REPLAY_FILE = "REPLAY_FILE"
    COINBASE_PUBLIC_REST = "COINBASE_PUBLIC_REST"
    COINBASE_PUBLIC_WEBSOCKET = "COINBASE_PUBLIC_WEBSOCKET"

    @property
    def is_real_market_data(self) -> bool:
        return self in (DataSource.COINBASE_PUBLIC_REST, DataSource.COINBASE_PUBLIC_WEBSOCKET)

    @property
    def label(self) -> str:
        return {
            DataSource.SYNTHETIC_FIXTURE: "SYNTHETIC FIXTURE — NOT MARKET DATA",
            DataSource.REPLAY_FILE: "REPLAYED RECORDING — NOT LIVE",
            DataSource.COINBASE_PUBLIC_REST: "COINBASE PUBLIC REST (polled)",
            DataSource.COINBASE_PUBLIC_WEBSOCKET: "COINBASE PUBLIC WEBSOCKET",
        }[self]


class DepthLevelObservation(FrozenModel):
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


class MarketObservation(FrozenModel):
    instrument: Identifier
    bids: tuple[DepthLevelObservation, ...] = Field(max_length=1000)
    asks: tuple[DepthLevelObservation, ...] = Field(max_length=1000)
    observed_at: datetime
    received_at: datetime
    sequence: int = Field(ge=0)
    source: DataSource
    lineage: str = Field(default="", max_length=200)

    @field_validator("observed_at", "received_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware observation timestamps required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered_book(self) -> MarketObservation:
        if any(left.price <= right.price for left, right in zip(self.bids, self.bids[1:])):
            raise ValueError("bids must be strictly descending")
        if any(left.price >= right.price for left, right in zip(self.asks, self.asks[1:])):
            raise ValueError("asks must be strictly ascending")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed book")
        if self.received_at < self.observed_at - timedelta(seconds=5):
            raise ValueError("observation received before it happened")
        return self

    @property
    def two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @property
    def mid(self) -> Decimal | None:
        if not self.two_sided:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid == 0:
            return None
        return (self.asks[0].price - self.bids[0].price) / mid * 10000

    def top_notional(self, side: str) -> Decimal:
        levels = self.bids if side == "BID" else self.asks
        return levels[0].price * levels[0].quantity if levels else Decimal(0)

    def age(self, now: datetime) -> timedelta:
        return now - self.observed_at

    def require_fresh(self, now: datetime, *, max_age: timedelta) -> None:
        if not self.two_sided:
            raise DomainError("OBSERVATION_ONE_SIDED")
        age = self.age(now)
        if age < timedelta(0) or age > max_age:
            raise DomainError("OBSERVATION_STALE")


class InstrumentCatalogEntry(FrozenModel):
    """One tradable instrument as the venue (or fixture) describes it."""

    instrument_id: Identifier
    # Venue asset codes can be a single character (Coinbase lists S, G, O, W, T).
    base_currency: str = Field(pattern=r"^[A-Z0-9]{1,20}$")
    quote_currency: str = Field(pattern=r"^[A-Z0-9]{1,20}$")
    subtype: ProductSubtype = ProductSubtype.SPOT
    status: str = Field(default="online", max_length=40)
    trading_disabled: bool = False
    cancel_only: bool = False
    limit_only: bool = False
    post_only: bool = False
    base_increment: ExactDecimal = Field(default=Decimal("0.00000001"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    quote_increment: ExactDecimal = Field(default=Decimal("0.01"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    # The venue's price tick. Spot products quote it as ``quote_increment``;
    # futures quote it separately (``price_increment`` 5 on BTC nano futures
    # while ``quote_increment`` stays 0.01). Absent, the quote increment applies.
    price_increment: ExactDecimal | None = Field(default=None, gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    base_min_size: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    quote_min_size: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    contract_multiplier: ExactDecimal = Field(default=Decimal(1), gt=0, allow_inf_nan=False, max_digits=20, decimal_places=18)
    volume_24h: ExactDecimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    # 24 h volume in the quote currency (``approximate_quote_24h_volume``). Base
    # volumes (coins or contracts) are not comparable across instruments; the
    # universe ranks by this value when the venue provides it.
    quote_volume_24h: ExactDecimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    # The venue's last price at catalog time. Used only to screen order-size
    # feasibility before a book is observed; never a mark, never a trade price.
    reference_price: ExactDecimal | None = Field(default=None, gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    expiry: datetime | None = None
    observed_at: datetime
    source: DataSource

    @property
    def tradable(self) -> bool:
        return self.status == "online" and not self.trading_disabled and not self.cancel_only

    @property
    def tick_size(self) -> Decimal:
        return self.price_increment if self.price_increment is not None else self.quote_increment

    @property
    def ranking_volume(self) -> Decimal:
        """Comparable 24 h activity: quote-currency volume when known, else base volume (or zero)."""
        if self.quote_volume_24h is not None:
            return self.quote_volume_24h
        return self.volume_24h if self.volume_24h is not None else Decimal(0)

    @property
    def minimum_notional(self) -> Decimal:
        return self.quote_min_size


class FeedState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"


class FeedHealth(FrozenModel):
    """Transport health (state, last message, gaps, reconnects) — distinct from per-instrument freshness.

    A connected, heartbeating transport says nothing about one instrument's
    book: ``instrument_observed_at`` carries the last observation time per
    subscribed instrument so freshness is judged per instrument, and
    ``sequence`` reports the detected sequence contract and its anomalies.
    """

    source: DataSource
    state: FeedState
    last_message_at: datetime | None = None
    sequence_gaps: int = Field(default=0, ge=0)
    reconnects: int = Field(default=0, ge=0)
    subscribed: tuple[str, ...] = ()
    detail: str = Field(default="", max_length=240)
    resynchronizations: int = Field(default=0, ge=0)
    instrument_observed_at: dict[str, datetime] = Field(default_factory=dict)
    sequence: dict[str, Any] = Field(default_factory=dict)
    overflows: int = Field(default=0, ge=0)
    catalog: dict[str, Any] = Field(default_factory=dict)  # last catalog refresh: entries, exclusions by code, unverified subtypes

    def instrument_age(self, instrument: str, now: datetime) -> timedelta | None:
        observed = self.instrument_observed_at.get(instrument)
        return now - observed if observed is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "label": self.source.label,
                "real_market_data": self.source.is_real_market_data}
