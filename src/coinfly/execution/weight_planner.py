"""Trusted weight-to-quantity translation using the active execution book only."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext

from pydantic import Field

from coinfly.domain.context import DomainError, FrozenModel, Identifier, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.execution.models import IntentKind, Side
from coinfly.risk.admission_actor import ActiveBookSnapshot
from coinfly.strategy.target_coordinator import BoundTarget


class InstrumentContract(FrozenModel):
    instrument: Identifier
    quantity_increment: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    price_increment: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    minimum_quantity: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    minimum_notional: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


class PlannedDelta(FrozenModel):
    instrument: Identifier
    side: Side
    kind: IntentKind
    quantity: ExactDecimal
    limit_price: ExactDecimal
    contract_multiplier: ExactDecimal


def plan_weights(
    target: BoundTarget,
    book: ActiveBookSnapshot,
    contracts: tuple[InstrumentContract, ...],
    actual_quantities: dict[str, Decimal],
    pending_signed_quantities: dict[str, Decimal],
    *,
    now: datetime,
) -> tuple[PlannedDelta, ...]:
    """Planning is not admission. Every returned delta still needs a fresh grant.

    UNKNOWN possible fills belong in pending quantities and are also fenced by
    admission. A proposal never crosses zero: while the reconciled position
    (``actual_quantities``) or any pending quantity in an instrument has the
    opposite sign of the desired exposure, only the closing leg is planned, and
    the opening leg waits for a later proposal against a snapshot that shows the
    instrument flat and unencumbered. This is what keeps a derivatives reversal
    from being one signed order that a partial fill or a cancelled leg would turn
    into an unlabeled position. Dust below certified increments is retained,
    never reset.
    """
    book.require_current(now)
    if target.context != book.context or target.proposal.expires_at <= now:
        raise DomainError("TARGET_EXECUTION_CONTEXT_STALE")
    contract_map = {contract.instrument: contract for contract in contracts}
    if len(contract_map) != len(contracts):
        raise DomainError("DUPLICATE_INSTRUMENT_CONTRACT")
    weights = {item.instrument_id: item.weight for item in target.proposal.weights}
    if set(weights) != set(contract_map):
        raise DomainError("TARGET_FULL_SCOPE_REQUIRED")
    if not set(actual_quantities).issubset(weights) or not set(pending_signed_quantities).issubset(weights):
        raise DomainError("UNMANAGED_EXECUTION_EXPOSURE")
    values = (*actual_quantities.values(), *pending_signed_quantities.values())
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or abs(value) > Decimal("1e38")
        for value in values
    ):
        raise DomainError("INVALID_EXECUTION_QUANTITY")
    marks = {mark.instrument: mark for mark in book.marks}
    result: list[PlannedDelta] = []
    with localcontext(financial_context()) as decimal_context:
        decimal_context.prec = 256
        if book.context.product_track == ProductTrack.SPOT and (
            any(weight < 0 or weight > 1 for weight in weights.values())
            or sum(weights.values(), Decimal(0)) > 1
        ):
            raise DomainError("INVALID_SPOT_WEIGHTS")
        for instrument in target.proposal.affected_instruments:
            weight = weights[instrument]
            contract = contract_map[instrument]
            mark = marks.get(instrument)
            if mark is None:
                raise DomainError("INSTRUMENT_MARK_UNVERIFIED")
            desired = weight * book.funded_equity / (mark.price * mark.contract_multiplier)
            actual = actual_quantities.get(instrument, Decimal(0))
            pending = pending_signed_quantities.get(instrument, Decimal(0))
            effective = actual + pending
            if desired and ((actual and (desired > 0) != (actual > 0)) or (pending and (desired > 0) != (pending > 0))):
                # Reversal: close what is held (and what may still fill) first;
                # the opening leg is planned only from a reconciled flat state.
                desired = Decimal(0)
            delta = desired - effective
            if not delta:
                continue
            reducing = effective != 0 and (delta > 0) != (effective > 0)
            if reducing:
                if not actual or (delta > 0) == (actual > 0):
                    # Only a pending leg stands in the way: nothing held can be
                    # reduced, and an order against an unfilled order is not a
                    # position change. The pending leg runs out under its TTL.
                    continue
                delta = min(abs(delta), abs(effective), abs(actual)) * (Decimal(1) if delta > 0 else Decimal(-1))
            quantity = (abs(delta) / contract.quantity_increment).to_integral_value(
                rounding=ROUND_DOWN
            ) * contract.quantity_increment
            # A limit may never be silently widened beyond the validated mark.
            price_rounding = ROUND_DOWN if delta > 0 else ROUND_UP
            price = (mark.price / contract.price_increment).to_integral_value(
                rounding=price_rounding
            ) * contract.price_increment
            if (
                not quantity
                or not price
                or quantity < contract.minimum_quantity
                or quantity * price * mark.contract_multiplier < contract.minimum_notional
            ):
                continue
            result.append(
                PlannedDelta(
                    instrument=instrument,
                    side=Side.BUY if delta > 0 else Side.SELL,
                    kind=IntentKind.EXIT if reducing else IntentKind.ENTRY,
                    quantity=quantity,
                    limit_price=price,
                    contract_multiplier=mark.contract_multiplier,
                )
            )
    return tuple(result)
