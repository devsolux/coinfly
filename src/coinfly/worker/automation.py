"""Worker-owned autonomous lifecycle: observe -> decide -> propose -> admit -> execute.

The desktop sends revision-bound commands (configure, enable, pause entries,
resume, stop-and-reconcile) and observes immutable status; it never runs a
decision. One controller instance owns scheduling, strategy/controller
lifecycle, slot reconciliation and execution admission for the effective
context, serialized by one lock so configuration changes, ticks, UI refreshes
and supervisor recovery can never overlap.

Lifecycle states (documented in docs/autonomous-lifecycle.md):

    NOT_CONFIGURED -> SETUP -> WARMING_UP -> READY -> OBSERVING -> RUNNING
    RUNNING <-> PAUSED_ENTRIES ; RUNNING -> DRAINING -> STOPPED ; any -> RECOVERING / FAILED

Every component (storage, dataset, kernel, feed, strategy, execution adapter,
account authority) reports its own readiness; a recoverable component problem
degrades the state without hiding the others.

Lock model (liveness): the control lock serializes configuration, the control
phases of a tick and status publication, all of which are short. Neural
computation runs *outside* that lock, bound to generation tokens (authority,
context epoch, configuration revision, slot identity, neural-state generation,
frame digest, observation freshness) that are revalidated before admission; a
result whose tokens no longer hold is discarded and its working neural state
is disposed without advancing the slot's committed generation. Safety commands
(pause, stop) and ``status`` therefore never wait for a propagation to finish.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from coinfly.config.models import EngineConfig
from coinfly.control.context_coordinator import ContextCoordinator
from coinfly.control.slot_service import SlotService
from coinfly.domain.context import (
    ContextKey,
    DomainError,
    EngineState,
    Environment,
    FrozenModel,
    ProductTrack,
)
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.execution.engine import require_execution_engine
from coinfly.execution.intent_causes import cause_summary_tx
from coinfly.market.feed import MarketFeed, stale_after
from coinfly.market.observations import DataSource, FeedState, InstrumentCatalogEntry, MarketObservation
from coinfly.observability.diagnostics import failure_record
from coinfly.research.neural_adapter import FixedDecoderWeightAdapter
from coinfly.simulation.paper_matching import DepthLevel, PaperInstrument
from coinfly.storage.component_schema import install_component_schema
from coinfly.storage.writer import Writer
from coinfly.strategy.contracts import InstrumentWeight
from coinfly.strategy.controllers import ControllerUnavailable, DecisionController
from coinfly.strategy.definition import ControllerProfile, MappingVersion, StrategyDefinition
from coinfly.strategy.derivatives_mapping import FixedDecoderDerivativesAdapter
from coinfly.strategy.ownership import StrategyOwnership
from coinfly.strategy.reward_ledger import PendingWindow, RewardLedger
from coinfly.strategy.sensory import SensoryFrameBuilder
from coinfly.strategy.target_coordinator import AllocationLimits, TargetCoordinator
from coinfly.strategy.turnover_ledger import TurnoverLedger
from coinfly.strategy.universe import UniverseSelection, plan_subscription, select_universe

logger = logging.getLogger(__name__)


class AutomationState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SETUP = "SETUP"
    WARMING_UP = "WARMING_UP"
    READY = "READY"
    OBSERVING = "OBSERVING"
    RUNNING = "RUNNING"
    PAUSED_ENTRIES = "PAUSED_ENTRIES"
    DRAINING = "DRAINING"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class FeedSelection(FrozenModel):
    kind: DataSource = DataSource.COINBASE_PUBLIC_REST
    # Name of a recording under the application's datasets/replays folder.
    replay_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
    fixture_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,40}$")


RECOVERY_POLICY_VERSION = 1


class RecoveryPolicy(FrozenModel):
    """Separately versioned, explicit opt-in resumption after a worker restart.

    The default stays manual re-enable. With ``auto_resume`` the worker may
    re-enable itself after a restart, but only inside the bounded mandate: the
    mandate has not expired, fewer than ``max_resumptions`` resumptions were
    used, and every reconciliation check passed (no unknown outcomes, no
    non-terminal intents, the ledger sequence intact, the infrastructure ready,
    a controller mode that can be restored — never a research session). A live
    context additionally needs an unexpired live mandate. Every resumption is
    recorded with the checks that admitted it.
    """

    policy_version: int = Field(default=RECOVERY_POLICY_VERSION, ge=1, le=RECOVERY_POLICY_VERSION)
    auto_resume: bool = False
    mandate_expires_at: datetime | None = None
    max_resumptions: int = Field(default=1, ge=1, le=100)
    require_flat: bool = False

    @model_validator(mode="after")
    def bounded(self) -> RecoveryPolicy:
        if self.auto_resume and self.mandate_expires_at is None:
            raise ValueError("automatic resumption needs an explicit mandate expiry")
        if self.mandate_expires_at is not None and self.mandate_expires_at.tzinfo is None:
            raise ValueError("aware mandate expiry required")
        return self


class AutomationConfig(FrozenModel):
    strategy: StrategyDefinition
    approved_capital: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    feed: FeedSelection = Field(default_factory=FeedSelection)
    research_mode: bool = False
    auto_manage_slots: bool = True
    # Explicit pipeline test mode: the only way a scripted fixture controller
    # (``fixture-sequence-v1``) can be configured. It is SANDBOX-only, persisted
    # with the configuration and shown in every status block, so a scripted
    # run can never be mistaken for a neural one.
    pipeline_test_mode: bool = False
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    configured_at: datetime
    request_id: str = Field(min_length=1, max_length=160)

    @property
    def plan_digest(self) -> str:
        return sha256(self.model_dump_json(exclude={"configured_at", "request_id"}).encode()).hexdigest()


class ComponentReadiness(FrozenModel):
    component: str
    ready: bool
    code: str
    detail: str = ""


class SlotDecisionRecord(FrozenModel):
    slot_id: str
    instrument: str
    decided_at: datetime
    side: str
    controller_profile: str
    pipeline_fixture: bool
    admission_reason: str
    outcome: str
    outcome_code: str = ""
    intent_ids: tuple[str, ...] = ()
    frame_sha256: str = ""
    compute_seconds: float = 0.0
    # Decision provenance: graph/kernel identity, controller mode, checkpoint or
    # untrained baseline, feature-transform version, native frame counters,
    # neural-state generation and the generation tokens the result was bound to.
    provenance: dict[str, Any] = Field(default_factory=dict)


FEATURE_TRANSFORM_VERSION = "sensory-frame-v1"


class ComputeRequest(FrozenModel):
    """One bounded neural computation, bound to the tokens revalidated before admission."""

    slot_id: str
    instrument: str
    decision_id: str
    decided_at: datetime
    frame_sha256: str
    observed_through: datetime
    observation_sequence: int
    epoch: int
    revision: int
    control_generation: int
    state_generation: int
    # RESEARCH_LEARNING: the reward window (its start) delivered with this observation.
    reward_window_start: datetime | None = None


class SlotStatus(FrozenModel):
    slot_id: str
    instrument: str
    strategy: str
    selection_reason: str
    lifecycle: str
    ownership_state: str
    target_weight: str
    actual_quantity: str
    allocated_budget: str
    warm: bool
    observations: int
    last_observation_at: datetime | None
    last_decision: SlotDecisionRecord | None
    last_execution: str
    blocking_reason: str


# Versioned, additive schema (see coinfly.storage.component_schema): version 1
# is the original table set, version 2 adds decision provenance. Each version
# applies in one transaction and is recorded in cf_schema_contracts.
AUTOMATION_SCHEMA_COMPONENT = "automation"
AUTOMATION_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS cf_automation (authority_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, config_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, entries_paused INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, allocation_revision INTEGER NOT NULL DEFAULT 0, targets_json TEXT NOT NULL DEFAULT '{}', restart_reason TEXT, enable_request_id TEXT, reactivation_operation_id TEXT, updated_at TEXT NOT NULL)",
    'CREATE TABLE IF NOT EXISTS cf_automation_receipts (request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL, context_id TEXT NOT NULL, recorded_at TEXT NOT NULL)',
    "CREATE TABLE IF NOT EXISTS cf_automation_decisions (decision_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, slot_id TEXT NOT NULL, instrument TEXT NOT NULL, decided_at TEXT NOT NULL, side TEXT NOT NULL, controller_profile TEXT NOT NULL, pipeline_fixture INTEGER NOT NULL, admission_reason TEXT NOT NULL, outcome TEXT NOT NULL, outcome_code TEXT NOT NULL, intent_ids TEXT NOT NULL, frame_sha256 TEXT NOT NULL, compute_seconds REAL NOT NULL, provenance_json TEXT NOT NULL DEFAULT '{}')",
    'CREATE TABLE IF NOT EXISTS cf_automation_orders (intent_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, decision_id TEXT NOT NULL, instrument TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL, outcome_code TEXT NOT NULL, filled_quantity TEXT NOT NULL, resolved_at TEXT, decided_at TEXT, reference_price TEXT, reference_at TEXT, reference_available_at TEXT)',
    'CREATE TABLE IF NOT EXISTS cf_automation_recoveries (recovery_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, context_id TEXT, policy_version INTEGER NOT NULL, decided_at TEXT NOT NULL, resumed INTEGER NOT NULL, reason TEXT NOT NULL, checks_json TEXT NOT NULL, request_id TEXT)',
)

FeedFactory = Callable[[EngineConfig, AutomationConfig], MarketFeed]
ControllerFactory = Callable[[StrategyDefinition, Environment, bool], DecisionController]


class AutomationController:
    def __init__(
        self, *, writer: Writer, coordinator: ContextCoordinator, slots: SlotService, engines: Mapping[str, Any],
        authority_id: str, feed_factory: FeedFactory, controller_factory: ControllerFactory,
        dataset_ready: Callable[[], tuple[bool, str]], kernel_ready: Callable[[], tuple[bool, str]],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_change: Callable[[], None] | None = None, tick_interval_seconds: float = 1.0, loop_enabled: bool = True,
    ) -> None:
        self.writer, self.coordinator, self.slots, self._engines = writer, coordinator, slots, engines
        self.authority_id = authority_id
        self._feed_factory, self._controller_factory = feed_factory, controller_factory
        self._dataset_ready, self._kernel_ready = dataset_ready, kernel_ready
        self._clock, self._on_change = clock, on_change
        self._tick_interval = tick_interval_seconds
        # Tests drive tick(now) deterministically; production runs the loop thread.
        self.loop_enabled = loop_enabled
        self._lock = threading.RLock()
        self.ownership = StrategyOwnership(writer, slots)
        self._feed: MarketFeed | None = None
        self._feed_context: ContextKey | None = None
        self._controller: DecisionController | None = None
        self._controller_error: str | None = None
        self._component_plan_digest: str | None = None
        self._targets: TargetCoordinator | None = None
        self._builders: dict[str, SensoryFrameBuilder] = {}
        self._last_decision_at: dict[str, datetime] = {}
        self._decisions: dict[str, SlotDecisionRecord] = {}
        self._selection: UniverseSelection | None = None
        self._components: tuple[ComponentReadiness, ...] = ()
        self._state = AutomationState.NOT_CONFIGURED
        self._failure: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._closed = False
        self._ticks = 0
        self._last_tick_at: datetime | None = None
        self._last_observation_at: datetime | None = None
        self._observation_counts: dict[str, int] = {}
        self._last_reconciliation: dict[str, Any] | None = None
        self._catalog: tuple[InstrumentCatalogEntry, ...] = ()
        # Liveness: one tick at a time; compute runs outside the control lock.
        self._tick_lock = threading.Lock()
        self._compute_budget_seconds: float | None = None
        self._frames: dict[str, Any] = {}
        # RESEARCH_LEARNING session: reward windows from the paper ledger, FIFO.
        self._reward_ledger: RewardLedger | None = None
        self._pending_rewards: dict[str, PendingWindow] = {}
        self._research_session: str | None = None
        self._control_generation = 0
        self._controller_failed: str | None = None
        self._compute: dict[str, Any] = {"last_seconds": 0.0, "max_seconds": 0.0, "deferred": 0, "discarded": 0,
                                         "queue_age_seconds": 0.0, "observations": 0}
        self._due_since: dict[str, datetime] = {}
        self._published: dict[str, Any] = {}
        self._ledger: TurnoverLedger | None = None
        self._expired_orders: list[dict[str, Any]] = []
        self._settlements: list[dict[str, Any]] = []
        self._last_recovery: dict[str, Any] | None = None
        self._verified_engines: set[int] = set()
        # Feed start/subscription: a failure degrades the feed component and is
        # retried with a bounded back-off; it never fails the control plane.
        self._feed_failures = 0
        self._feed_retry_at: datetime | None = None
        self._feed_problem: dict[str, Any] | None = None
        self.writer.call(self._schema)
        self._mark_restart()
        self._publish_status()

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        install_component_schema(connection, AUTOMATION_SCHEMA_COMPONENT, AUTOMATION_SCHEMA)

    # ------------------------------------------------------------------ persistence
    def _mark_restart(self) -> None:
        """A restart never resumes automation: the operator re-enables explicitly."""
        def save(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE cf_automation SET enabled=0, state='RECOVERING', restart_reason='RESTART_REQUIRES_REENABLE', updated_at=? "
                               "WHERE enabled=1", (self._clock().isoformat(),))
        self.writer.call(save)

    def _row(self, context: ContextKey | None = None) -> sqlite3.Row | None:
        row = self.writer.call(lambda connection: connection.execute(
            "SELECT * FROM cf_automation WHERE authority_id=?", (self.authority_id,)).fetchone())
        return row if isinstance(row, sqlite3.Row) else None

    def config(self, context: ContextKey | None = None) -> AutomationConfig | None:
        row = self._row()
        return AutomationConfig.model_validate_json(row["config_json"]) if row is not None else None

    @staticmethod
    def _check_strategy(strategy: StrategyDefinition, context: ContextKey, effective: EngineConfig) -> None:
        if context.environment not in strategy.permitted_environments:
            raise DomainError("STRATEGY_ENVIRONMENT_NOT_PERMITTED")
        if strategy.product_track is not context.product_track or strategy.product_subtype is not effective.effective_subtype:
            raise DomainError("STRATEGY_PRODUCT_MISMATCH")
        if effective.reporting_currency not in strategy.quote_currencies:
            raise DomainError("STRATEGY_QUOTE_MISMATCH")
        if strategy.controller_profile is ControllerProfile.FIXTURE_SEQUENCE_V1 and context.environment is not Environment.SANDBOX:
            raise DomainError("FIXTURE_CONTROLLER_SANDBOX_ONLY")

    def _effective(self) -> tuple[ContextKey | None, EngineConfig | None]:
        status = self.coordinator.status(self.authority_id)
        return status.effective_context, status.effective_config

    def _engine(self, context: ContextKey) -> Any:
        engine = self._engines.get(context.identity)
        if engine is None:
            raise DomainError("RECOVERY_ENGINE_REQUIRED")
        if id(engine) not in self._verified_engines:
            require_execution_engine(engine)  # the automation dispatches through one interface, paper or live
            self._verified_engines.add(id(engine))
        return engine

    # ------------------------------------------------------------------ commands
    def configure(self, config: AutomationConfig) -> dict[str, Any]:
        with self._lock:
            context, effective = self._effective()
            if context is None or effective is None:
                raise DomainError("NO_EFFECTIVE_CONTEXT")
            strategy = config.strategy
            self._check_strategy(strategy, context, effective)
            if config.research_mode and not strategy.research_only:
                raise DomainError("RESEARCH_MODE_REQUIRES_RESEARCH_STRATEGY")
            if strategy.learning_enabled and not config.research_mode:
                raise DomainError("LEARNING_REQUIRES_RESEARCH_MODE")
            if strategy.controller_profile is ControllerProfile.FIXTURE_SEQUENCE_V1 and not config.pipeline_test_mode:
                raise DomainError("FIXTURE_REQUIRES_TEST_MODE",
                                  "FIXTURE_REQUIRES_TEST_MODE: scripted decisions run only in explicit pipeline test mode")
            if config.pipeline_test_mode and context.environment is not Environment.SANDBOX:
                raise DomainError("TEST_MODE_REQUIRES_SANDBOX")
            if config.feed.kind is DataSource.REPLAY_FILE and not config.feed.replay_name:
                # Refused here, where the operator can correct it, instead of
                # failing the loop at the first tick after Enable.
                raise DomainError("REPLAY_NAME_REQUIRED", "REPLAY_NAME_REQUIRED: name a recording under datasets/replays")
            stored = config.model_dump_json()
            # Idempotency digest: the request content, not the receipt time.
            digest = config.model_dump_json(exclude={"configured_at"})
            def save(connection: sqlite3.Connection) -> dict[str, Any]:
                receipt = connection.execute("SELECT kind,digest FROM cf_automation_receipts WHERE request_id=?", (config.request_id,)).fetchone()
                if receipt is not None:
                    if tuple(receipt) != ("CONFIGURE", digest):
                        raise DomainError("REQUEST_ID_CONFLICT")
                    return {"replayed": True}
                row = connection.execute("SELECT enabled,config_json FROM cf_automation WHERE authority_id=?", (self.authority_id,)).fetchone()
                if row is not None and row["enabled"]:
                    previous = AutomationConfig.model_validate_json(row["config_json"])
                    if previous.plan_digest != config.plan_digest:
                        raise DomainError("AUTOMATION_STOP_REQUIRED")
                self.ownership.approve_budget_tx(connection, context, strategy, config.approved_capital, now=self._clock())
                connection.execute(
                    "INSERT INTO cf_automation(authority_id,context_id,config_json,enabled,entries_paused,state,updated_at) VALUES(?,?,?,0,0,?,?) "
                    "ON CONFLICT(authority_id) DO UPDATE SET config_json=excluded.config_json, context_id=excluded.context_id, "
                    "updated_at=excluded.updated_at, "
                    "state=CASE WHEN cf_automation.enabled=1 THEN cf_automation.state ELSE excluded.state END",
                    (self.authority_id, context.identity, stored, AutomationState.SETUP.value, self._clock().isoformat()))
                connection.execute("INSERT INTO cf_automation_receipts VALUES(?,?,?,?,?)",
                                   (config.request_id, "CONFIGURE", digest, context.identity, self._clock().isoformat()))
                return {"replayed": False}
            result = self.writer.call(save)
            if result["replayed"]:
                return {**result, "strategy": strategy.identity, "state": self._state.value}
            if self._component_plan_digest is not None and self._component_plan_digest != config.plan_digest:
                # Stop the old components before a new plan can enter the loop.
                if self._feed is not None:
                    self._feed.stop()
                self._feed = None
                self._feed_context = None
                self._drop_controller()
                self._builders.clear()
                self._targets = None
                self._last_decision_at.clear()
                self._component_plan_digest = None
            if not result["replayed"] and self._state in (AutomationState.NOT_CONFIGURED, AutomationState.STOPPED, AutomationState.RECOVERING):
                self._state = AutomationState.SETUP
                self._failure = None
            self._control_generation += 1
            self._publish_status()
        self._notify()
        return {**result, "strategy": strategy.identity, "state": self._state.value}

    def enable(self, *, expected_revision: int, expected_epoch: int, request_id: str) -> dict[str, Any]:
        """Explicit authorization: arm the context and start the worker-owned loop."""
        with self._lock:
            if self._closed:
                raise DomainError("AUTOMATION_CLOSED")
            context, effective = self._effective()
            if context is None or effective is None:
                raise DomainError("NO_EFFECTIVE_CONTEXT")
            config = self.config()
            if config is None:
                raise DomainError("AUTOMATION_NOT_CONFIGURED")
            self._check_strategy(config.strategy, context, effective)
            status = self.coordinator.status(self.authority_id)
            if status.effective_revision != expected_revision or status.transition_epoch != expected_epoch:
                raise DomainError("CONFIGURATION_CONFLICT")
            if context.environment is Environment.LIVE:
                # Live automation needs the explicit mandate on top of every other
                # gate; a missing one is reported, never assumed.
                from coinfly.operations.live_arming import LiveMandateStore

                if LiveMandateStore(self.writer).covering(effective, now=self._clock()) is None:
                    raise DomainError("LIVE_MANDATE_REQUIRED")
            if status.recovery_required:
                raise DomainError("RECOVERY_REQUIRED")
            if status.operation is not None and status.operation.in_progress:
                raise DomainError("TRANSITION_IN_PROGRESS")
            if status.operation is not None and status.operation.retryable:
                raise DomainError("TRANSITION_FAILED_RETRY_REQUIRED")
            reactivation: str | None = None
            if status.engine_state in (EngineState.DRAINING, EngineState.STOPPED):
                # A recovered (quiesced) or stopped context never re-arms by itself.
                # Enabling automation is the explicit operator mandate: re-apply the
                # SAME effective configuration (no reseed) and arm once it is READY.
                preview = self.coordinator.preview(effective, expected_revision=expected_revision, expected_epoch=expected_epoch)
                operation = self.coordinator.apply(preview.preview_id, preview.config_digest, expected_revision=expected_revision,
                                                   expected_epoch=expected_epoch, request_id=f"automation-reactivate:{request_id}")
                reactivation = operation.operation_id
            elif not status.armed:
                self.coordinator.arm(self.authority_id, expected_revision=expected_revision, expected_epoch=expected_epoch,
                                     request_id=str(uuid5(NAMESPACE_URL, f"coinfly:automation-arm:{request_id}")))
            def save(connection: sqlite3.Connection) -> None:
                connection.execute("UPDATE cf_automation SET enabled=1, entries_paused=0, state=?, restart_reason=NULL, "
                                   "enable_request_id=?, reactivation_operation_id=?, updated_at=? WHERE authority_id=?",
                                   (AutomationState.RECOVERING.value if reactivation else AutomationState.SETUP.value, request_id,
                                    reactivation, self._clock().isoformat(), self.authority_id))
                connection.execute("INSERT OR IGNORE INTO cf_automation_receipts VALUES(?,?,?,?,?)",
                                   (request_id, "ENABLE", str(expected_epoch), context.identity, self._clock().isoformat()))
            self.writer.call(save)
            if reactivation:
                self._state = AutomationState.RECOVERING
            elif self._state in (AutomationState.NOT_CONFIGURED, AutomationState.STOPPED, AutomationState.RECOVERING, AutomationState.FAILED, AutomationState.PAUSED_ENTRIES):
                self._state = AutomationState.SETUP
            self._failure = None
            if self._controller_failed is not None and self._controller is not None:
                # Explicit re-enable after an inference failure rebuilds the controller
                # (a research session starts over: its reward windows are not resumed).
                self._drop_controller()
            self._control_generation += 1
            self._start_loop()
            self._publish_status()
        self._notify()
        return {"enabled": True, "state": self._state.value, "reactivation_operation_id": reactivation}

    def pause_entries(self) -> dict[str, Any]:
        with self._lock:
            context, _ = self._effective()
            self.coordinator.pause_entries(self.authority_id)
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET entries_paused=1, updated_at=? WHERE authority_id=?",
                (self._clock().isoformat(), self.authority_id)).rowcount)
            if self._state in (AutomationState.RUNNING, AutomationState.OBSERVING, AutomationState.READY, AutomationState.WARMING_UP):
                self._state = AutomationState.PAUSED_ENTRIES
            self._control_generation += 1
            self._publish_status()
        self._notify()
        return {"entries_paused": True, "state": self._state.value}

    def resume_entries(self, *, expected_revision: int, expected_epoch: int, request_id: str) -> dict[str, Any]:
        return self.enable(expected_revision=expected_revision, expected_epoch=expected_epoch, request_id=request_id)

    def stop_and_reconcile(self) -> dict[str, Any]:
        """Fence entries, retire every owned slot through its protected drain, then stop."""
        with self._lock:
            context, effective = self._effective()
            self.coordinator.pause_entries(self.authority_id)
            retiring: list[str] = []
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET enabled=0, entries_paused=1, state=?, reactivation_operation_id=NULL, updated_at=? WHERE authority_id=?",
                (AutomationState.DRAINING.value, self._clock().isoformat(), self.authority_id)).rowcount)
            if context is not None:
                config = self.config()
                if config is not None:
                    empty = UniverseSelection(strategy_identity=config.strategy.identity, selected=(), reasons=(),
                                              selected_at=self._clock(), inputs_digest="0" * 64)
                    scope = effective.execution_scope[0] if effective else ""
                    result = self.ownership.reconcile(context=context, scope_id=scope, definition=config.strategy, selection=empty,
                                                      config_revision=self.coordinator.status(self.authority_id).effective_revision,
                                                      now=self._clock(), may_start=False, start_epoch=context.epoch, retire_immediately=True)
                    retiring = list(result.retiring)
            self._state = AutomationState.DRAINING if retiring else AutomationState.STOPPED
            self._stop_event.set()
            self._control_generation += 1
            self._publish_status()
        self._notify()
        return {"state": self._state.value, "retiring": retiring, "flat_claimed": False}

    # ------------------------------------------------------------------ loop
    def _start_loop(self) -> None:
        self._stop_event.clear()
        if not self.loop_enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="coinfly-automation", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set() and not self._closed:
            try:
                self.tick(self._clock())
            except Exception as error:  # the tick already recorded FAILED; never crash the thread
                logger.warning("Automation tick failed outside its guard: %s", type(error).__name__)
            self._stop_event.wait(self._tick_interval)

    def tick(self, now: datetime) -> dict[str, Any]:
        """One iteration: control phase A, then per due slot (lock-free compute → locked commit).

        Deterministic for tests (call with an explicit clock), scheduled for
        production. Only one tick runs at a time; commands and status never wait
        for a computation. Slots are computed one after another and each result
        is committed or discarded before the shared brain is swapped to the next
        slot, so an uncommitted working state is never overwritten by another
        instrument's observation.
        """
        if not self._tick_lock.acquire(blocking=False):
            raise DomainError("TICK_IN_PROGRESS")
        try:
            with self._lock:
                if self._closed:
                    raise DomainError("AUTOMATION_CLOSED")
                self._ticks += 1
                self._last_tick_at = now
                try:
                    report, requests = self._tick(now)
                except Exception as error:
                    report, requests = self._fail(error, now), ()
            if requests:
                # One slot at a time: its computation runs lock-free, then its
                # result is revalidated and committed (or discarded) under the
                # lock BEFORE the next slot's computation swaps the shared brain
                # to another slot's state. Commands interleave between slots.
                started = time.perf_counter()
                budget = self._compute_budget_seconds
                for index, request in enumerate(requests):
                    result = self._compute_one(request, index=index, started=started, budget=budget)
                    with self._lock:
                        try:
                            report = self._admit_results(now, [result], report)
                        except Exception as error:
                            report = self._fail(error, now)
                            break
                total = time.perf_counter() - started
                self._compute["last_seconds"] = total
                self._compute["max_seconds"] = max(float(self._compute["max_seconds"]), total)
            with self._lock:
                self._sample_reward_ledger()
                self._publish_status()
        finally:
            with self._lock:
                for pending_request in locals().get("requests", ()):
                    self._frames.pop(pending_request.decision_id, None)
                    self._pending_rewards.pop(pending_request.decision_id, None)
            self._tick_lock.release()
        self._notify()
        return report

    def _fail(self, error: Exception, now: datetime) -> dict[str, Any]:
        """Control-plane failure: fence entries and stop the loop; nothing else is guessed."""
        stage = self._state.value if self._state is not AutomationState.FAILED else \
            str((self._failure or {}).get("stage") or AutomationState.SETUP.value)
        self._failure = failure_record(error, stage=stage, recorded_at=now)
        # A non-domain exception is a defect, not an operating condition: say
        # where the redacted chain is and that re-enabling retries from SETUP.
        self._failure["corrective_action"] = "ACTION_REENABLE_AUTOMATION" if isinstance(error, DomainError) \
            else "ACTION_INSPECT_WORKER_LOG"
        self._failure["retry_safe"] = True
        # Redacted: the chain of exception types and the code location, never exception text.
        origin = self._failure.get("origin")
        logger.warning("Automation entered FAILED at %s: %s%s", stage, self._failure.get("summary"),
                       f" at {origin}" if origin else "")
        self._state = AutomationState.FAILED
        try:
            self.coordinator.pause_entries(self.authority_id)
        except Exception:
            pass
        self._stop_event.set()
        return {"state": self._state.value, "failure": self._failure}

    def _compute_one(self, request: ComputeRequest, *, index: int, started: float, budget: float | None) -> tuple[ComputeRequest, Any, str | None]:
        """Run one bounded neural computation WITHOUT the control lock.

        A controller failure is captured per request (the loop and every safety
        service keep running); the compute budget defers what does not fit.
        """
        controller = self._controller
        elapsed = time.perf_counter() - started
        if index and budget is not None and elapsed > budget:
            return request, None, "DEFERRED_COMPUTE_BUDGET"
        if controller is None or self._controller_failed is not None:
            return request, None, "CONTROLLER_UNAVAILABLE"
        frame = self._frames.get(request.decision_id)
        if frame is None:
            return request, None, "FRAME_MISSING"
        pending = self._pending_rewards.get(request.decision_id)
        try:
            controller.admit_slot(request.instrument)
            decision = controller.observe(frame, reinforcement="none", decision_id=request.decision_id, slot_key=request.instrument,
                                          outcome=pending.window if pending is not None else None)
            return request, decision, None
        except Exception as error:
            code = f"CONTROLLER_OBSERVE_FAILED:{type(error).__name__}" if not isinstance(error, DomainError) else error.code
            self._controller_failed = code
            try:
                controller.discard(request.instrument)
            except Exception:
                pass
            return request, None, code

    def _readiness(self, context: ContextKey | None, config: AutomationConfig | None) -> tuple[ComponentReadiness, ...]:
        components: list[ComponentReadiness] = []
        try:
            self.writer.call(lambda connection: connection.execute("SELECT 1").fetchone())
            components.append(ComponentReadiness(component="storage", ready=True, code="STORAGE_READY"))
        except DomainError as error:
            components.append(ComponentReadiness(component="storage", ready=False, code=error.code))
        dataset_ok, dataset_code = self._dataset_ready()
        components.append(ComponentReadiness(component="dataset", ready=dataset_ok, code=dataset_code))
        kernel_ok, kernel_code = self._kernel_ready()
        components.append(ComponentReadiness(component="kernel", ready=kernel_ok, code=kernel_code,
                                             detail="" if kernel_ok else ("Native execution of the full graph is required before neural "
                                                                          "automation is ready; no Python or reduced-graph substitute runs.")))
        if self._feed is None:
            components.append(ComponentReadiness(component="feed", ready=False, code="FEED_NOT_STARTED"))
        elif self._feed_problem is not None:
            components.append(ComponentReadiness(component="feed", ready=False, code=str(self._feed_problem["code"]),
                                                 detail=f"{self._feed_problem['detail']}; retry at {self._feed_problem['retry_at']}"))
        else:
            health = self._feed.health()
            fresh = health.last_message_at is not None and config is not None and \
                (self._clock() - health.last_message_at) <= timedelta(seconds=config.strategy.market_data.max_observation_age_seconds * 3)
            components.append(ComponentReadiness(component="feed", ready=health.state is FeedState.CONNECTED and fresh,
                                                 code=health.state.value if health.state is not FeedState.CONNECTED else ("FEED_FRESH" if fresh else "FEED_STALE"),
                                                 detail=health.source.label))
        if config is None:
            components.append(ComponentReadiness(component="strategy", ready=False, code="AUTOMATION_NOT_CONFIGURED"))
        elif self._controller is None:
            components.append(ComponentReadiness(component="strategy", ready=False, code=self._controller_error or "CONTROLLER_NOT_BUILT"))
        elif self._controller_failed is not None:
            components.append(ComponentReadiness(component="strategy", ready=False, code=self._controller_failed,
                                                 detail="Inference failed; re-enable rebuilds the controller. Safety services continue."))
        else:
            components.append(ComponentReadiness(component="strategy", ready=True, code="CONTROLLER_READY",
                                                 detail=self._controller.profile.value))
        engine = self._engines.get(context.identity) if context is not None else None
        components.append(ComponentReadiness(component="execution_adapter", ready=engine is not None,
                                             code="PAPER_ENGINE_READY" if engine is not None else "NO_EFFECTIVE_CONTEXT"))
        status = self.coordinator.status(self.authority_id)
        authority_ready = status.armed and not status.admissions_fenced and not status.recovery_required and status.operation is None
        code = "ARMED" if authority_ready else ("RECOVERY_REQUIRED" if status.recovery_required else
                                                "TRANSITION_IN_PROGRESS" if status.operation is not None else
                                                "ENTRIES_FENCED" if status.admissions_fenced else "UNARMED")
        components.append(ComponentReadiness(component="account_authority", ready=authority_ready, code=code))
        return tuple(components)

    def _ensure_feed(self, context: ContextKey, effective: EngineConfig, config: AutomationConfig) -> MarketFeed:
        if self._feed is not None and self._feed_context == context:
            return self._feed
        if self._feed is not None:
            self._feed.stop()
        feed = self._feed_factory(effective, config)
        self._feed, self._feed_context = feed, context
        self._feed_failures, self._feed_retry_at, self._feed_problem = 0, None, None
        self._component_plan_digest = config.plan_digest
        self.coordinator.transports.register(context, _FeedTransport(feed))
        return feed

    def _subscription_keep(self, context: ContextKey, config: AutomationConfig) -> tuple[str, ...]:
        """Instruments the feed must carry regardless of ranking: owned slots and open positions (marks)."""
        owned = [slot.instrument for slot in self.ownership.owned(context, config.strategy) if slot.state != "RETIRED"]
        held = self.writer.call(lambda connection: [str(row[0]) for row in connection.execute(
            "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (context.identity,)) if Decimal(str(row[1])) != 0])
        return tuple(dict.fromkeys([*owned, *sorted(held)]))

    def _subscription_capacity(self, feed: MarketFeed, config: AutomationConfig) -> int:
        capacity = int(getattr(feed, "max_instruments", 200))
        if getattr(feed, "source", None) is DataSource.COINBASE_PUBLIC_REST:
            # A REST sweep polls each book once, one after another (≥ 0.15 s
            # apart, plus the round trip): keep the sweep short enough that
            # every book stays inside the strategy's observation age — one
            # candidate per slot plus two spares.
            capacity = min(capacity, max(3, config.strategy.max_active_slots + 2))
        return max(1, min(capacity, 200))

    @staticmethod
    def _sizing_capital(effective: EngineConfig, config: AutomationConfig) -> Decimal:
        """The capital orders are sized and admitted against: the approved capital within the
        strategy budget and, for a paper book, its funding (a screening bound, not a balance)."""
        capital = min(config.approved_capital, config.strategy.capital_budget)
        if effective.environment is Environment.SANDBOX:
            capital = min(capital, effective.paper_funding.amount)
        return capital

    def _subscribe(self, feed: MarketFeed, context: ContextKey, config: AutomationConfig, now: datetime,
                   max_order_notional: Decimal | None = None, capital: Decimal | None = None) -> bool:
        """Start the feed on the planned instruments; False (with a recorded reason) when it cannot start now."""
        if self._feed_retry_at is not None and now < self._feed_retry_at:
            return False
        try:
            catalog = feed.catalog()
            self._catalog = catalog
            wanted = plan_subscription(config.strategy, catalog, capacity=self._subscription_capacity(feed, config),
                                       keep=self._subscription_keep(context, config), max_order_notional=max_order_notional,
                                       capital=capital)
            if not wanted:
                last_error = feed.health().catalog.get("last_error") if not catalog else None
                code = "NO_ELIGIBLE_INSTRUMENTS" if catalog else f"FEED_CATALOG_UNAVAILABLE:{last_error}" if last_error \
                    else "FEED_CATALOG_EMPTY"
                raise DomainError(code.split(":")[0], code)
            feed.start(wanted)
        except DomainError as error:
            self._feed_failures += 1
            delay = min(60.0, 2.0 * (2 ** min(self._feed_failures - 1, 5)))
            self._feed_retry_at = now + timedelta(seconds=delay)
            detail = error.args[0] if error.args and isinstance(error.args[0], str) else error.code
            self._feed_problem = {"code": error.code, "detail": detail[:160], "failures": self._feed_failures,
                                  "retry_at": self._feed_retry_at.isoformat()}
            if self._feed_failures == 1 or self._feed_failures % 10 == 0:
                logger.warning("Automation feed not started (%s); retrying in %.0fs", error.code, delay)
            return False
        self._feed_failures, self._feed_retry_at, self._feed_problem = 0, None, None
        return True

    def _ensure_controller(self, context: ContextKey, config: AutomationConfig) -> DecisionController | None:
        if self._controller is not None:
            return self._controller
        try:
            self._controller = self._controller_factory(config.strategy, context.environment, config.research_mode)
            self._controller_error = None
        except ControllerUnavailable as error:
            self._controller_error = error.code
            return None
        except DomainError as error:
            self._controller_error = error.code
            return None
        if getattr(self._controller, "mode", None) == "RESEARCH_LEARNING":
            try:
                self._start_research_session(context, config)
            except DomainError as error:
                self._controller_error = error.code
                self._drop_controller()
                return None
        return self._controller

    # ------------------------------------------------------------------ recovery policy
    def restoration_report(self, context: ContextKey | None, config: AutomationConfig | None,
                           readiness: tuple[ComponentReadiness, ...] | None = None) -> dict[str, Any]:
        """Infrastructure, financial and neural restoration reported separately, never blended."""
        components = {item.component: item for item in (readiness if readiness is not None else self._readiness(context, config))}
        infrastructure = {name: {"ready": components[name].ready, "code": components[name].code}
                          for name in ("storage", "dataset", "kernel", "feed") if name in components}
        financial: dict[str, Any] = {"ready": False, "code": "NO_EFFECTIVE_CONTEXT"}
        if context is not None:
            health = self._execution_health(context)
            positions = self.writer.call(lambda connection: [(str(r[0]), str(r[1])) for r in connection.execute(
                "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (context.identity,)) if Decimal(str(r[1])) != 0])
            seeds = self.writer.call(lambda connection: int(connection.execute(
                "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0]))
            problems = []
            if health["unknown"]:
                problems.append("UNKNOWN_OUTCOMES")
            if health["open_intents"]:
                problems.append("OPEN_INTENTS")
            if health["fenced"]:
                problems.append("AUTHORITY_FENCED")
            financial = {"ready": not problems, "code": problems[0] if problems else "LEDGER_RECONCILED", "open_intents": health["open_intents"],
                         "unknown_outcomes": health["unknown"], "fenced": health["fenced"], "positions": positions,
                         "simulation_seeds": seeds, "problems": problems}
        neural: dict[str, Any] = {"ready": False, "code": "AUTOMATION_NOT_CONFIGURED"}
        if config is not None:
            mode = config.strategy.neural.mode.value
            if config.strategy.controller_profile is ControllerProfile.FIXTURE_SEQUENCE_V1:
                neural = {"ready": True, "code": "PIPELINE_FIXTURE", "mode": "PIPELINE_FIXTURE", "session_resume": False}
            elif mode == "RESEARCH_LEARNING":
                neural = {"ready": False, "code": "RESEARCH_SESSION_NOT_RESUMABLE", "mode": mode, "session_resume": False}
            else:
                neural = {"ready": bool(components.get("kernel") and components["kernel"].ready and components["dataset"].ready),
                          "code": "CONTROLLER_RESTORABLE" if components["kernel"].ready else components["kernel"].code, "mode": mode,
                          "session_resume": False}
        return {"infrastructure": infrastructure, "financial": financial, "neural": neural}

    def _consider_auto_resume(self, now: datetime, context: ContextKey, effective: EngineConfig, config: AutomationConfig, status: Any) -> dict[str, Any]:
        """Resume under the recovery policy only when every check passes; record the decision either way."""
        policy = config.recovery
        used = self.writer.call(lambda connection: int(connection.execute(
            "SELECT COUNT(*) FROM cf_automation_recoveries WHERE authority_id=? AND resumed=1", (self.authority_id,)).fetchone()[0]))
        report = self.restoration_report(context, config)
        checks: dict[str, Any] = {"policy_version": policy.policy_version, "mandate_expires_at": policy.mandate_expires_at.isoformat() if policy.mandate_expires_at else None,
                                  "resumptions_used": used, "max_resumptions": policy.max_resumptions, "restoration": report}
        reasons: list[str] = []
        if policy.mandate_expires_at is None or now >= policy.mandate_expires_at:
            reasons.append("RECOVERY_MANDATE_EXPIRED")
        if used >= policy.max_resumptions:
            reasons.append("RECOVERY_RESUMPTIONS_EXHAUSTED")
        # Storage and the dataset must be back; the feed starts with the enable and
        # the kernel requirement is the neural block's (a fixture needs none).
        if not all(report["infrastructure"][name]["ready"] for name in ("storage", "dataset") if name in report["infrastructure"]):
            reasons.append("INFRASTRUCTURE_NOT_READY")
        if not report["financial"]["ready"]:
            reasons.append(str(report["financial"]["code"]))
        if policy.require_flat and report["financial"].get("positions"):
            reasons.append("POSITIONS_NOT_FLAT")
        if not report["neural"]["ready"]:
            reasons.append(str(report["neural"]["code"]))
        if context.environment is Environment.LIVE:
            from coinfly.operations.live_arming import LiveMandateStore

            if LiveMandateStore(self.writer).covering(effective, now=now) is None:
                reasons.append("LIVE_MANDATE_REQUIRED")
        if status.operation is not None and status.operation.in_progress:
            reasons.append("TRANSITION_IN_PROGRESS")
        recovery_id = str(uuid5(NAMESPACE_URL, f"coinfly:recovery:{self.authority_id}:{now.isoformat()}"))
        request_id = f"recovery-resume:{recovery_id}"
        resumed = not reasons
        self.writer.call(lambda connection: connection.execute(
            "INSERT OR IGNORE INTO cf_automation_recoveries (recovery_id, authority_id, context_id, policy_version, decided_at, resumed, reason, "
            "checks_json, request_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (recovery_id, self.authority_id, context.identity, policy.policy_version, now.isoformat(), int(resumed),
             "RESUMED_UNDER_RECOVERY_POLICY" if resumed else ",".join(reasons), json.dumps(checks, default=str), request_id if resumed else None)).rowcount)
        self._last_recovery = {"recovery_id": recovery_id, "resumed": resumed, "reasons": reasons, "checks": checks, "decided_at": now.isoformat()}
        if not resumed:
            # Refused once per restart: the reasons stay visible; the operator re-enables explicitly.
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET restart_reason='RESTART_REQUIRES_REENABLE:RECOVERY_REFUSED', updated_at=? WHERE authority_id=?",
                (now.isoformat(), self.authority_id)).rowcount)
            return self._last_recovery
        # Under the (reentrant) control lock already: the explicit-enable sequence runs on the operator's recorded mandate.
        try:
            self.enable(expected_revision=status.effective_revision, expected_epoch=status.transition_epoch, request_id=request_id)
        except DomainError as error:
            code = error.code
            self._last_recovery = {**self._last_recovery, "resumed": False, "reasons": [code]}
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation_recoveries SET resumed=0, reason=? WHERE recovery_id=?", (code, recovery_id)).rowcount)
            return self._last_recovery
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_automation SET restart_reason='RESUMED_UNDER_RECOVERY_POLICY', updated_at=? WHERE authority_id=?",
            (now.isoformat(), self.authority_id)).rowcount)
        return self._last_recovery

    def _drop_controller(self) -> None:
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                pass
        self._controller, self._controller_failed = None, None
        self._reward_ledger, self._research_session = None, None
        self._pending_rewards.clear()

    def _start_research_session(self, context: ContextKey, config: AutomationConfig) -> None:
        """One reward ledger per research session: windows from now on, fed strictly in order."""
        training = config.strategy.neural.training
        assert training is not None
        engine = self._engines.get(context.identity)
        if engine is None or not hasattr(engine, "matcher"):
            raise DomainError("RESEARCH_PAPER_ENGINE_REQUIRED")
        _, effective = self._effective()
        currency = effective.reporting_currency if effective is not None else "USD"
        started = self._clock()
        self._research_session = f"research:{uuid5(NAMESPACE_URL, f'coinfly:research:{context.identity}:{started.isoformat()}')}"
        self._reward_ledger = RewardLedger(self.writer, context=context, session_id=self._research_session,
                                           interval_seconds=training.reward_interval_seconds, currency=currency,
                                           fee_rate=Decimal(engine.matcher.fee_rate), credit_horizon_seconds=training.reward_credit_horizon_seconds,
                                           started_at=started)
        self._pending_rewards.clear()

    def _sample_reward_ledger(self, at: datetime | None = None) -> None:
        """Equity samples close reward windows: at the start of a tick (the book is
        exactly as the previous tick left it, so windows that ended since then close
        before this tick's decision is requested) and after it (post-fill equity)."""
        ledger = self._reward_ledger
        if ledger is None or self._controller is None or self._controller_failed is not None:
            return
        engine = self._engines.get(ledger.context.identity)
        if engine is None:
            return
        try:
            book = engine.observe_execution_book()
            ledger.sample(at or datetime.now(timezone.utc), book.funded_equity)
        except DomainError as error:
            if error.code in ("REWARD_CAPITAL_INVALID", "REWARD_SAMPLE_TIME_REGRESSION"):
                # The research session cannot continue on an invalid ledger: fail it explicitly.
                self._controller_failed = error.code
            # Other engine errors (unarmed, quiesced) simply produce no sample this tick.

    def _research_status(self) -> dict[str, Any] | None:
        ledger, controller = self._reward_ledger, self._controller
        if ledger is None or controller is None:
            return None
        backlog = ledger.backlog(self._clock())
        return {"session_id": ledger.session_id, "reward_interval_seconds": ledger.interval_seconds,
                "credit_horizon_seconds": ledger.credit_horizon_seconds, **backlog,
                "last_training": dict(getattr(controller, "last_training", {}) or {}),
                "discarded_observations": int(getattr(controller, "discarded", 0))}

    def _tick(self, now: datetime) -> tuple[dict[str, Any], tuple[ComputeRequest, ...]]:
        """Control phase A: everything up to the bounded computations, under the control lock."""
        context, effective = self._effective()
        config = self.config()
        row = self._row()
        enabled = bool(row["enabled"]) if row is not None else False
        entries_paused = bool(row["entries_paused"]) if row is not None else False
        if context is None or effective is None or config is None:
            self._state = AutomationState.NOT_CONFIGURED
            self._components = self._readiness(context, config)
            return {"state": self._state.value}, ()
        assert row is not None
        self._compute_budget_seconds = float(config.strategy.execution.compute_budget_seconds)
        self._sample_reward_ledger(at=now)
        status = self.coordinator.status(self.authority_id)
        if enabled and status.operation is not None:
            # Re-activation (or another transition) is in flight: wait, never act.
            if status.operation.retryable:
                self._state = AutomationState.FAILED
                self._failure = {"code": status.operation.error_code or "TRANSITION_FAILED", "stage": "REACTIVATION",
                                 "summary": status.operation.failure.summary if status.operation.failure else "", "retry_safe": True,
                                 "corrective_action": "ACTION_RETRY_OPERATION", "operation_id": status.operation.operation_id}
            else:
                self._state = AutomationState.RECOVERING
            self._components = self._readiness(context, config)
            return {"state": self._state.value, "operation_id": status.operation.operation_id}, ()
        if row["context_id"] != context.identity:
            # The context was re-activated (new epoch): slots of the retired context
            # are history; ownership rows are closed and the budget re-approved.
            self._retire_previous_context(row["context_id"], now)
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET context_id=?, targets_json='{}', updated_at=? WHERE authority_id=?",
                (context.identity, now.isoformat(), self.authority_id)).rowcount)
            self.ownership.approve_budget(context, config.strategy, config.approved_capital, now=now)
            self._builders.clear()
            self._decisions.clear()
            self._last_decision_at.clear()
            self._targets = None
        if enabled and row["reactivation_operation_id"] and not status.armed and not status.recovery_required \
                and status.engine_state is EngineState.INITIALIZING:
            # Re-activation completed READY_UNARMED: arm under the recorded enable mandate.
            self.coordinator.arm(self.authority_id, expected_revision=status.effective_revision, expected_epoch=status.transition_epoch,
                                 request_id=str(uuid5(NAMESPACE_URL, f"coinfly:automation-arm:{row['enable_request_id']}:{status.transition_epoch}")))
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET reactivation_operation_id=NULL, state=?, updated_at=? WHERE authority_id=?",
                (AutomationState.SETUP.value, now.isoformat(), self.authority_id)).rowcount)
            self._state = AutomationState.SETUP
        self._check_strategy(config.strategy, context, effective)
        if not enabled:
            if self._state not in (AutomationState.DRAINING, AutomationState.STOPPED, AutomationState.FAILED):
                self._state = AutomationState.SETUP if row is not None and row["state"] == "SETUP" else AutomationState.RECOVERING if row is not None and row["restart_reason"] else AutomationState.STOPPED
            self._components = self._readiness(context, config)
            if self._state is AutomationState.DRAINING:
                self._finish_draining(context, config, now)
            if row is not None and row["restart_reason"] == "RESTART_REQUIRES_REENABLE" and config.recovery.auto_resume:
                resumed = self._consider_auto_resume(now, context, effective, config, status)
                return {"state": self._state.value, "auto_resume": resumed}, ()
            return {"state": self._state.value}, ()
        engine = self._engine(context)
        feed = self._ensure_feed(context, effective, config)
        sizing_capital = self._sizing_capital(effective, config)
        if not feed.health().subscribed and not self._subscribe(feed, context, config, now, effective.limits.max_order_notional,
                                                                sizing_capital):
            # The feed could not start (catalog unreachable, nothing eligible):
            # a degraded component, retried on a later tick — never a FAILED
            # control plane. Readiness and the universe explain why (from the
            # catalog the start attempt read; no extra fetch per tick). Order
            # TTL expiry needs no market data and keeps running.
            expired = self._expire_resting_orders(now, context, effective, config, engine, {})
            if expired:
                self._expired_orders = (self._expired_orders + expired)[-50:]
            self._selection = select_universe(config.strategy, self._catalog, {}, now=now,
                                              max_order_notional=effective.limits.max_order_notional, capital=sizing_capital)
            self._components = self._readiness(context, config)
            self._state = AutomationState.PAUSED_ENTRIES if entries_paused else AutomationState.SETUP
            return {"state": self._state.value, "feed": self._feed_problem}, ()
        produced = feed.poll(now)
        self._catalog = feed.catalog()
        latest_produced: dict[str, MarketObservation] = {}
        for observation in produced:
            self._observation_counts[observation.instrument] = self._observation_counts.get(observation.instrument, 0) + 1
            self._last_observation_at = observation.received_at
            latest_produced[observation.instrument] = observation
        controller = self._ensure_controller(context, config)
        self._components = self._readiness(context, config)
        # Feed market data into the paper venue for every known instrument.
        observations: dict[str, MarketObservation | None] = {}
        known = set(engine.instrument_ids()) if hasattr(engine, "instrument_ids") else set()
        for entry in self._catalog:
            latest = feed.latest(entry.instrument_id)
            observations[entry.instrument_id] = latest
        # One book update per instrument and tick: the venue needs the current
        # book, and a level2 stream can deliver hundreds of changes per second
        # (each paper update is a journaled transaction). Applied in receive
        # order: the paper venue's clock never runs backwards across instruments.
        for observation in sorted(latest_produced.values(), key=lambda item: item.received_at):
            if observation.instrument in known and observation.two_sided:
                engine.update_market(observation.instrument,
                                     bids=tuple(DepthLevel(price=level.price, quantity=level.quantity) for level in observation.bids),
                                     asks=tuple(DepthLevel(price=level.price, quantity=level.quantity) for level in observation.asks),
                                     now=observation.received_at)
        if hasattr(engine, "settle"):
            settled = engine.settle(now)
            if settled:
                self._settlements = (self._settlements + list(settled))[-50:]
        status = self.coordinator.status(self.authority_id)
        authority_ready = status.armed and not status.admissions_fenced and not status.recovery_required and status.operation is None
        expired = self._expire_resting_orders(now, context, effective, config, engine, observations)
        if expired:
            self._expired_orders = (self._expired_orders + expired)[-50:]
        selection = select_universe(config.strategy, self._catalog, observations, now=now,
                                    max_order_notional=effective.limits.max_order_notional, capital=sizing_capital)
        self._selection = selection
        if config.auto_manage_slots:
            self._ensure_engine_instruments(engine, context, selection)
            reconciliation = self.ownership.reconcile(context=context, scope_id=effective.execution_scope[0], definition=config.strategy,
                                                      selection=selection, config_revision=status.effective_revision, now=now,
                                                      may_start=authority_ready and not entries_paused, start_epoch=context.epoch)
            self._last_reconciliation = reconciliation.as_dict()
        # Sensory warm-up per owned slot (observations flow even while paused).
        owned = {slot.instrument: slot for slot in self.ownership.owned(context, config.strategy) if slot.state != "RETIRED"}
        active_slots = {slot.instrument: slot for slot in self.slots.list(context) if slot.engine_state is EngineState.ACTIVE and slot.instrument in owned}
        for instrument in owned:
            builder = self._builders.get(instrument)
            if builder is None:
                tick = self._price_tick(instrument)
                builder = SensoryFrameBuilder(instrument=instrument, price_tick=tick,
                                              warmup_observations=config.strategy.market_data.warmup_observations,
                                              feature_window=config.strategy.market_data.feature_window)
                self._builders[instrument] = builder
            latest = observations.get(instrument)
            if latest is not None:
                builder.ingest(latest)
        for instrument in list(self._builders):
            if instrument not in owned:
                self._builders.pop(instrument)
                if controller is not None:
                    controller.release_slot(instrument)
        if controller is None or self._controller_failed is not None:
            self._state = AutomationState.PAUSED_ENTRIES if entries_paused else AutomationState.SETUP
            return {"state": self._state.value, "components": [component.model_dump() for component in self._components]}, ()
        if not authority_ready or entries_paused:
            self._state = AutomationState.PAUSED_ENTRIES
            return {"state": self._state.value}, ()
        if not active_slots:
            self._state = AutomationState.READY if all(component.ready for component in self._components if component.component != "feed") else AutomationState.SETUP
            return {"state": self._state.value, "universe": selection.as_dict()}, ()
        warm = [instrument for instrument in active_slots if self._builders[instrument].warm]
        if not warm:
            self._state = AutomationState.WARMING_UP
            return {"state": self._state.value}, ()
        self._state = AutomationState.OBSERVING
        requests: list[ComputeRequest] = []
        for instrument in sorted(warm):
            slot = active_slots[instrument]
            last = self._last_decision_at.get(instrument)
            if last is not None and now - last < timedelta(seconds=config.strategy.execution.decision_interval_seconds):
                continue
            self._due_since.setdefault(instrument, now)
            latest = observations.get(instrument)
            if stale_after(latest, now, config.strategy.max_observation_age):
                self._decisions[instrument] = SlotDecisionRecord(
                    slot_id=slot.slot_id, instrument=instrument, decided_at=now, side="NONE",
                    controller_profile=controller.profile.value, pipeline_fixture=False, admission_reason="not_applicable",
                    outcome="SKIPPED", outcome_code="OBSERVATION_STALE")
                continue
            assert latest is not None
            try:
                # Admission (per-slot state allocation against the byte budget)
                # happens here, under the lock, so the state generation the
                # request binds to is the admitted slot's — not the "-1" of an
                # unadmitted one.
                controller.admit_slot(instrument)
            except DomainError as error:
                self._decisions[instrument] = SlotDecisionRecord(
                    slot_id=slot.slot_id, instrument=instrument, decided_at=now, side="NONE",
                    controller_profile=controller.profile.value, pipeline_fixture=False, admission_reason="not_applicable",
                    outcome="SKIPPED", outcome_code=error.code)
                continue
            builder = self._builders[instrument]
            frame = builder.frame(decision_at=now)
            decision_id = str(uuid5(NAMESPACE_URL, f"coinfly:decision:{context.identity}:{slot.slot_id}:{now.isoformat()}"))
            self._frames[decision_id] = frame
            pending = self._reward_ledger.next_pending(now) if self._reward_ledger is not None else None
            if pending is not None:
                self._pending_rewards[decision_id] = pending
            requests.append(ComputeRequest(
                slot_id=slot.slot_id, instrument=instrument, decision_id=decision_id, decided_at=now, frame_sha256=frame.sha256,
                observed_through=frame.observed_through, observation_sequence=latest.sequence, epoch=context.epoch,
                revision=status.effective_revision, control_generation=self._control_generation,
                state_generation=self._state_generation(controller, instrument),
                reward_window_start=pending.window.start_at if pending is not None else None))
        if requests:
            oldest = min(self._due_since.get(request.instrument, now) for request in requests)
            self._compute["queue_age_seconds"] = max(0.0, (now - oldest).total_seconds())
        # Oldest-due first avoids starving later-sorted instruments when the
        # compute budget fits only one full-graph propagation per tick.
        requests.sort(key=lambda request: (self._due_since.get(request.instrument, now), request.instrument))
        return {"state": self._state.value, "requested": len(requests)}, tuple(requests)

    @staticmethod
    def _state_generation(controller: DecisionController, instrument: str) -> int:
        try:
            value = controller.provenance(instrument).get("state_generation")
        except Exception:
            return -1
        return int(value) if isinstance(value, int) else -1

    def _admit_results(self, now: datetime, results: list[tuple[ComputeRequest, Any, str | None]], report: dict[str, Any]) -> dict[str, Any]:
        """Control phase C: revalidate every generation token, then commit+execute or discard."""
        context, effective = self._effective()
        config = self.config()
        row = self._row()
        status = self.coordinator.status(self.authority_id)
        controller = self._controller
        decided, discarded = int(report.get("decided", 0)), int(report.get("discarded", 0))
        for request, decision, error in results:
            record_base: dict[str, Any] = dict(slot_id=request.slot_id, instrument=request.instrument, decided_at=now,
                                              controller_profile=controller.profile.value if controller else "", pipeline_fixture=False,
                                              admission_reason="not_applicable", frame_sha256=request.frame_sha256)
            if error is not None:
                if error == "DEFERRED_COMPUTE_BUDGET":
                    self._compute["deferred"] = int(self._compute["deferred"]) + 1
                    # Keep only its due time. The next tick creates a fresh frame;
                    # an unconsumed research window stays in the durable ledger.
                    self._frames.pop(request.decision_id, None)
                    self._pending_rewards.pop(request.decision_id, None)
                    continue
                self._frames.pop(request.decision_id, None)
                self._pending_rewards.pop(request.decision_id, None)
                self._decisions[request.instrument] = SlotDecisionRecord(**record_base, side="NONE", outcome="SKIPPED", outcome_code=error)
                self._due_since.pop(request.instrument, None)
                continue
            assert controller is not None and decision is not None
            frame = self._frames.pop(request.decision_id, None)
            pending = self._pending_rewards.pop(request.decision_id, None)
            reason = self._stale_reason(request, context, effective, config, row, status)
            if reason is not None:
                # The reward window (if any) was restored with the session: it stays CLOSED and is fed next time.
                controller.discard(request.instrument)
                discarded += 1
                self._compute["discarded"] = int(self._compute["discarded"]) + 1
                self._decisions[request.instrument] = SlotDecisionRecord(
                    **{**record_base, "controller_profile": decision.controller_profile, "pipeline_fixture": decision.pipeline_fixture},
                    side=decision.side, outcome="DISCARDED", outcome_code=reason, compute_seconds=decision.compute_seconds,
                    provenance=self._provenance(request, decision))
                if context is not None:
                    self._persist_decision(context, self._decisions[request.instrument], request.decision_id)
                self._due_since.pop(request.instrument, None)
                continue
            assert context is not None and effective is not None and config is not None
            controller.commit(request.instrument)
            if pending is not None and self._reward_ledger is not None:
                self._reward_ledger.mark_fed(pending, decision_id=request.decision_id, now=now)
            self._compute["observations"] = int(self._compute["observations"]) + 1
            engine = self._engine(context)
            slot = next((item for item in self.slots.list(context) if item.slot_id == request.slot_id), None)
            record = SlotDecisionRecord(
                **{**record_base, "controller_profile": decision.controller_profile, "pipeline_fixture": decision.pipeline_fixture,
                   "admission_reason": decision.admission_reason},
                side=decision.side, outcome="HOLD" if decision.side == "HOLD" else "PROPOSED",
                compute_seconds=decision.compute_seconds, provenance=self._provenance(request, decision))
            if decision.side == "HOLD":
                record = record.model_copy(update={"outcome": "HOLD", "outcome_code": "GENUINE_HOLD"})
            elif not decision.admission_allowed:
                record = record.model_copy(update={"outcome": "NEURAL_ADMISSION_REJECTED", "outcome_code": decision.admission_reason})
            else:
                record = self._execute_decision(now, context, effective, config, engine, decision, slot, record, frame)
            self._decisions[request.instrument] = record
            self._persist_decision(context, record, request.decision_id)
            self._last_decision_at[request.instrument] = now
            self._due_since.pop(request.instrument, None)
            decided += 1
        if self._controller_failed is not None:
            # The failure happened outside the lock: refresh readiness now so the
            # published status names it while reconciliation keeps running.
            self._components = self._readiness(context, config)
            self._state = AutomationState.SETUP
        elif self._state is AutomationState.OBSERVING and decided:
            self._state = AutomationState.RUNNING
        return {**report, "state": self._state.value, "decided": decided, "discarded": discarded}

    def _stale_reason(self, request: ComputeRequest, context: ContextKey | None, effective: EngineConfig | None,
                      config: AutomationConfig | None, row: sqlite3.Row | None, status: Any) -> str | None:
        """Every token the request was bound to must still hold; the first failure names the reason."""
        if self._closed:
            return "DISCARDED_CLOSED"
        if context is None or effective is None or config is None:
            return "DISCARDED_CONTEXT_CHANGED"
        if context.epoch != request.epoch or status.effective_revision != request.revision:
            return "DISCARDED_CONTEXT_CHANGED"
        if row is None or not row["enabled"]:
            return "DISCARDED_DISABLED"
        if row["entries_paused"]:
            return "DISCARDED_ENTRIES_PAUSED"
        if not (status.armed and not status.admissions_fenced and not status.recovery_required and status.operation is None):
            return "DISCARDED_AUTHORITY_NOT_READY"
        if request.control_generation != self._control_generation:
            return "DISCARDED_GENERATION_CHANGED"
        owned = {slot.instrument: slot for slot in self.ownership.owned(context, config.strategy) if slot.state == "ACTIVE"}
        active = {slot.instrument for slot in self.slots.list(context) if slot.engine_state is EngineState.ACTIVE}
        if request.instrument not in owned or request.instrument not in active:
            return "DISCARDED_SLOT_INACTIVE"
        if self._controller is not None and self._state_generation(self._controller, request.instrument) != request.state_generation:
            return "DISCARDED_STATE_GENERATION_CHANGED"
        checked_at = self._clock()
        if not timedelta(0) <= checked_at - request.observed_through <= config.strategy.max_observation_age:
            return "DISCARDED_OBSERVATION_STALE"
        latest = self._feed.latest(request.instrument) if self._feed is not None else None
        if stale_after(latest, checked_at, config.strategy.max_observation_age):
            return "DISCARDED_OBSERVATION_STALE"
        return None

    def _provenance(self, request: ComputeRequest, decision: Any) -> dict[str, Any]:
        return {**dict(decision.provenance), "feature_transform": FEATURE_TRANSFORM_VERSION,
                "observation_sequence": request.observation_sequence, "observed_through": request.observed_through.isoformat(),
                "tokens": {"epoch": request.epoch, "revision": request.revision, "control_generation": request.control_generation,
                           "state_generation": request.state_generation, "frame_sha256": request.frame_sha256}}

    def _retire_previous_context(self, previous_context_id: str, now: datetime) -> None:
        def save(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE cf_strategy_slots SET state='RETIRED', allocated_budget='0', updated_at=? "
                               "WHERE context_id=? AND state<>'RETIRED'", (now.isoformat(), previous_context_id))
        self.writer.call(save)

    def _finish_draining(self, context: ContextKey, config: AutomationConfig, now: datetime) -> None:
        empty = UniverseSelection(strategy_identity=config.strategy.identity, selected=(), reasons=(), selected_at=now, inputs_digest="0" * 64)
        status = self.coordinator.status(self.authority_id)
        _, effective = self._effective()
        result = self.ownership.reconcile(context=context, scope_id=effective.execution_scope[0] if effective else "", definition=config.strategy,
                                          selection=empty, config_revision=status.effective_revision, now=now, may_start=False, start_epoch=context.epoch,
                                          retire_immediately=True)
        self._last_reconciliation = result.as_dict()
        if not result.retiring and not result.blocked:
            self._state = AutomationState.STOPPED
            self.writer.call(lambda connection: connection.execute(
                "UPDATE cf_automation SET state='STOPPED', updated_at=? WHERE authority_id=?", (now.isoformat(), self.authority_id)).rowcount)

    def _price_tick(self, instrument: str) -> Decimal:
        for entry in self._catalog:
            if entry.instrument_id == instrument:
                return entry.tick_size
        return Decimal("0.01")

    def _ensure_engine_instruments(self, engine: Any, context: ContextKey, selection: UniverseSelection) -> None:
        if not hasattr(engine, "ensure_instruments"):
            return
        entries = {entry.instrument_id: entry for entry in self._catalog}
        additions = []
        known = set(engine.instrument_ids())
        for instrument in selection.selected:
            entry = entries.get(instrument)
            if entry is None or instrument in known:
                continue
            additions.append(PaperInstrument(
                instrument_id=entry.instrument_id, base_currency=entry.base_currency, quote_currency=entry.quote_currency,
                product_track=context.product_track, quantity_increment=entry.base_increment, price_increment=entry.tick_size,
                minimum_notional=entry.minimum_notional, contract_multiplier=entry.contract_multiplier,
                subtype=entry.subtype, expiry=entry.expiry))
        if additions:
            engine.ensure_instruments(tuple(additions))

    def _previous_targets(self, context: ContextKey, instruments: tuple[str, ...]) -> tuple[InstrumentWeight, ...]:
        row = self._row()
        stored: dict[str, str] = {}
        if row is not None:
            stored = json.loads(row["targets_json"] or "{}")
        return tuple(InstrumentWeight(instrument_id=name, weight=Decimal(stored.get(name, "0"))) for name in instruments)

    def _store_targets(self, context: ContextKey, weights: tuple[InstrumentWeight, ...], allocation_revision: int) -> None:
        payload = json.dumps({item.instrument_id: str(item.weight) for item in weights}, sort_keys=True)
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_automation SET targets_json=?, allocation_revision=MAX(allocation_revision, ?), updated_at=? WHERE authority_id=?",
            (payload, allocation_revision, self._clock().isoformat(), self.authority_id)).rowcount)

    def _next_allocation_revision(self) -> int:
        """Consume one durable, strictly increasing proposal revision.

        Every proposal takes a revision whether or not it is executed: the
        target coordinator refuses a revision it has already bound, so a refused
        proposal (turnover, exposure, execution) must never leave the durable
        counter behind the coordinator's.
        """
        def bump(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "UPDATE cf_automation SET allocation_revision=allocation_revision+1, updated_at=? WHERE authority_id=? RETURNING allocation_revision",
                (self._clock().isoformat(), self.authority_id)).fetchone()
            if row is None:
                raise DomainError("AUTOMATION_NOT_CONFIGURED")
            return int(row[0])
        return self.writer.call(bump)

    def _execute_decision(self, now: datetime, context: ContextKey, effective: EngineConfig, config: AutomationConfig, engine: Any,
                          decision: Any, slot: Any, record: SlotDecisionRecord, frame: Any = None) -> SlotDecisionRecord:
        instruments = tuple(engine.instrument_ids())
        if self._targets is None or self._targets.context != context or set(self._targets.eligible_instruments) != set(instruments):
            self._targets = TargetCoordinator(context, instruments, limits=AllocationLimits(
                max_gross=Decimal(1) if context.product_track is ProductTrack.SPOT else (config.strategy.derivatives.max_leverage if config.strategy.derivatives else Decimal(1)),
                max_per_instrument=min(Decimal(1), config.strategy.per_slot_max_exposure_fraction * (config.strategy.derivatives.max_leverage if config.strategy.derivatives else Decimal(1)))),
                max_feature_age=timedelta(seconds=config.strategy.market_data.max_observation_age_seconds),
                max_proposal_ttl=timedelta(seconds=config.strategy.execution.proposal_ttl_seconds))
        targets = self._targets
        targets.set_snapshot(decision.frame_sha256, decision.observed_through, warm=True)
        previous = self._previous_targets(context, instruments)
        revision = self._next_allocation_revision()
        expires = now + timedelta(seconds=config.strategy.execution.proposal_ttl_seconds)
        if config.strategy.mapping_version is MappingVersion.SPOT_STEP_V1:
            adapter = FixedDecoderWeightAdapter(instruments, step=config.strategy.execution.target_step)
            proposal = adapter.propose(side=decision.side, instrument_id=decision.instrument, previous_targets=previous,
                                       product_track=context.product_track, decision_id=decision.decision_id,
                                       snapshot_id=decision.frame_sha256, allocation_revision=revision, expires_at=expires)
        else:
            assert config.strategy.derivatives is not None
            adapter_d = FixedDecoderDerivativesAdapter(instruments, directions=config.strategy.derivatives.directions,
                                                       max_gross=config.strategy.derivatives.max_leverage,
                                                       max_per_instrument=min(config.strategy.derivatives.max_leverage, Decimal(1)),
                                                       step=config.strategy.execution.target_step,
                                                       allow_reversal=config.strategy.derivatives.allow_reversal)
            proposal = adapter_d.propose(side=decision.side, instrument_id=decision.instrument, previous_targets=previous,
                                         product_track=context.product_track, decision_id=decision.decision_id,
                                         snapshot_id=decision.frame_sha256, allocation_revision=revision, expires_at=expires)
        if proposal is None:
            return record.model_copy(update={"outcome": "NO_CHANGE", "outcome_code": "TARGET_STEP_EXHAUSTED"})
        # Per-slot exposure cap: the strategy budget limits this instrument's weight.
        cap = config.strategy.per_slot_max_exposure_fraction
        if any(abs(item.weight) > cap + Decimal("1e-18") and item.instrument_id == decision.instrument for item in proposal.weights):
            return record.model_copy(update={"outcome": "RISK_REJECTED", "outcome_code": "PER_SLOT_EXPOSURE_LIMIT"})
        try:
            bound = targets.bind(proposal, expected_epoch=context.epoch, now=now)
        except DomainError as error:
            return record.model_copy(update={"outcome": "RISK_REJECTED", "outcome_code": error.code})
        if bound is None:
            return record.model_copy(update={"outcome": "NO_CHANGE", "outcome_code": "NO_PROPOSAL"})
        bound = bound.model_copy(update={"strategy_identity": config.strategy.identity})
        # Exact turnover guard (strict < fraction of interval-start equity) as the
        # declared execution constraint: it may reduce a quantity (CONSTRAIN) or
        # refuse the order (REJECT); it never selects another trade.
        turnover_notes: list[dict[str, Any]] = []
        ledger = self._turnover_ledger(config)

        def constrain(delta: Any, book: Any) -> Any:
            contract = engine.instrument_contract(delta.instrument)
            marks = {mark.instrument: mark.price for mark in book.marks}
            verdict = ledger.evaluate(
                context, config.strategy.identity, now=self._clock(), funded_equity=book.funded_equity, marks=marks,
                quantity=delta.quantity, price=delta.limit_price, multiplier=delta.contract_multiplier,
                quantity_increment=contract.quantity_increment, minimum_quantity=contract.quantity_increment,
                minimum_notional=contract.minimum_notional, policy=config.strategy.execution.turnover_policy)
            turnover_notes.append({"instrument": delta.instrument, "side": delta.side.value, **verdict.as_dict()})
            if not verdict.admitted:
                raise DomainError(verdict.code)
            if verdict.constrained_quantity is not None:
                return delta.model_copy(update={"quantity": verdict.constrained_quantity})
            return delta

        try:
            intent_ids = engine.submit_target(bound, constraint=constrain,
                                              cause={"decision_id": decision.decision_id, "controller_profile": decision.controller_profile,
                                                     "pipeline_fixture": decision.pipeline_fixture})
        except DomainError as error:
            return record.model_copy(update={"outcome": "RISK_REJECTED", "outcome_code": error.code,
                                             "provenance": {**record.provenance, "turnover": turnover_notes}})
        except Exception as error:
            return record.model_copy(update={"outcome": "EXECUTION_FAILED", "outcome_code": type(error).__name__})
        self._store_targets(context, proposal.weights, revision)
        for intent_id in intent_ids:
            self._record_order(context, intent_id, decision.decision_id, decision.instrument, now, frame)
        constrained = any(note.get("code") == "TURNOVER_CONSTRAINED" for note in turnover_notes)
        return record.model_copy(update={"outcome": "DISPATCHED" if intent_ids else "NO_ORDER_NEEDED",
                                         "outcome_code": ("ADMITTED_CONSTRAINED" if constrained else "ADMITTED") if intent_ids else "PLANNER_NO_DELTA",
                                         "intent_ids": tuple(intent_ids),
                                         "provenance": {**record.provenance, "turnover": turnover_notes}})

    def _turnover_ledger(self, config: AutomationConfig) -> TurnoverLedger:
        settings = config.strategy.execution
        ledger = self._ledger
        if ledger is None or ledger.fraction != settings.max_turnover_fraction or ledger.interval_seconds != settings.accounting_interval_seconds:
            ledger = TurnoverLedger(self.writer, fraction=settings.max_turnover_fraction, interval_seconds=settings.accounting_interval_seconds)
            self._ledger = ledger
        return ledger

    # ------------------------------------------------------------------ orders (TTL policy)
    def _record_order(self, context: ContextKey, intent_id: str, decision_id: str, instrument: str, now: datetime,
                      frame: Any = None) -> None:
        reference = (str(frame.reference_price), frame.observed_through.isoformat(), frame.available_through.isoformat()) \
            if frame is not None else (None, None, None)
        self.writer.call(lambda connection: connection.execute(
            "INSERT OR IGNORE INTO cf_automation_orders (intent_id, context_id, decision_id, instrument, created_at, state, "
            "outcome_code, filled_quantity, resolved_at, decided_at, reference_price, reference_at, reference_available_at) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (intent_id, context.identity, decision_id, instrument, now.isoformat(), "RESTING", "", "0", now.isoformat(), *reference)).rowcount)

    def _expire_resting_orders(self, now: datetime, context: ContextKey, effective: EngineConfig, config: AutomationConfig,
                               engine: Any, observations: Mapping[str, MarketObservation | None]) -> list[dict[str, Any]]:
        """Cancel resting orders older than the declared TTL; classify why they did not fill.

        A resting order is neither HOLD nor execution. The classification uses
        the book at cancel time: NONMARKETABLE (limit never crossed), LIQUIDITY
        (crossed but no fill at the displayed depth) or PARTIAL. Under
        ROLLBACK_UNFILLED_TARGET the intended target returns to what is actually
        held, so targets never accumulate silently across expired orders.
        """
        ttl = timedelta(seconds=config.strategy.execution.order_ttl_seconds)
        expired: list[dict[str, Any]] = []
        rows = self.writer.call(lambda connection: [dict(row) for row in connection.execute(
            "SELECT a.intent_id, a.decision_id, a.instrument, a.created_at, o.terminal, o.filled_quantity, o.resolution "
            "FROM cf_automation_orders a JOIN ex_outbox o USING(intent_id) WHERE a.context_id=? AND a.state='RESTING'",
            (context.identity,))])
        for row in rows:
            created = datetime.fromisoformat(row["created_at"])
            if row["terminal"]:
                filled = Decimal(row["filled_quantity"])
                code = "FILLED" if filled > 0 else "TERMINAL_UNFILLED"
                self._update_order(row["intent_id"], "SETTLED", code, filled, now)
                continue
            if row["resolution"] == "UNKNOWN":
                continue  # unknown outcomes are fenced by the reducer, never cancelled blindly
            if now - created < ttl:
                continue
            if not hasattr(engine, "cancel_intent"):
                continue
            result = engine.cancel_intent(row["intent_id"])
            if not result.get("cancelled"):
                continue
            filled = Decimal(str(result.get("filled_quantity", "0")))
            if filled > 0:
                code = "ORDER_EXPIRED_PARTIAL"
            elif result.get("limit_crossed_at_cancel"):
                code = "ORDER_EXPIRED_LIQUIDITY_MISSING"
            else:
                code = "ORDER_EXPIRED_NONMARKETABLE"
            self._update_order(row["intent_id"], "EXPIRED", code, filled, now)
            expired.append({"intent_id": row["intent_id"], "instrument": row["instrument"], "code": code, "filled": str(filled)})
            # The originating decision's durable outcome becomes ORDER_EXPIRED:
            # it was neither a hold nor an execution.
            self._mark_decision_expired(str(row["decision_id"]), code)
            record = self._decisions.get(row["instrument"])
            if record is not None and row["intent_id"] in record.intent_ids:
                self._decisions[row["instrument"]] = record.model_copy(update={"outcome": "ORDER_EXPIRED", "outcome_code": code})
        if expired and config.strategy.execution.expiry_policy == "ROLLBACK_UNFILLED_TARGET":
            self._rollback_targets_to_actual(context, engine, {item["instrument"] for item in expired})
        return expired

    def _mark_decision_expired(self, decision_id: str, code: str) -> None:
        def save(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE cf_automation_decisions SET outcome='ORDER_EXPIRED', outcome_code=? WHERE decision_id=? AND outcome='DISPATCHED'",
                (code, decision_id))
        self.writer.call(save)

    def _update_order(self, intent_id: str, state: str, code: str, filled: Decimal, now: datetime) -> None:
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_automation_orders SET state=?, outcome_code=?, filled_quantity=?, resolved_at=? WHERE intent_id=?",
            (state, code, str(filled), now.isoformat(), intent_id)).rowcount)

    def _rollback_targets_to_actual(self, context: ContextKey, engine: Any, instruments: set[str]) -> None:
        """Intended target := weight actually held (exact), for instruments whose order expired."""
        try:
            book = engine.observe_execution_book()
        except DomainError:
            return
        if book.funded_equity <= 0:
            return
        marks = {mark.instrument: mark for mark in book.marks}
        positions = self.writer.call(lambda connection: {str(r[0]): Decimal(r[1]) for r in connection.execute(
            "SELECT instrument,quantity FROM ex_positions WHERE context_id=?", (context.identity,))})
        row = self._row()
        stored: dict[str, str] = json.loads(row["targets_json"] or "{}") if row is not None else {}
        with localcontext(financial_context(80)):
            for instrument in instruments:
                mark = marks.get(instrument)
                if mark is None:
                    continue
                held = positions.get(instrument, Decimal(0)) * mark.price * mark.contract_multiplier / book.funded_equity
                stored[instrument] = str(held.quantize(Decimal("1e-18")))
        revision = int(row["allocation_revision"]) + 1 if row is not None else 1
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_automation SET targets_json=?, allocation_revision=?, updated_at=? WHERE authority_id=?",
            (json.dumps(stored, sort_keys=True), revision, self._clock().isoformat(), self.authority_id)).rowcount)

    def _persist_decision(self, context: ContextKey, record: SlotDecisionRecord, decision_id: str) -> None:
        def save(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO cf_automation_decisions (decision_id, context_id, slot_id, instrument, decided_at, side, "
                "controller_profile, pipeline_fixture, admission_reason, outcome, outcome_code, intent_ids, frame_sha256, "
                "compute_seconds, provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, context.identity, record.slot_id, record.instrument, record.decided_at.isoformat(), record.side,
                 record.controller_profile, int(record.pipeline_fixture), record.admission_reason, record.outcome, record.outcome_code,
                 ",".join(record.intent_ids), record.frame_sha256, record.compute_seconds,
                 json.dumps(record.provenance, sort_keys=True, default=str)))
        self.writer.call(save)

    # ------------------------------------------------------------------ status
    def _controller_provenance(self) -> dict[str, Any] | None:
        """Controller-level provenance for the status block (graph, kernel, mode, checkpoint, profile).

        Slot-level fields (state generation, native frame counters) live in each
        decision's provenance; here the shared identity is reported once.
        """
        controller = self._controller
        if controller is None:
            return None
        try:
            provenance = controller.provenance("")
        except Exception as error:  # a provenance failure is reported, never hidden
            return {"controller": controller.profile.value, "error": type(error).__name__}
        provenance.pop("state_generation", None)
        provenance["feature_transform"] = FEATURE_TRANSFORM_VERSION
        return provenance

    def status(self) -> dict[str, Any]:
        """The last published immutable snapshot; never waits for a computation."""
        return dict(self._published)

    def _publish_status(self) -> None:
        try:
            self._published = self._build_status()
        except Exception as error:  # publication must never take the loop down
            logger.warning("Automation status publication failed: %s", type(error).__name__)

    def _build_status(self) -> dict[str, Any]:
        with self._lock:
            context, effective = self._effective()
            config = self.config()
            row = self._row()
            feed_health = self._feed.health().as_dict() if self._feed is not None else None
            slots: list[dict[str, Any]] = []
            if context is not None and config is not None:
                owned = {slot.instrument: slot for slot in self.ownership.owned(context, config.strategy)}
                positions = self.writer.call(lambda connection: {str(r[0]): str(r[1]) for r in connection.execute(
                    "SELECT instrument,quantity FROM ex_positions WHERE context_id=?", (context.identity,))})
                targets = {item.instrument_id: str(item.weight) for item in self._previous_targets(context, tuple(owned))}
                for record in self.slots.list(context):
                    owner = owned.get(record.instrument)
                    if owner is None:
                        continue
                    builder = self._builders.get(record.instrument)
                    decision = self._decisions.get(record.instrument)
                    blocking = ""
                    if self._last_reconciliation:
                        for instrument, code in self._last_reconciliation.get("blocked", ()):
                            if instrument == record.instrument:
                                blocking = code
                    latest = self._feed.latest(record.instrument) if self._feed is not None else None
                    slots.append(SlotStatus(
                        slot_id=record.slot_id, instrument=record.instrument, strategy=owner.strategy_identity,
                        selection_reason=owner.selection_reason, lifecycle=record.engine_state.value, ownership_state=owner.state,
                        target_weight=targets.get(record.instrument, "0"), actual_quantity=positions.get(record.instrument, "0"),
                        allocated_budget=str(owner.allocated_budget), warm=builder.warm if builder else False,
                        observations=builder.observations if builder else 0,
                        last_observation_at=latest.received_at if latest is not None else None,
                        last_decision=decision, last_execution=decision.outcome if decision else "—",
                        blocking_reason=blocking or (record.operation_id or ""),
                    ).model_dump(mode="json"))
            last_decision = max(self._decisions.values(), key=lambda item: item.decided_at) if self._decisions else None
            return {
                "state": self._state.value, "configured": config is not None,
                "enabled": bool(row["enabled"]) if row is not None else False,
                "entries_paused": bool(row["entries_paused"]) if row is not None else False,
                "restart_reason": row["restart_reason"] if row is not None else None,
                "reactivation_operation_id": row["reactivation_operation_id"] if row is not None else None,
                "strategy": {"identity": config.strategy.identity, "label": config.strategy.label,
                             "controller_profile": config.strategy.controller_profile.value,
                             "mapping_version": config.strategy.mapping_version.value, "research_only": config.strategy.research_only,
                             "research_mode": config.research_mode, "learning_enabled": config.strategy.learning_enabled,
                             "max_active_slots": config.strategy.max_active_slots,
                             "capital_budget": str(config.strategy.capital_budget), "approved_capital": str(config.approved_capital),
                             "feed": config.feed.model_dump(mode="json"), "auto_manage_slots": config.auto_manage_slots,
                             "pipeline_test_mode": config.pipeline_test_mode,
                             "definition_digest": config.strategy.digest} if config is not None else None,
                "pipeline_test_mode": config.pipeline_test_mode if config is not None else False,
                "provenance": self._controller_provenance(),
                "research": self._research_status(),
                "recovery": {"policy": config.recovery.model_dump(mode="json") if config is not None else None,
                             "last": self._last_recovery,
                             "restoration": self.restoration_report(context, config, self._components)
                             if row is not None and row["restart_reason"] else None},
                "components": [component.model_dump(mode="json") for component in self._components],
                "feed": feed_health, "universe": self._selection.as_dict() if self._selection is not None else None,
                "reconciliation": self._last_reconciliation, "slots": slots,
                "last_observation_at": self._last_observation_at.isoformat() if self._last_observation_at else None,
                "last_decision": last_decision.model_dump(mode="json") if last_decision else None,
                "decision_reason": (last_decision.outcome_code or last_decision.outcome) if last_decision else None,
                "failure": self._failure, "ticks": self._ticks,
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "resources": self._controller.resource_usage() if self._controller is not None else None,
                "execution_health": self._execution_health(context),
                "compute": dict(self._compute), "control_generation": self._control_generation,
                "expired_orders": list(self._expired_orders[-10:]),
                "settlements": list(self._settlements[-10:]),
                "controller_failed": self._controller_failed,
                "published_at": self._clock().isoformat(),
            }

    def _execution_health(self, context: ContextKey | None) -> dict[str, Any]:
        if context is None:
            return {"open_intents": 0, "unknown": 0, "fenced": False}
        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            from coinfly.execution.evidence import authority_fenced_tx

            open_count = connection.execute("SELECT COUNT(*) FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND o.terminal=0",
                                            (context.identity,)).fetchone()[0]
            unknown = connection.execute("SELECT COUNT(*) FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND o.resolution='UNKNOWN'",
                                         (context.identity,)).fetchone()[0]
            fills = connection.execute("SELECT COUNT(*) FROM ex_fills WHERE context_id=?", (context.identity,)).fetchone()[0]
            causes: dict[str, int] = {}
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='cf_intent_causes'").fetchone() is not None:
                causes = cause_summary_tx(connection, context.identity)
            return {"open_intents": int(open_count), "unknown": int(unknown), "fills": int(fills),
                    "fenced": authority_fenced_tx(connection, context.authority_id), "intent_causes": causes}
        return self.writer.call(read)

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_event.set()
            if self._feed is not None:
                try:
                    self._feed.stop()
                except Exception:
                    pass
            if self._controller is not None:
                self._controller.close()
        if self._thread is not None:
            self._thread.join(timeout=10)


class _FeedTransport:
    """Registers a feed with the context transports so a handover retires it."""

    def __init__(self, feed: MarketFeed) -> None:
        self._feed = feed

    def retire(self) -> None:
        self._feed.stop()
