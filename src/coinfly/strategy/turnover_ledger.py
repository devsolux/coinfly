"""Exact execution-turnover guard for the worker: strict < fraction of interval-start equity.

This mirrors the research contract in ``coinfly.research.turnover``
(``turnover_admissible``: ``spent_and_reserved + proposed < capital × LIMIT``)
with every denominator and window made explicit:

* **Accounting window** — ``[start, start + accounting_interval_seconds)``,
  aligned to the Unix epoch, shared by every slot of one strategy in one
  context (the strategy trades one book).
* **Capital** — the book's funded equity observed at the first admission
  inside the window (the interval-start equity), fixed for the window.
* **Spent** — gross executed notional (both sides, every fill, terminal
  liquidations included) of fills recorded inside the window.
* **Reserved** — remaining notional of every non-terminal intent of the
  context at a conservative price ceiling (BUY: limit; SELL: max(limit, mark)).
* **Proposed** — quantity × price × multiplier of the order being admitted.

Admission requires ``spent + reserved + proposed < capital × fraction`` with
exact ``Decimal`` arithmetic. The equality case is refused: equal-denominator
5 % is not strictly below 5 %. The feasible quantity for an allowance is the
largest multiple of the instrument's quantity increment whose notional is
strictly below the remaining allowance; when that is below the instrument's
minimum quantity or notional the order is reported infeasible
(``TURNOVER_ALLOWANCE_INFEASIBLE``) — nothing is rounded up.

Policy (declared per strategy, ``ExecutionSettings.turnover_policy``):
``REJECT`` refuses an order that does not fit (the research default: a guard
never shrinks an order); ``CONSTRAIN`` reduces the quantity to the feasible
maximum and records it. Neither policy ever selects a different trade.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any, Literal

from coinfly.domain.context import ContextKey, DomainError
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.models import OrderIntent, Side
from coinfly.research.turnover import TURNOVER_LIMIT, turnover_admissible
from coinfly.storage.component_schema import install_component_schema
from coinfly.storage.writer import Writer

TURNOVER_SCHEMA_COMPONENT = "turnover"
TURNOVER_SCHEMA: tuple[str, ...] = (
    'CREATE TABLE IF NOT EXISTS cf_turnover_windows (context_id TEXT NOT NULL, strategy_identity TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, capital TEXT NOT NULL, fraction TEXT NOT NULL, opened_at TEXT NOT NULL, PRIMARY KEY(context_id, strategy_identity, window_start))',
)

TurnoverPolicy = Literal["REJECT", "CONSTRAIN"]


def window_bounds(now: datetime, interval_seconds: int) -> tuple[datetime, datetime]:
    if interval_seconds < 1:
        raise DomainError("ACCOUNTING_INTERVAL_INVALID")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((now.astimezone(timezone.utc) - epoch).total_seconds())
    start = epoch + timedelta(seconds=elapsed - elapsed % interval_seconds)
    return start, start + timedelta(seconds=interval_seconds)


def feasible_quantity(allowance: Decimal, *, price: Decimal, multiplier: Decimal, quantity_increment: Decimal,
                      minimum_quantity: Decimal, minimum_notional: Decimal) -> Decimal | None:
    """Largest increment multiple with notional strictly below the allowance, or None."""
    with localcontext(financial_context(512)):
        if allowance <= 0 or price <= 0 or multiplier <= 0 or quantity_increment <= 0:
            return None
        unit = price * multiplier
        quantity = (allowance / unit / quantity_increment).to_integral_value(rounding=ROUND_DOWN) * quantity_increment
        if quantity * unit >= allowance:
            quantity -= quantity_increment
        if quantity <= 0 or quantity < minimum_quantity or quantity * unit < minimum_notional:
            return None
        return quantity


@dataclass(frozen=True)
class TurnoverDecision:
    admitted: bool
    code: str
    capital: Decimal
    spent: Decimal
    reserved: Decimal
    proposed: Decimal
    allowance: Decimal
    window_start: datetime
    window_end: datetime
    constrained_quantity: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"admitted": self.admitted, "code": self.code, "capital": str(self.capital), "spent": str(self.spent),
                "reserved": str(self.reserved), "proposed": str(self.proposed), "allowance": str(self.allowance),
                "window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
                "constrained_quantity": str(self.constrained_quantity) if self.constrained_quantity is not None else None}


class TurnoverLedger:
    """Persisted accounting windows; spent/reserved are read from the execution ledger."""

    def __init__(self, writer: Writer, *, fraction: Decimal = TURNOVER_LIMIT, interval_seconds: int = 60) -> None:
        if not isinstance(fraction, Decimal) or not fraction.is_finite() or not 0 < fraction <= TURNOVER_LIMIT:
            raise DomainError("TURNOVER_FRACTION_INVALID")
        self.writer = writer
        self.fraction = fraction
        self.interval_seconds = interval_seconds
        self.writer.call(lambda connection: install_component_schema(connection, TURNOVER_SCHEMA_COMPONENT, TURNOVER_SCHEMA))

    def window_capital(self, context: ContextKey, strategy_identity: str, *, now: datetime, funded_equity: Decimal) -> tuple[datetime, datetime, Decimal]:
        """The interval-start equity for the current window (recorded once per window)."""
        start, end = window_bounds(now, self.interval_seconds)
        if not isinstance(funded_equity, Decimal) or not funded_equity.is_finite() or funded_equity <= 0:
            raise DomainError("TURNOVER_CAPITAL_INVALID")

        def read_or_open(connection: sqlite3.Connection) -> Decimal:
            row = connection.execute(
                "SELECT capital FROM cf_turnover_windows WHERE context_id=? AND strategy_identity=? AND window_start=?",
                (context.identity, strategy_identity, start.isoformat())).fetchone()
            if row is not None:
                return Decimal(row[0])
            connection.execute("INSERT INTO cf_turnover_windows VALUES(?,?,?,?,?,?,?)",
                               (context.identity, strategy_identity, start.isoformat(), end.isoformat(), str(funded_equity),
                                str(self.fraction), now.isoformat()))
            return funded_equity
        return start, end, self.writer.call(read_or_open)

    def spent_and_reserved(self, context: ContextKey, *, start: datetime, end: datetime, marks: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
        """Gross filled notional inside the window plus remaining notional of open intents at ceilings."""
        def read(connection: sqlite3.Connection) -> tuple[Decimal, Decimal]:
            with localcontext(financial_context(512)):
                spent = Decimal(0)
                for row in connection.execute(
                        "SELECT payload_json FROM ex_fills WHERE context_id=? AND created_at>=? AND created_at<?",
                        (context.identity, start.isoformat(), end.isoformat())):
                    from coinfly.execution.models import Fill

                    fill = Fill.model_validate_json(row[0])
                    spent += fill.quantity * fill.price * fill.contract_multiplier
                reserved = Decimal(0)
                for row in connection.execute(
                        "SELECT i.payload_json, o.filled_quantity FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
                        "WHERE i.context_id=? AND o.terminal=0", (context.identity,)):
                    intent = OrderIntent.model_validate_json(row[0])
                    remaining = max(Decimal(0), intent.quantity - Decimal(row[1]))
                    ceiling = intent.limit_price
                    if intent.side is Side.SELL:
                        ceiling = max(ceiling, marks.get(intent.instrument, ceiling))
                    reserved += remaining * ceiling * intent.contract_multiplier
                return spent, reserved
        return self.writer.call(read)

    def evaluate(self, context: ContextKey, strategy_identity: str, *, now: datetime, funded_equity: Decimal,
                 marks: dict[str, Decimal], quantity: Decimal, price: Decimal, multiplier: Decimal,
                 quantity_increment: Decimal, minimum_quantity: Decimal, minimum_notional: Decimal,
                 policy: TurnoverPolicy) -> TurnoverDecision:
        start, end, capital = self.window_capital(context, strategy_identity, now=now, funded_equity=funded_equity)
        spent, reserved = self.spent_and_reserved(context, start=start, end=end, marks=marks)
        with localcontext(financial_context(512)):
            proposed = quantity * price * multiplier
            allowance = capital * self.fraction - spent - reserved
        if turnover_admissible(capital, spent + reserved, proposed):
            return TurnoverDecision(True, "TURNOVER_ADMITTED", capital, spent, reserved, proposed, allowance, start, end)
        if policy == "REJECT":
            return TurnoverDecision(False, "TURNOVER_LIMIT_EXCEEDED", capital, spent, reserved, proposed, allowance, start, end)
        constrained = feasible_quantity(allowance, price=price, multiplier=multiplier, quantity_increment=quantity_increment,
                                        minimum_quantity=minimum_quantity, minimum_notional=minimum_notional)
        if constrained is None:
            return TurnoverDecision(False, "TURNOVER_ALLOWANCE_INFEASIBLE", capital, spent, reserved, proposed, allowance, start, end)
        return TurnoverDecision(True, "TURNOVER_CONSTRAINED", capital, spent, reserved, proposed, allowance, start, end,
                                constrained_quantity=constrained)
