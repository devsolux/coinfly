"""One serialized admission/send boundary for an entire account authority."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Protocol, TypeVar, cast

from coinfly.domain.context import ContextKey, DomainError, EngineState
from coinfly.storage.writer import Writer

T = TypeVar("T")


class BoundIntent(Protocol):
    @property
    def intent_id(self) -> str: ...
    @property
    def context(self) -> ContextKey: ...
    @property
    def slot_id(self) -> str: ...
    @property
    def scope_id(self) -> str: ...
    @property
    def kind(self) -> object: ...


AUTHORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS cf_authorities (
    authority_id TEXT PRIMARY KEY,
    effective_context TEXT,
    effective_config TEXT,
    effective_revision INTEGER NOT NULL DEFAULT 0,
    transition_epoch INTEGER NOT NULL DEFAULT 0,
    engine_state TEXT NOT NULL DEFAULT 'STOPPED',
    armed INTEGER NOT NULL DEFAULT 0,
    admissions_fenced INTEGER NOT NULL DEFAULT 1,
    recovery_required INTEGER NOT NULL DEFAULT 0,
    active_operation TEXT
)
"""


class ContextInterlock:
    """Shared by coordinator, risk actor, slots and the actual network owner.

    ``invoke`` must synchronously begin a single transport invocation and return a
    Future without waiting for its response. The lock covers this initiation, not
    the network wait; SQL transactions are complete before it is called. A process
    ownership lease is a separate deployment requirement.
    """

    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self.lock = RLock()
        self._provisional: dict[str, str] = {}
        self.writer.call(lambda connection: connection.execute(AUTHORITY_SCHEMA).rowcount)

    def provisional_fence(self, authority_id: str, reason: str) -> None:
        with self.lock:
            existing = self._provisional.get(authority_id)
            if existing is None or existing == "UNKNOWN_SUBMISSION":
                self._provisional[authority_id] = reason

    def is_provisionally_fenced(self, authority_id: str) -> bool:
        with self.lock:
            return authority_id in self._provisional

    def reconcile_provisional_unknown(self, context: ContextKey) -> None:
        """Clear only a resolved UNKNOWN memory fence after authoritative commits.

        This cannot clear late exposure, stop-proof invalidation, manual incident
        fences, or any durable alarm/fence. Alarm acknowledgement is unrelated.
        """
        from coinfly.execution.evidence import authority_fenced_tx

        with self.lock:
            reason = self._provisional.get(context.authority_id)
            if reason is None:
                return
            if reason != "UNKNOWN_SUBMISSION":
                raise DomainError("INCIDENT_REQUIRES_RECOVERY")
            def verify(connection: sqlite3.Connection) -> None:
                if authority_fenced_tx(connection, context.authority_id):
                    raise DomainError("AUTHORITY_FENCED")
                unresolved = connection.execute(
                    "SELECT 1 FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.authority_id=? AND o.resolution='UNKNOWN' LIMIT 1",
                    (context.authority_id,),
                ).fetchone()
                if unresolved is not None:
                    raise DomainError("UNKNOWN_UNRESOLVED")
                if connection.execute("SELECT 1 FROM ex_slot_fences WHERE authority_id=? AND active=1 LIMIT 1", (context.authority_id,)).fetchone():
                    raise DomainError("SLOT_FENCED")
            self.writer.call(verify)
            self._provisional.pop(context.authority_id, None)

    def validate_context_tx(
        self, connection: sqlite3.Connection, context: ContextKey, *, risk_increase: bool = True,
    ) -> sqlite3.Row:
        from coinfly.execution.evidence import authority_fenced_tx

        row = connection.execute("SELECT * FROM cf_authorities WHERE authority_id=?", (context.authority_id,)).fetchone()
        if row is None or row["effective_context"] is None:
            raise DomainError("NO_EFFECTIVE_CONTEXT")
        if ContextKey.model_validate_json(row["effective_context"]) != context:
            raise DomainError("STALE_CONTEXT")
        if row["recovery_required"]:
            raise DomainError("AUTHORITY_RECOVERY_REQUIRED")
        if row["engine_state"] == EngineState.STOPPED:
            raise DomainError("CONTEXT_STOPPED")
        retired = connection.execute(
            "SELECT 1 FROM ex_context_retirements WHERE context_id=?", (context.identity,),
        ).fetchone()
        if retired is not None:
            raise DomainError("CONTEXT_RETIRED")
        if risk_increase:
            if context.authority_id in self._provisional or authority_fenced_tx(connection, context.authority_id):
                raise DomainError("AUTHORITY_FENCED")
            if not row["armed"] or row["admissions_fenced"] or row["active_operation"] is not None:
                raise DomainError("CONTEXT_UNARMED_OR_TRANSITIONING")
            if row["engine_state"] != EngineState.ACTIVE or context.epoch != row["transition_epoch"]:
                raise DomainError("STALE_CONTEXT_EPOCH")
        return cast(sqlite3.Row, row)

    def validate_context(self, context: ContextKey, *, risk_increase: bool = True) -> None:
        with self.lock:
            self.writer.call(lambda connection: self.validate_context_tx(connection, context, risk_increase=risk_increase))

    def validate(self, intent: BoundIntent) -> None:
        with self.lock:
            self.writer.call(lambda connection: self.validate_intent_tx(connection, intent))

    def validate_intent_tx(self, connection: sqlite3.Connection, intent: BoundIntent, *, require_grant: bool = True) -> None:
        from coinfly.execution.evidence import slot_fenced_tx

        risk_increase = str(intent.kind) == "ENTRY"
        self.validate_context_tx(connection, intent.context, risk_increase=risk_increase)
        if risk_increase and slot_fenced_tx(connection, intent.context, intent.slot_id):
            raise DomainError("SLOT_FENCED")
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cf_slots'").fetchone()
        if table is None:
            raise DomainError("SLOT_UNVERIFIED")
        row = connection.execute("SELECT * FROM cf_slots WHERE slot_id=?", (intent.slot_id,)).fetchone()
        if row is None or row["context_id"] != intent.context.identity or row["tombstoned"]:
            raise DomainError("SLOT_UNVERIFIED")
        if risk_increase and (row["engine_state"] != EngineState.ACTIVE or not row["scheduler_enabled"]):
            raise DomainError("SLOT_NOT_ACTIVE")
        if not risk_increase and row["engine_state"] not in (EngineState.ACTIVE, EngineState.DRAINING):
            raise DomainError("SLOT_STOPPED")
        if require_grant:
            self._validate_grant_tx(connection, intent, risk_increase)

    @staticmethod
    def _validate_grant_tx(connection: sqlite3.Connection, intent: BoundIntent, risk_increase: bool) -> None:
        from coinfly.config.models import EngineConfig
        from coinfly.domain.context import Environment
        from coinfly.execution.recovery import context_fact_sequence_tx
        from coinfly.risk.admission_actor import ActiveBookSnapshot
        from coinfly.risk.drawdown import DrawdownStore

        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cf_risk_grants'").fetchone() is None:
            raise DomainError("RISK_GRANT_MISSING")
        row = connection.execute(
            "SELECT g.*,b.version AS current_book_version,b.snapshot_json FROM cf_risk_grants g "
            "JOIN cf_risk_books b USING(context_id,scope_id) WHERE g.intent_id=?", (intent.intent_id,),
        ).fetchone()
        if row is None or row["context_id"] != intent.context.identity or row["scope_id"] != intent.scope_id:
            raise DomainError("RISK_GRANT_MISSING")
        now = datetime.now(timezone.utc)
        if row["revoked"] or row["book_version"] != row["current_book_version"] or datetime.fromisoformat(row["expires_at"]) <= now:
            raise DomainError("RISK_GRANT_STALE")
        book = ActiveBookSnapshot.model_validate_json(row["snapshot_json"])
        book.require_current(now)
        if book.fact_sequence != context_fact_sequence_tx(connection, intent.context):
            raise DomainError("RISK_GRANT_BEHIND_FACTS")
        if intent.context.environment is Environment.LIVE:
            from coinfly.operations.live_arming import require_order_mandate_tx
            current_config = EngineConfig.model_validate_json(connection.execute(
                "SELECT effective_config FROM cf_authorities WHERE authority_id=?", (intent.context.authority_id,)).fetchone()[0])
            require_order_mandate_tx(connection, intent, current_config, book, now=now)
        if risk_increase:
            from decimal import Decimal

            from coinfly.execution.models import OrderIntent
            from coinfly.strategy.capital import require_intent_tx

            if isinstance(intent, OrderIntent):
                require_intent_tx(connection, intent, book, fee_buffer_fraction=Decimal("0.01"))
            config = EngineConfig.model_validate_json(connection.execute("SELECT effective_config FROM cf_authorities WHERE authority_id=?", (intent.context.authority_id,)).fetchone()[0])
            book_ids = (book.scope_id, "authority") if intent.context.environment == Environment.LIVE else (book.scope_id,)
            for book_id in book_ids:
                state = DrawdownStore.read_tx(connection, intent.context, book_id, book.reporting_currency)
                if state is None:
                    raise DomainError("LOSS_HISTORY_UNVERIFIED")
                if state.drawdown_fraction >= config.limits.max_drawdown_fraction or (state.day == now.date().isoformat() and state.daily_net_result <= -config.limits.max_daily_loss):
                    raise DomainError("LOSS_LIMIT_INVALIDATES_GRANT")

    def run_at_send_boundary(self, intent: BoundIntent, invoke: Callable[[], T]) -> T:
        with self.lock:
            self.validate(intent)
            return invoke()
