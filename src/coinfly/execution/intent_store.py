"""Phase A: immutable, semantically deduplicated intent plus reservation."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from coinfly.accounting.journal import utc_now
from coinfly.accounting.reservations import reserve_tx
from coinfly.domain.context import DomainError
from coinfly.execution.evidence import event_tx
from coinfly.execution.models import (
    DispatchState,
    IntentRecord,
    OrderIntent,
    Reservation,
    Resolution,
    Submission,
)
from coinfly.storage.writer import Writer


def get_tx(connection: sqlite3.Connection, intent_id: str) -> IntentRecord:
    row = connection.execute(
        "SELECT i.payload_json,o.*,r.remaining_amount FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
        "JOIN ex_reservations r USING(intent_id) WHERE i.intent_id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise DomainError("INTENT_NOT_FOUND")
    return IntentRecord(
        intent=OrderIntent.model_validate_json(row["payload_json"]),
        submission=Submission(row["submission"]),
        dispatch=DispatchState(row["dispatch"]),
        resolution=Resolution(row["resolution"]),
        venue_order_id=row["venue_order_id"],
        filled_quantity=Decimal(row["filled_quantity"]),
        terminal=bool(row["terminal"]),
        reservation_remaining=Decimal(row["remaining_amount"]),
    )


def prepare_tx(connection: sqlite3.Connection, intent: OrderIntent, reservation: Reservation) -> IntentRecord:
    row = connection.execute(
        "SELECT intent_id,payload_json FROM ex_intents WHERE semantic_key=?", (intent.semantic_key,)
    ).fetchone()
    if row is not None:
        original = OrderIntent.model_validate_json(row["payload_json"])
        # Duplicate requests can have a freshly generated UUID; economics may not change.
        if original.model_dump(exclude={"intent_id"}) != intent.model_dump(exclude={"intent_id"}):
            raise DomainError("SEMANTIC_INTENT_CONFLICT")
        existing = connection.execute(
            "SELECT asset,original_amount FROM ex_reservations WHERE intent_id=?", (original.intent_id,)
        ).fetchone()
        if (
            existing["asset"] != reservation.asset
            or Decimal(existing["original_amount"]) != reservation.amount
        ):
            raise DomainError("RESERVATION_CONFLICT")
        return get_tx(connection, original.intent_id)
    if connection.execute(
        "SELECT 1 FROM ex_context_retirements WHERE context_id=?", (intent.context.identity,)
    ).fetchone():
        raise DomainError("CONTEXT_RETIRED")
    connection.execute(
        "INSERT INTO ex_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent.intent_id,
            intent.semantic_key,
            intent.context.authority_id,
            intent.context.environment.value,
            intent.context.product_track.value,
            intent.context.broker_profile_id,
            intent.context.identity,
            intent.context.epoch,
            intent.scope_id,
            intent.profile_version,
            intent.signer_version,
            intent.slot_id,
            intent.model_dump_json(),
            intent.payload_hash,
            utc_now(),
        ),
    )
    connection.execute(
        "INSERT INTO ex_outbox VALUES(?,?,?,?,?,?,?,?)",
        (
            intent.intent_id,
            Submission.PENDING_SUBMISSION.value,
            DispatchState.NOT_DISPATCHED.value,
            Resolution.NONE.value,
            None,
            "0",
            0,
            utc_now(),
        ),
    )
    reserve_tx(connection, intent, reservation)
    event_tx(
        connection, intent.context, intent.scope_id, "INTENT_PREPARED", intent.intent_id, intent.payload_hash
    )
    return get_tx(connection, intent.intent_id)


class IntentStore:
    def __init__(self, writer: Writer) -> None:
        self.writer = writer

    def prepare(self, intent: OrderIntent, reservation: Reservation) -> IntentRecord:
        """Worker internal primitive; admission must compose prepare_tx under risk CAS."""
        return self.writer.call(lambda connection: prepare_tx(connection, intent, reservation))

    def get(self, intent_id: str) -> IntentRecord:
        return self.writer.call(lambda connection: get_tx(connection, intent_id))
