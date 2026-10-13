"""Safety-only decisions. The guardian never invents an exit fill or replacement trade."""

from enum import StrEnum

from coinfly.domain.context import ContextKey, DomainError
from coinfly.execution.evidence import fence_authority_tx
from coinfly.risk.context_interlock import ContextInterlock


class GuardianAction(StrEnum):
    RETAIN_PROTECTION = "RETAIN_PROTECTION"
    DRAIN_REQUIRED = "DRAIN_REQUIRED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class Guardian:
    def __init__(self, interlock: ContextInterlock) -> None:
        self.interlock = interlock

    def incident(self, context: ContextKey, *, reason: str, evidence_id: str) -> GuardianAction:
        if not reason or not evidence_id:
            raise DomainError("INCIDENT_EVIDENCE_REQUIRED")
        self.interlock.provisional_fence(context.authority_id, reason)
        self.interlock.writer.call(lambda connection: fence_authority_tx(connection, context, reason, evidence_id))
        return GuardianAction.RECONCILE_REQUIRED
