"""Atomic active-book admission, durable reservations, and expiring send grants."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Callable

from pydantic import Field, field_validator, model_validator

from coinfly.config.models import EngineConfig
from coinfly.domain.context import ContextKey, DomainError, Environment, FrozenModel, Identifier, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.execution.intent_store import get_tx, prepare_tx
from coinfly.execution.models import IntentKind, IntentRecord, OrderIntent, Reservation, Side
from coinfly.execution.recovery import context_fact_sequence_tx
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drawdown import DrawdownStore
from coinfly.risk.margin_models import MarginModel, ReductionMarginModel
from coinfly.storage.writer import Writer


class AvailableAsset(FrozenModel):
    asset: str = Field(pattern=r"^[A-Z0-9]{1,20}$")  # single-character venue codes exist (e.g. S)
    amount: ExactDecimal = Field(ge=0, allow_inf_nan=False)


class ExecutionMark(FrozenModel):
    instrument: Identifier
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False)
    contract_multiplier: ExactDecimal = Field(default=Decimal(1), gt=0, allow_inf_nan=False)


class ActiveBookSnapshot(FrozenModel):
    """Produced by authenticated execution reconciliation, never the UI collector.

    Available assets have already excluded remote order holds. The explicit set
    of local reservations represented in those holds prevents counting one hold
    twice; unrepresented reservations are subtracted by admission itself.
    """

    context: ContextKey
    scope_id: Identifier
    evidence_id: Identifier
    observed_at: datetime
    expires_at: datetime
    fact_sequence: int = Field(ge=0)
    reporting_currency: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    funded_equity: ExactDecimal = Field(allow_inf_nan=False)
    eligible_collateral: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    available_assets: tuple[AvailableAsset, ...]
    marks: tuple[ExecutionMark, ...]
    reservations_in_remote_holds: tuple[Identifier, ...] = ()
    complete_execution_scope: bool
    authoritative_account_evidence: bool
    conflicting_external_activity: bool

    @field_validator("observed_at", "expires_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware execution evidence required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_assets(self) -> ActiveBookSnapshot:
        if len({asset.asset for asset in self.available_assets}) != len(self.available_assets):
            raise ValueError("duplicate available asset")
        if len({mark.instrument for mark in self.marks}) != len(self.marks):
            raise ValueError("duplicate instrument mark")
        if len(set(self.reservations_in_remote_holds)) != len(self.reservations_in_remote_holds):
            raise ValueError("duplicate hold provenance")
        return self

    def require_current(self, now: datetime) -> None:
        if not self.observed_at <= now < self.expires_at or (now - self.observed_at).total_seconds() > 30:
            raise DomainError("EXECUTION_BOOK_STALE")
        if not self.complete_execution_scope or not self.authoritative_account_evidence:
            raise DomainError("EXECUTION_BOOK_UNVERIFIED")
        if self.conflicting_external_activity:
            raise DomainError("EXTERNAL_ACTIVITY_CONFLICT")
        if self.funded_equity <= 0:
            raise DomainError("NONPOSITIVE_FUNDED_EQUITY")


class AdmissionActor:
    def __init__(
        self, writer: Writer, interlock: ContextInterlock, *, margin_model: MarginModel | None = None,
        fee_buffer_fraction: Decimal = Decimal("0.01"),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(fee_buffer_fraction, Decimal) or not fee_buffer_fraction.is_finite() or not 0 <= fee_buffer_fraction < 1:
            raise DomainError("INVALID_FEE_BUFFER")
        self.writer, self.interlock, self.margin_model = writer, interlock, margin_model
        self.fee_buffer_fraction, self.clock, self.on_change = fee_buffer_fraction, clock, on_change
        interlock.fee_buffer_fraction = fee_buffer_fraction  # the final-send re-check uses the same buffer
        self.drawdown = DrawdownStore(writer)
        self.writer.call(self._schema)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cf_risk_books (context_id TEXT NOT NULL,scope_id TEXT NOT NULL,version INTEGER NOT NULL,"
            "snapshot_json TEXT NOT NULL,PRIMARY KEY(context_id,scope_id))",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cf_risk_grants (intent_id TEXT PRIMARY KEY REFERENCES ex_intents(intent_id),context_id TEXT NOT NULL,"
            "scope_id TEXT NOT NULL,book_version INTEGER NOT NULL,expires_at TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)",
        )

    def observe(self, snapshot: ActiveBookSnapshot) -> int:
        snapshot.require_current(self.clock())
        with self.interlock.lock:
            def save(connection: sqlite3.Connection) -> int:
                row = self.interlock.validate_context_tx(connection, snapshot.context, risk_increase=False)
                config = EngineConfig.model_validate_json(row["effective_config"])
                if snapshot.scope_id not in config.execution_scope or snapshot.reporting_currency != config.reporting_currency:
                    raise DomainError("EXECUTION_BOOK_WRONG_SCOPE")
                if context_fact_sequence_tx(connection, snapshot.context) != snapshot.fact_sequence:
                    raise DomainError("EXECUTION_BOOK_BEHIND_FACTS")
                prior = connection.execute("SELECT version,snapshot_json FROM cf_risk_books WHERE context_id=? AND scope_id=?", (snapshot.context.identity, snapshot.scope_id)).fetchone()
                if prior is not None:
                    previous = ActiveBookSnapshot.model_validate_json(prior["snapshot_json"])
                    if previous.evidence_id == snapshot.evidence_id:
                        if previous != snapshot:
                            raise DomainError("RISK_EVIDENCE_CONFLICT")
                        return int(prior["version"])
                    if snapshot.observed_at <= previous.observed_at:
                        raise DomainError("RISK_EVIDENCE_OUT_OF_ORDER")
                known_ids = {row[0] for row in connection.execute(
                    "SELECT intent_id FROM ex_reservations WHERE authority_id=? AND environment=? AND scope_id=?",
                    (snapshot.context.authority_id, snapshot.context.environment.value, snapshot.scope_id),
                )}
                if not set(snapshot.reservations_in_remote_holds).issubset(known_ids):
                    raise DomainError("RESERVATION_HOLD_PROVENANCE_UNKNOWN")
                version = int(prior["version"]) + 1 if prior is not None else 1
                connection.execute(
                    "INSERT INTO cf_risk_books VALUES(?,?,?,?) ON CONFLICT(context_id,scope_id) DO UPDATE SET version=excluded.version,snapshot_json=excluded.snapshot_json",
                    (snapshot.context.identity, snapshot.scope_id, version, snapshot.model_dump_json()),
                )
                return version
            version = self.writer.call(save)
        self._notify()
        return version

    def admit(self, intent: OrderIntent) -> IntentRecord:
        with self.interlock.lock:
            def admission(connection: sqlite3.Connection) -> IntentRecord:
                previous = connection.execute("SELECT intent_id,payload_json FROM ex_intents WHERE semantic_key=?", (intent.semantic_key,)).fetchone()
                if previous is not None:
                    original = OrderIntent.model_validate_json(previous["payload_json"])
                    if original.model_dump(exclude={"intent_id"}) != intent.model_dump(exclude={"intent_id"}):
                        raise DomainError("SEMANTIC_INTENT_CONFLICT")
                    return get_tx(connection, original.intent_id)
                # Grant existence is checked at dispatch, after this transaction
                # creates it. Here context and slot preconditions are checked.
                self.interlock.validate_intent_tx(connection, intent, require_grant=False)
                authority = connection.execute("SELECT effective_config FROM cf_authorities WHERE authority_id=?", (intent.context.authority_id,)).fetchone()
                config = EngineConfig.model_validate_json(authority[0])
                row = connection.execute("SELECT * FROM cf_risk_books WHERE context_id=? AND scope_id=?", (intent.context.identity, intent.scope_id)).fetchone()
                if row is None:
                    raise DomainError("EXECUTION_BOOK_UNVERIFIED")
                book = ActiveBookSnapshot.model_validate_json(row["snapshot_json"])
                book.require_current(self.clock())
                if book.context != intent.context or intent.scope_id not in config.execution_scope:
                    raise DomainError("EXECUTION_BOOK_WRONG_SCOPE")
                if context_fact_sequence_tx(connection, intent.context) != book.fact_sequence:
                    raise DomainError("EXECUTION_BOOK_BEHIND_FACTS")
                if intent.quote_asset != book.reporting_currency:
                    raise DomainError("EXECUTION_QUOTE_CONVERSION_UNCERTIFIED")
                with localcontext(financial_context()) as decimal_context:
                    decimal_context.prec = 100
                    reservation = self._evaluate_tx(connection, intent, book, config)
                    record = prepare_tx(connection, intent, reservation)
                expires = min(book.expires_at, self.clock() + timedelta(seconds=5))
                connection.execute("INSERT INTO cf_risk_grants VALUES(?,?,?,?,?,0)", (record.intent.intent_id, intent.context.identity, intent.scope_id, row["version"], expires.isoformat()))
                return record
            record = self.writer.call(admission)
        self._notify()
        return record

    def _evaluate_tx(self, connection: sqlite3.Connection, intent: OrderIntent, book: ActiveBookSnapshot, config: EngineConfig) -> Reservation:
        now = self.clock()
        risk_increase = intent.kind == IntentKind.ENTRY
        from coinfly.strategy.capital import require_intent_tx

        require_intent_tx(connection, intent, book, fee_buffer_fraction=self.fee_buffer_fraction)
        if intent.context.environment is Environment.LIVE:
            from coinfly.operations.live_arming import require_order_mandate_tx
            require_order_mandate_tx(connection, intent, config, book, now=now)
        notional = intent.quantity * intent.limit_price * intent.contract_multiplier
        if notional > config.limits.max_order_notional:
            raise DomainError("ORDER_NOTIONAL_LIMIT")
        mark_map = {mark.instrument: mark for mark in book.marks}
        if intent.instrument not in mark_map or mark_map[intent.instrument].contract_multiplier != intent.contract_multiplier:
            raise DomainError("INSTRUMENT_MARK_UNVERIFIED")
        position_rows = connection.execute("SELECT instrument,quantity FROM ex_positions WHERE context_id=? AND scope_id=?", (intent.context.identity, intent.scope_id)).fetchall()
        positions = {row["instrument"]: Decimal(row["quantity"]) for row in position_rows}
        gross = Decimal(0)
        for instrument, quantity in positions.items():
            if quantity == 0:
                continue  # a closed position row carries no exposure and needs no mark
            mark = mark_map.get(instrument)
            if mark is None:
                raise DomainError("POSITION_MARK_UNVERIFIED")
            gross += abs(quantity) * mark.price * mark.contract_multiplier
        worst_case_gross = gross
        pending: list[tuple[OrderIntent, Decimal]] = []
        for row in connection.execute(
            "SELECT i.payload_json,o.filled_quantity FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND i.scope_id=? AND o.terminal=0",
            (intent.context.identity, intent.scope_id),
        ):
            older = OrderIntent.model_validate_json(row["payload_json"])
            remaining = max(Decimal(0), older.quantity - Decimal(row["filled_quantity"]))
            pending.append((older, remaining))
            gross += remaining * older.limit_price * older.contract_multiplier
            if older.kind == IntentKind.ENTRY:
                worst_case_gross += remaining * older.limit_price * older.contract_multiplier
        if risk_increase:
            if gross + notional > book.funded_equity * config.limits.max_gross_exposure:
                raise DomainError("GROSS_EXPOSURE_LIMIT")
            active_count = connection.execute("SELECT COUNT(*) FROM cf_slots WHERE context_id=? AND engine_state='ACTIVE' AND tombstoned=0", (intent.context.identity,)).fetchone()[0]
            if active_count > config.limits.max_active_episodes:
                raise DomainError("EPISODE_LIMIT")
            books = (book.scope_id, "authority") if intent.context.environment == Environment.LIVE else (book.scope_id,)
            for book_id in books:
                loss = self.drawdown.read_tx(connection, intent.context, book_id, book.reporting_currency)
                if loss is None:
                    raise DomainError("LOSS_HISTORY_UNVERIFIED")
                if loss.drawdown_fraction >= config.limits.max_drawdown_fraction:
                    raise DomainError("DRAWDOWN_LIMIT")
                if loss.day == now.date().isoformat() and loss.daily_net_result <= -config.limits.max_daily_loss:
                    raise DomainError("DAILY_LOSS_LIMIT")
        else:
            position = positions.get(intent.instrument, Decimal(0))
            closes = sum((quantity for older, quantity in pending if older.instrument == intent.instrument and older.side == intent.side), Decimal(0))
            if position == 0 or (position > 0 and intent.side != Side.SELL) or (position < 0 and intent.side != Side.BUY) or intent.quantity + closes > abs(position):
                raise DomainError("REDUCE_ONLY_QUANTITY_UNPROVEN")
        reservation_asset = intent.quote_asset
        amount = notional * (Decimal(1) + self.fee_buffer_fraction)
        if intent.context.product_track == ProductTrack.SPOT and intent.side == Side.SELL:
            reservation_asset, amount = intent.base_asset, intent.quantity
        elif intent.context.product_track == ProductTrack.PERPETUAL:
            if self.margin_model is None or self.margin_model.profile_id != intent.context.broker_profile_id:
                raise DomainError("PERPETUAL_MARGIN_UNCERTIFIED")
            if risk_increase:
                amount = self.margin_model.reservation_for(notional)
            else:
                if not isinstance(self.margin_model, ReductionMarginModel):
                    raise DomainError("REDUCTION_MARGIN_UNCERTIFIED")
                amount = self.margin_model.reduction_reservation_for(notional)
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
                raise DomainError("INVALID_MARGIN_RESERVATION")
        local = sum((Decimal(row["remaining_amount"]) for row in connection.execute(
            "SELECT intent_id,remaining_amount FROM ex_reservations WHERE authority_id=? AND environment=? AND scope_id=? AND asset=?",
            (intent.context.authority_id, intent.context.environment.value, intent.scope_id, reservation_asset),
        ) if row["intent_id"] not in book.reservations_in_remote_holds), Decimal(0))
        available = next((asset.amount for asset in book.available_assets if asset.asset == reservation_asset), None)
        if available is None or available - local < amount:
            raise DomainError("INSUFFICIENT_ACTIVE_BOOK_FUNDS")
        if intent.context.product_track == ProductTrack.PERPETUAL:
            assert self.margin_model is not None
            if risk_increase:
                scenarios = self.margin_model.evaluate(eligible_equity=book.eligible_collateral, projected_gross=gross + notional, incremental_reservations=local + amount)
                if not scenarios:
                    raise DomainError("MARGIN_SCENARIOS_UNVERIFIED")
                for scenario in scenarios:
                    scenario.require_safe()
            else:
                assert isinstance(self.margin_model, ReductionMarginModel)
                mark = mark_map[intent.instrument]
                reduction_notional = intent.quantity * mark.price * mark.contract_multiplier
                evidence = self.margin_model.evaluate_reduction(
                    eligible_equity=book.eligible_collateral, current_gross=worst_case_gross,
                    projected_gross=max(Decimal(0), worst_case_gross - reduction_notional),
                    incremental_reservations=local + amount,
                )
                evidence.require_safe_reduction()
        return Reservation(asset=reservation_asset, amount=amount)

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass
