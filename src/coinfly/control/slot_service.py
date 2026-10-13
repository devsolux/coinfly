"""Durable slot exclusion, revision checks, and proof-bound deletion."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Protocol, cast
from uuid import uuid4

from pydantic import Field

from coinfly.domain.context import ContextKey, DomainError, EngineState, FrozenModel, Identifier
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import StopCertificate
from coinfly.storage.writer import Writer


class SlotRecord(FrozenModel):
    slot_id: Identifier
    context: ContextKey
    scope_id: Identifier
    instrument: Identifier
    revision: int = Field(ge=1)
    engine_state: EngineState
    scheduler_enabled: bool
    tombstoned: bool
    operation_id: str | None = None


class SlotOperationPhase(StrEnum):
    REQUESTED = "REQUESTED"
    DRAINING = "DRAINING"
    BLOCKED_DRAIN = "BLOCKED_DRAIN"
    COMPLETED = "COMPLETED"


class SlotOperation(FrozenModel):
    operation_id: Identifier
    slot_id: Identifier
    action: str
    phase: SlotOperationPhase
    error_code: str | None = None
    replacement_slot_id: Identifier | None = None
    replacement_instrument: Identifier | None = None
    requested_revision: int | None = Field(default=None, ge=1)


class SlotStopProof(FrozenModel):
    slot_id: Identifier
    certificate: StopCertificate


class SlotDriver(Protocol):
    """All methods bind the persisted originating slot; no mutable global routes."""

    def start(self, slot: SlotRecord) -> None: ...
    def quiesce(self, slot: SlotRecord) -> None: ...
    def drain(self, slot: SlotRecord, policy: DrainPolicy) -> None: ...
    def reconcile(self, slot: SlotRecord) -> SlotStopProof: ...


class SlotService:
    def __init__(
        self, writer: Writer, interlock: ContextInterlock, driver: SlotDriver,
        *, readiness: Callable[[SlotRecord], None] | None = None,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.writer, self.interlock, self.driver = writer, interlock, driver
        self.readiness, self.on_change, self.clock = readiness, on_change, clock
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coinfly-slot-drain")
        self._futures: dict[str, Future[None]] = {}
        self._closed = False
        self.writer.call(self._schema)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        for statement in (
            "CREATE TABLE IF NOT EXISTS cf_slots (slot_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, "
            "authority_id TEXT NOT NULL, context_json TEXT NOT NULL, scope_id TEXT NOT NULL, instrument TEXT NOT NULL, "
            "revision INTEGER NOT NULL, engine_state TEXT NOT NULL, scheduler_enabled INTEGER NOT NULL, "
            "tombstoned INTEGER NOT NULL DEFAULT 0, operation_id TEXT, stop_proof TEXT)",
            "CREATE UNIQUE INDEX IF NOT EXISTS cf_slots_live_instrument ON cf_slots(context_id,scope_id,instrument) WHERE tombstoned=0",
            "CREATE TABLE IF NOT EXISTS cf_slot_operations (operation_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, "
            "operation_json TEXT NOT NULL, drain_policy TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS cf_slot_receipts (request_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, "
            "action TEXT NOT NULL, payload_digest TEXT NOT NULL, operation_id TEXT)",
            "CREATE TABLE IF NOT EXISTS cf_slot_exclusions (authority_id TEXT NOT NULL, environment TEXT NOT NULL, "
            "product_track TEXT NOT NULL, scope_id TEXT NOT NULL, instrument TEXT NOT NULL, reason TEXT NOT NULL, "
            "PRIMARY KEY(authority_id,environment,product_track,scope_id,instrument))",
            "CREATE TABLE IF NOT EXISTS cf_slot_replacement_targets (operation_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, "
            "scope_id TEXT NOT NULL, instrument TEXT NOT NULL, replacement_slot_id TEXT NOT NULL UNIQUE, completed INTEGER NOT NULL DEFAULT 0)",
            "CREATE UNIQUE INDEX IF NOT EXISTS cf_slot_pending_replacement_target ON cf_slot_replacement_targets(context_id,scope_id,instrument) WHERE completed=0",
        ):
            connection.execute(statement)

    @staticmethod
    def _record(row: sqlite3.Row) -> SlotRecord:
        return SlotRecord(
            slot_id=row["slot_id"], context=ContextKey.model_validate_json(row["context_json"]),
            scope_id=row["scope_id"], instrument=row["instrument"], revision=row["revision"],
            engine_state=EngineState(row["engine_state"]), scheduler_enabled=bool(row["scheduler_enabled"]),
            tombstoned=bool(row["tombstoned"]), operation_id=row["operation_id"],
        )

    @classmethod
    def _get_tx(cls, connection: sqlite3.Connection, slot_id: str) -> SlotRecord:
        row = connection.execute("SELECT * FROM cf_slots WHERE slot_id=?", (slot_id,)).fetchone()
        if row is None:
            raise DomainError("SLOT_UNKNOWN")
        return cls._record(row)

    def get(self, slot_id: str) -> SlotRecord:
        return self.writer.call(lambda connection: self._get_tx(connection, slot_id))

    def list(self, context: ContextKey, *, include_tombstones: bool = False) -> tuple[SlotRecord, ...]:
        return self.writer.call(lambda connection: tuple(self._record(row) for row in connection.execute(
            "SELECT * FROM cf_slots WHERE context_id=?" + ("" if include_tombstones else " AND (tombstoned=0 OR stop_proof IS NULL)") + " ORDER BY slot_id",
            (context.identity,),
        )))

    @staticmethod
    def _digest(*values: object) -> str:
        return hashlib.sha256(repr(values).encode()).hexdigest()

    @staticmethod
    def _event_tx(connection: sqlite3.Connection, slot: SlotRecord, kind: str, evidence_id: str) -> None:
        from coinfly.execution.evidence import event_tx

        event_tx(connection, slot.context, slot.scope_id, kind, slot.slot_id, evidence_id)

    @staticmethod
    def _receipt_tx(connection: sqlite3.Connection, request_id: str, slot_id: str, action: str, digest: str) -> sqlite3.Row | None:
        receipt = connection.execute("SELECT * FROM cf_slot_receipts WHERE request_id=?", (request_id,)).fetchone()
        if receipt is not None and (receipt["slot_id"], receipt["action"], receipt["payload_digest"]) != (slot_id, action, digest):
            raise DomainError("REQUEST_ID_CONFLICT")
        return cast(sqlite3.Row | None, receipt)

    def create(
        self, *, slot_id: str, context: ContextKey, scope_id: str, instrument: str,
        expected_config_revision: int, request_id: str,
    ) -> SlotRecord:
        candidate = SlotRecord(
            slot_id=slot_id, context=context, scope_id=scope_id, instrument=instrument,
            revision=1, engine_state=EngineState.STOPPED, scheduler_enabled=False, tombstoned=False,
        )
        digest = self._digest(candidate.model_dump_json(), expected_config_revision)
        with self.interlock.lock:
            def save(connection: sqlite3.Connection) -> SlotRecord:
                if self._receipt_tx(connection, request_id, slot_id, "CREATE", digest) is not None:
                    return self._get_tx(connection, slot_id)
                authority = self.interlock.validate_context_tx(connection, context)
                if authority["effective_revision"] != expected_config_revision:
                    raise DomainError("CONFIGURATION_CONFLICT")
                from coinfly.config.models import EngineConfig

                config = EngineConfig.model_validate_json(authority["effective_config"])
                if scope_id not in config.execution_scope:
                    raise DomainError("SLOT_SCOPE_UNAUTHORIZED")
                if connection.execute("SELECT 1 FROM cf_slots WHERE slot_id=?", (slot_id,)).fetchone() is not None:
                    raise DomainError("SLOT_ID_RETIRED_OR_EXISTS")
                self._require_unreserved_target_tx(connection, context, scope_id, instrument)
                # A previously disabled instrument cannot be silently rotated back
                # into a newly named slot. Explicit start uses its existing ID.
                disabled = connection.execute(
                    "SELECT 1 FROM cf_slot_exclusions WHERE authority_id=? AND environment=? AND product_track=? AND scope_id=? AND instrument=?",
                    (context.authority_id, context.environment.value, context.product_track.value, scope_id, instrument),
                ).fetchone()
                if disabled is not None:
                    raise DomainError("INSTRUMENT_MANUALLY_DISABLED")
                try:
                    connection.execute(
                        "INSERT INTO cf_slots(slot_id,context_id,authority_id,context_json,scope_id,instrument,revision,engine_state,scheduler_enabled,tombstoned) VALUES(?,?,?,?,?,?,1,'STOPPED',0,0)",
                        (slot_id, context.identity, context.authority_id, context.model_dump_json(), scope_id, instrument),
                    )
                except sqlite3.IntegrityError as error:
                    raise DomainError("SLOT_ALREADY_EXISTS") from error
                connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,NULL)", (request_id, slot_id, "CREATE", digest))
                self._event_tx(connection, candidate, "SLOT_CREATED", request_id)
                return candidate
            result = self.writer.call(save)
        self._notify()
        return result

    def start(self, slot_id: str, *, expected_revision: int, expected_epoch: int, request_id: str) -> SlotRecord:
        digest = self._digest(slot_id, expected_revision, expected_epoch)
        with self.interlock.lock:
            prior = self.writer.call(lambda connection: self._receipt_tx(connection, request_id, slot_id, "START", digest))
            if prior is not None:
                return self.get(slot_id)
            slot = self.get(slot_id)
            self._require_revision(slot, expected_revision, expected_epoch)
            self.interlock.validate_context(slot.context)
            if slot.tombstoned or slot.engine_state != EngineState.STOPPED or slot.operation_id:
                raise DomainError("SLOT_NOT_STARTABLE")
            from coinfly.execution.recovery import assert_slot_closed_tx

            def check_start(connection: sqlite3.Connection) -> None:
                from coinfly.config.models import EngineConfig

                authority = self.interlock.validate_context_tx(connection, slot.context)
                config = EngineConfig.model_validate_json(authority["effective_config"])
                if slot.scope_id not in config.execution_scope:
                    raise DomainError("SLOT_SCOPE_UNAUTHORIZED")
                active = connection.execute("SELECT COUNT(*) FROM cf_slots WHERE context_id=? AND engine_state='ACTIVE' AND tombstoned=0", (slot.context.identity,)).fetchone()[0]
                if active >= config.limits.max_active_episodes:
                    raise DomainError("EPISODE_LIMIT")
                assert_slot_closed_tx(connection, slot.context, slot.slot_id)
            self.writer.call(check_start)
            if self.readiness is not None:
                self.readiness(slot)
            self.driver.start(slot)
            def save(connection: sqlite3.Connection) -> SlotRecord:
                self.interlock.validate_context_tx(connection, slot.context)
                self._require_revision(self._get_tx(connection, slot_id), expected_revision, expected_epoch)
                connection.execute("UPDATE cf_slots SET engine_state='ACTIVE',scheduler_enabled=1,revision=revision+1,stop_proof=NULL WHERE slot_id=?", (slot_id,))
                connection.execute(
                    "DELETE FROM cf_slot_exclusions WHERE authority_id=? AND environment=? AND product_track=? AND scope_id=? AND instrument=?",
                    (slot.context.authority_id, slot.context.environment.value, slot.context.product_track.value, slot.scope_id, slot.instrument),
                )
                connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,NULL)", (request_id, slot_id, "START", digest))
                self._event_tx(connection, slot, "SLOT_STARTED", request_id)
                return self._get_tx(connection, slot_id)
            result = self.writer.call(save)
        self._notify()
        return result

    @staticmethod
    def _require_revision(slot: SlotRecord, revision: int, epoch: int) -> None:
        if slot.revision != revision or slot.context.epoch != epoch:
            raise DomainError("SLOT_REVISION_CONFLICT")

    def stop(
        self, slot_id: str, *, expected_revision: int, expected_epoch: int, request_id: str,
        policy: DrainPolicy | None = None, delete: bool = False,
    ) -> SlotOperation:
        policy = policy or DrainPolicy()
        action = "DELETE" if delete else "STOP"
        digest = self._digest(slot_id, expected_revision, expected_epoch, action, policy.model_dump_json())
        with self.interlock.lock:
            if self._closed:
                raise DomainError("SLOT_SERVICE_CLOSED")
            def accept(connection: sqlite3.Connection) -> tuple[SlotOperation, bool]:
                receipt = self._receipt_tx(connection, request_id, slot_id, action, digest)
                if receipt is not None:
                    return self._operation_tx(connection, receipt["operation_id"]), False
                slot = self._get_tx(connection, slot_id)
                if slot.operation_id:
                    existing = self._operation_tx(connection, slot.operation_id)
                    if existing.action != action or slot.context.epoch != expected_epoch:
                        raise DomainError("SLOT_OPERATION_IN_PROGRESS")
                    connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,?)", (request_id, slot_id, action, digest, existing.operation_id))
                    return existing, False
                self._require_revision(slot, expected_revision, expected_epoch)
                if slot.tombstoned:
                    raise DomainError("SLOT_TOMBSTONED")
                operation = SlotOperation(operation_id=str(uuid4()), slot_id=slot_id, action=action, phase=SlotOperationPhase.REQUESTED)
                connection.execute("INSERT INTO cf_slot_operations VALUES(?,?,?,?)", (operation.operation_id, slot_id, operation.model_dump_json(), policy.model_dump_json()))
                connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,?)", (request_id, slot_id, action, digest, operation.operation_id))
                connection.execute(
                    "UPDATE cf_slots SET engine_state='DRAINING',scheduler_enabled=0,revision=revision+1,operation_id=? WHERE slot_id=?",
                    (operation.operation_id, slot_id),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO cf_slot_exclusions VALUES(?,?,?,?,?,?)",
                    (slot.context.authority_id, slot.context.environment.value, slot.context.product_track.value, slot.scope_id, slot.instrument, action),
                )
                self._event_tx(connection, slot, "SLOT_" + action + "_REQUESTED", request_id)
                return operation, True
            operation, accepted = self.writer.call(accept)
            if accepted:
                self._schedule(operation.operation_id)
        self._notify()
        return operation

    def delete(self, slot_id: str, *, expected_revision: int, expected_epoch: int, request_id: str, policy: DrainPolicy | None = None) -> SlotOperation:
        return self.stop(slot_id, expected_revision=expected_revision, expected_epoch=expected_epoch, request_id=request_id, policy=policy, delete=True)

    @staticmethod
    def _require_unreserved_target_tx(
        connection: sqlite3.Connection, context: ContextKey, scope_id: str, instrument: str,
        *, own_operation_id: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT operation_id FROM cf_slot_replacement_targets WHERE context_id=? AND scope_id=? AND instrument=? AND completed=0",
            (context.identity, scope_id, instrument),
        ).fetchone()
        if row is not None and row[0] != own_operation_id:
            raise DomainError("SLOT_REPLACEMENT_TARGET_IN_PROGRESS")

    def edit(
        self, slot_id: str, *, expected_revision: int, expected_epoch: int, instrument: str, request_id: str,
    ) -> SlotRecord | SlotOperation:
        """Replace an immutable instrument binding after an ordinary protected drain.

        The proposed slot has a durable new identity immediately, but no slot row,
        engine initialization or Start side effect exists until the source's proof
        and the still-current context pass the final transaction together.
        """
        digest = self._digest(slot_id, expected_revision, expected_epoch, instrument)
        with self.interlock.lock:
            if self._closed:
                raise DomainError("SLOT_SERVICE_CLOSED")
            def accept(connection: sqlite3.Connection) -> tuple[SlotRecord | SlotOperation, bool]:
                receipt = self._receipt_tx(connection, request_id, slot_id, "EDIT", digest)
                if receipt is not None:
                    if receipt["operation_id"] is not None:
                        return self._operation_tx(connection, receipt["operation_id"]), False
                    return self._get_tx(connection, slot_id), False
                identical = connection.execute(
                    "SELECT operation_id FROM cf_slot_receipts WHERE slot_id=? AND action='EDIT' AND payload_digest=? AND operation_id IS NOT NULL LIMIT 1",
                    (slot_id, digest),
                ).fetchone()
                if identical is not None:
                    operation = self._operation_tx(connection, identical[0])
                    connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,?)", (request_id, slot_id, "EDIT", digest, operation.operation_id))
                    return operation, False
                slot = self._get_tx(connection, slot_id)
                if slot.operation_id is not None:
                    existing = self._operation_tx(connection, slot.operation_id)
                    if (existing.action != "EDIT" or existing.replacement_instrument != instrument
                            or slot.context.epoch != expected_epoch
                            or expected_revision not in (slot.revision, existing.requested_revision)):
                        raise DomainError("SLOT_OPERATION_IN_PROGRESS")
                    connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,?)", (request_id, slot_id, "EDIT", digest, existing.operation_id))
                    return existing, False
                self._require_revision(slot, expected_revision, expected_epoch)
                authority = self.interlock.validate_context_tx(connection, slot.context, risk_increase=False)
                if (authority["active_operation"] is not None or authority["transition_epoch"] != slot.context.epoch
                        or authority["engine_state"] != EngineState.ACTIVE):
                    raise DomainError("CONTEXT_TRANSITION_IN_PROGRESS")
                if slot.tombstoned or slot.engine_state not in (EngineState.ACTIVE, EngineState.STOPPED):
                    raise DomainError("SLOT_NOT_EDITABLE")
                if instrument == slot.instrument:
                    connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,NULL)", (request_id, slot_id, "EDIT", digest))
                    self._event_tx(connection, slot, "SLOT_EDIT_UNCHANGED", request_id)
                    return slot, False
                replacement = SlotRecord(
                    slot_id=str(uuid4()), context=slot.context, scope_id=slot.scope_id, instrument=instrument,
                    revision=1, engine_state=EngineState.STOPPED, scheduler_enabled=False, tombstoned=False,
                )
                self._require_replacement_target_tx(connection, slot, replacement)
                operation = SlotOperation(
                    operation_id=str(uuid4()), slot_id=slot_id, action="EDIT", phase=SlotOperationPhase.REQUESTED,
                    replacement_slot_id=replacement.slot_id, replacement_instrument=replacement.instrument,
                    requested_revision=slot.revision,
                )
                connection.execute("INSERT INTO cf_slot_operations VALUES(?,?,?,?)", (operation.operation_id, slot_id, operation.model_dump_json(), DrainPolicy().model_dump_json()))
                connection.execute("INSERT INTO cf_slot_receipts VALUES(?,?,?,?,?)", (request_id, slot_id, "EDIT", digest, operation.operation_id))
                connection.execute(
                    "INSERT INTO cf_slot_replacement_targets VALUES(?,?,?,?,?,0)",
                    (operation.operation_id, slot.context.identity, slot.scope_id, replacement.instrument, replacement.slot_id),
                )
                connection.execute(
                    "UPDATE cf_slots SET engine_state='DRAINING',scheduler_enabled=0,revision=revision+1,operation_id=? WHERE slot_id=?",
                    (operation.operation_id, slot_id),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO cf_slot_exclusions VALUES(?,?,?,?,?,?)",
                    (slot.context.authority_id, slot.context.environment.value, slot.context.product_track.value, slot.scope_id, slot.instrument, "EDIT"),
                )
                self._event_tx(connection, slot, "SLOT_EDIT_REQUESTED", request_id)
                return operation, True
            result, accepted = self.writer.call(accept)
            if accepted:
                assert isinstance(result, SlotOperation)
                self._schedule(result.operation_id)
        self._notify()
        return result

    def _require_replacement_target_tx(
        self, connection: sqlite3.Connection, source: SlotRecord, replacement: SlotRecord,
        *, own_operation_id: str | None = None,
    ) -> None:
        self._require_unreserved_target_tx(connection, source.context, source.scope_id, replacement.instrument, own_operation_id=own_operation_id)
        if connection.execute(
            "SELECT 1 FROM cf_slots WHERE context_id=? AND scope_id=? AND instrument=? AND tombstoned=0",
            (source.context.identity, source.scope_id, replacement.instrument),
        ).fetchone() is not None:
            raise DomainError("SLOT_ALREADY_EXISTS")
        if connection.execute(
            "SELECT 1 FROM cf_slot_exclusions WHERE authority_id=? AND environment=? AND product_track=? AND scope_id=? AND instrument=?",
            (source.context.authority_id, source.context.environment.value, source.context.product_track.value, source.scope_id, replacement.instrument),
        ).fetchone() is not None:
            raise DomainError("INSTRUMENT_MANUALLY_DISABLED")

    def _replacement_tx(self, connection: sqlite3.Connection, operation: SlotOperation, source: SlotRecord) -> SlotRecord:
        from coinfly.config.models import EngineConfig
        from coinfly.execution.evidence import authority_fenced_tx

        if operation.replacement_slot_id is None or operation.replacement_instrument is None:
            raise DomainError("REPLACEMENT_TARGET_MISSING")
        authority = self.interlock.validate_context_tx(connection, source.context, risk_increase=False)
        if (authority["active_operation"] is not None or authority["transition_epoch"] != source.context.epoch
                or authority["engine_state"] != EngineState.ACTIVE):
            raise DomainError("REPLACEMENT_CONTEXT_CHANGED")
        if authority_fenced_tx(connection, source.context.authority_id):
            raise DomainError("AUTHORITY_FENCED")
        config = EngineConfig.model_validate_json(authority["effective_config"])
        if source.scope_id not in config.execution_scope:
            raise DomainError("SLOT_SCOPE_UNAUTHORIZED")
        target = connection.execute("SELECT * FROM cf_slot_replacement_targets WHERE operation_id=?", (operation.operation_id,)).fetchone()
        if target is None or target["completed"] or (target["context_id"], target["scope_id"], target["instrument"], target["replacement_slot_id"]) != (
            source.context.identity, source.scope_id, operation.replacement_instrument, operation.replacement_slot_id,
        ):
            raise DomainError("REPLACEMENT_TARGET_CHANGED")
        replacement = SlotRecord(
            slot_id=operation.replacement_slot_id, context=source.context, scope_id=source.scope_id,
            instrument=operation.replacement_instrument, revision=1, engine_state=EngineState.STOPPED,
            scheduler_enabled=False, tombstoned=False,
        )
        self._require_replacement_target_tx(connection, source, replacement, own_operation_id=operation.operation_id)
        return replacement

    @staticmethod
    def _operation_tx(connection: sqlite3.Connection, operation_id: str) -> SlotOperation:
        row = connection.execute("SELECT operation_json FROM cf_slot_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise DomainError("OPERATION_UNKNOWN")
        return SlotOperation.model_validate_json(row[0])

    def operation(self, operation_id: str) -> SlotOperation:
        return self.writer.call(lambda connection: self._operation_tx(connection, operation_id))

    def _phase(self, operation_id: str, phase: SlotOperationPhase, error_code: str | None = None) -> None:
        def save(connection: sqlite3.Connection) -> None:
            operation = self._operation_tx(connection, operation_id).model_copy(update={"phase": phase, "error_code": error_code})
            connection.execute("UPDATE cf_slot_operations SET operation_json=? WHERE operation_id=?", (operation.model_dump_json(), operation_id))
            self._event_tx(connection, self._get_tx(connection, operation.slot_id), "SLOT_" + phase.value, operation_id)
        self.writer.call(save)
        self._notify()

    def _drain(self, operation_id: str) -> None:
        operation = self.operation(operation_id)
        slot = self.get(operation.slot_id)
        try:
            policy = self.writer.call(lambda connection: DrainPolicy.model_validate_json(connection.execute(
                "SELECT drain_policy FROM cf_slot_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()[0]))
            self._phase(operation_id, SlotOperationPhase.DRAINING)
            self.driver.quiesce(slot)
            self.driver.drain(slot, policy)
            proof = self.driver.reconcile(slot)
            if proof.slot_id != slot.slot_id:
                raise DomainError("STOP_PROOF_WRONG_SLOT")
            proof.certificate.require_valid(slot.context, (slot.scope_id,), self.clock())
            with self.interlock.lock:
                if operation.action == "EDIT" and self.interlock.is_provisionally_fenced(slot.context.authority_id):
                    raise DomainError("AUTHORITY_FENCED")
                def stop(connection: sqlite3.Connection) -> None:
                    from coinfly.execution.recovery import assert_slot_closed_tx, slot_fact_sequence_tx

                    assert_slot_closed_tx(connection, slot.context, slot.slot_id)
                    if slot_fact_sequence_tx(connection, slot.context, slot.slot_id) != proof.certificate.fact_sequence:
                        raise DomainError("STOP_PROOF_FACTS_CHANGED")
                    proof.certificate.require_valid(slot.context, (slot.scope_id,), self.clock())
                    current = self._get_tx(connection, slot.slot_id)
                    if current.operation_id != operation_id:
                        raise DomainError("OPERATION_OWNERSHIP_LOST")
                    replacement = self._replacement_tx(connection, operation, slot) if operation.action == "EDIT" else None
                    connection.execute(
                        "UPDATE cf_slots SET engine_state='STOPPED',scheduler_enabled=0,revision=revision+1,tombstoned=?,operation_id=NULL,stop_proof=? WHERE slot_id=?",
                        (int(operation.action in ("DELETE", "EDIT")), proof.model_dump_json(), slot.slot_id),
                    )
                    if replacement is not None:
                        connection.execute(
                            "INSERT INTO cf_slots(slot_id,context_id,authority_id,context_json,scope_id,instrument,revision,engine_state,scheduler_enabled,tombstoned) VALUES(?,?,?,?,?,?,1,'STOPPED',0,0)",
                            (replacement.slot_id, replacement.context.identity, replacement.context.authority_id,
                             replacement.context.model_dump_json(), replacement.scope_id, replacement.instrument),
                        )
                        connection.execute("UPDATE cf_slot_replacement_targets SET completed=1 WHERE operation_id=?", (operation_id,))
                        self._event_tx(connection, replacement, "SLOT_CREATED_BY_EDIT", operation_id)
                    complete = operation.model_copy(update={"phase": SlotOperationPhase.COMPLETED, "error_code": None})
                    connection.execute("UPDATE cf_slot_operations SET operation_json=? WHERE operation_id=?", (complete.model_dump_json(), operation_id))
                    event_kind = {"DELETE": "SLOT_DELETED", "EDIT": "SLOT_REPLACED", "STOP": "SLOT_STOPPED"}[operation.action]
                    self._event_tx(connection, slot, event_kind, proof.certificate.proof_id)
                self.writer.call(stop)
            self._notify()
        except Exception as error:
            self._phase(operation_id, SlotOperationPhase.BLOCKED_DRAIN, error.code if isinstance(error, DomainError) else "SLOT_DRAIN_FAILED")

    def retry(self, operation_id: str) -> SlotOperation:
        with self.interlock.lock:
            if self._closed:
                raise DomainError("SLOT_SERVICE_CLOSED")
            operation = self.operation(operation_id)
            future = self._futures.get(operation_id)
            if future is not None and not future.done():
                return operation
            if operation.phase not in (SlotOperationPhase.REQUESTED, SlotOperationPhase.DRAINING, SlotOperationPhase.BLOCKED_DRAIN):
                raise DomainError("OPERATION_NOT_RETRYABLE")
            self._schedule(operation_id)
            return operation

    def _schedule(self, operation_id: str) -> None:
        future = self._executor.submit(self._drain, operation_id)
        self._futures[operation_id] = future

        def completed(done: Future[None]) -> None:
            with self.interlock.lock:
                if self._futures.get(operation_id) is done:
                    self._futures.pop(operation_id)

        future.add_done_callback(completed)

    def wait(self, operation_id: str, timeout: float = 10) -> SlotOperation:
        with self.interlock.lock:
            future = self._futures.get(operation_id)
        if future is not None:
            future.result(timeout=timeout)
            with self.interlock.lock:
                if self._futures.get(operation_id) is future:
                    self._futures.pop(operation_id)
        return self.operation(operation_id)

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def close(self) -> None:
        with self.interlock.lock:
            self._closed = True
        self._executor.shutdown(wait=True)
