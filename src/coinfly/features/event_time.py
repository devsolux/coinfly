"""Non-overlapping UTC bars. Missing intervals never inherit a fictitious price."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import Field, field_validator

from coinfly.domain.context import DomainError, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class MarketTrade(FrozenModel):
    event_id: Identifier
    instrument_id: Identifier
    sequence: int = Field(ge=0)
    event_time: datetime
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)

    @field_validator("event_time")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware event timestamp required")
        return value.astimezone(timezone.utc)


class Bar(FrozenModel):
    instrument_id: Identifier
    start: datetime
    end: datetime
    open: ExactDecimal | None
    high: ExactDecimal | None
    low: ExactDecimal | None
    close: ExactDecimal | None
    volume: ExactDecimal
    trade_count: int
    gap: bool
    sequence_gap: bool


class EventTimeBars:
    def __init__(
        self, instrument_id: str, *, interval: timedelta = timedelta(minutes=1),
        allowed_lateness: timedelta = timedelta(seconds=2), max_buffered_trades: int = 100000,
        max_intervals_per_advance: int = 10000,
    ) -> None:
        if interval <= timedelta(0) or allowed_lateness < timedelta(0):
            raise ValueError("invalid event-time intervals")
        if max_buffered_trades < 1 or max_intervals_per_advance < 1:
            raise ValueError("positive bounded buffers required")
        self.instrument_id = instrument_id
        self.interval = interval
        self.allowed_lateness = allowed_lateness
        self.max_buffered_trades = max_buffered_trades
        self.max_intervals_per_advance = max_intervals_per_advance
        self._trades: dict[datetime, list[MarketTrade]] = {}
        self._identities: dict[str, MarketTrade] = {}
        self._next_start: datetime | None = None
        self._max_event_time: datetime | None = None
        self._watermark: datetime | None = None
        self._last_final_sequence: int | None = None
        self.late_events = 0

    def ingest(self, trade: MarketTrade, *, received_at: datetime) -> tuple[Bar, ...]:
        self._aware(received_at)
        if trade.instrument_id != self.instrument_id:
            raise DomainError("WRONG_FEATURE_INSTRUMENT")
        if trade.event_time > received_at + timedelta(seconds=1):
            raise DomainError("FUTURE_MARKET_EVENT")
        prior = self._identities.get(trade.event_id)
        if prior is not None:
            if prior != trade:
                raise DomainError("MARKET_EVENT_ID_CONFLICT")
            return ()
        start = self._bucket(trade.event_time)
        if self._watermark is not None and start + self.interval <= self._watermark:
            self.late_events += 1
            return ()
        if len(self._identities) >= self.max_buffered_trades:
            raise DomainError("MARKET_BUFFER_FULL")
        self._identities[trade.event_id] = trade
        self._trades.setdefault(start, []).append(trade)
        if self._next_start is None or start < self._next_start:
            self._next_start = start
        if self._max_event_time is None or trade.event_time > self._max_event_time:
            self._max_event_time = trade.event_time
        return self.advance_watermark(self._max_event_time - self.allowed_lateness)

    @exact_decimal
    def advance_watermark(self, watermark: datetime) -> tuple[Bar, ...]:
        self._aware(watermark)
        if self._watermark is not None and watermark < self._watermark:
            return ()
        if self._next_start is not None:
            count = (watermark - self._next_start) // self.interval
            if count > self.max_intervals_per_advance:
                raise DomainError("FEATURE_GAP_REQUIRES_RESET")
        self._watermark = watermark
        closed: list[Bar] = []
        while self._next_start is not None and self._next_start + self.interval <= watermark:
            start = self._next_start
            trades = sorted(self._trades.pop(start, []), key=lambda item: (item.event_time, item.sequence, item.event_id))
            for item in trades:
                self._identities.pop(item.event_id, None)
            sequences = sorted(item.sequence for item in trades)
            sequence_gap = bool(sequences and self._last_final_sequence is not None and sequences[0] != self._last_final_sequence + 1)
            sequence_gap = sequence_gap or any(b != a + 1 for a, b in zip(sequences, sequences[1:]))
            if sequences:
                self._last_final_sequence = sequences[-1]
            prices = tuple(item.price for item in trades)
            closed.append(Bar(
                instrument_id=self.instrument_id, start=start, end=start + self.interval,
                open=prices[0] if prices else None, high=max(prices) if prices else None,
                low=min(prices) if prices else None, close=prices[-1] if prices else None,
                volume=sum((item.quantity for item in trades), Decimal(0)),
                trade_count=len(trades), gap=not trades, sequence_gap=sequence_gap,
            ))
            self._next_start += self.interval
        return tuple(closed)

    def _bucket(self, stamp: datetime) -> datetime:
        return _EPOCH + ((stamp - _EPOCH) // self.interval) * self.interval

    @staticmethod
    def _aware(stamp: datetime) -> None:
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise DomainError("AWARE_TIMESTAMP_REQUIRED")
