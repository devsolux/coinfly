"""Automatic operational strategy construction; no alternative trade signal.

Selection/slots are managed by the existing worker reconciler. Entries and
strategy exits still originate only from the full MaleCNS controller. This
policy selects neither profitable future instruments nor unapproved models.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, localcontext

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, ProductTrack
from coinfly.domain.decimal_math import financial_context

from .definition import (
    ControllerMode,
    ControllerProfile,
    DerivativePermissions,
    DirectionPermission,
    ExecutionSettings,
    MappingVersion,
    NeuralSettings,
    StrategyDefinition,
)


def automatic_strategy(config: EngineConfig, *, capital: Decimal, neural: NeuralSettings,
                       max_active_slots: int = 1,
                       directions: DirectionPermission = DirectionPermission.LONG_ONLY) -> StrategyDefinition:
    if not capital.is_finite() or capital <= 0:
        raise DomainError("INVALID_CAPITAL_BUDGET")
    if not 1 <= max_active_slots <= 100:
        raise DomainError("INVALID_STRATEGY_SLOT_LIMIT")
    if neural.mode is ControllerMode.RESEARCH_LEARNING:
        # A research session needs explicit reward/horizon/evidence semantics;
        # it is not created by a one-click inference preset.
        raise DomainError("EXPLICIT_RESEARCH_STRATEGY_REQUIRED")
    if config.product_track is ProductTrack.SPOT and directions is not DirectionPermission.LONG_ONLY:
        raise DomainError("SPOT_SHORT_PERMISSION_FORBIDDEN")
    derivative = config.product_track is not ProductTrack.SPOT
    with localcontext(financial_context(256)):
        fraction = (Decimal(1) / Decimal(max_active_slots)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return StrategyDefinition(
        strategy_id="malecns-autonomous", version=1,
        label="Full MaleCNS automatic strategy — no profitability qualification implied",
        research_only=config.environment is Environment.SANDBOX,
        controller_profile=ControllerProfile.PRESERVED_MALECNS_V1,
        mapping_version=MappingVersion.DERIVATIVES_STEP_V1 if derivative else MappingVersion.SPOT_STEP_V1,
        product_track=config.product_track, product_subtype=config.effective_subtype,
        capital_budget=capital, max_active_slots=max_active_slots,
        per_slot_max_exposure_fraction=fraction,
        quote_currencies=(config.reporting_currency,),
        execution=ExecutionSettings(turnover_policy="CONSTRAIN"),
        derivatives=DerivativePermissions(directions=directions) if derivative else None,
        neural=neural, learning_enabled=False, permitted_environments=(config.environment,),
    )
