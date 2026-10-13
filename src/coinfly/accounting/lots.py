"""Exact FIFO lot matching with immutable movement history and rebuildable totals."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext

from coinfly.domain.decimal_math import financial_context
from coinfly.execution.models import Fill, Side


@dataclass(frozen=True)
class LotChange:
    quantity_before: Decimal
    quantity_after: Decimal
    average_before: Decimal
    average_after: Decimal
    realized_pnl: Decimal


def apply_lots_tx(connection: sqlite3.Connection, fill: Fill, sequence: int) -> LotChange:
    key = (
        fill.context.authority_id,
        fill.context.environment.value,
        fill.scope_id,
        fill.context.identity,
        fill.instrument,
    )
    row = connection.execute(
        "SELECT quantity,average_price,realized_pnl,fees_quote FROM ex_positions WHERE authority_id=? AND environment=? AND scope_id=? AND context_id=? AND instrument=?",
        key,
    ).fetchone()
    before = Decimal(row["quantity"]) if row is not None else Decimal(0)
    avg_before = Decimal(row["average_price"]) if row is not None else Decimal(0)
    realized_before = Decimal(row["realized_pnl"]) if row is not None else Decimal(0)
    fees_before = Decimal(row["fees_quote"]) if row is not None else Decimal(0)
    with localcontext(financial_context()) as decimal_context:
        decimal_context.prec = 256
        signed = fill.quantity if fill.side == Side.BUY else -fill.quantity
        remaining = signed
        realized = Decimal(0)
        lots = connection.execute(
            "SELECT * FROM ex_open_lots WHERE context_id=? AND scope_id=? AND instrument=? ORDER BY sequence,lot_id",
            (fill.context.identity, fill.scope_id, fill.instrument),
        ).fetchall()
        for lot in lots:
            old_quantity = Decimal(lot["quantity_remaining"])
            if not remaining or not old_quantity or (old_quantity > 0) == (remaining > 0):
                continue
            matched = min(abs(old_quantity), abs(remaining))
            direction = Decimal(1) if old_quantity > 0 else Decimal(-1)
            realized += (
                matched * (fill.price - Decimal(lot["price"])) * direction * Decimal(lot["multiplier"])
            )
            connection.execute(
                "UPDATE ex_open_lots SET quantity_remaining=? WHERE lot_id=?",
                (str(old_quantity - matched * direction), lot["lot_id"]),
            )
            remaining += matched * direction
        if remaining:
            connection.execute(
                "INSERT INTO ex_open_lots VALUES(?,?,?,?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.context.identity,
                    fill.scope_id,
                    fill.instrument,
                    str(remaining),
                    str(fill.price),
                    str(fill.contract_multiplier),
                    sequence,
                ),
            )
        after = before + signed
        cost = Decimal(0)
        for lot in connection.execute(
            "SELECT quantity_remaining,price FROM ex_open_lots WHERE context_id=? AND scope_id=? AND instrument=?",
            (fill.context.identity, fill.scope_id, fill.instrument),
        ):
            cost += abs(Decimal(lot[0])) * Decimal(lot[1])
        average_after = cost / abs(after) if after else Decimal(0)
        fees = fees_before + (fill.fee if fill.fee_asset == fill.quote_asset else Decimal(0))
        connection.execute(
            "INSERT INTO ex_positions VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(authority_id,environment,scope_id,context_id,instrument) "
            "DO UPDATE SET quantity=excluded.quantity,average_price=excluded.average_price,realized_pnl=excluded.realized_pnl,fees_quote=excluded.fees_quote",
            (
                *key,
                fill.base_asset,
                fill.quote_asset,
                str(after),
                str(average_after),
                str(realized_before + realized),
                str(fees),
            ),
        )
    return LotChange(before, after, avg_before, average_after, realized)
