"""Transition phases are separate from an engine's administrative lifecycle."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from coinfly.config.models import EngineConfig
from coinfly.domain.context import ContextKey, EngineState, FrozenModel, Identifier


class TransitionPhase(StrEnum):
    REQUESTED = "REQUESTED"
    QUIESCING = "QUIESCING"
    DRAINING = "DRAINING"
    RECONCILING = "RECONCILING"
    STOPPED_OLD = "STOPPED_OLD"
    RESETTING = "RESETTING"
    # First activation: there is no source engine to quiesce, drain, stop or
    # reset. The explicit phase replaces STOPPED_OLD/RESETTING for that case.
    NO_PREVIOUS_CONTEXT = "NO_PREVIOUS_CONTEXT"
    INITIALIZING_NEW = "INITIALIZING_NEW"
    READY_UNARMED = "READY_UNARMED"
    COMPLETED = "COMPLETED"
    BLOCKED_DRAIN = "BLOCKED_DRAIN"
    FAILED_INITIALIZATION = "FAILED_INITIALIZATION"


IN_PROGRESS_PHASES: frozenset[TransitionPhase] = frozenset({
    TransitionPhase.REQUESTED, TransitionPhase.QUIESCING, TransitionPhase.DRAINING, TransitionPhase.RECONCILING,
    TransitionPhase.STOPPED_OLD, TransitionPhase.RESETTING, TransitionPhase.NO_PREVIOUS_CONTEXT,
    TransitionPhase.INITIALIZING_NEW,
})
RETRYABLE_PHASES: frozenset[TransitionPhase] = frozenset({
    TransitionPhase.BLOCKED_DRAIN, TransitionPhase.FAILED_INITIALIZATION,
})


class TransitionStage(StrEnum):
    """Where inside a handover an exception was raised; finer than the phase."""

    SOURCE_PROOF_CHECK = "SOURCE_PROOF_CHECK"
    SOURCE_QUIESCE = "SOURCE_QUIESCE"
    SOURCE_DRAIN = "SOURCE_DRAIN"
    SOURCE_RECONCILE = "SOURCE_RECONCILE"
    SOURCE_RETIREMENT = "SOURCE_RETIREMENT"
    SOURCE_RESET = "SOURCE_RESET"
    TARGET_VALIDATION = "TARGET_VALIDATION"
    AUTHORITY_FENCE_CHECK = "AUTHORITY_FENCE_CHECK"
    NETWORK_HANDLES = "NETWORK_HANDLES"
    ENGINE_FACTORY = "ENGINE_FACTORY"
    ENGINE_INITIALIZE = "ENGINE_INITIALIZE"
    EFFECTIVE_PUBLICATION = "EFFECTIVE_PUBLICATION"


class CorrectiveAction(StrEnum):
    """Stable, translatable operator guidance attached to a recorded failure."""

    VERIFY_DATASET = "ACTION_VERIFY_DATASET"
    RESTART_WORKER = "ACTION_RESTART_WORKER"
    RESOLVE_INCIDENTS = "ACTION_RESOLVE_INCIDENTS"
    CHECK_STORAGE = "ACTION_CHECK_STORAGE"
    RETRY_OPERATION = "ACTION_RETRY_OPERATION"
    INSPECT_WORKER_LOG = "ACTION_INSPECT_WORKER_LOG"


class ExceptionLinkRecord(FrozenModel):
    type: str = Field(min_length=1, max_length=240)
    relation: str = Field(min_length=1, max_length=20)
    code: str | None = Field(default=None, max_length=80)
    module: str | None = Field(default=None, max_length=160)
    errno: str | None = Field(default=None, max_length=40)


class TransitionFailure(FrozenModel):
    """Durable, redacted record of why a handover stopped.

    ``code`` is the stable code shown to operators (the underlying exception's
    own code where it has one; otherwise ENGINE_OPERATION_FAILED as a category
    with the concrete exception type preserved in ``chain``).
    """

    stage: TransitionStage
    code: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=40)
    chain: tuple[ExceptionLinkRecord, ...] = Field(max_length=8)
    retry_safe: bool
    corrective_action: CorrectiveAction
    recorded_at: datetime

    @property
    def summary(self) -> str:
        parts = []
        for link in self.chain:
            text = link.type.rsplit(".", 1)[-1]
            if link.code:
                text += f"[{link.code}]"
            elif link.module:
                text += f"[{link.module}]"
            elif link.errno:
                text += f"[{link.errno}]"
            parts.append(text)
        return " <- ".join(parts)


class ContextPreview(FrozenModel):
    preview_id: Identifier
    authority_id: Identifier
    config: EngineConfig
    config_digest: str
    expected_revision: int = Field(ge=0)
    expected_epoch: int = Field(ge=0)
    expires_at: datetime


class TransitionOperation(FrozenModel):
    operation_id: Identifier
    request_id: Identifier
    authority_id: Identifier
    phase: TransitionPhase
    source_context: ContextKey | None
    target_context: ContextKey
    proposed_config: EngineConfig
    proposed_revision: int = Field(ge=1)
    transition_epoch: int = Field(ge=1)
    error_code: str | None = None
    failure: TransitionFailure | None = None

    @property
    def in_progress(self) -> bool:
        return self.phase in IN_PROGRESS_PHASES

    @property
    def retryable(self) -> bool:
        return self.phase in RETRYABLE_PHASES


class ContextStatus(FrozenModel):
    authority_id: Identifier
    effective_context: ContextKey | None
    effective_config: EngineConfig | None
    effective_revision: int = Field(ge=0)
    transition_epoch: int = Field(ge=0)
    engine_state: EngineState
    armed: bool
    admissions_fenced: bool
    recovery_required: bool
    operation: TransitionOperation | None
