"""Fixed sensory transformation for autonomous paper decisions (no Polars needed).

Mirrors the research projection exactly: exact Decimal features -> centered
integer ticks (integer arithmetic, off-grid values rejected) -> float64 ratio
-> clip to [-1, 1] -> 8-bit pixels replicated over RGB. Scales are fitted from
a declared warm-up window only and carry ``fitted_through`` lineage, so a frame
can never use statistics fitted on later observations. This is an engineered
observation proxy; it is not a calibrated market model.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

import numpy as np
from numpy.typing import NDArray

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import financial_context
from coinfly.market.observations import MarketObservation

FEATURE_NAMES = ("mid_offset", "spread_bps", "depth_imbalance", "mid_return_1")
LIMIT = 2**53 - 1


class SensoryScale:
    """Origin/tick/scale for one feature with explicit fitting lineage."""

    __slots__ = ("name", "origin", "tick", "scale_ticks", "fitted_through")

    def __init__(self, name: str, origin: Decimal, tick: Decimal, scale_ticks: int, fitted_through: datetime) -> None:
        if tick <= 0 or scale_ticks < 1 or scale_ticks > 2**40 or fitted_through.tzinfo is None:
            raise ValueError("invalid sensory scale")
        self.name, self.origin, self.tick, self.scale_ticks, self.fitted_through = name, origin, tick, scale_ticks, fitted_through

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "origin": str(self.origin), "tick": str(self.tick),
                "scale_ticks": self.scale_ticks, "fitted_through": self.fitted_through.isoformat()}


class SensoryFrame:
    __slots__ = ("rgb", "ticks", "features", "sha256", "decision_at", "observed_through", "available_through", "fitted_through",
                 "instrument")

    def __init__(self, *, rgb: NDArray[np.uint8], ticks: NDArray[np.int64], features: tuple[tuple[Decimal, ...], ...],
                 decision_at: datetime, observed_through: datetime, fitted_through: datetime, instrument: str,
                 available_through: datetime | None = None) -> None:
        self.rgb, self.ticks, self.features = rgb, ticks, features
        self.decision_at, self.observed_through, self.fitted_through, self.instrument = decision_at, observed_through, fitted_through, instrument
        # When the window's observations were available to this process (their
        # received_at), for the causal evidence chain; never after the decision.
        self.available_through = available_through if available_through is not None else observed_through
        self.sha256 = hashlib.sha256(rgb.tobytes()).hexdigest()

    @property
    def reference_price(self) -> Decimal:
        """The decision-time reference mid (the newest row's exact mid)."""
        return self.features[-1][0]

    def owned_rgb(self) -> NDArray[np.uint8]:
        frame = np.array(self.rgb, copy=True, order="C")
        if hashlib.sha256(frame.tobytes()).hexdigest() != self.sha256:
            raise DomainError("SENSORY_FRAME_MUTATED")
        frame.flags.writeable = False
        return frame


def _quantize(value: Decimal, tick: Decimal) -> Decimal:
    with localcontext(financial_context(80)):
        return (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


def observation_features(previous: MarketObservation | None, current: MarketObservation, *, price_tick: Decimal) -> tuple[Decimal, ...]:
    """Exact Decimal features of one observation; on the declared tick grid."""
    if not current.two_sided:
        raise DomainError("OBSERVATION_ONE_SIDED")
    with localcontext(financial_context(80)):
        mid = _quantize((current.bids[0].price + current.asks[0].price) / 2, price_tick)
        spread = _quantize((current.asks[0].price - current.bids[0].price) / mid * 10000, Decimal("0.01"))
        bid_quantity = sum((level.quantity for level in current.bids), Decimal(0))
        ask_quantity = sum((level.quantity for level in current.asks), Decimal(0))
        total = bid_quantity + ask_quantity
        imbalance = _quantize((bid_quantity - ask_quantity) / total if total else Decimal(0), Decimal("0.000001"))
        previous_mid = _quantize((previous.bids[0].price + previous.asks[0].price) / 2, price_tick) if previous is not None and previous.two_sided else mid
        change = mid - previous_mid
    return (mid, spread, imbalance, change)


def fit_scales(warmup: tuple[tuple[Decimal, ...], ...], *, price_tick: Decimal, fitted_through: datetime,
               offset_range_fraction: Decimal = Decimal("0.02"), return_range_fraction: Decimal = Decimal("0.005")) -> tuple[SensoryScale, ...]:
    """Fit origin/scale from warm-up rows only. ``fitted_through`` is their last time."""
    if not warmup:
        raise DomainError("SENSORY_WARMUP_REQUIRED")
    with localcontext(financial_context(80)):
        origin_mid = warmup[0][0]
        mids = [row[0] for row in warmup]
        mean_mid = _quantize(sum(mids, Decimal(0)) / len(mids), price_tick)
        offset_ticks = max(1, int((mean_mid * offset_range_fraction / price_tick).to_integral_value()))
        return_ticks = max(1, int((mean_mid * return_range_fraction / price_tick).to_integral_value()))
        del origin_mid
    return (
        SensoryScale("mid_offset", mean_mid, price_tick, offset_ticks, fitted_through),
        SensoryScale("spread_bps", Decimal(0), Decimal("0.01"), 5000, fitted_through),
        SensoryScale("depth_imbalance", Decimal(0), Decimal("0.000001"), 1_000_000, fitted_through),
        SensoryScale("mid_return_1", Decimal(0), price_tick, return_ticks, fitted_through),
    )


def exact_ticks(rows: tuple[tuple[Decimal, ...], ...], scales: tuple[SensoryScale, ...]) -> NDArray[np.int64]:
    """Integer-only tick offsets; identical algorithm to research vectorization."""
    units = np.empty((len(rows), len(scales)), dtype=np.int64)
    for row_index, row in enumerate(rows):
        if len(row) != len(scales):
            raise DomainError("SENSORY_FEATURE_WIDTH")
        for column, (value, scale) in enumerate(zip(row, scales)):
            numerator, denominator = value.as_integer_ratio()
            origin_n, origin_d = scale.origin.as_integer_ratio()
            tick_n, tick_d = scale.tick.as_integer_ratio()
            offset, remainder = divmod((numerator * origin_d - origin_n * denominator) * tick_d, denominator * origin_d * tick_n)
            if remainder or abs(offset) > LIMIT:
                raise DomainError("SENSORY_VALUE_OFF_GRID")
            units[row_index, column] = offset
    return units


def render_frame(ticks: NDArray[np.int64], scales: tuple[SensoryScale, ...]) -> NDArray[np.uint8]:
    scale_array = np.array([scale.scale_ticks for scale in scales], dtype=np.int64)
    values = ticks.astype(np.float64) / scale_array.astype(np.float64)
    if not np.all(np.isfinite(values)) or np.any(np.rint(values * scale_array) != ticks):
        raise DomainError("SENSORY_NORMALIZATION_ROUNDTRIP")
    pixels = np.rint((np.clip(values, -1, 1) + 1) * 127.5).astype(np.uint8)
    rgb = np.repeat(pixels[:, :, None], 3, axis=2)
    rgb.flags.writeable = False
    return rgb


class SensoryFrameBuilder:
    """Per-instrument rolling window -> audited RGB frame; warm-up gates decisions."""

    def __init__(self, *, instrument: str, price_tick: Decimal, warmup_observations: int, feature_window: int) -> None:
        if warmup_observations < 1 or feature_window < 2:
            raise ValueError("positive warm-up and window sizes required")
        self.instrument = instrument
        self.price_tick = price_tick
        self.warmup_observations = warmup_observations
        self.feature_window = feature_window
        self._rows: list[tuple[datetime, tuple[Decimal, ...]]] = []
        self._received: list[datetime] = []
        self._previous: MarketObservation | None = None
        self._last_sequence: int | None = None
        self._scales: tuple[SensoryScale, ...] | None = None

    @property
    def warm(self) -> bool:
        return self._scales is not None and len(self._rows) >= self.feature_window

    @property
    def observations(self) -> int:
        return len(self._rows)

    @property
    def scales(self) -> tuple[SensoryScale, ...] | None:
        return self._scales

    def ingest(self, observation: MarketObservation) -> bool:
        """Accept a newer observation; duplicates and regressions are ignored."""
        if observation.instrument != self.instrument or not observation.two_sided:
            return False
        if self._last_sequence is not None and observation.sequence <= self._last_sequence:
            return False
        if self._rows and observation.observed_at < self._rows[-1][0]:
            raise DomainError("OBSERVATION_TIME_REGRESSION")
        row = observation_features(self._previous, observation, price_tick=self.price_tick)
        self._rows.append((observation.observed_at, row))
        self._received.append(observation.received_at)
        self._previous = observation
        self._last_sequence = observation.sequence
        if len(self._rows) > max(self.feature_window, self.warmup_observations) * 4:
            keep = max(self.feature_window, self.warmup_observations) * 2
            del self._rows[: len(self._rows) - keep]
            del self._received[: len(self._received) - keep]
        if self._scales is None and len(self._rows) >= self.warmup_observations:
            warmup = tuple(item[1] for item in self._rows[: self.warmup_observations])
            self._scales = fit_scales(warmup, price_tick=self.price_tick, fitted_through=self._rows[self.warmup_observations - 1][0])
        return True

    def frame(self, *, decision_at: datetime) -> SensoryFrame:
        if not self.warm or self._scales is None:
            raise DomainError("SENSORY_WARMUP_INCOMPLETE")
        window = self._rows[-self.feature_window:]
        observed_through = window[-1][0]
        if observed_through > decision_at:
            raise DomainError("SENSORY_FUTURE_OBSERVATION")
        available_through = max(self._received[-self.feature_window:])
        if available_through > decision_at:
            raise DomainError("SENSORY_FUTURE_OBSERVATION")
        rows = tuple(item[1] for item in window)
        ticks = exact_ticks(rows, self._scales)
        rgb = render_frame(ticks, self._scales)
        return SensoryFrame(rgb=rgb, ticks=ticks, features=rows, decision_at=decision_at, observed_through=observed_through,
                            fitted_through=self._scales[0].fitted_through, instrument=self.instrument,
                            available_through=max(available_through, observed_through))
