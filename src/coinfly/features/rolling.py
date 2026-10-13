"""Rolling features consume finalized bars only; gaps reset warmup."""

from collections import deque
from datetime import datetime
from decimal import Decimal

from coinfly.domain.context import DomainError, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal
from coinfly.features.event_time import Bar


class RollingSnapshot(FrozenModel):
    as_of: datetime
    warm: bool
    sample_count: int
    mean_close: ExactDecimal | None
    simple_return: ExactDecimal | None


class RollingFeatures:
    def __init__(self, period: int = 20) -> None:
        if period < 2 or period > 100000:
            raise ValueError("period must be between 2 and 100000")
        self.period = period
        self._bars: deque[Bar] = deque(maxlen=period)
        self._last_end: datetime | None = None
        self._instrument: str | None = None

    @exact_decimal
    def update(self, bar: Bar) -> RollingSnapshot:
        if self._last_end is not None and bar.start < self._last_end:
            raise DomainError("FEATURE_TIME_REGRESSION")
        if self._instrument is not None and self._instrument != bar.instrument_id:
            raise DomainError("WRONG_FEATURE_INSTRUMENT")
        if self._last_end is not None and bar.start != self._last_end:
            self._bars.clear()
        self._instrument = bar.instrument_id
        self._last_end = bar.end
        if bar.gap or bar.sequence_gap or bar.close is None:
            self._bars.clear()
        else:
            self._bars.append(bar)
        prices = tuple(item.close for item in self._bars if item.close is not None)
        warm = len(prices) == self.period
        return RollingSnapshot(
            as_of=bar.end, warm=warm, sample_count=len(prices),
            mean_close=sum(prices, Decimal(0)) / Decimal(len(prices)) if warm else None,
            simple_return=prices[-1] / prices[0] - 1 if warm else None,
        )
