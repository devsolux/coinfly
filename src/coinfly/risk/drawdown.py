"""Durable cashflow-adjusted unit NAV and realized daily results by realm."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from pydantic import Field

from coinfly.domain.context import ContextKey, DomainError, Environment, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.storage.writer import Writer


class DrawdownState(FrozenModel):
    authority_id: str
    environment: Environment
    book_id: str
    currency: str
    units: ExactDecimal
    unit_nav: ExactDecimal
    high_water_nav: ExactDecimal
    drawdown_fraction: ExactDecimal
    daily_net_result: ExactDecimal
    day: str
    version: int = Field(ge=1)


class DrawdownStore:
    """No context epoch in the key: handover cannot erase financial memory.

    Record book state and, when verified aggregation exists, a separate
    ``book_id='authority'`` state for the same reporting currency. These are
    independent evidence series; an observed wealth display is not such evidence.
    """

    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self.writer.call(self._schema)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cf_drawdown (authority_id TEXT NOT NULL,environment TEXT NOT NULL,book_id TEXT NOT NULL,currency TEXT NOT NULL,"
            "units TEXT NOT NULL,unit_nav TEXT NOT NULL,high_water_nav TEXT NOT NULL,day TEXT NOT NULL,daily_net_result TEXT NOT NULL,"
            "version INTEGER NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(authority_id,environment,book_id,currency))",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cf_drawdown_samples (authority_id TEXT NOT NULL,environment TEXT NOT NULL,book_id TEXT NOT NULL,currency TEXT NOT NULL,"
            "evidence_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(authority_id,environment,book_id,currency,evidence_id))",
        )

    @staticmethod
    def _state(row: sqlite3.Row) -> DrawdownState:
        high, nav = Decimal(row["high_water_nav"]), Decimal(row["unit_nav"])
        with localcontext(financial_context()) as ctx:
            ctx.prec = 80
            drawdown = max(Decimal(0), Decimal(1) - nav / high)
        return DrawdownState(
            authority_id=row["authority_id"], environment=Environment(row["environment"]), book_id=row["book_id"], currency=row["currency"],
            units=Decimal(row["units"]), unit_nav=nav, high_water_nav=high,
            drawdown_fraction=drawdown, daily_net_result=Decimal(row["daily_net_result"]), day=row["day"], version=row["version"],
        )

    @classmethod
    def read_tx(cls, connection: sqlite3.Connection, context: ContextKey, book_id: str, currency: str) -> DrawdownState | None:
        row = connection.execute("SELECT * FROM cf_drawdown WHERE authority_id=? AND environment=? AND book_id=? AND currency=?",
                                 (context.authority_id, context.environment.value, book_id, currency)).fetchone()
        return cls._state(row) if row is not None else None

    def read(self, context: ContextKey, book_id: str, currency: str = "USD") -> DrawdownState | None:
        return self.writer.call(lambda connection: self.read_tx(connection, context, book_id, currency))

    def record(
        self, context: ContextKey, book_id: str, *, equity: Decimal, evidence_id: str,
        observed_at: datetime, currency: str = "USD", external_flow: Decimal = Decimal(0), realized_pnl: Decimal = Decimal(0),
    ) -> DrawdownState:
        return self.writer.call(lambda connection: self.record_tx(
            connection, context, book_id, equity=equity, evidence_id=evidence_id, observed_at=observed_at,
            currency=currency, external_flow=external_flow, realized_pnl=realized_pnl,
        ))

    def record_tx(
        self, connection: sqlite3.Connection, context: ContextKey, book_id: str, *, equity: Decimal, evidence_id: str,
        observed_at: datetime, currency: str = "USD", external_flow: Decimal = Decimal(0), realized_pnl: Decimal = Decimal(0),
    ) -> DrawdownState:
        """Compose with fill accounting in the caller's single writer transaction.

        No commit, publication, nested writer call or network work occurs here.
        A failing sibling ledger operation rolls back both this state and its
        evidence receipt, so reprocessing cannot drop or double-book a loss.
        """
        if not connection.in_transaction:
            raise DomainError("RISK_TRANSACTION_REQUIRED")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise DomainError("RISK_TIMESTAMP_REQUIRED")
        for value in (equity, external_flow, realized_pnl):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise DomainError("EXACT_FINITE_DECIMAL_REQUIRED")
        observed_at = observed_at.astimezone(timezone.utc)
        digest = hashlib.sha256(repr((str(equity), str(external_flow), str(realized_pnl), observed_at.isoformat())).encode()).hexdigest()
        key = (context.authority_id, context.environment.value, book_id, currency)
        receipt = connection.execute("SELECT digest FROM cf_drawdown_samples WHERE authority_id=? AND environment=? AND book_id=? AND currency=? AND evidence_id=?", (*key, evidence_id)).fetchone()
        if receipt is not None:
            if receipt[0] != digest:
                raise DomainError("RISK_EVIDENCE_CONFLICT")
            state = self.read_tx(connection, context, book_id, currency)
            assert state is not None
            return state
        row = connection.execute("SELECT * FROM cf_drawdown WHERE authority_id=? AND environment=? AND book_id=? AND currency=?", key).fetchone()
        day = observed_at.date().isoformat()
        with localcontext(financial_context()) as decimal_context:
            decimal_context.prec = 80
            if row is None:
                if equity <= 0:
                    raise DomainError("POSITIVE_INITIAL_EQUITY_REQUIRED")
                units, nav, high, daily, version = equity, Decimal(1), Decimal(1), realized_pnl, 1
            else:
                if observed_at <= datetime.fromisoformat(row["observed_at"]):
                    raise DomainError("RISK_EVIDENCE_OUT_OF_ORDER")
                old_nav = Decimal(row["unit_nav"])
                if old_nav <= 0 and external_flow != 0:
                    raise DomainError("INSOLVENT_NAV_REQUIRES_RECOVERY")
                units = Decimal(row["units"]) + (external_flow / old_nav if external_flow else Decimal(0))
                if units <= 0:
                    raise DomainError("RISK_UNIT_REDEMPTION_UNPROVEN")
                nav = equity / units
                high = max(Decimal(row["high_water_nav"]), nav)
                daily = (Decimal(row["daily_net_result"]) if row["day"] == day else Decimal(0)) + realized_pnl
                version = row["version"] + 1
        connection.execute(
            "INSERT INTO cf_drawdown VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(authority_id,environment,book_id,currency) DO UPDATE SET "
            "units=excluded.units,unit_nav=excluded.unit_nav,high_water_nav=excluded.high_water_nav,day=excluded.day,daily_net_result=excluded.daily_net_result,version=excluded.version,observed_at=excluded.observed_at",
            (*key, str(units), str(nav), str(high), day, str(daily), version, observed_at.isoformat()),
        )
        connection.execute("INSERT INTO cf_drawdown_samples VALUES(?,?,?,?,?,?)", (*key, evidence_id, digest))
        state = self.read_tx(connection, context, book_id, currency)
        assert state is not None
        return state
