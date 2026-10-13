"""Margin scenarios for the LOCAL PAPER perpetual book only.

The paper venue's linear margin (``PaperMarginBook``: initial 10 %, maintenance
5 % of gross notional, fees settled from collateral) is explicit and synthetic.
This model exposes exactly those rules to the admission actor so that paper
derivative intents are evaluated with the same stressed scenarios the live path
demands, and it declares its profile as ``paper-local``: it can never be bound
to a live broker context (the admission actor compares ``profile_id`` with the
context's broker profile), and no live venue margin is inferred from it.

Every evaluation returns the base scenario plus an adverse-mark scenario in
which the whole projected gross notional moves ``stress_fraction`` against the
book; reductions are proven non-worsening for both the full and a half fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from coinfly.domain.decimal_math import exact_decimal, financial_context
from coinfly.risk.margin_models import MarginReductionEvidence, MarginScenario, ReductionScenario

PAPER_MARGIN_PROFILE_ID = "paper-local"


@dataclass
class PaperLinearMarginModel:
    profile_id: str = PAPER_MARGIN_PROFILE_ID
    initial_margin_fraction: Decimal = Decimal("0.1")
    maintenance_margin_fraction: Decimal = Decimal("0.05")
    fee_buffer_fraction: Decimal = Decimal("0.002")
    stress_fraction: Decimal = Decimal("0.10")
    initial_buffer_fraction: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        for value in (self.initial_margin_fraction, self.maintenance_margin_fraction, self.fee_buffer_fraction,
                      self.stress_fraction, self.initial_buffer_fraction):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("finite non-negative margin fractions required")
        if not 0 < self.maintenance_margin_fraction <= self.initial_margin_fraction <= 1 or self.fee_buffer_fraction <= 0:
            raise ValueError("invalid paper margin configuration")

    @exact_decimal
    def reservation_for(self, notional: Decimal) -> Decimal:
        with localcontext(financial_context()):
            return notional * (self.initial_margin_fraction + self.fee_buffer_fraction)

    @exact_decimal
    def reduction_reservation_for(self, notional: Decimal) -> Decimal:
        with localcontext(financial_context()):
            return notional * self.fee_buffer_fraction

    @exact_decimal
    def _scenario(self, evidence_id: str, *, eligible_equity: Decimal, gross: Decimal, incremental_reservations: Decimal,
                  stressed: bool) -> MarginScenario:
        with localcontext(financial_context()):
            equity = eligible_equity - gross * self.stress_fraction if stressed else eligible_equity
            return MarginScenario(
                evidence_id=evidence_id, eligible_equity=equity,
                initial_margin=gross * self.initial_margin_fraction, maintenance_margin=gross * self.maintenance_margin_fraction,
                incremental_reservations=incremental_reservations, initial_buffer=gross * self.initial_buffer_fraction,
                maintenance_buffer=Decimal(0),
            )

    def evaluate(self, *, eligible_equity: Decimal, projected_gross: Decimal, incremental_reservations: Decimal) -> tuple[MarginScenario, ...]:
        return (
            self._scenario("paper-margin-base", eligible_equity=eligible_equity, gross=projected_gross,
                           incremental_reservations=incremental_reservations, stressed=False),
            self._scenario("paper-margin-stressed", eligible_equity=eligible_equity, gross=projected_gross,
                           incremental_reservations=incremental_reservations, stressed=True),
        )

    def evaluate_reduction(self, *, eligible_equity: Decimal, current_gross: Decimal, projected_gross: Decimal,
                           incremental_reservations: Decimal) -> MarginReductionEvidence:
        with localcontext(financial_context()):
            partial_gross = current_gross - (current_gross - projected_gross) / 2
        scenarios = []
        for stressed in (False, True):
            before = self._scenario("paper-reduction-before", eligible_equity=eligible_equity, gross=current_gross,
                                    incremental_reservations=incremental_reservations, stressed=stressed)
            for after_gross in (projected_gross, partial_gross):
                after = self._scenario("paper-reduction-after", eligible_equity=eligible_equity, gross=after_gross,
                                       incremental_reservations=incremental_reservations, stressed=stressed)
                scenarios.append(ReductionScenario(before=before, after=after))
        return MarginReductionEvidence(
            evidence_id="paper-linear-margin-reduction",
            # The paper matcher enforces reduce-only itself (PAPER_REDUCE_ONLY_VIOLATION),
            # quantities are contract units with the instrument multiplier, and the
            # scenarios cover the full and a partial fill under base and stress.
            venue_reduce_only_enforced=True, contract_units_verified=True, all_fill_scenarios_covered=True,
            scenarios=tuple(scenarios),
        )
