"""CAS send marker; DISPATCH_STARTED is never a replay instruction."""

from __future__ import annotations

import sqlite3

from coinfly.accounting.journal import utc_now
from coinfly.domain.context import DomainError
from coinfly.execution.evidence import event_tx
from coinfly.execution.intent_store import get_tx
from coinfly.execution.models import IntentRecord


def claim_dispatch_tx(
    connection: sqlite3.Connection, intent_id: str, expected_payload_hash: str
) -> IntentRecord:
    record = get_tx(connection, intent_id)
    if record.intent.payload_hash != expected_payload_hash:
        raise DomainError("DISPATCH_PAYLOAD_MISMATCH")
    cursor = connection.execute(
        "UPDATE ex_outbox SET dispatch='DISPATCH_STARTED',updated_at=? WHERE intent_id=? AND dispatch='NOT_DISPATCHED' "
        "AND submission='PENDING_SUBMISSION' AND resolution='NONE' AND terminal=0",
        (utc_now(), intent_id),
    )
    if cursor.rowcount != 1:
        raise DomainError("DISPATCH_ALREADY_CLAIMED_OR_RESOLVED")
    event_tx(
        connection,
        record.intent.context,
        record.intent.scope_id,
        "DISPATCH_STARTED",
        intent_id,
        expected_payload_hash,
    )
    return get_tx(connection, intent_id)
