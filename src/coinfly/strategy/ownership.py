"""Durable strategy ownership of slots, transactional budget and idempotent reconciliation.

Slots created by automation carry a deterministic identity derived from the
strategy version, context, scope and instrument, so repeated discovery,
concurrent refreshes, restarts and interrupted creations converge on one slot.
Capital is reserved in the same writer transaction that records ownership:
the sum of allocations can never exceed the approved capital, and two slots can
never spend the same budget independently.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from coinfly.control.slot_service import SlotOperationPhase, SlotRecord, SlotService
from coinfly.domain.context import ContextKey, DomainError, EngineState, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.storage.component_schema import install_component_schema
from coinfly.storage.writer import Writer
from coinfly.strategy.definition import StrategyDefinition
from coinfly.strategy.universe import SelectionReason, UniverseSelection


def strategy_slot_id(definition: StrategyDefinition, context: ContextKey, scope_id: str, instrument: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"coinfly:strategy-slot:{definition.identity}:{context.identity}:{scope_id}:{instrument}"))


class OwnedSlot(FrozenModel):
    slot_id: Identifier
    context_id: str
    strategy_identity: str
    instrument: Identifier
    allocated_budget: ExactDecimal
    state: str = Field(pattern=r"^(ACTIVE|PENDING_START|RETIRING|RETIRED)$")
    selection_reason: str = Field(max_length=400)
    created_at: datetime
    updated_at: datetime
    retire_operation_id: str | None = None
    # Hysteresis counters: consecutive selections the instrument was absent from,
    # and when it was last retired (re-entry cooldown).
    absent_count: int = 0
    retired_at: datetime | None = None


OWNERSHIP_SCHEMA_COMPONENT = "strategy_ownership"
OWNERSHIP_SCHEMA: tuple[str, ...] = (
    'CREATE TABLE IF NOT EXISTS cf_strategy_slots (slot_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, strategy_identity TEXT NOT NULL, instrument TEXT NOT NULL, allocated_budget TEXT NOT NULL, state TEXT NOT NULL, selection_reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retire_operation_id TEXT, absent_count INTEGER NOT NULL DEFAULT 0, retired_at TEXT)',
    'CREATE TABLE IF NOT EXISTS cf_strategy_budgets (context_id TEXT NOT NULL, strategy_identity TEXT NOT NULL, approved_capital TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(context_id,strategy_identity))',
    "CREATE UNIQUE INDEX IF NOT EXISTS cf_strategy_slots_live ON cf_strategy_slots(context_id,strategy_identity,instrument) WHERE state<>'RETIRED'",
)


class BudgetLedger(FrozenModel):
    context_id: str
    strategy_identity: str
    approved_capital: ExactDecimal
    allocated: ExactDecimal
    slots: int = Field(ge=0)

    @property
    def remaining(self) -> Decimal:
        return self.approved_capital - self.allocated


class SlotReconciliation(FrozenModel):
    created: tuple[str, ...] = ()
    started: tuple[str, ...] = ()
    retiring: tuple[str, ...] = ()
    blocked: tuple[tuple[str, str], ...] = ()
    budget: BudgetLedger | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"created": list(self.created), "started": list(self.started), "retiring": list(self.retiring),
                "blocked": [list(item) for item in self.blocked],
                "budget": self.budget.model_dump(mode="json") if self.budget else None}


class StrategyOwnership:
    """Ownership rows, budget accounting and reconciliation against the slot service."""

    def __init__(self, writer: Writer, slots: SlotService) -> None:
        self.writer = writer
        self.slots = slots
        self.writer.call(self._schema)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        install_component_schema(connection, OWNERSHIP_SCHEMA_COMPONENT, OWNERSHIP_SCHEMA)

    @staticmethod
    def _owned(row: sqlite3.Row) -> OwnedSlot:
        return OwnedSlot(slot_id=row["slot_id"], context_id=row["context_id"], strategy_identity=row["strategy_identity"],
                         instrument=row["instrument"], allocated_budget=Decimal(row["allocated_budget"]), state=row["state"],
                         selection_reason=row["selection_reason"], created_at=datetime.fromisoformat(row["created_at"]),
                         updated_at=datetime.fromisoformat(row["updated_at"]), retire_operation_id=row["retire_operation_id"],
                         absent_count=int(row["absent_count"]),
                         retired_at=datetime.fromisoformat(row["retired_at"]) if row["retired_at"] else None)

    def owned(self, context: ContextKey, definition: StrategyDefinition | None = None) -> tuple[OwnedSlot, ...]:
        def read(connection: sqlite3.Connection) -> tuple[OwnedSlot, ...]:
            if definition is None:
                rows = connection.execute("SELECT * FROM cf_strategy_slots WHERE context_id=? ORDER BY instrument", (context.identity,))
            else:
                rows = connection.execute("SELECT * FROM cf_strategy_slots WHERE context_id=? AND strategy_identity=? ORDER BY instrument",
                                          (context.identity, definition.identity))
            return tuple(self._owned(row) for row in rows)
        return self.writer.call(read)

    def approve_budget(self, context: ContextKey, definition: StrategyDefinition, approved_capital: Decimal, *, now: datetime) -> BudgetLedger:
        return self.writer.call(lambda connection: self.approve_budget_tx(connection, context, definition, approved_capital, now=now))

    def approve_budget_tx(self, connection: sqlite3.Connection, context: ContextKey, definition: StrategyDefinition,
                          approved_capital: Decimal, *, now: datetime) -> BudgetLedger:
        """Compose plan, allocation and receipt in the writer's one transaction."""
        if not isinstance(approved_capital, Decimal) or not approved_capital.is_finite() or approved_capital <= 0:
            raise DomainError("INVALID_CAPITAL_BUDGET")
        if approved_capital > definition.capital_budget:
            raise DomainError("BUDGET_EXCEEDS_STRATEGY_LIMIT")
        allocated = self._allocated_tx(connection, context, definition)
        if approved_capital < allocated:
            raise DomainError("BUDGET_BELOW_EXISTING_ALLOCATIONS")
        connection.execute(
            "INSERT INTO cf_strategy_budgets VALUES(?,?,?,?) ON CONFLICT(context_id,strategy_identity) "
            "DO UPDATE SET approved_capital=excluded.approved_capital, updated_at=excluded.updated_at",
            (context.identity, definition.identity, str(approved_capital), now.isoformat()))
        return self._budget_tx(connection, context, definition)

    @staticmethod
    def _allocated_tx(connection: sqlite3.Connection, context: ContextKey, definition: StrategyDefinition) -> Decimal:
        with localcontext(financial_context()):
            return sum((Decimal(row[0]) for row in connection.execute(
                "SELECT allocated_budget FROM cf_strategy_slots WHERE context_id=? AND strategy_identity=? AND state<>'RETIRED'",
                (context.identity, definition.identity))), Decimal(0))

    def _budget_tx(self, connection: sqlite3.Connection, context: ContextKey, definition: StrategyDefinition) -> BudgetLedger:
        row = connection.execute("SELECT approved_capital FROM cf_strategy_budgets WHERE context_id=? AND strategy_identity=?",
                                 (context.identity, definition.identity)).fetchone()
        approved = Decimal(row[0]) if row is not None else Decimal(0)
        count = connection.execute("SELECT COUNT(*) FROM cf_strategy_slots WHERE context_id=? AND strategy_identity=? AND state<>'RETIRED'",
                                   (context.identity, definition.identity)).fetchone()[0]
        return BudgetLedger(context_id=context.identity, strategy_identity=definition.identity, approved_capital=approved,
                            allocated=self._allocated_tx(connection, context, definition), slots=int(count))

    def budget(self, context: ContextKey, definition: StrategyDefinition) -> BudgetLedger:
        return self.writer.call(lambda connection: self._budget_tx(connection, context, definition))

    def reconcile(self, *, context: ContextKey, scope_id: str, definition: StrategyDefinition, selection: UniverseSelection,
                  config_revision: int, now: datetime, may_start: bool, start_epoch: int,
                  retire_immediately: bool = False) -> SlotReconciliation:
        """Create/start eligible slots, retire departed ones; idempotent and budget-safe.

        ``retire_immediately`` bypasses the retirement-grace hysteresis: an
        operator stop or a context drain retires every owned slot now; only a
        ranking-driven departure waits ``retirement_grace_selections`` rounds.
        """
        created: list[str] = []
        started: list[str] = []
        retiring: list[str] = []
        blocked: list[tuple[str, str]] = []
        reasons = {reason.instrument: reason for reason in selection.reasons}
        existing = {slot.instrument: slot for slot in self.slots.list(context)}
        owned = {slot.instrument: slot for slot in self.owned(context, definition)}
        with localcontext(financial_context()):
            per_slot_cap = self.budget(context, definition).approved_capital * definition.per_slot_max_exposure_fraction
        cooldown = definition.filters.reentry_cooldown_seconds
        grace = 1 if retire_immediately else definition.filters.retirement_grace_selections
        retired_rows = {slot.instrument: slot for slot in self.owned(context, definition) if slot.state == "RETIRED"}
        for instrument in selection.selected:
            slot_id = strategy_slot_id(definition, context, scope_id, instrument)
            reason = reasons.get(instrument)
            current = existing.get(instrument)
            if current is not None and current.slot_id != slot_id:
                blocked.append((instrument, "MANUAL_SLOT_OCCUPIES_INSTRUMENT"))
                continue
            previous = retired_rows.get(instrument)
            if (previous is not None and previous.retired_at is not None and cooldown
                    and (now - previous.retired_at).total_seconds() < cooldown):
                blocked.append((instrument, "SELECTION_COOLDOWN"))
                continue
            if instrument in owned and owned[instrument].absent_count:
                self._set_absent(owned[instrument].slot_id, 0)
            if current is None:
                allocation = self._reserve_budget(context, definition, instrument, slot_id, per_slot_cap, reason, now)
                if allocation is None:
                    blocked.append((instrument, "BUDGET_EXHAUSTED"))
                    continue
                try:
                    current = self.slots.create(slot_id=slot_id, context=context, scope_id=scope_id, instrument=instrument,
                                                expected_config_revision=config_revision, request_id=f"strategy-slot:{slot_id}")
                    created.append(instrument)
                except DomainError as error:
                    self._release_budget(slot_id)
                    blocked.append((instrument, error.code))
                    continue
            elif instrument not in owned:
                blocked.append((instrument, "SLOT_NOT_OWNED_BY_STRATEGY"))
                continue
            elif owned[instrument].state == "RETIRING":
                blocked.append((instrument, "SLOT_RETIRING"))
                continue
            elif owned[instrument].state == "RETIRED":
                # Re-admission after the cooldown: the stopped slot record survived
                # its protected drain; it needs budget again before it restarts.
                if current.engine_state is not EngineState.STOPPED or current.tombstoned or current.operation_id is not None:
                    blocked.append((instrument, "SLOT_RETIRING"))
                    continue
                allocation = self._reserve_budget(context, definition, instrument, slot_id, per_slot_cap, reason, now)
                if allocation is None:
                    blocked.append((instrument, "BUDGET_EXHAUSTED"))
                    continue
            if current.engine_state is EngineState.STOPPED and not current.tombstoned and current.operation_id is None:
                if not may_start:
                    self._set_state(slot_id, "PENDING_START", now)
                    continue
                try:
                    self.slots.start(slot_id, expected_revision=current.revision, expected_epoch=start_epoch,
                                     request_id=f"strategy-start:{slot_id}:{current.revision}")
                    started.append(instrument)
                    self._set_state(slot_id, "ACTIVE", now)
                except DomainError as error:
                    blocked.append((instrument, error.code))
        selected = set(selection.selected)
        for instrument, slot in owned.items():
            if slot.state == "RETIRED" or instrument in selected:
                continue
            if slot.state in ("ACTIVE", "PENDING_START") and slot.absent_count + 1 < grace:
                # Hysteresis: a transient ranking change never forces a drain.
                self._set_absent(slot.slot_id, slot.absent_count + 1)
                blocked.append((instrument, f"RETIREMENT_GRACE:{slot.absent_count + 1}/{grace}"))
                continue
            record = existing.get(instrument)
            if record is None:
                self._set_state(slot.slot_id, "RETIRED", now)
                continue
            if record.operation_id is not None:
                operation = self.slots.operation(record.operation_id)
                if operation.phase is SlotOperationPhase.COMPLETED:
                    self._set_state(slot.slot_id, "RETIRED", now)
                else:
                    retiring.append(instrument)
                continue
            if record.engine_state is EngineState.STOPPED and slot.state == "RETIRING":
                self._set_state(slot.slot_id, "RETIRED", now)
                continue
            if record.engine_state is EngineState.STOPPED and slot.state == "PENDING_START":
                # Never started; nothing to drain. Release its budget.
                self._set_state(slot.slot_id, "RETIRED", now)
                continue
            try:
                operation = self.slots.stop(slot.slot_id, expected_revision=record.revision, expected_epoch=record.context.epoch,
                                            request_id=f"strategy-retire:{slot.slot_id}:{record.revision}")
                self._set_state(slot.slot_id, "RETIRING", now, operation_id=operation.operation_id)
                retiring.append(instrument)
            except DomainError as error:
                blocked.append((instrument, error.code))
        budget = self.budget(context, definition)
        return SlotReconciliation(created=tuple(created), started=tuple(started), retiring=tuple(retiring),
                                  blocked=tuple(blocked), budget=budget)

    def _reserve_budget(self, context: ContextKey, definition: StrategyDefinition, instrument: str, slot_id: str,
                        per_slot_cap: Decimal, reason: SelectionReason | None, now: datetime) -> Decimal | None:
        def reserve(connection: sqlite3.Connection) -> Decimal | None:
            existing = connection.execute("SELECT state,allocated_budget FROM cf_strategy_slots WHERE slot_id=?", (slot_id,)).fetchone()
            if existing is not None and existing[0] != "RETIRED":
                # Resuming an interrupted creation: this row's allocation is already counted in the
                # ledger, so the slot cap and remaining budget must not be applied to it a second time.
                return Decimal(existing[1])
            ledger = self._budget_tx(connection, context, definition)
            if ledger.approved_capital <= 0:
                return None
            with localcontext(financial_context()):
                allocation = min(per_slot_cap, ledger.remaining)
            if allocation <= 0:
                return None
            if ledger.slots >= definition.max_active_slots:
                return None
            if existing is not None:
                connection.execute("UPDATE cf_strategy_slots SET state='PENDING_START',allocated_budget=?,updated_at=?,retire_operation_id=NULL,"
                                   "retired_at=NULL,absent_count=0 WHERE slot_id=?",
                                   (str(allocation), now.isoformat(), slot_id))
                return allocation
            # Named columns bind the fresh-install schema explicitly; a
            # positional INSERT would break on every new column.
            connection.execute(
                "INSERT INTO cf_strategy_slots (slot_id, context_id, strategy_identity, instrument, allocated_budget, state, "
                "selection_reason, created_at, updated_at, retire_operation_id, absent_count, retired_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL,0,NULL)",
                (slot_id, context.identity, definition.identity, instrument, str(allocation), "PENDING_START",
                 (reason.detail if reason is not None else "")[:400], now.isoformat(), now.isoformat()))
            return allocation
        return self.writer.call(reserve)

    def _release_budget(self, slot_id: str) -> None:
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_strategy_slots SET state='RETIRED',allocated_budget='0' WHERE slot_id=? AND state='PENDING_START'", (slot_id,)).rowcount)

    def _set_absent(self, slot_id: str, count: int) -> None:
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_strategy_slots SET absent_count=? WHERE slot_id=?", (count, slot_id)).rowcount)

    def _set_state(self, slot_id: str, state: str, now: datetime, *, operation_id: str | None = None) -> None:
        def save(connection: sqlite3.Connection) -> None:
            if state == "RETIRED":
                connection.execute("UPDATE cf_strategy_slots SET state=?,allocated_budget='0',updated_at=?,retired_at=?,absent_count=0 WHERE slot_id=?",
                                   (state, now.isoformat(), now.isoformat(), slot_id))
            elif operation_id is not None:
                connection.execute("UPDATE cf_strategy_slots SET state=?,updated_at=?,retire_operation_id=? WHERE slot_id=?",
                                   (state, now.isoformat(), operation_id, slot_id))
            else:
                connection.execute("UPDATE cf_strategy_slots SET state=?,updated_at=? WHERE slot_id=?", (state, now.isoformat(), slot_id))
        self.writer.call(save)

    def describe(self, context: ContextKey, definition: StrategyDefinition | None) -> list[dict[str, Any]]:
        return [dict(slot.model_dump(mode="json")) for slot in self.owned(context, definition)]


__all__ = ["BudgetLedger", "OwnedSlot", "SlotReconciliation", "StrategyOwnership", "strategy_slot_id", "SlotRecord"]
