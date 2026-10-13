"""Pure exact arithmetic; this guard does not select replacement trades."""

from decimal import Decimal, localcontext

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import exact_decimal, financial_context


def gross_notional(positions: tuple[tuple[Decimal, Decimal, Decimal], ...]) -> Decimal:
    with localcontext(financial_context()) as context:
        context.prec = 100
        result = Decimal(0)
        for quantity, price, multiplier in positions:
            if not all(isinstance(value, Decimal) and value.is_finite() for value in (quantity, price, multiplier)) or price <= 0 or multiplier <= 0:
                raise DomainError("INVALID_EXPOSURE_INPUT")
            result += abs(quantity) * price * multiplier
        return result


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
