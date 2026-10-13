"""Exact strategy capital derived from owned execution facts, not account wealth.

This is an allocation view over the ONE execution ledger, not a second exchange
balance. A plan's initial allocation is a ceiling: gains do not automatically
enlarge it; losses reduce deployable capital. Intent admission and final dispatch
both recheck owned positions plus worst-case pending entries in their existing
writer transaction. Unfilled exits never release an entry allowance.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, cast

from coinfly.domain.context import ContextKey, DomainError, ProductTrack
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.models import Fill, IntentKind, OrderIntent, Side

if TYPE_CHECKING:
    from coinfly.risk.admission_actor import ActiveBookSnapshot


@dataclass(frozen=True)
class StrategyCapitalSnapshot:
    strategy_identity: str
    approved: Decimal
    net_asset_value: Decimal
    cash: Decimal
    quantities: tuple[tuple[str, Decimal], ...]
    gross: Decimal
    pending_entries: Decimal
    pending_cash: Decimal
    slot_gross: tuple[tuple[str, Decimal], ...]

    @property
    def sizing_equity(self) -> Decimal:
        return min(self.approved, self.net_asset_value)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def strategy_for_slot_tx(connection: sqlite3.Connection, context: ContextKey, slot_id: str) -> sqlite3.Row | None:
    if not _table_exists(connection, "cf_strategy_slots"):
        return None  # A manually managed slot still passes ordinary account risk.
    row = connection.execute(
        "SELECT * FROM cf_strategy_slots WHERE slot_id=? AND context_id=?", (slot_id, context.identity)
    ).fetchone()
    return cast("sqlite3.Row | None", row)  # the writer's connections use sqlite3.Row


def snapshot_tx(connection: sqlite3.Connection, book: ActiveBookSnapshot, strategy_identity: str,
                *, exclude_intent_id: str | None = None) -> StrategyCapitalSnapshot:
    budget = connection.execute(
        "SELECT approved_capital FROM cf_strategy_budgets WHERE context_id=? AND strategy_identity=?",
        (book.context.identity, strategy_identity),
    ).fetchone()
    if budget is None:
        raise DomainError("STRATEGY_CAPITAL_NOT_APPROVED")
    marks = {mark.instrument: mark for mark in book.marks}
    with localcontext(financial_context(256)):
        approved = Decimal(budget[0])
        if not approved.is_finite() or approved <= 0:
            raise DomainError("INVALID_CAPITAL_BUDGET")
        quantities: dict[str, Decimal] = {}
        cash = approved
        net_cost = Decimal(0)
        fees = Decimal(0)
        # Include retained history across context reactivation. A retry/restart
        # does not give the same strategy a new loss-free principal.
        rows = connection.execute(
            "SELECT f.payload_json FROM ex_fills f JOIN ex_intents i ON i.intent_id=f.intent_id "
            "JOIN cf_strategy_slots s ON s.slot_id=i.slot_id "
            "WHERE i.authority_id=? AND i.environment=? AND i.scope_id=? AND s.strategy_identity=? "
            "ORDER BY f.created_at,f.fill_id",
            (book.context.authority_id, book.context.environment.value, book.scope_id, strategy_identity),
        )
        multipliers: dict[str, Decimal] = {}
        for row in rows:
            fill = Fill.model_validate_json(row[0])
            if fill.quote_asset != book.reporting_currency:
                raise DomainError("STRATEGY_COST_CONVERSION_UNVERIFIED")
            signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
            cost = signed * fill.price * fill.contract_multiplier
            quantities[fill.instrument] = quantities.get(fill.instrument, Decimal(0)) + signed
            previous = multipliers.setdefault(fill.instrument, fill.contract_multiplier)
            if previous != fill.contract_multiplier:
                raise DomainError("STRATEGY_CONTRACT_CHANGED")
            net_cost += cost
            if fill.fee:
                if fill.fee_asset != book.reporting_currency:
                    raise DomainError("STRATEGY_FEE_CONVERSION_UNVERIFIED")
                fees += fill.fee
        marked = Decimal(0)
        gross_by_instrument: dict[str, Decimal] = {}
        for instrument, quantity in quantities.items():
            if not quantity:
                continue
            mark = marks.get(instrument)
            if mark is None or mark.contract_multiplier != multipliers[instrument]:
                raise DomainError("STRATEGY_POSITION_MARK_UNVERIFIED")
            value = quantity * mark.price * mark.contract_multiplier
            marked += value
            gross_by_instrument[instrument] = abs(value)
        nav = approved + marked - net_cost - fees
        cash = approved - net_cost - fees if book.context.product_track is ProductTrack.SPOT else nav
        pending_entries = pending_cash = Decimal(0)
        for row in connection.execute(
            "SELECT i.payload_json,o.filled_quantity,r.remaining_amount,r.asset FROM ex_intents i "
            "JOIN ex_outbox o USING(intent_id) JOIN cf_strategy_slots s ON s.slot_id=i.slot_id "
            "LEFT JOIN ex_reservations r ON r.intent_id=i.intent_id "
            "WHERE i.authority_id=? AND i.environment=? AND i.scope_id=? AND s.strategy_identity=? AND o.terminal=0",
            (book.context.authority_id, book.context.environment.value, book.scope_id, strategy_identity),
        ):
            pending = OrderIntent.model_validate_json(row[0])
            if pending.intent_id == exclude_intent_id:
                continue
            remaining = max(Decimal(0), pending.quantity - Decimal(row[1]))
            if pending.kind is IntentKind.ENTRY:
                mark = marks.get(pending.instrument)
                if mark is None:
                    raise DomainError("STRATEGY_PENDING_MARK_UNVERIFIED")
                risk_price = max(mark.price, pending.limit_price)
                notional = remaining * risk_price * pending.contract_multiplier
                pending_entries += notional
                gross_by_instrument[pending.instrument] = gross_by_instrument.get(pending.instrument, Decimal(0)) + notional
            if row[2] is not None and row[3] == book.reporting_currency:
                pending_cash += Decimal(row[2])
        return StrategyCapitalSnapshot(strategy_identity, approved, nav, cash, tuple(quantities.items()),
                                       sum((abs(q) * marks[i].price * marks[i].contract_multiplier
                                            for i, q in quantities.items() if q), Decimal(0)),
                                       pending_entries, pending_cash, tuple(gross_by_instrument.items()))


def planning_book_tx(connection: sqlite3.Connection, book: ActiveBookSnapshot, strategy_identity: str | None
                     ) -> tuple[ActiveBookSnapshot, dict[str, Decimal] | None]:
    if strategy_identity is None:
        return book, None
    allocation = snapshot_tx(connection, book, strategy_identity)
    equity = min(book.funded_equity, allocation.sizing_equity)
    if equity <= 0:
        raise DomainError("STRATEGY_CAPITAL_EXHAUSTED")
    # Only the planner/turnover calculation sees this view. The admission actor's
    # authoritative account evidence is never replaced with an allocated NAV.
    return book.model_copy(update={"funded_equity": equity}), dict(allocation.quantities)


def require_intent_tx(connection: sqlite3.Connection, intent: OrderIntent, book: ActiveBookSnapshot,
                      *, fee_buffer_fraction: Decimal) -> None:
    owner = strategy_for_slot_tx(connection, intent.context, intent.slot_id)
    if owner is None:
        return
    if owner["instrument"] != intent.instrument:
        raise DomainError("STRATEGY_SLOT_INSTRUMENT_MISMATCH")
    if intent.kind is not IntentKind.ENTRY:
        return  # Independent reduce-only proof still applies to every exit.
    if owner["state"] != "ACTIVE":
        raise DomainError("STRATEGY_SLOT_NOT_ACTIVE")
    allocation = snapshot_tx(connection, book, str(owner["strategy_identity"]), exclude_intent_id=intent.intent_id)
    with localcontext(financial_context(256)):
        notional = intent.quantity * intent.limit_price * intent.contract_multiplier
        mark = next((m for m in book.marks if m.instrument == intent.instrument), None)
        if mark is None:
            raise DomainError("STRATEGY_POSITION_MARK_UNVERIFIED")
        risk_notional = intent.quantity * max(intent.limit_price, mark.price) * intent.contract_multiplier
        ceiling = min(book.funded_equity, allocation.sizing_equity)
        # Conservative unlevered strategy allocation. Derivative margin checks
        # are independent; leverage never expands the approved cash authority.
        if ceiling <= 0 or allocation.gross + allocation.pending_entries + risk_notional > ceiling:
            raise DomainError("STRATEGY_CAPITAL_LIMIT")
        slot_ceiling = min(Decimal(owner["allocated_budget"]), ceiling)
        if dict(allocation.slot_gross).get(intent.instrument, Decimal(0)) + risk_notional > slot_ceiling:
            raise DomainError("STRATEGY_SLOT_CAPITAL_LIMIT")
        if intent.context.product_track is ProductTrack.SPOT and intent.side is Side.BUY:
            needed = notional * (Decimal(1) + fee_buffer_fraction)
            if allocation.cash - allocation.pending_cash < needed:
                raise DomainError("STRATEGY_CASH_LIMIT")
