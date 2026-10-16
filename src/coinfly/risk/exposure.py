"""Pure exact arithmetic; this guard does not select replacement trades."""

from decimal import Decimal

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import exact_decimal


@exact_decimal
def require_concentration(exposure: dict[str, Decimal], groups: dict[str, str], caps: dict[str, Decimal]) -> None:
    totals: dict[str, Decimal] = {}
    for instrument, notional in exposure.items():
        if not notional.is_finite():
            raise DomainError("INVALID_EXPOSURE_INPUT")
        group = groups.get(instrument)
        if group is None or group not in caps:
            raise DomainError("CONCENTRATION_POLICY_UNVERIFIED")
        totals[group] = totals.get(group, Decimal(0)) + abs(notional)
    if any(not cap.is_finite() or cap < 0 for cap in caps.values()):
        raise DomainError("INVALID_CONCENTRATION_LIMIT")
    if any(total > caps[group] for group, total in totals.items()):
        raise DomainError("CONCENTRATION_LIMIT")
