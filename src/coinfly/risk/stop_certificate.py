"""Explicit evidence for closedness; empty query responses are insufficient."""

from datetime import datetime, timezone

from pydantic import Field, field_validator

from coinfly.domain.context import ContextKey, DomainError, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal


class PositionResidual(FrozenModel):
    scope_id: Identifier
    instrument: Identifier
    quantity: ExactDecimal = Field(allow_inf_nan=False)


class ScopeWatermark(FrozenModel):
    scope_id: Identifier
    evidence_id: Identifier
    account_observed_at: datetime
    account_stream_sequence: int = Field(ge=0)
    applied_stream_sequence: int = Field(ge=0)
    history_complete: bool
    account_complete: bool

    @field_validator("account_observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware account evidence required")
        return value.astimezone(timezone.utc)


class RetirementEvidence(FrozenModel):
    context: ContextKey
    evidence_id: Identifier
    mutation_transports_retired: bool
    signing_handles_retired: bool
    all_tasks_joined_or_fenced: bool
    grants_revoked: bool

    def require_retired(self, context: ContextKey) -> None:
        if self.context != context or not all((
            self.mutation_transports_retired, self.signing_handles_retired,
            self.all_tasks_joined_or_fenced, self.grants_revoked,
        )):
            raise DomainError("MUTATION_RETIREMENT_UNPROVEN")


class StopCertificate(FrozenModel):
    proof_id: Identifier
    context: ContextKey
    execution_scope: tuple[Identifier, ...] = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    fact_sequence: int = Field(ge=0)
    watermarks: tuple[ScopeWatermark, ...] = Field(min_length=1)
    residual_positions: tuple[PositionResidual, ...]
    outstanding_order_ids: tuple[Identifier, ...]
    unresolved_create_ids: tuple[Identifier, ...]
    unapplied_fill_count: int = Field(ge=0)
    all_delegated_positions_covered: bool
    all_related_orders_terminal: bool
    dispatch_tasks_fenced: bool
    task_fence_evidence_id: Identifier

    @field_validator("issued_at", "expires_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware stop evidence required")
        return value.astimezone(timezone.utc)

    def require_valid(
        self, context: ContextKey, execution_scope: tuple[str, ...], now: datetime,
        max_account_age_seconds: int = 30,
    ) -> None:
        if self.context != context or set(self.execution_scope) != set(execution_scope):
            raise DomainError("STOP_PROOF_WRONG_REALM")
        if len(self.execution_scope) != len(set(self.execution_scope)):
            raise DomainError("STOP_PROOF_DUPLICATE_SCOPE")
        if not self.issued_at <= now < self.expires_at:
            raise DomainError("STOP_PROOF_STALE")
        if self.outstanding_order_ids or self.unresolved_create_ids or self.unapplied_fill_count:
            raise DomainError("STOP_PROOF_OPEN_OBLIGATIONS")
        if not all((self.all_delegated_positions_covered, self.all_related_orders_terminal, self.dispatch_tasks_fenced)):
            raise DomainError("STOP_PROOF_INCOMPLETE")
        if any(position.quantity != 0 or position.scope_id not in execution_scope for position in self.residual_positions):
            raise DomainError("STOP_PROOF_RESIDUAL_POSITION")
        if len(self.watermarks) != len(execution_scope) or {w.scope_id for w in self.watermarks} != set(execution_scope):
            raise DomainError("STOP_PROOF_INCOMPLETE_SCOPES")
        for watermark in self.watermarks:
            age = (now - watermark.account_observed_at).total_seconds()
            if not watermark.history_complete or not watermark.account_complete:
                raise DomainError("STOP_PROOF_INCOMPLETE_HISTORY")
            if watermark.applied_stream_sequence < watermark.account_stream_sequence:
                raise DomainError("STOP_PROOF_UNAPPLIED_STREAM")
            if age < 0 or age > max_account_age_seconds or watermark.account_observed_at > self.issued_at:
                raise DomainError("STOP_PROOF_STALE_ACCOUNT")
