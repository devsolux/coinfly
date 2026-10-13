"""Durable alarm/fence primitives shared with lifecycle admission."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from coinfly.accounting.journal import utc_now
from coinfly.domain.context import ContextKey


def event_tx(
    connection: sqlite3.Connection,
    context: ContextKey,
    scope_id: str,
    kind: str,
    entity_id: str,
    evidence_id: str,
) -> int:
    cursor = connection.execute(
        "INSERT INTO ex_events(authority_id,environment,context_id,scope_id,event_kind,entity_id,evidence_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            context.authority_id,
            context.environment.value,
            context.identity,
            scope_id,
            kind,
            entity_id,
            evidence_id,
            utc_now(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def alarm_tx(
    connection: sqlite3.Connection, context: ContextKey, entity_id: str, reason: str, evidence_id: str
) -> str:
    row = connection.execute(
        "SELECT alarm_id FROM ex_alarms WHERE authority_id=? AND entity_id=? AND reason=? AND evidence_id=?",
        (context.authority_id, entity_id, reason, evidence_id),
    ).fetchone()
    if row is not None:
        return str(row[0])
    alarm_id = str(uuid4())
    connection.execute(
        "INSERT INTO ex_alarms VALUES(?,?,?,?,?,?,?,?)",
        (alarm_id, context.authority_id, context.identity, entity_id, reason, evidence_id, 0, utc_now()),
    )
    return alarm_id


def fence_authority_tx(
    connection: sqlite3.Connection, context: ContextKey, reason: str, evidence_id: str
) -> None:
    connection.execute(
        "INSERT INTO ex_authority_fences VALUES(?,?,?,?,?,?) ON CONFLICT(authority_id,evidence_id) DO UPDATE SET active=1",
        (context.authority_id, evidence_id, reason, context.identity, 1, utc_now()),
    )
    alarm_tx(connection, context, context.identity, reason, evidence_id)


def authority_fenced_tx(connection: sqlite3.Connection, authority_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM ex_authority_fences WHERE authority_id=? AND active=1 LIMIT 1", (authority_id,)
        ).fetchone()
        is not None
    )


def slot_fenced_tx(connection: sqlite3.Connection, context: ContextKey, slot_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM ex_slot_fences f JOIN ex_intents i USING(intent_id) WHERE i.context_id=? AND i.slot_id=? AND f.active=1 LIMIT 1",
            (context.identity, slot_id),
        ).fetchone()
        is not None
    )


def acknowledge_alarm_tx(connection: sqlite3.Connection, alarm_id: str) -> None:
    # Deliberately no fence mutation: acknowledgment is presentation state only.
    connection.execute("UPDATE ex_alarms SET acknowledged=1 WHERE alarm_id=?", (alarm_id,))
