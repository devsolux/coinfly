"""Origin-realm evidence reduction, deliberately without a create method."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from pydantic import Field, model_validator

from coinfly.accounting.journal import utc_now
from coinfly.domain.context import ContextKey, DomainError, FrozenModel, Identifier
from coinfly.execution.fact_reducer import FactReducer, acknowledge_tx, reject_tx, unknown_tx
from coinfly.execution.intent_store import IntentStore, get_tx
from coinfly.execution.models import IntentRecord
from coinfly.execution.recovery import ambiguous_intents_tx
from coinfly.storage.writer import Writer


class EvidenceKind(StrEnum):
    POSITIVE_IDENTITY = "POSITIVE_IDENTITY"
    AUTHORITATIVE_NONCREATION = "AUTHORITATIVE_NONCREATION"
    ABSENT_OR_AMBIGUOUS = "ABSENT_OR_AMBIGUOUS"


class ReconciliationEvidence(FrozenModel):
    evidence_id: Identifier
    context: ContextKey
    intent_id: Identifier
    kind: EvidenceKind
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    history_complete: bool
    venue_order_id: Identifier | None = None

    @model_validator(mode="after")
    def qualified(self) -> ReconciliationEvidence:
        if self.kind == EvidenceKind.POSITIVE_IDENTITY and self.venue_order_id is None:
            raise ValueError("positive attribution needs an authoritative order identity")
        if self.kind == EvidenceKind.AUTHORITATIVE_NONCREATION and not self.history_complete:
            raise ValueError("incomplete evidence cannot establish non-creation")
        return self


class Reconciler:
    """Only a certified adapter may produce positive/non-creation evidence.

    Complete empty history is still ABSENT_OR_AMBIGUOUS unless the venue provides
    a documented authoritative non-creation guarantee. Labels/amounts and elapsed
    time never upgrade evidence. This core provides no live evidence adapter.
    """

    def __init__(self, writer: Writer, reducer: FactReducer | None = None) -> None:
        self.writer = writer
        self.reducer = reducer or FactReducer(writer)
        self.intents = IntentStore(writer)

    def recover(self) -> tuple[IntentRecord, ...]:
        identifiers = self.writer.call(ambiguous_intents_tx)
        return tuple(self.reducer.unknown(intent_id, "restart-dispatch-marker") for intent_id in identifiers)

    def reconcile(self, evidence: ReconciliationEvidence) -> IntentRecord:
        def apply(connection: sqlite3.Connection) -> IntentRecord:
            record = get_tx(connection, evidence.intent_id)
            if evidence.context != record.intent.context:
                raise DomainError("RECONCILIATION_REALM_MISMATCH")
            existing = connection.execute(
                "SELECT source_digest,kind,order_id,intent_id FROM ex_reconciliation_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (
                    evidence.source_digest,
                    evidence.kind.value,
                    evidence.venue_order_id,
                    evidence.intent_id,
                ):
                    raise DomainError("RECONCILIATION_EVIDENCE_CONFLICT")
                return record
            connection.execute(
                "INSERT INTO ex_reconciliation_evidence VALUES(?,?,?,?,?,?,?)",
                (
                    evidence.evidence_id,
                    evidence.intent_id,
                    evidence.kind.value,
                    int(evidence.history_complete),
                    evidence.venue_order_id,
                    evidence.source_digest,
                    utc_now(),
                ),
            )
            if evidence.kind == EvidenceKind.POSITIVE_IDENTITY:
                assert evidence.venue_order_id is not None
                return acknowledge_tx(
                    connection, evidence.intent_id, evidence.venue_order_id, evidence.evidence_id
                )
            if evidence.kind == EvidenceKind.AUTHORITATIVE_NONCREATION:
                return reject_tx(
                    connection, evidence.intent_id, evidence.evidence_id, proven_no_creation=True
                )
            return unknown_tx(connection, evidence.intent_id, evidence.evidence_id)

        return self.writer.call(apply)
