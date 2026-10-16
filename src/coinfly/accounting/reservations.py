"""Reservations stay durable until conclusive non-creation/terminal evidence."""

from __future__ import annotations

import sqlite3
from decimal import Decimal, localcontext

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.models import OrderIntent, Reservation


def reserve_tx(connection: sqlite3.Connection, intent: OrderIntent, reservation: Reservation) -> None:
    connection.execute(
        "INSERT INTO ex_reservations VALUES(?,?,?,?,?,?,?,?)",
        (
            intent.intent_id,
            intent.context.authority_id,
            intent.context.environment.value,
            intent.scope_id,
            intent.context.identity,
            reservation.asset,
            str(reservation.amount),
            str(reservation.amount),
        ),
    )


def consume_for_fill_tx(
    connection: sqlite3.Connection, intent: OrderIntent, total_filled: Decimal
) -> Decimal:
    row = connection.execute(
        "SELECT original_amount,remaining_amount FROM ex_reservations WHERE intent_id=?", (intent.intent_id,)
    ).fetchone()
    if row is None:
        raise DomainError("RESERVATION_MISSING")
    with localcontext(financial_context()) as decimal_context:
        decimal_context.prec = 256
        # Round upward to retain headroom for any executable remainder.
        decimal_context.rounding = "ROUND_CEILING"
        unfilled = max(Decimal(0), intent.quantity - total_filled)
        # Multiply first: e.g. 0.3 * (1 / 3) otherwise persists rounding dust
        # even though the true reservation, (0.3 * 1) / 3, is exactly 0.1.
        remaining = Decimal(row["original_amount"]) * unfilled / intent.quantity
    connection.execute(
        "UPDATE ex_reservations SET remaining_amount=? WHERE intent_id=?", (str(remaining), intent.intent_id)
    )
    return remaining


def release_tx(connection: sqlite3.Connection, intent_id: str) -> None:
    connection.execute("UPDATE ex_reservations SET remaining_amount='0' WHERE intent_id=?", (intent_id,))
