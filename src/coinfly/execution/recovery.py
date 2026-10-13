"""Crash recovery reads durable ambiguity and never replays creates."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from coinfly.accounting.journal import utc_now
from coinfly.domain.context import ContextKey, DomainError
from coinfly.execution.evidence import authority_fenced_tx


def context_fact_sequence_tx(connection: sqlite3.Connection, context: ContextKey) -> int:
    row = connection.execute(
        "SELECT sequence FROM ex_fact_state WHERE context_id=?", (context.identity,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def slot_fact_sequence_tx(connection: sqlite3.Connection, context: ContextKey, slot_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(e.sequence),0) FROM ex_events e JOIN ex_intents i ON e.entity_id=i.intent_id "
        "WHERE i.context_id=? AND i.slot_id=? AND e.event_kind IN ('FILL_APPLIED','TERMINAL_PROVEN')",
        (context.identity, slot_id),
    ).fetchone()
    return int(row[0])


def assert_slot_closed_tx(connection: sqlite3.Connection, context: ContextKey, slot_id: str) -> None:
    _assert_orders_closed(connection, context, slot_id)
    instruments = connection.execute(
        "SELECT DISTINCT json_extract(payload_json,'$.instrument') FROM ex_intents WHERE context_id=? AND slot_id=?",
        (context.identity, slot_id),
    ).fetchall()
    for instrument in instruments:
        rows = connection.execute(
            "SELECT quantity FROM ex_positions WHERE context_id=? AND instrument=?",
            (context.identity, instrument[0]),
        ).fetchall()
        if any(Decimal(row[0]) != 0 for row in rows):
            raise DomainError("STOP_PROOF_POSITION_NONZERO")


def _assert_orders_closed(
    connection: sqlite3.Connection, context: ContextKey, slot_id: str | None = None
) -> None:
    extra = " AND i.slot_id=?" if slot_id is not None else ""
    parameters = (context.identity, slot_id) if slot_id is not None else (context.identity,)
    rows = connection.execute(
        "SELECT o.terminal,o.resolution,r.remaining_amount FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
        "JOIN ex_reservations r USING(intent_id) WHERE i.context_id=?" + extra,
        parameters,
    ).fetchall()
    if any(
        not row["terminal"] or row["resolution"] == "UNKNOWN" or Decimal(row["remaining_amount"]) != 0
        for row in rows
    ):
        raise DomainError("STOP_PROOF_OUTSTANDING_INTENT")


def assert_context_closed_tx(connection: sqlite3.Connection, context: ContextKey) -> None:
    if authority_fenced_tx(connection, context.authority_id):
        raise DomainError("STOP_PROOF_AUTHORITY_FENCED")
    _assert_orders_closed(connection, context)
    rows = connection.execute(
        "SELECT quantity FROM ex_positions WHERE context_id=?", (context.identity,)
    ).fetchall()
    if any(Decimal(row[0]) != 0 for row in rows):
        raise DomainError("STOP_PROOF_POSITION_NONZERO")
    if connection.execute(
        "SELECT 1 FROM ex_fills WHERE authority_id=? AND environment=? AND intent_id IS NULL LIMIT 1",
        (context.authority_id, context.environment.value),
    ).fetchone():
        raise DomainError("STOP_PROOF_UNATTRIBUTED_FILL")


def record_retirement_tx(
    connection: sqlite3.Connection, context: ContextKey, proof_id: str, expected_fact_seq: int
) -> None:
    assert_context_closed_tx(connection, context)
    if context_fact_sequence_tx(connection, context) != expected_fact_seq:
        raise DomainError("STOP_PROOF_FACTS_CHANGED")
    existing = connection.execute(
        "SELECT proof_id,valid FROM ex_context_retirements WHERE context_id=?", (context.identity,)
    ).fetchone()
    if existing is not None:
        if existing["proof_id"] != proof_id or not existing["valid"]:
            raise DomainError("RETIREMENT_CONFLICT")
        return
    connection.execute(
        "INSERT INTO ex_context_retirements VALUES(?,?,?,?,?,?,?)",
        (
            context.identity,
            context.authority_id,
            context.model_dump_json(),
            proof_id,
            expected_fact_seq,
            1,
            utc_now(),
        ),
    )


def ambiguous_intents_tx(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT intent_id FROM ex_outbox WHERE submission='PENDING_SUBMISSION' AND dispatch='DISPATCH_STARTED' ORDER BY intent_id"
        )
    )
