"""Scenario contract for a certified venue margin implementation."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import Field

from coinfly.domain.context import DomainError, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal


class MarginScenario(FrozenModel):
    evidence_id: str = Field(min_length=1)
    eligible_equity: ExactDecimal = Field(allow_inf_nan=False)
    initial_margin: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    maintenance_margin: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    incremental_reservations: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    initial_buffer: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    maintenance_buffer: ExactDecimal = Field(ge=0, allow_inf_nan=False)

    @exact_decimal
    def require_safe(self) -> None:
        if self.eligible_equity <= 0:
            raise DomainError("NONPOSITIVE_ELIGIBLE_EQUITY")
        if self.eligible_equity - self.initial_margin - self.incremental_reservations < self.initial_buffer:
            raise DomainError("INITIAL_MARGIN_HEADROOM")
        if self.eligible_equity - self.maintenance_margin < self.maintenance_buffer:
            raise DomainError("MAINTENANCE_MARGIN_HEADROOM")

    @property
    @exact_decimal
    def maintenance_utilization(self) -> Decimal:
        if self.eligible_equity <= 0:
            raise DomainError("NONPOSITIVE_ELIGIBLE_EQUITY")
        return self.maintenance_margin / self.eligible_equity


class MarginModel(Protocol):
    profile_id: str

    def evaluate(self, *, eligible_equity: Decimal, projected_gross: Decimal, incremental_reservations: Decimal) -> tuple[MarginScenario, ...]: ...

    def reservation_for(self, notional: Decimal) -> Decimal: ...


class ReductionScenario(FrozenModel):
    before: MarginScenario
    after: MarginScenario

    @exact_decimal
    def require_nonworsening(self) -> None:
        before, after = self.before, self.after
        before_initial_deficit = max(Decimal(0), before.initial_margin + before.incremental_reservations + before.initial_buffer - before.eligible_equity)
        after_initial_deficit = max(Decimal(0), after.initial_margin + after.incremental_reservations + after.initial_buffer - after.eligible_equity)
        before_maintenance_deficit = max(Decimal(0), before.maintenance_margin + before.maintenance_buffer - before.eligible_equity)
        after_maintenance_deficit = max(Decimal(0), after.maintenance_margin + after.maintenance_buffer - after.eligible_equity)
        if (after.initial_margin > before.initial_margin or after.maintenance_margin > before.maintenance_margin
                or after_initial_deficit > before_initial_deficit or after_maintenance_deficit > before_maintenance_deficit):
            raise DomainError("REDUCTION_WORSENS_STRESSED_MARGIN")


class MarginReductionEvidence(FrozenModel):
    evidence_id: str = Field(min_length=1)
    venue_reduce_only_enforced: bool
    contract_units_verified: bool
    all_fill_scenarios_covered: bool
    scenarios: tuple[ReductionScenario, ...] = Field(min_length=1)

    def require_safe_reduction(self) -> None:
        if not all((self.venue_reduce_only_enforced, self.contract_units_verified, self.all_fill_scenarios_covered)):
            raise DomainError("REDUCTION_MARGIN_UNCERTIFIED")
        for scenario in self.scenarios:
            scenario.require_nonworsening()


@runtime_checkable
class ReductionMarginModel(MarginModel, Protocol):
    """Separately certified close behavior, including fees and every partial fill."""

    def reduction_reservation_for(self, notional: Decimal) -> Decimal: ...

    def evaluate_reduction(
        self, *, eligible_equity: Decimal, current_gross: Decimal,
        projected_gross: Decimal, incremental_reservations: Decimal,
    ) -> MarginReductionEvidence: ...
