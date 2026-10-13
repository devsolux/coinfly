"""Loss-audited Decimal -> centered integer ticks -> normalized float64.

Floats do NOT preserve arbitrary 38-digit money. Retained exact ticks and Decimal
origins are the accounting boundary; model arrays never become order quantities.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

import numpy as np
from numba import njit
from numpy.typing import NDArray

from coinfly.domain.decimal_math import financial_context


@njit(cache=True, nogil=True, fastmath=False)
def _normalize_ticks_native(ticks: Any, scales: Any) -> Any:
    result = np.empty(ticks.shape, dtype=np.float64)
    for row in range(ticks.shape[0]):
        for col in range(ticks.shape[1]):
            value = ticks[row, col] / scales[col]
            if not np.isfinite(value) or np.rint(value * scales[col]) != ticks[row, col]:
                raise ValueError("float normalization cannot roundtrip to exact ticks")
            result[row, col] = value
    return result


def normalize_ticks(ticks: NDArray[Any], scales: NDArray[Any]) -> NDArray[np.float64]:
    """Validate integer bounds BEFORE the JIT kernel performs its first float cast."""
    limit = 2**53 - 1
    if (not isinstance(ticks, np.ndarray) or not isinstance(scales, np.ndarray)
            or ticks.dtype != np.int64 or scales.dtype != np.int64
            or ticks.ndim != 2 or scales.ndim != 1 or min(ticks.shape) < 1
            or ticks.shape[1] != len(scales) or np.any(ticks < -limit) or np.any(ticks > limit)
            or np.any(scales < 1) or np.any(scales > 2**40)):
        raise ValueError("bounded exact int64 ticks and normalization scales required")
    # Own immutable inputs across the released-GIL kernel boundary.
    owned_ticks, owned_scales = ticks.copy(), scales.copy()
    if np.any(owned_ticks < -limit) or np.any(owned_ticks > limit):
        raise ValueError("ticks changed outside exact float64 integer range")
    if np.any(owned_scales < 1) or np.any(owned_scales > 2**40):
        raise ValueError("scales changed outside normalization bounds")
    result: NDArray[np.float64] = _normalize_ticks_native(owned_ticks, owned_scales)
    return result


def _bounded(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("finite Decimal required")
    parts = value.as_tuple()
    if len(parts.digits) > 38 or not isinstance(parts.exponent, int) or not -18 <= parts.exponent <= 18:
        raise ValueError("monetary value outside the 38 digit / 18 place boundary")


@dataclass(frozen=True)
class TickScale:
    """Origin/tick/scale are fitted statistics: they decide which values saturate.

    ``fitted_through`` declares the last instant of data used to choose them so
    the causal loader can reject scales fitted on future or holdout data. Hosts
    that omit it take responsibility for that lineage themselves.
    """

    name: str
    origin: Decimal
    tick: Decimal
    scale_ticks: int
    fitted_through: datetime | None = None

    def __post_init__(self) -> None:
        _bounded(self.origin)
        _bounded(self.tick)
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 80:
            raise ValueError("bounded nonempty feature name required")
        if self.tick <= 0 or type(self.scale_ticks) is not int or not 1 <= self.scale_ticks <= 2**40:
            raise ValueError("positive tick and bounded integer normalization scale required")
        if self.fitted_through is not None and (not isinstance(self.fitted_through, datetime)
                                                or self.fitted_through.tzinfo is None
                                                or self.fitted_through.utcoffset() is None):
            raise ValueError("aware fitted_through timestamp required")


@dataclass(frozen=True)
class TensorBatch:
    values: NDArray[np.float64]
    ticks: NDArray[np.int64]
    scales: tuple[TickScale, ...]

    def exact_value(self, row: int, column: int) -> Decimal:
        with localcontext(financial_context(100)):
            spec = self.scales[column]
            return spec.origin + Decimal(int(self.ticks[row, column])) * spec.tick


def vectorize(rows: Sequence[Sequence[Decimal]], scales: tuple[TickScale, ...]) -> TensorBatch:
    if not rows or not scales or len({spec.name for spec in scales}) != len(scales):
        raise ValueError("nonempty rows and unique feature scales required")
    units = np.empty((len(rows), len(scales)), dtype=np.int64)
    ratios = [(spec.origin.as_integer_ratio(), spec.tick.as_integer_ratio()) for spec in scales]
    for row_index, row in enumerate(rows):
        if len(row) != len(scales):
            raise ValueError("feature width mismatch")
        for col, value in enumerate(row):
            _bounded(value)
            numerator, denominator = value.as_integer_ratio()
            (origin_n, origin_d), (tick_n, tick_d) = ratios[col]
            # Integer arithmetic is independent of ambient Decimal context and
            # catches off-grid values BEFORE any binary float conversion.
            offset, remainder = divmod((numerator * origin_d - origin_n * denominator) * tick_d,
                                       denominator * origin_d * tick_n)
            if remainder or abs(offset) > 2**53 - 1:
                raise ValueError("off-grid value or tick offset exceeds exact float64 integer range")
            units[row_index, col] = offset
    values = normalize_ticks(units, np.array([spec.scale_ticks for spec in scales], dtype=np.int64))
    values.flags.writeable = units.flags.writeable = False
    return TensorBatch(values, units, scales)
