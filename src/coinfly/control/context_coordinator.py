"""Durable, revision-bound context handover with an asynchronous operation API."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

from coinfly.config.models import EngineConfig
from coinfly.control.transition_state import (
    ContextPreview,
    ContextStatus,
    CorrectiveAction,
    ExceptionLinkRecord,
    TransitionFailure,
    TransitionOperation,
    TransitionPhase,
    TransitionStage,
)
from coinfly.domain.context import ContextKey, DomainError, EngineState, Environment
from coinfly.network.context_transports import ContextTransports
from coinfly.observability.diagnostics import redacted_chain, stable_code
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import RetirementEvidence, StopCertificate
from coinfly.storage.writer import Writer

logger = logging.getLogger(__name__)

_DATASET_CODES = frozenset({
    "DATASET_NOT_READY", "DATASET_CACHE_MISSING", "DATA_DIGEST_OR_LENGTH_MISMATCH", "DATASET_PATH_NOT_ATTESTED",
    "DATA_DIRECTORY_SYMLINK_REJECTED", "BOOTSTRAP_BUSY", "SIGNED_MANIFEST_MISSING", "PUBLISHER_TRUST_ROOT_MISSING",
})
_RESTART_CODES = frozenset({
    "LOCAL_DATASET_READINESS_INVALID", "WRITER_CLOSED", "WRITER_FAILED", "COORDINATOR_CLOSED",
    "AUTHORITY_NOT_OWNED", "SLOT_SERVICE_CLOSED",
})
_INCIDENT_CODES = frozenset({
    "AUTHORITY_FENCED", "TARGET_RETIREMENT_UNPROVEN", "NETWORK_RETIREMENT_UNPROVEN", "STOP_PROOF_INVALIDATED",
    "STOP_PROOF_FACTS_CHANGED", "OPERATION_OWNERSHIP_LOST", "PAPER_PREVIOUS_BOOK_NOT_FLAT",
    "STOP_PROOF_RESIDUAL_POSITION", "STOP_PROOF_OUTSTANDING_INTENT", "STOP_PROOF_POSITION_NONZERO",
})
_STORAGE_CODES = frozenset({
    "STORAGE_PERMISSION_DENIED", "STORAGE_PATH_CONFLICT", "STORAGE_IO_FAILURE", "STORAGE_DIRECTORY_MISSING",
    "UNSAFE_RUNTIME_PATH", "RUNTIME_PATH_NOT_PRIVATE", "DATABASE_INITIALIZATION_TIMEOUT", "WRITER_BACKPRESSURE",
})


def describe_failure(error: BaseException, *, stage: TransitionStage, target_retired: bool,
                     recorded_at: datetime) -> TransitionFailure:
    """Turn an exception into a durable, redacted, actionable failure record.

    The underlying stable code is never collapsed away: a coded exception that
    is not a DomainError (for example a BootstrapError) keeps its own code.
    Only genuinely uncoded exceptions become ENGINE_OPERATION_FAILED, and even
    then the concrete exception types remain visible in the chain.
    """
    chain = redacted_chain(error)
    code = stable_code(error)
    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(causes) < 8 and current not in causes:
        causes.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    if code is None:
        category = "UNEXPECTED"
        if any(isinstance(item, ImportError) for item in causes):
            category = "IMPORT"
        elif any(isinstance(item, (sqlite3.Error, OSError)) for item in causes):
            category = "STORAGE"
        code = "ENGINE_OPERATION_FAILED"
    elif isinstance(error, DomainError):
        category = "DOMAIN"
    else:
        category = "CODED"
    action = CorrectiveAction.INSPECT_WORKER_LOG
    retry_safe = target_retired
    if not target_retired:
        action = CorrectiveAction.RESOLVE_INCIDENTS
    elif code in _DATASET_CODES or code.startswith("DATASET_") or code.startswith("BOOTSTRAP_"):
        action = CorrectiveAction.VERIFY_DATASET
    elif code in _RESTART_CODES or category == "IMPORT":
        action = CorrectiveAction.RESTART_WORKER
    elif code in _INCIDENT_CODES:
        action = CorrectiveAction.RESOLVE_INCIDENTS
    elif code in _STORAGE_CODES or category == "STORAGE":
        action = CorrectiveAction.CHECK_STORAGE
    elif code in ("DRAIN_FRESH_MARKET_REQUIRED", "DRAIN_AWAITING_LIQUIDITY", "DRAIN_DEADLINE_EXCEEDED",
                  "STOP_PROOF_STALE", "STOP_PROOF_STALE_ACCOUNT", "RECOVERY_ENGINE_REQUIRED"):
        action = CorrectiveAction.RETRY_OPERATION
    return TransitionFailure(
        stage=stage, code=code, category=category,
        chain=tuple(ExceptionLinkRecord(type=link.type, relation=link.relation, code=link.code,
                                        module=link.module, errno=link.errno) for link in chain),
        retry_safe=retry_safe, corrective_action=action, recorded_at=recorded_at,
    )


class EngineLifecycle(Protocol):
    context: ContextKey

    def initialize(self, config: EngineConfig, context: ContextKey) -> None: ...
    def quiesce(self) -> None: ...
    def drain(self, policy: DrainPolicy) -> None: ...
    def reconcile(self) -> StopCertificate: ...
    def retire(self) -> RetirementEvidence: ...
    def reset_transient(self) -> None: ...
    def arm(self) -> None: ...


EngineFactory = Callable[[EngineConfig, ContextKey], EngineLifecycle]
PreviewValidator = Callable[[EngineConfig], None]


def _sandbox_preview(config: EngineConfig) -> None:
    if config.environment == Environment.LIVE:
        raise DomainError("LIVE_CAPABILITY_UNAVAILABLE")


def _request_digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ContextCoordinator:
    """The factory must be nonmutating until explicit context-bound ``arm``.

    Source engines remain bound to their original routes throughout a drain.
    Restart always fences admissions. A recovered source must be attached by the
    authority-owning supervisor; the coordinator cannot invent exchange truth.
    Failed transitions require explicit ``retry`` and never auto-resume entries.
    """

    def __init__(
        self, writer: Writer, interlock: ContextInterlock, engine_factory: EngineFactory,
        preview_validator: PreviewValidator = _sandbox_preview,
        *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        recover_existing: bool = True,
        on_change: Callable[[], None] | None = None,
        engines: dict[str, EngineLifecycle] | None = None,
        transports: ContextTransports | None = None,
    ) -> None:
        self.writer = writer
        self.interlock = interlock
        self._factory = engine_factory
        self._validate_preview = preview_validator
        self._clock = clock
        self._on_change = on_change
        # Share lifecycle ownership with the supervisor/slot driver. A second
        # registry otherwise retains every retired engine and its complete book.
        self._engines = engines if engines is not None else {}
        self.transports = transports or ContextTransports()
        self._futures: dict[str, Future[None]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coinfly-context-control")
        self._closed = False
        self.writer.call(self._schema)
        if recover_existing:
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_authorities SET armed=0, admissions_fenced=1, recovery_required=1 "
                "WHERE effective_context IS NOT NULL OR active_operation IS NOT NULL",
            ).rowcount)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        for statement in (
            "CREATE TABLE IF NOT EXISTS cf_previews (preview_id TEXT PRIMARY KEY, preview_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS cf_transition_operations (operation_id TEXT PRIMARY KEY, "
            "authority_id TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL, "
            "operation_json TEXT NOT NULL, drain_policy TEXT NOT NULL, stop_proof TEXT, retirement TEXT, "
            "UNIQUE(authority_id, request_id))",
            "CREATE TABLE IF NOT EXISTS cf_transition_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "operation_id TEXT NOT NULL, phase TEXT NOT NULL, recorded_at TEXT NOT NULL, error_code TEXT)",
            "CREATE TABLE IF NOT EXISTS cf_control_receipts (authority_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, request_digest TEXT NOT NULL, PRIMARY KEY(authority_id,request_id))",
        ):
            connection.execute(statement)

    def status(self, authority_id: str) -> ContextStatus:
        def read(connection: sqlite3.Connection) -> ContextStatus:
            row = connection.execute("SELECT * FROM cf_authorities WHERE authority_id=?", (authority_id,)).fetchone()
            if row is None:
                return ContextStatus(
                    authority_id=authority_id, effective_context=None, effective_config=None,
                    effective_revision=0, transition_epoch=0, engine_state=EngineState.STOPPED,
                    armed=False, admissions_fenced=True, recovery_required=False, operation=None,
                )
            operation = self._operation_tx(connection, row["active_operation"]) if row["active_operation"] else None
            return ContextStatus(
                authority_id=authority_id,
                effective_context=ContextKey.model_validate_json(row["effective_context"]) if row["effective_context"] else None,
                effective_config=EngineConfig.model_validate_json(row["effective_config"]) if row["effective_config"] else None,
                effective_revision=row["effective_revision"], transition_epoch=row["transition_epoch"],
                engine_state=EngineState(row["engine_state"]), armed=bool(row["armed"]),
                admissions_fenced=bool(row["admissions_fenced"]), recovery_required=bool(row["recovery_required"]),
                operation=operation,
            )
        return self.writer.call(read)

    def preview(self, config: EngineConfig, *, expected_revision: int, expected_epoch: int) -> ContextPreview:
        self._validate_preview(config)
        preview = ContextPreview(
            preview_id=str(uuid4()), authority_id=config.authority_id, config=config,
            config_digest=config.digest, expected_revision=expected_revision, expected_epoch=expected_epoch,
            expires_at=self._clock() + timedelta(seconds=60),
        )
        with self.interlock.lock:
            def save(connection: sqlite3.Connection) -> None:
                connection.execute("INSERT OR IGNORE INTO cf_authorities(authority_id) VALUES(?)", (config.authority_id,))
                row = connection.execute("SELECT * FROM cf_authorities WHERE authority_id=?", (config.authority_id,)).fetchone()
                self._require_revision(row, expected_revision, expected_epoch)
                connection.execute("INSERT INTO cf_previews VALUES(?,?)", (preview.preview_id, preview.model_dump_json()))
            self.writer.call(save)
        return preview

    @staticmethod
    def _require_revision(row: sqlite3.Row, revision: int, epoch: int) -> None:
        if row["effective_revision"] != revision or row["transition_epoch"] != epoch:
            raise DomainError("CONFIGURATION_CONFLICT")

    def apply(
        self, preview_id: str, config_digest: str, *, expected_revision: int, expected_epoch: int,
        request_id: str, drain_policy: DrainPolicy | None = None,
    ) -> TransitionOperation:
        policy = drain_policy or DrainPolicy()
        request_hash = _request_digest((preview_id, config_digest, expected_revision, expected_epoch, policy.model_dump(mode="json")))
        with self.interlock.lock:
            if self._closed:
                raise DomainError("COORDINATOR_CLOSED")
            def accept(connection: sqlite3.Connection) -> tuple[TransitionOperation, bool]:
                raw = connection.execute("SELECT preview_json FROM cf_previews WHERE preview_id=?", (preview_id,)).fetchone()
                if raw is None:
                    raise DomainError("PREVIEW_UNKNOWN")
                preview = ContextPreview.model_validate_json(raw[0])
                prior = connection.execute(
                    "SELECT * FROM cf_transition_operations WHERE authority_id=? AND request_id=?",
                    (preview.authority_id, request_id),
                ).fetchone()
                if prior is not None:
                    if prior["request_digest"] != request_hash:
                        raise DomainError("REQUEST_ID_CONFLICT")
                    return TransitionOperation.model_validate_json(prior["operation_json"]), False
                if preview.expires_at <= self._clock() or preview.config_digest != config_digest or preview.config.digest != config_digest:
                    raise DomainError("PREVIEW_STALE_OR_CHANGED")
                if preview.expected_revision != expected_revision or preview.expected_epoch != expected_epoch:
                    raise DomainError("PREVIEW_REVISION_CONFLICT")
                row = connection.execute("SELECT * FROM cf_authorities WHERE authority_id=?", (preview.authority_id,)).fetchone()
                self._require_revision(row, expected_revision, expected_epoch)
                if row["active_operation"] is not None:
                    raise DomainError("CONTEXT_TRANSITION_IN_PROGRESS")
                target = ContextKey(
                    authority_id=preview.authority_id, environment=preview.config.environment,
                    product_track=preview.config.product_track, broker_profile_id=preview.config.broker_profile_id,
                    epoch=expected_epoch + 1,
                )
                operation = TransitionOperation(
                    operation_id=str(uuid4()), request_id=request_id, authority_id=preview.authority_id,
                    phase=TransitionPhase.REQUESTED,
                    source_context=ContextKey.model_validate_json(row["effective_context"]) if row["effective_context"] else None,
                    target_context=target, proposed_config=preview.config, proposed_revision=expected_revision + 1,
                    transition_epoch=expected_epoch + 1,
                )
                connection.execute(
                    "INSERT INTO cf_transition_operations(operation_id,authority_id,request_id,request_digest,operation_json,drain_policy) VALUES(?,?,?,?,?,?)",
                    (operation.operation_id, operation.authority_id, request_id, request_hash, operation.model_dump_json(), policy.model_dump_json()),
                )
                connection.execute(
                    "UPDATE cf_authorities SET active_operation=?, transition_epoch=?, armed=0, admissions_fenced=1 WHERE authority_id=?",
                    (operation.operation_id, operation.transition_epoch, operation.authority_id),
                )
                self._event_tx(connection, operation)
                return operation, True
            operation, accepted = self.writer.call(accept)
            if accepted:
                self._schedule(operation.operation_id)
        if accepted:
            self._notify()
        return operation

    @staticmethod
    def _operation_tx(connection: sqlite3.Connection, operation_id: str) -> TransitionOperation:
        row = connection.execute("SELECT operation_json FROM cf_transition_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise DomainError("OPERATION_UNKNOWN")
        return TransitionOperation.model_validate_json(row[0])

    def operation(self, operation_id: str) -> TransitionOperation:
        return self.writer.call(lambda connection: self._operation_tx(connection, operation_id))

    def phase_history(self, operation_id: str) -> tuple[TransitionPhase, ...]:
        return self.writer.call(lambda connection: tuple(TransitionPhase(row[0]) for row in connection.execute(
            "SELECT phase FROM cf_transition_events WHERE operation_id=? ORDER BY sequence", (operation_id,),
        )))

    def _event_tx(self, connection: sqlite3.Connection, operation: TransitionOperation) -> None:
        from coinfly.execution.evidence import event_tx

        connection.execute(
            "INSERT INTO cf_transition_events(operation_id,phase,recorded_at,error_code) VALUES(?,?,?,?)",
            (operation.operation_id, operation.phase, self._clock().isoformat(), operation.error_code),
        )
        context = operation.target_context if operation.phase in (TransitionPhase.READY_UNARMED, TransitionPhase.COMPLETED) else operation.source_context or operation.target_context
        event_tx(connection, context, "account-authority", "CONTEXT_" + operation.phase.value, operation.operation_id, operation.request_id)

    def _phase(self, operation_id: str, phase: TransitionPhase, *, error_code: str | None = None,
               failure: TransitionFailure | None = None) -> TransitionOperation:
        def save(connection: sqlite3.Connection) -> TransitionOperation:
            operation = self._operation_tx(connection, operation_id).model_copy(
                update={"phase": phase, "error_code": error_code, "failure": failure})
            connection.execute("UPDATE cf_transition_operations SET operation_json=? WHERE operation_id=?", (operation.model_dump_json(), operation_id))
            if phase in (TransitionPhase.QUIESCING, TransitionPhase.DRAINING, TransitionPhase.RECONCILING, TransitionPhase.BLOCKED_DRAIN):
                connection.execute("UPDATE cf_authorities SET engine_state='DRAINING' WHERE authority_id=? AND effective_context IS NOT NULL", (operation.authority_id,))
            self._event_tx(connection, operation)
            return operation
        operation = self.writer.call(save)
        self._notify()
        return operation

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                # Observer failure cannot undo a financial/control commit.
                # The IPC adapter must resnapshot clients after publication gaps.
                pass

    def attach_recovered_engine(self, engine: EngineLifecycle) -> None:
        """Supervisor calls only after owning/fencing the complete account authority."""
        with self.interlock.lock:
            status = self.status(engine.context.authority_id)
            if status.effective_context != engine.context or not status.recovery_required:
                raise DomainError("RECOVERY_CONTEXT_MISMATCH")
            engine.quiesce()
            self._engines[engine.context.identity] = engine
            def attach(connection: sqlite3.Connection) -> None:
                retired = connection.execute("SELECT 1 FROM ex_context_retirements WHERE context_id=?", (engine.context.identity,)).fetchone()
                state = EngineState.STOPPED if retired is not None else EngineState.DRAINING
                connection.execute(
                    "UPDATE cf_authorities SET recovery_required=0, engine_state=?, armed=0, admissions_fenced=1 WHERE authority_id=?",
                    (state, engine.context.authority_id),
                )
            self.writer.call(attach)
        self._notify()

    def refresh_retired_source_proof(self, operation_id: str) -> StopCertificate:
        """Obtain new read-only evidence without restoring retired mutation rights."""
        operation = self.operation(operation_id)
        source = operation.source_context
        if source is None:
            raise DomainError("SOURCE_CONTEXT_MISSING")
        source_config = self.status(operation.authority_id).effective_config
        if source_config is None:
            raise DomainError("SOURCE_CONFIGURATION_MISSING")
        raw = self.writer.call(lambda connection: connection.execute(
            "SELECT stop_proof FROM cf_transition_operations WHERE operation_id=?", (operation_id,),
        ).fetchone()[0])
        if not raw:
            raise DomainError("SOURCE_RETIREMENT_UNPROVEN")
        previous = StopCertificate.model_validate_json(raw)
        self._verify_retirement(source, previous)
        observer = self._engines.get(source.identity)
        if observer is None:
            raise DomainError("RETIRED_SOURCE_READ_ONLY_RECONCILIATION_REQUIRED")
        fresh = observer.reconcile()
        fresh.require_valid(source, source_config.execution_scope, self._clock())
        with self.interlock.lock:
            def save(connection: sqlite3.Connection) -> None:
                from coinfly.execution.evidence import event_tx

                self._verify_retirement_tx(connection, source, previous)
                if fresh.fact_sequence != previous.fact_sequence:
                    raise DomainError("STOP_PROOF_INVALIDATED")
                fresh.require_valid(source, source_config.execution_scope, self._clock())
                connection.execute("UPDATE cf_transition_operations SET stop_proof=? WHERE operation_id=?", (fresh.model_dump_json(), operation_id))
                event_tx(connection, source, "account-authority", "STOP_PROOF_REFRESHED", operation_id, fresh.proof_id)
            self.writer.call(save)
        self._notify()
        return fresh

    def _run(self, operation_id: str) -> None:
        operation = self.operation(operation_id)
        retired = False
        target_engine: EngineLifecycle | None = None
        target_retired = True
        stage = TransitionStage.SOURCE_PROOF_CHECK
        try:
            source = operation.source_context
            raw = self.writer.call(lambda connection: dict(connection.execute(
                "SELECT stop_proof,retirement,drain_policy FROM cf_transition_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()))
            source_engine = self._engines.get(source.identity) if source is not None else None
            source_config = self.status(operation.authority_id).effective_config
            if source is not None and raw["stop_proof"] and raw["retirement"]:
                proof = StopCertificate.model_validate_json(raw["stop_proof"])
                retirement = RetirementEvidence.model_validate_json(raw["retirement"])
                if source_config is None:
                    raise DomainError("SOURCE_CONFIGURATION_MISSING")
                retirement.require_retired(source)
                self._verify_retirement(source, proof)
                retired = True
                try:
                    proof.require_valid(source, source_config.execution_scope, self._clock())
                except DomainError as error:
                    if error.code not in ("STOP_PROOF_STALE", "STOP_PROOF_STALE_ACCOUNT"):
                        raise
                    proof = self.refresh_retired_source_proof(operation_id)
            elif source is not None:
                if source_engine is None or source_config is None:
                    raise DomainError("RECOVERY_ENGINE_REQUIRED")
                stage = TransitionStage.SOURCE_QUIESCE
                self._phase(operation_id, TransitionPhase.QUIESCING)
                source_engine.quiesce()
                stage = TransitionStage.SOURCE_DRAIN
                self._phase(operation_id, TransitionPhase.DRAINING)
                source_engine.drain(DrainPolicy.model_validate_json(raw["drain_policy"]))
                stage = TransitionStage.SOURCE_RECONCILE
                self._phase(operation_id, TransitionPhase.RECONCILING)
                proof = source_engine.reconcile()
                proof.require_valid(source, source_config.execution_scope, self._clock())
                stage = TransitionStage.SOURCE_RETIREMENT
                retirement = source_engine.retire()
                retirement.require_retired(source)
                self.transports.retire(source)
                with self.interlock.lock:
                    def stopped(connection: sqlite3.Connection) -> None:
                        from coinfly.execution.recovery import record_retirement_tx

                        proof.require_valid(source, source_config.execution_scope, self._clock())
                        record_retirement_tx(connection, source, proof.proof_id, proof.fact_sequence)
                        connection.execute(
                            "UPDATE cf_transition_operations SET stop_proof=?,retirement=? WHERE operation_id=?",
                            (proof.model_dump_json(), retirement.model_dump_json(), operation_id),
                        )
                        connection.execute(
                            "UPDATE cf_authorities SET engine_state='STOPPED', armed=0, recovery_required=0 WHERE authority_id=?",
                            (operation.authority_id,),
                        )
                    self.writer.call(stopped)
                retired = True
            if source is None:
                # Explicit first activation: nothing to stop or reset. Recording
                # STOPPED_OLD/RESETTING here would claim a source that never was.
                self._phase(operation_id, TransitionPhase.NO_PREVIOUS_CONTEXT)
            else:
                self._phase(operation_id, TransitionPhase.STOPPED_OLD)
                stage = TransitionStage.SOURCE_RESET
                self._phase(operation_id, TransitionPhase.RESETTING)
                if source_engine is not None:
                    source_engine.reset_transient()
            stage = TransitionStage.TARGET_VALIDATION
            self._validate_preview(operation.proposed_config)
            stage = TransitionStage.AUTHORITY_FENCE_CHECK
            self._require_no_authority_fence(operation.authority_id)
            self._phase(operation_id, TransitionPhase.INITIALIZING_NEW)
            stage = TransitionStage.NETWORK_HANDLES
            self.transports.begin(operation.target_context)
            stage = TransitionStage.ENGINE_FACTORY
            target_engine = self._factory(operation.proposed_config, operation.target_context)
            if target_engine.context != operation.target_context:
                raise DomainError("ENGINE_FACTORY_CONTEXT_MISMATCH")
            stage = TransitionStage.ENGINE_INITIALIZE
            target_engine.initialize(operation.proposed_config, operation.target_context)
            stage = TransitionStage.EFFECTIVE_PUBLICATION
            with self.interlock.lock:
                if self.interlock.is_provisionally_fenced(operation.authority_id):
                    raise DomainError("AUTHORITY_FENCED")
                def ready(connection: sqlite3.Connection) -> None:
                    from coinfly.execution.evidence import authority_fenced_tx

                    if authority_fenced_tx(connection, operation.authority_id):
                        raise DomainError("AUTHORITY_FENCED")
                    current = connection.execute("SELECT active_operation FROM cf_authorities WHERE authority_id=?", (operation.authority_id,)).fetchone()
                    if current[0] != operation_id:
                        raise DomainError("OPERATION_OWNERSHIP_LOST")
                    if source is not None:
                        assert source_config is not None
                        self._verify_retirement_tx(connection, source, proof)
                        proof.require_valid(source, source_config.execution_scope, self._clock())
                    connection.execute(
                        "UPDATE cf_authorities SET effective_context=?, effective_config=?, effective_revision=?, "
                        "engine_state='INITIALIZING',armed=0,admissions_fenced=1,recovery_required=0,active_operation=NULL WHERE authority_id=?",
                        (operation.target_context.model_dump_json(), operation.proposed_config.model_dump_json(), operation.proposed_revision, operation.authority_id),
                    )
                    ready_operation = self._operation_tx(connection, operation_id).model_copy(update={"phase": TransitionPhase.READY_UNARMED, "error_code": None})
                    connection.execute("UPDATE cf_transition_operations SET operation_json=? WHERE operation_id=?", (ready_operation.model_dump_json(), operation_id))
                    self._event_tx(connection, ready_operation)
                self.writer.call(ready)
                self._engines[operation.target_context.identity] = target_engine
                if source is not None:
                    self._engines.pop(source.identity, None)
            self._notify()
        except Exception as error:
            try:
                self.transports.retire(operation.target_context)
            except DomainError:
                target_retired = False
                self.interlock.provisional_fence(operation.authority_id, "NETWORK_RETIREMENT_UNPROVEN")
            if target_engine is not None:
                # A partially initialized target cannot keep mutation resources.
                try:
                    target_engine.quiesce()
                    target_engine.retire().require_retired(operation.target_context)
                except Exception:
                    target_retired = False
                    self.interlock.provisional_fence(operation.authority_id, "TARGET_RETIREMENT_UNPROVEN")
                    def record_incident(connection: sqlite3.Connection) -> None:
                        from coinfly.execution.evidence import fence_authority_tx

                        fence_authority_tx(connection, operation.target_context, "TARGET_RETIREMENT_UNPROVEN", operation_id)
                        connection.execute("UPDATE cf_authorities SET recovery_required=1 WHERE authority_id=?", (operation.authority_id,))
                    self.writer.call(record_incident)
            phase = TransitionPhase.FAILED_INITIALIZATION if retired or operation.source_context is None else TransitionPhase.BLOCKED_DRAIN
            failure = describe_failure(error, stage=stage, target_retired=target_retired, recorded_at=self._clock())
            logger.warning("Context operation %s stopped at %s/%s: %s (%s); retry_safe=%s action=%s",
                           operation_id, phase.value, stage.value, failure.code, failure.summary,
                           failure.retry_safe, failure.corrective_action.value)
            self._phase(operation_id, phase, error_code=failure.code, failure=failure)

    @staticmethod
    def _verify_retirement_tx(connection: sqlite3.Connection, source: ContextKey, proof: StopCertificate) -> None:
        from coinfly.execution.recovery import assert_context_closed_tx, context_fact_sequence_tx

        row = connection.execute("SELECT * FROM ex_context_retirements WHERE context_id=?", (source.identity,)).fetchone()
        if row is None or not row["valid"] or context_fact_sequence_tx(connection, source) != proof.fact_sequence:
            raise DomainError("STOP_PROOF_INVALIDATED")
        assert_context_closed_tx(connection, source)

    def _verify_retirement(self, source: ContextKey, proof: StopCertificate) -> None:
        self.writer.call(lambda connection: self._verify_retirement_tx(connection, source, proof))

    def _require_no_authority_fence(self, authority_id: str) -> None:
        from coinfly.execution.evidence import authority_fenced_tx

        with self.interlock.lock:
            if self.interlock.is_provisionally_fenced(authority_id) or self.writer.call(lambda connection: authority_fenced_tx(connection, authority_id)):
                raise DomainError("AUTHORITY_FENCED")

    def retry(self, operation_id: str) -> TransitionOperation:
        with self.interlock.lock:
            if self._closed:
                raise DomainError("COORDINATOR_CLOSED")
            operation = self.operation(operation_id)
            prior = self._futures.get(operation_id)
            if prior is not None and not prior.done():
                return operation
            if operation.phase in (TransitionPhase.READY_UNARMED, TransitionPhase.COMPLETED):
                raise DomainError("OPERATION_NOT_RETRYABLE")
            if self.status(operation.authority_id).operation is None:
                raise DomainError("OPERATION_OWNERSHIP_LOST")
            self._schedule(operation_id)
            return operation

    def _schedule(self, operation_id: str) -> None:
        # Caller holds the interlock, including callback registration. Receipts
        # and operation history stay durable; only running futures stay in RAM.
        future = self._executor.submit(self._run, operation_id)
        self._futures[operation_id] = future

        def completed(done: Future[None]) -> None:
            with self.interlock.lock:
                if self._futures.get(operation_id) is done:
                    self._futures.pop(operation_id)

        future.add_done_callback(completed)

    def wait(self, operation_id: str, timeout: float = 10) -> TransitionOperation:
        with self.interlock.lock:
            future = self._futures.get(operation_id)
        if future is not None:
            future.result(timeout=timeout)
            with self.interlock.lock:
                if self._futures.get(operation_id) is future:
                    self._futures.pop(operation_id)
        return self.operation(operation_id)

    def arm(self, authority_id: str, *, expected_revision: int, expected_epoch: int, request_id: str) -> ContextStatus:
        digest = _request_digest((authority_id, expected_revision, expected_epoch, "ARM"))
        with self.interlock.lock:
            status = self.status(authority_id)
            prior = self.writer.call(lambda connection: connection.execute(
                "SELECT kind,request_digest FROM cf_control_receipts WHERE authority_id=? AND request_id=?", (authority_id, request_id),
            ).fetchone())
            if prior is not None:
                if tuple(prior) != ("ARM", digest):
                    raise DomainError("REQUEST_ID_CONFLICT")
                return status
            if status.effective_revision != expected_revision or status.transition_epoch != expected_epoch:
                raise DomainError("CONFIGURATION_CONFLICT")
            if status.operation is not None or status.recovery_required or status.effective_context is None:
                raise DomainError("CONTEXT_NOT_READY")
            if status.engine_state not in (EngineState.INITIALIZING, EngineState.ACTIVE) or status.armed:
                raise DomainError("CONTEXT_NOT_READY")
            self._require_no_authority_fence(authority_id)
            if status.effective_config is None:
                raise DomainError("CONTEXT_NOT_READY")
            self._validate_preview(status.effective_config)
            engine = self._engines.get(status.effective_context.identity)
            if engine is None:
                raise DomainError("RECOVERY_ENGINE_REQUIRED")
            engine.arm()
            def commit(connection: sqlite3.Connection) -> None:
                from coinfly.execution.evidence import authority_fenced_tx, event_tx

                if authority_fenced_tx(connection, authority_id):
                    raise DomainError("AUTHORITY_FENCED")
                connection.execute("INSERT INTO cf_control_receipts VALUES(?,?,?,?)", (authority_id, request_id, "ARM", digest))
                connection.execute("UPDATE cf_authorities SET armed=1,admissions_fenced=0,engine_state='ACTIVE' WHERE authority_id=?", (authority_id,))
                assert status.effective_context is not None
                event_tx(connection, status.effective_context, "account-authority", "CONTEXT_ARMED", authority_id, request_id)
            self.writer.call(commit)
            result = self.status(authority_id)
        self._notify()
        return result

    def pause_entries(self, authority_id: str) -> ContextStatus:
        """Durably fence entries while keeping source protection/drain services alive."""
        with self.interlock.lock:
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_authorities SET armed=0,admissions_fenced=1 WHERE authority_id=?", (authority_id,),
            ).rowcount)
            status = self.status(authority_id)
        self._notify()
        return status

    def close(self) -> None:
        with self.interlock.lock:
            self._closed = True
        self._executor.shutdown(wait=True)
