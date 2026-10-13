"""Dated Coinbase spot research presets, never live account fee authority.

Advanced's public VIP page conflicts with its 2026-09-16 regional announcement.
Keep them separately named; never infer account tier, region or eligibility.
Stable pairs, liquidity programs, rebates and derivatives are outside this table.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from coinfly.research.reward import TakerFees

D = Decimal


@dataclass(frozen=True)
class FeePreset:
    venue: Literal["coinbase-exchange", "coinbase-advanced"]
    source: str
    thresholds: tuple[Decimal, ...]
    rates: tuple[Decimal, ...]
    volume_ceiling: Decimal | None = None


PRESETS = {
    "exchange-standard-2026-09-20": FeePreset(
        "coinbase-exchange", "https://help.coinbase.com/en/exchange/trading-and-funding/exchange-fees",
        tuple(map(D, ("0", "10000", "50000", "100000", "1000000", "15000000", "75000000", "250000000", "400000000"))),
        tuple(map(D, ("0.006", "0.004", "0.0025", "0.002", "0.0018", "0.0016", "0.001", "0.0006", "0.0004")))),
    "advanced-public-vip-page-2026-09-20": FeePreset(
        "coinbase-advanced", "https://www.coinbase.com/en-au/advanced-vip",
        tuple(map(D, ("0", "10000", "25000", "75000", "250000", "500000", "1000000", "5000000", "10000000", "20000000", "50000000", "100000000", "250000000"))),
        tuple(map(D, ("0.012", "0.008", "0.005", "0.0025", "0.0015", "0.00125", "0.001", "0.00085", "0.00065", "0.0005", "0.00035", "0.00025", "0.0002")))),
}
for _region, _rate in (("us", "0.009"), ("eu-uk", "0.005"), ("international", "0.001")):
    PRESETS[f"advanced-entry-{_region}-2026-09-16"] = FeePreset(
        "coinbase-advanced", "https://www.coinbase.com/blog/were-lowering-fees-for-many-active-traders-on-coinbase-advanced",
        (D(0),), (D(_rate),), D(10000))


def preset_fees(preset_id: str, *, trailing_volume_usd: Decimal, observed_at: datetime,
                expires_at: datetime) -> TakerFees:
    """Explicit research assumption, with original Decimal tier boundaries.

    Volume must be known before submission; no future cumulative turnover or
    account-balance VIP qualification is inferred. The caller owns this evidence.
    """
    if (not isinstance(trailing_volume_usd, Decimal) or not trailing_volume_usd.is_finite()
            or trailing_volume_usd < 0 or observed_at.tzinfo is None or expires_at.tzinfo is None):
        raise ValueError("finite exact volume and aware dates required")
    preset = PRESETS[preset_id]
    if preset.volume_ceiling is not None and trailing_volume_usd >= preset.volume_ceiling:
        raise ValueError("published entry preset does not cover this tier")
    rate = preset.rates[0]
    for threshold, candidate in zip(preset.thresholds, preset.rates):
        if trailing_volume_usd < threshold:
            break
        rate = candidate
    return TakerFees(venue=preset.venue, rate=rate, effective_at=observed_at,
                     observed_at=observed_at, expires_at=expires_at, evidence_id=preset_id)


PRESET_RETRIEVED_AT = datetime(2026, 9, 20, tzinfo=timezone.utc)
