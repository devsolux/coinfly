"""Persisted, versioned strategy definitions: what automation is allowed to do.

A strategy definition is configuration, not a model. It names the controller
profile that produces decisions, the mapping that turns decisions into target
weights, the instrument universe rules (transparent filters, deterministic
ordering), the capital budget and exposure limits, turnover constraints,
execution settings and — for derivatives — explicit leverage, margin and
direction permissions. Every field is visible in the desktop; nothing here is a
hidden profit heuristic.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from coinfly.domain.context import Environment, FrozenModel, Identifier, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal
from coinfly.domain.products import ProductSubtype

# Current rich strategy contract, independent of application version 1.0.0.
# No older persisted strategy schema is imported into the fresh application.
STRATEGY_SCHEMA_VERSION: Literal[3] = 3


class ControllerProfile(StrEnum):
    # The preserved MaleCNS graph, fixed decoder, dopamine routes and propagator.
    PRESERVED_MALECNS_V1 = "coinfly-dual-compartment-v1"
    # Test-only scripted decisions that exercise pipeline mechanics. Never a
    # trading policy; refused outside SANDBOX and outside explicit fixtures.
    FIXTURE_SEQUENCE_V1 = "fixture-sequence-v1"


class MappingVersion(StrEnum):
    SPOT_STEP_V1 = "fixed-decoder-spot-step-v1"
    DERIVATIVES_STEP_V1 = "fixed-decoder-derivatives-step-v1"


class SelectionOrder(StrEnum):
    VOLUME_24H_DESC = "VOLUME_24H_DESC"
    PRIORITY_LIST = "PRIORITY_LIST"
    INSTRUMENT_ID = "INSTRUMENT_ID"


class DirectionPermission(StrEnum):
    LONG_ONLY = "LONG_ONLY"
    SHORT_ONLY = "SHORT_ONLY"
    BOTH = "BOTH"


class MarketDataRequirements(FrozenModel):
    max_observation_age_seconds: int = Field(default=5, ge=1, le=300)
    min_depth_levels: int = Field(default=1, ge=1, le=50)
    warmup_observations: int = Field(default=8, ge=1, le=10000)
    feature_window: int = Field(default=16, ge=2, le=4096)


class UniverseFilters(FrozenModel):
    """Transparent, deterministic eligibility thresholds (shown per rejection).

    Hysteresis: an owned instrument is retired only after it has been absent
    from ``retirement_grace_selections`` consecutive selections, and a retired
    instrument is not re-selected before ``reentry_cooldown_seconds`` elapsed
    (``SELECTION_COOLDOWN``). Both prevent transient ranking changes from
    forcing repeated drains; both are declared here, not hidden.
    """

    require_tradable: bool = True
    retirement_grace_selections: int = Field(default=3, ge=1, le=1000)
    reentry_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    max_spread_bps: ExactDecimal = Field(default=Decimal("50"), gt=0, allow_inf_nan=False, max_digits=20, decimal_places=6)
    min_top_of_book_notional: ExactDecimal = Field(default=Decimal("100"), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    min_volume_24h: ExactDecimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    order: SelectionOrder = SelectionOrder.VOLUME_24H_DESC
    priority: tuple[Identifier, ...] = ()


class DerivativePermissions(FrozenModel):
    max_leverage: ExactDecimal = Field(default=Decimal("1"), ge=1, le=Decimal("10"), allow_inf_nan=False, max_digits=6, decimal_places=2)
    directions: DirectionPermission = DirectionPermission.LONG_ONLY
    margin_mode: Literal["CROSS", "ISOLATED"] = "CROSS"
    allow_reversal: bool = False
    max_maintenance_utilization: ExactDecimal = Field(default=Decimal("0.5"), gt=0, lt=1, allow_inf_nan=False, max_digits=6, decimal_places=4)


class ExecutionSettings(FrozenModel):
    proposal_ttl_seconds: int = Field(default=5, ge=1, le=30)
    decision_interval_seconds: int = Field(default=2, ge=1, le=3600)
    # Exact turnover guard (coinfly.strategy.turnover_ledger): gross executed +
    # reserved + proposed notional must stay STRICTLY below
    # ``max_turnover_fraction`` of the equity observed at the start of each
    # ``accounting_interval_seconds`` window. ``turnover_policy`` says what
    # happens to an order that does not fit: REJECT (research default; a guard
    # never shrinks an order) or CONSTRAIN (reduce to the exact feasible
    # quantity and record it). Note that a target step equal to the fraction is
    # never admissible from a flat book under REJECT (5 % is not strictly below
    # 5 %); CONSTRAIN derives the feasible quantity instead.
    accounting_interval_seconds: int = Field(default=60, ge=1, le=86400)
    turnover_policy: Literal["REJECT", "CONSTRAIN"] = "REJECT"
    # A resting limit that has not filled within ``order_ttl_seconds`` is
    # cancelled (never converted to a market order); ``expiry_policy`` rolls
    # the intended target back by the unfilled portion so targets never
    # accumulate silently. A later decoded decision re-proposes at its own mark.
    order_ttl_seconds: int = Field(default=30, ge=1, le=86400)
    expiry_policy: Literal["ROLLBACK_UNFILLED_TARGET", "KEEP_TARGET"] = "ROLLBACK_UNFILLED_TARGET"
    # One decoded decision moves the target weight of one instrument by at most
    # ``target_step`` (the preserved fixed-decoder step mapping). The research
    # turnover guard (coinfly.research.turnover: gross notional < 5 % of equity
    # per interval) is mirrored here as ``max_turnover_fraction``; the exact
    # notional check happens per planned order (see ``turnover_policy``).
    target_step: ExactDecimal = Field(default=Decimal("0.05"), gt=0, le=1, allow_inf_nan=False, max_digits=6, decimal_places=4)
    max_turnover_fraction: ExactDecimal = Field(default=Decimal("0.05"), gt=0, le=1, allow_inf_nan=False, max_digits=6, decimal_places=4)
    # Wall-clock budget for the bounded neural computations of one tick; work
    # that does not fit is deferred (never skipped silently) and reported.
    compute_budget_seconds: float = Field(default=30.0, gt=0, le=600)

    @model_validator(mode="after")
    def step_within_turnover(self) -> ExecutionSettings:
        if self.max_turnover_fraction > Decimal("0.05"):
            raise ValueError("the turnover fraction cannot exceed the preserved research limit (5 %)")
        if self.target_step > self.max_turnover_fraction:
            raise ValueError("target step cannot exceed the turnover fraction")
        return self


class ControllerMode(StrEnum):
    # The preserved graph with its initial (untrained) efficacies, weights frozen.
    BASELINE_INFERENCE = "BASELINE_INFERENCE"
    # Weights restored from a named, provenance-checked frozen checkpoint.
    FROZEN_CHECKPOINT = "FROZEN_CHECKPOINT"
    # Full-graph paper research: experimental plasticity under a declared
    # training profile, rewards from the paper ledger. Research mode, SANDBOX,
    # one slot (one chronological session).
    RESEARCH_LEARNING = "RESEARCH_LEARNING"


TRAINING_PROFILE_IDS = ("malecns-risk-adjusted-experiment-v2", "malecns-risk-adjusted-experiment-v3")


class TrainingSettings(FrozenModel):
    """The declared experimental profile for RESEARCH_LEARNING; nothing is loaded silently.

    ``plasticity`` and ``risk_reward`` hold explicit overrides of the research
    ``PlasticityConfig`` / ``RiskRewardConfig`` fields; they are validated by
    those dataclasses when the controller is built, and recorded in provenance.
    """

    profile_id: Literal["malecns-risk-adjusted-experiment-v2", "malecns-risk-adjusted-experiment-v3"]
    implementation_revision: int = Field(ge=1, le=100)
    seed: int = Field(default=1729, ge=0, lt=2**63)
    # Declared decision-to-reward credit horizon of THIS host (seconds); the
    # worker measures the real delay and reports every window that exceeds it.
    reward_credit_horizon_seconds: float = Field(default=5.0, gt=0, le=150)
    # Reward windows are the accounting windows (contiguous, equal duration).
    reward_interval_seconds: float = Field(default=1.0, gt=0, le=86400)
    plasticity: dict[str, float | int] = Field(default_factory=dict)
    risk_reward: dict[str, float | int] = Field(default_factory=dict)


class NeuralSettings(FrozenModel):
    mode: ControllerMode = ControllerMode.BASELINE_INFERENCE
    # FROZEN_CHECKPOINT: the checkpoint file name under the application's
    # checkpoints directory, and the training profile it must declare (None for
    # a frozen baseline checkpoint without an experiment section).
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    expected_training_profile_id: Literal["malecns-risk-adjusted-experiment-v2", "malecns-risk-adjusted-experiment-v3"] | None = None
    training: TrainingSettings | None = None

    @model_validator(mode="after")
    def coherent(self) -> NeuralSettings:
        if self.mode is ControllerMode.FROZEN_CHECKPOINT:
            if self.checkpoint_id is None:
                raise ValueError("a frozen checkpoint mode names its checkpoint")
            if self.training is not None:
                raise ValueError("a frozen checkpoint never carries a training profile")
        elif self.mode is ControllerMode.RESEARCH_LEARNING:
            if self.training is None:
                raise ValueError("research learning requires an explicit training profile")
            if self.checkpoint_id is not None:
                raise ValueError("research learning starts from the fresh qualified graph, not a checkpoint")
        else:
            if self.checkpoint_id is not None or self.training is not None or self.expected_training_profile_id is not None:
                raise ValueError("baseline inference declares neither checkpoint nor training profile")
        return self


class StrategyDefinition(FrozenModel):
    schema_version: Literal[3] = STRATEGY_SCHEMA_VERSION
    strategy_id: Identifier
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    research_only: bool = True
    controller_profile: ControllerProfile
    mapping_version: MappingVersion
    product_track: ProductTrack = ProductTrack.SPOT
    product_subtype: ProductSubtype = ProductSubtype.SPOT
    permitted_instruments: tuple[Identifier, ...] = Field(default=(), max_length=200)
    # Operator exclusions always win over selection (EXCLUDED_BY_OPERATOR).
    excluded_instruments: tuple[Identifier, ...] = Field(default=(), max_length=200)
    quote_currencies: tuple[str, ...] = Field(default=("USD",), min_length=1, max_length=8)
    market_data: MarketDataRequirements = Field(default_factory=MarketDataRequirements)
    filters: UniverseFilters = Field(default_factory=UniverseFilters)
    max_active_slots: int = Field(default=3, ge=1, le=100)
    capital_budget: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    per_slot_max_exposure_fraction: ExactDecimal = Field(default=Decimal("0.34"), gt=0, le=1, allow_inf_nan=False, max_digits=6, decimal_places=4)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    derivatives: DerivativePermissions | None = None
    neural: NeuralSettings = Field(default_factory=NeuralSettings)
    learning_enabled: bool = False
    permitted_environments: tuple[Environment, ...] = (Environment.SANDBOX,)
    # Scripted sides for the FIXTURE_SEQUENCE_V1 pipeline controller only; the
    # persisted strategy therefore shows exactly which scripted decisions ran.
    fixture_sides: tuple[Literal["BUY", "SELL", "HOLD"], ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def coherent(self) -> StrategyDefinition:
        if self.product_subtype.track != self.product_track:
            raise ValueError("strategy subtype does not match its product track")
        if self.product_track is ProductTrack.SPOT:
            if self.mapping_version is not MappingVersion.SPOT_STEP_V1:
                raise ValueError("spot strategies use the spot mapping")
            if self.derivatives is not None:
                raise ValueError("spot strategies carry no derivative permissions")
        else:
            if self.mapping_version is not MappingVersion.DERIVATIVES_STEP_V1:
                raise ValueError("derivative strategies use the derivatives mapping")
            if self.derivatives is None:
                raise ValueError("derivative strategies require explicit leverage, margin and direction permissions")
        if self.controller_profile is ControllerProfile.FIXTURE_SEQUENCE_V1:
            if Environment.LIVE in self.permitted_environments:
                raise ValueError("fixture controllers are never permitted live")
            if not self.fixture_sides:
                raise ValueError("fixture controllers require an explicit scripted sequence")
        elif self.fixture_sides:
            raise ValueError("scripted sides belong to the fixture controller only")
        if self.learning_enabled and not self.research_only:
            raise ValueError("learning requires the research-only flag")
        if self.learning_enabled != (self.neural.mode is ControllerMode.RESEARCH_LEARNING):
            raise ValueError("learning_enabled is exactly the RESEARCH_LEARNING controller mode; nothing learns silently")
        if self.neural.mode is ControllerMode.RESEARCH_LEARNING:
            if self.controller_profile is not ControllerProfile.PRESERVED_MALECNS_V1:
                raise ValueError("research learning runs the preserved controller only")
            if self.max_active_slots != 1:
                raise ValueError("research learning is one chronological session: exactly one slot")
            if Environment.LIVE in self.permitted_environments:
                raise ValueError("research learning is never permitted live")
            assert self.neural.training is not None
            if abs(self.neural.training.reward_interval_seconds - self.execution.accounting_interval_seconds) > 1e-9:
                raise ValueError("reward windows are the accounting windows: intervals must match")
        elif self.neural.mode is not ControllerMode.BASELINE_INFERENCE and self.controller_profile is not ControllerProfile.PRESERVED_MALECNS_V1:
            raise ValueError("checkpoint modes belong to the preserved controller")
        if len(set(self.permitted_instruments)) != len(self.permitted_instruments):
            raise ValueError("duplicate permitted instruments")
        if set(self.permitted_instruments) & set(self.excluded_instruments):
            raise ValueError("an instrument cannot be both permitted and excluded")
        if self.per_slot_max_exposure_fraction * self.max_active_slots < Decimal("0.999") and self.max_active_slots > 1:
            pass  # Under-allocation is allowed; over-allocation is bounded by the budget.
        return self

    @property
    def identity(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def max_observation_age(self) -> timedelta:
        return timedelta(seconds=self.market_data.max_observation_age_seconds)


def research_paper_preset(*, capital_budget: Decimal = Decimal("100"), controller: ControllerProfile = ControllerProfile.PRESERVED_MALECNS_V1) -> StrategyDefinition:
    """The clearly labeled research-only paper preset for first use.

    It uses the preserved MaleCNS controller and the spot step mapping on the
    USD-quoted synthetic universe. Learning stays off; nothing here claims
    profitability. Live capital and authority require a separate, deliberate
    approval and are not granted by this preset.
    """
    return StrategyDefinition(
        strategy_id="research-paper-spot", version=1, label="RESEARCH ONLY · paper spot · fixed decoder step",
        research_only=True, controller_profile=controller, mapping_version=MappingVersion.SPOT_STEP_V1,
        product_track=ProductTrack.SPOT, product_subtype=ProductSubtype.SPOT, quote_currencies=("USD",),
        max_active_slots=3, capital_budget=capital_budget, per_slot_max_exposure_fraction=Decimal("0.34"),
        filters=UniverseFilters(max_spread_bps=Decimal("50"), min_top_of_book_notional=Decimal("100")),
        # The preserved step (5 % of equity) equals the strict turnover limit, so
        # from a flat book only the exact feasible quantity below it can execute.
        execution=ExecutionSettings(turnover_policy="CONSTRAIN"),
    )
