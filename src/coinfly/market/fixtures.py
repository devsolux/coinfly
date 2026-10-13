"""Named, deterministic synthetic price paths for offline paper demonstrations.

These are *fixtures*: labeled SYNTHETIC_FIXTURE in every observation and status.
They exercise the pipeline mechanics with reproducible books. They are not
market data and never stand in for it in reports.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_EVEN, Decimal

from coinfly.domain.context import DomainError
from coinfly.market.feed import PathFunction, SyntheticFixtureFeed

_TICK = Decimal("0.01")


def _seeded_walk(seed: str, start: Decimal, step_bps: int, spread_bps: int) -> PathFunction:
    """Deterministic walk: the direction of each tick is a hash bit of (seed, tick)."""

    def path(tick: int) -> tuple[Decimal, Decimal]:
        price = start
        for index in range(1, tick + 1):
            digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
            direction = 1 if digest[0] & 1 else -1
            magnitude = 1 + digest[1] % 3  # 1..3 multiples of the step
            price = price * (Decimal(10000) + Decimal(direction * magnitude * step_bps)) / Decimal(10000)
        mid = (price / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK
        half = max(_TICK, (mid * Decimal(spread_bps) / Decimal(20000) / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK)
        return mid - half, mid + half

    return path


def _cached_walk(seed: str, start: Decimal, step_bps: int, spread_bps: int) -> PathFunction:
    """Same walk, computed incrementally so long runs stay O(1) per tick."""
    state: dict[str, int | Decimal] = {"tick": 0, "price": start}
    base = _seeded_walk(seed, start, step_bps, spread_bps)

    def path(tick: int) -> tuple[Decimal, Decimal]:
        last = int(state["tick"])
        price = state["price"]
        assert isinstance(price, Decimal)
        if tick < last:
            return base(tick)
        for index in range(last + 1, tick + 1):
            digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
            direction = 1 if digest[0] & 1 else -1
            magnitude = 1 + digest[1] % 3
            price = price * (Decimal(10000) + Decimal(direction * magnitude * step_bps)) / Decimal(10000)
        state["tick"], state["price"] = tick, price
        mid = (price / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK
        half = max(_TICK, (mid * Decimal(spread_bps) / Decimal(20000) / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK)
        return mid - half, mid + half

    return path


PROFILES: dict[str, dict[str, tuple[Decimal, int, int]]] = {
    # instrument base -> (start price, step bps per tick, spread bps)
    "random-walk-3": {"BTC": (Decimal("100.00"), 4, 4), "ETH": (Decimal("50.00"), 5, 6), "SOL": (Decimal("10.00"), 6, 10)},
    "trend-up-1": {"BTC": (Decimal("100.00"), 3, 4)},
    "flat-1": {"BTC": (Decimal("100.00"), 0, 4)},
}


def fixture_feed(profile: str, *, quote: str = "USD") -> SyntheticFixtureFeed:
    definition = PROFILES.get(profile)
    if definition is None:
        raise DomainError("FIXTURE_PROFILE_UNKNOWN")
    paths: dict[str, PathFunction] = {}
    for base, (start, step, spread) in definition.items():
        if profile == "trend-up-1":
            paths[f"{base}-{quote}"] = _trend(start, step, spread)
        else:
            paths[f"{base}-{quote}"] = _cached_walk(f"{profile}:{base}", start, step, spread)
    return SyntheticFixtureFeed(paths, quote=quote)


def _trend(start: Decimal, step_bps: int, spread_bps: int) -> PathFunction:
    def path(tick: int) -> tuple[Decimal, Decimal]:
        price = start * (Decimal(10000) + Decimal(step_bps)) ** tick / Decimal(10000) ** tick
        mid = (price / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK
        half = max(_TICK, (mid * Decimal(spread_bps) / Decimal(20000) / _TICK).to_integral_value(rounding=ROUND_HALF_EVEN) * _TICK)
        return mid - half, mid + half

    return path
