"""Trusted worker stamps context; accepting weights does not authorize an order."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field

from coinfly.domain.context import ContextKey, DomainError, EngineState, FrozenModel, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal
from coinfly.strategy.contracts import TargetWeights


class BoundTarget(FrozenModel):
    context: ContextKey
    proposal: TargetWeights
    accepted_at: datetime
    # Stamped by worker ownership, never by the neural model or GUI.
    strategy_identity: str | None = None

    @property
    def is_explicit_zero(self) -> bool:
        return all(item.weight == 0 for item in self.proposal.weights)


class AllocationLimits(FrozenModel):
    max_gross: ExactDecimal = Field(default=Decimal("1"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    max_per_instrument: ExactDecimal = Field(default=Decimal("1"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


class TargetCoordinator:
    """One serialized worker-owned inbox, scoped to one immutable context.

    ``None`` means no proposal; validation failures raise DomainError; a vector of
    zeros is an explicit request that still goes through execution/risk admission.
    The coordinator owns no account balance or broker capability.
    """

    def __init__(
        self,
        context: ContextKey,
        eligible_instruments: tuple[str, ...],
        *,
        limits: AllocationLimits | None = None,
        max_feature_age: timedelta = timedelta(seconds=5),
        max_proposal_ttl: timedelta = timedelta(seconds=5),
    ) -> None:
        if not eligible_instruments or len(set(eligible_instruments)) != len(eligible_instruments):
            raise ValueError("nonempty unique universe required")
        if max_feature_age <= timedelta(0) or max_proposal_ttl <= timedelta(0):
            raise ValueError("positive freshness budgets required")
        self.context = context
        self.eligible_instruments = frozenset(eligible_instruments)
        self.limits = limits or AllocationLimits()
        self.max_feature_age = max_feature_age
        self.max_proposal_ttl = max_proposal_ttl
        self.state = EngineState.INITIALIZING
        self._snapshot: tuple[str, datetime] | None = None
        self._pending: BoundTarget | None = None
        self._last_revision = -1
        self._retired = False

    def set_snapshot(self, snapshot_id: str, as_of: datetime, *, warm: bool) -> None:
        self._aware(as_of)
        if self._retired or self.state in (EngineState.DRAINING, EngineState.STOPPED):
            raise DomainError("STRATEGY_FENCED")
        if self._snapshot is not None and as_of < self._snapshot[1]:
            raise DomainError("FEATURE_TIME_REGRESSION")
        self._snapshot = (snapshot_id, as_of)
        self.state = EngineState.ACTIVE if warm else EngineState.INITIALIZING

    @exact_decimal
    def bind(
        self, proposal: TargetWeights | None, *, expected_epoch: int, now: datetime
    ) -> BoundTarget | None:
        self._aware(now)
        if expected_epoch != self.context.epoch:
            raise DomainError("STALE_CONTEXT")
        if self._retired or self.state != EngineState.ACTIVE:
            raise DomainError("STRATEGY_FENCED")
        if proposal is None:
            return None
        if not isinstance(proposal, TargetWeights):
            raise DomainError("INVALID_WEIGHT_PROPOSAL")
        if self._snapshot is None or proposal.snapshot_id != self._snapshot[0]:
            raise DomainError("STALE_FEATURE_SNAPSHOT")
        age = now - self._snapshot[1]
        if age < timedelta(0) or age > self.max_feature_age:
            raise DomainError("STALE_FEATURE_SNAPSHOT")
        ttl = proposal.expires_at - now
        if ttl <= timedelta(0) or ttl > self.max_proposal_ttl:
            raise DomainError("INVALID_PROPOSAL_EXPIRY")
        instruments = frozenset(item.instrument_id for item in proposal.weights)
        if instruments != self.eligible_instruments:
            raise DomainError("INCOMPLETE_WEIGHT_VECTOR")
        values = tuple(item.weight for item in proposal.weights)
        if any(not value.is_finite() for value in values):
            raise DomainError("NONFINITE_WEIGHT")
        if any(abs(value) > self.limits.max_per_instrument for value in values):
            raise DomainError("INSTRUMENT_WEIGHT_LIMIT")
        gross = sum((abs(value) for value in values), Decimal(0))
        if gross > self.limits.max_gross:
            raise DomainError("GROSS_WEIGHT_LIMIT")
        if self.context.product_track == ProductTrack.SPOT:
            if any(value < 0 or value > 1 for value in values) or gross > 1:
                raise DomainError("INVALID_SPOT_WEIGHT")
        if self._pending is not None and proposal.decision_id == self._pending.proposal.decision_id:
            if proposal != self._pending.proposal:
                raise DomainError("DECISION_ID_CONFLICT")
            return self._pending
        if proposal.allocation_revision <= self._last_revision:
            raise DomainError("STALE_ALLOCATION_REVISION")
        bound = BoundTarget(context=self.context, proposal=proposal, accepted_at=now)
        self._pending = bound
        self._last_revision = proposal.allocation_revision
        return bound

    def quiesce(self) -> None:
        if self.state != EngineState.STOPPED:
            self.state = EngineState.DRAINING
        self._pending = None

    def retire(self, *, stopped_context: ContextKey) -> None:
        if stopped_context != self.context or self.state != EngineState.DRAINING:
            raise DomainError("STRATEGY_RETIREMENT_WITHOUT_STOP")
        # Caller invokes only after the durable stop certificate has committed.
        self._pending = None
        self._snapshot = None
        self._retired = True
        self.state = EngineState.STOPPED

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise DomainError("AWARE_TIMESTAMP_REQUIRED")
