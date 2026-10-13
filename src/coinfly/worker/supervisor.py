"""Independent worker: durable commands, safety lifetime, and reactive desktop state."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError

from coinfly import __application_id__, __version__
from coinfly.bootstrap.manager import BootstrapManager
from coinfly.config.models import EngineConfig, default_config
from coinfly.config.service import decode_operator_config
from coinfly.control.context_coordinator import ContextCoordinator, EngineLifecycle
from coinfly.control.slot_service import (
    SlotOperation,
    SlotOperationPhase,
    SlotRecord,
    SlotService,
    SlotStopProof,
)
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack, RunProfile
from coinfly.execution.evidence import alarm_tx, event_tx, fence_authority_tx
from coinfly.execution.fact_reducer import FactReducer
from coinfly.execution.reconciler import Reconciler
from coinfly.ipc.auth import OwnerOnlySandboxPolicy, PeerIdentity
from coinfly.ipc.local import LocalRPCServer, default_socket_path
from coinfly.ipc.protocol import PROTOCOL_VERSION, READ_METHODS, RPCError, RPCRequest, thaw
from coinfly.observability.diagnostics import build_identity, runtime_identity
from coinfly.reporting.history_service import HistoryQuery, HistoryService, WriterHistorySource
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.storage.writer import Writer
from coinfly.venues.coinbase.adapter_factory import AdapterFactory
from coinfly.venues.coinbase.live_services import QualifiedLiveServices
from coinfly.worker.automation import AutomationConfig, AutomationController, FeedSelection, RecoveryPolicy
from coinfly.worker.ownership import AuthorityOwnership
from coinfly.worker.paper_engine import LocalPaperEngine
from coinfly.worker.paths import WorkerPaths, native_user_home

logger = logging.getLogger(__name__)


class ManagedEngine(EngineLifecycle, Protocol):
    def start_slot(self, slot: SlotRecord) -> None: ...
    def quiesce_slot(self, slot: SlotRecord) -> None: ...
    def drain_slot(self, slot: SlotRecord, policy: DrainPolicy) -> None: ...
    def reconcile_slot(self, slot: SlotRecord) -> SlotStopProof: ...
    def balance_snapshot(self) -> dict[str, Any]: ...


class _SlotDriver:
    def __init__(self, engines: dict[str, EngineLifecycle]) -> None:
        self.engines = engines

    def _engine(self, slot: SlotRecord) -> ManagedEngine:
        engine = self.engines.get(slot.context.identity)
        if engine is None:
            raise DomainError("RECOVERY_ENGINE_REQUIRED")
        return cast(ManagedEngine, engine)

    def start(self, slot: SlotRecord) -> None:
        self._engine(slot).start_slot(slot)

    def quiesce(self, slot: SlotRecord) -> None:
        self._engine(slot).quiesce_slot(slot)

    def drain(self, slot: SlotRecord, policy: DrainPolicy) -> None:
        self._engine(slot).drain_slot(slot, policy)

    def reconcile(self, slot: SlotRecord) -> SlotStopProof:
        return self._engine(slot).reconcile_slot(slot)


class WorkerSupervisor:
    """One per-user process, with a separately held account-authority lock.

    ``paths``, ``bootstrap`` and ``engine_factory`` are constructor injection for
    native packaging/tests, never RPC, environment or financial CLI settings.
    Closing the desktop does not close this worker or its reconciliation services.
    """

    def __init__(
        self, *, paths: WorkerPaths | None = None, socket_path: Path | None = None,
        bootstrap: BootstrapManager | None = None,
        engine_factory: Callable[[EngineConfig, ContextKey], EngineLifecycle] | None = None,
        startup_progress: Callable[[str], None] | None = None,
        startup_timeout: float = 10.0,
        shutdown_request: Callable[[], None] | None = None,
        live_services: QualifiedLiveServices | None = None,
    ) -> None:
        self.paths = paths or WorkerPaths.for_current_user()
        self.paths.prepare()
        # Identity of the code this process started with. The desktop compares
        # it with its own so a worker that outlived a rebuilt checkout/bundle is
        # reported as stale instead of failing lazily inside an operation.
        self.runtime = runtime_identity()
        self.build = build_identity()  # immutable: captured once, at startup
        self._handshakes: dict[int, dict[str, Any]] = {}
        self._retirement: dict[str, Any] | None = None
        self._shutdown_request = shutdown_request
        self._live_services = live_services
        self.authority_id = default_config().authority_id
        self._ownership = AuthorityOwnership(self.authority_id, lock_directory=self.paths.locks)
        # Register each acquired resource immediately. A constructor failure must
        # release the writer thread and authority even before a supervisor can be
        # returned to the launcher. Reverse order keeps the writer alive while
        # shutting down any constructed control actors.
        with ExitStack() as startup:
            self._ownership.acquire()
            startup.callback(self._ownership.close)
            self.writer = Writer(self.paths.database, startup_progress=startup_progress,
                                 startup_timeout=startup_timeout)
            startup.callback(self.writer.close)
            self.interlock = ContextInterlock(self.writer)
            self.reducer = FactReducer(self.writer, provisional_fence=self.interlock.provisional_fence)
            self.bootstrap = bootstrap or BootstrapManager(home=native_user_home())
            self._injected_factory = engine_factory
            self._engines: dict[str, EngineLifecycle] = {}
            startup.callback(self._close_engine_background)
            self._sequence = 0
            self._publication_lock = threading.RLock()
            self._closed = False
            self._last_operation_id: str | None = None
            self._instance_id = str(uuid4())
            self._server: LocalRPCServer | None = None
            self._bootstrap_loop: asyncio.AbstractEventLoop | None = None
            self._bootstrap_thread: threading.Thread | None = None
            self._bootstrap_stop: asyncio.Event | None = None
            self._bootstrap_started = threading.Event()
            self._socket_path = socket_path or default_socket_path()
            self.coordinator = ContextCoordinator(self.writer, self.interlock, self._create_engine,
                                                  self._validate_candidate, on_change=self._publish,
                                                  engines=self._engines)
            startup.callback(self.coordinator.close)
            self.slots = SlotService(self.writer, self.interlock, _SlotDriver(self._engines),
                                     readiness=lambda slot: self._require_bootstrap(), on_change=self._publish)
            startup.callback(self.slots.close)
            self.history = HistoryService(WriterHistorySource(self.writer))
            # Recovery converts possible pre-crash invocation into durable UNKNOWN;
            # it never resends any create or trusts the selected GUI configuration.
            Reconciler(self.writer, self.reducer).recover()
            self._recover_context()
            self.kernels = self._kernel_registry()
            self.automation = AutomationController(
                writer=self.writer, coordinator=self.coordinator, slots=self.slots, engines=self._engines,
                authority_id=self.authority_id, feed_factory=self._feed_factory,
                controller_factory=self._controller_factory, dataset_ready=self._dataset_readiness,
                kernel_ready=self.kernels.readiness, on_change=self._publish)
            startup.callback(self.automation.close)
            startup.pop_all()

    def _recover_context(self) -> None:
        status = self.coordinator.status(self.authority_id)
        if status.effective_context is None or status.effective_config is None:
            return
        try:
            engine: Any
            if status.effective_context.environment is Environment.LIVE:
                if self._live_services is None:
                    raise DomainError("LIVE_NATIVE_COMPOSITION_REQUIRED")
                from coinfly.operations.live_arming import LiveMandateStore
                from coinfly.venues.coinbase.live_spot_engine import LiveSpotEngine
                cfg, ctx = status.effective_config, status.effective_context
                services = self._live_services
                if cfg.product_track is not ProductTrack.SPOT:
                    raise DomainError("LIVE_DERIVATIVES_ENGINE_UNAVAILABLE")
                engine = LiveSpotEngine(cfg, ctx, self.writer, self.reducer, self.interlock,
                    transport_factory=lambda: services.transport(cfg, ctx, now=datetime.now(timezone.utc),
                                                                transports=self.coordinator.transports),
                    mandate_check=lambda config, now: LiveMandateStore(self.writer).covering(config, now=now) is not None,
                    on_change=self._publish)
                self._engines[ctx.identity] = engine
                engine.initialize(cfg, ctx)
                # Account recovery needs no model and does not grant entry authority.
                engine.recover_existing()
            else:
                engine = LocalPaperEngine(status.effective_config, status.effective_context, self.writer,
                    self.reducer, self.interlock, ready=self._require_bootstrap, on_change=self._publish)
                engine.recover_existing()
            self._engines[engine.context.identity] = engine
            self.coordinator.attach_recovered_engine(engine)
        except DomainError as error:
            code = error.code
            context = status.effective_context
            self.interlock.provisional_fence(self.authority_id, code)
            self.writer.call(lambda connection: fence_authority_tx(connection, context,
                              code, "execution-recovery-blocked"))

    def _kernel_registry(self) -> Any:
        from coinfly.research.kernel_registry import KernelRegistry

        application = self.paths.application
        kernels = application.kernels if application is not None else self.paths.root / "kernels"
        bundle_root = Path(getattr(sys, "_MEIPASS")) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else None
        return KernelRegistry(kernels, bundle_root=bundle_root)

    def _dataset_readiness(self) -> tuple[bool, str]:
        progress = self.bootstrap.progress
        if not progress.ready:
            return False, progress.reason or progress.state.value
        try:
            self.bootstrap.verified_dataset()
        except Exception as error:  # BootstrapError or a fixture sentinel problem
            code = getattr(error, "code", None)
            return False, code if isinstance(code, str) else "DATASET_NOT_READY"
        return True, "DATASET_READY"

    def _feed_factory(self, config: EngineConfig, automation: AutomationConfig) -> Any:
        from coinfly.market.feed import ReplayFeed
        from coinfly.market.observations import DataSource

        selection = automation.feed
        if selection.kind is DataSource.SYNTHETIC_FIXTURE:
            from coinfly.market.fixtures import fixture_feed

            return fixture_feed(selection.fixture_profile or "random-walk-3", quote=config.reporting_currency)
        if selection.kind is DataSource.REPLAY_FILE:
            application = self.paths.application
            if application is None or not selection.replay_name:
                raise DomainError("REPLAY_FILE_INVALID")
            return ReplayFeed(application.datasets / "replays" / selection.replay_name)
        # The catalog is requested for the context's contract family: a
        # perpetual-style or dated-futures context reads the FUTURE product
        # list, a spot context the SPOT list. Without the subtype every context
        # would be offered spot products that its strategy can never select.
        if selection.kind is DataSource.COINBASE_PUBLIC_REST:
            from coinfly.market.coinbase_public import CoinbasePublicRestFeed

            # One-second pause between sweeps: with a handful of books per
            # sweep each stays well inside the default 5 s observation age.
            return CoinbasePublicRestFeed(quote_currencies=(config.reporting_currency,), subtype=config.effective_subtype,
                                          poll_interval_seconds=1.0)
        from coinfly.market.coinbase_public import CoinbasePublicWebSocketFeed

        return CoinbasePublicWebSocketFeed(quote_currencies=(config.reporting_currency,), subtype=config.effective_subtype)

    def _controller_factory(self, strategy: Any, environment: Environment, research_mode: bool) -> Any:
        from coinfly.strategy.controllers import build_controller

        dataset = None
        if self._dataset_readiness()[0]:
            dataset = self.bootstrap.verified_dataset()
        kernel = self.kernels.verified()
        application = self.paths.application
        checkpoints = application.checkpoints if application is not None else self.paths.root / "checkpoints"
        return build_controller(strategy.controller_profile, environment=environment, dataset=dataset, kernel=kernel,
                                learning=bool(strategy.learning_enabled and research_mode),
                                fixture_sides=strategy.fixture_sides or None, neural=strategy.neural,
                                checkpoints=checkpoints, research_mode=research_mode)

    def _validate_candidate(self, config: EngineConfig) -> None:
        self._ownership.assert_held()
        if config.authority_id != self.authority_id:
            raise DomainError("AUTHORITY_MISMATCH")
        available = (self._live_services.availability(config, now=datetime.now(timezone.utc))
                     if config.environment is Environment.LIVE and self._live_services is not None
                     else AdapterFactory().availability(config))
        if not available.available:
            raise DomainError(available.blocking_codes[0])
        required_profile = RunProfile.STANDARD if config.environment is Environment.LIVE else RunProfile.PAPER
        if config.run_profile != required_profile:
            raise DomainError("RUN_PROFILE_FEED_UNQUALIFIED")
        if config.environment is Environment.LIVE:
            return  # Paper books are not evidence about a live execution portfolio.
        def scope_conflict(connection: sqlite3.Connection) -> bool:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='wp_paper_books'").fetchone() is None:
                return False
            return any(connection.execute("SELECT 1 FROM wp_paper_books WHERE authority_id=? AND scope_id=? "
                       "AND product_track<>?", (config.authority_id, scope, config.product_track.value)).fetchone()
                       is not None for scope in config.execution_scope)
        if self.writer.call(scope_conflict):
            raise DomainError("PAPER_TRACK_REQUIRES_SEPARATE_BOOK")

    def _require_bootstrap(self) -> None:
        self.bootstrap.verified_dataset()

    def _create_engine(self, config: EngineConfig, context: ContextKey) -> EngineLifecycle:
        """One execution engine per context behind the common ``ExecutionEngine`` interface.

        SANDBOX contexts get the local paper venue. A LIVE spot context gets the
        Coinbase Advanced spot engine, whose transport is assembled only at arm
        time from the OS keychain (``_live_transport``); a LIVE derivative
        context has no engine (``LIVE_DERIVATIVES_ENGINE_UNAVAILABLE``): no
        derivative label inherits the spot gateway's guarantees. Either way the
        interface is asserted, never duck-typed.
        """
        from coinfly.execution.engine import require_execution_engine

        self._ownership.assert_held()
        self._require_bootstrap()
        engine: Any
        if self._injected_factory is not None:
            engine = self._injected_factory(config, context)
        elif config.environment is Environment.LIVE:
            if config.product_track is not ProductTrack.SPOT:
                raise DomainError("LIVE_DERIVATIVES_ENGINE_UNAVAILABLE")
            from coinfly.operations.live_arming import LiveMandateStore
            from coinfly.venues.coinbase.live_spot_engine import LiveSpotEngine

            store = LiveMandateStore(self.writer)
            engine = LiveSpotEngine(config, context, self.writer, self.reducer, self.interlock,
                                    transport_factory=lambda: self._live_transport(config, context),
                                    mandate_check=lambda cfg, now: store.covering(cfg, now=now) is not None,
                                    on_change=self._publish)
        else:
            engine = LocalPaperEngine(config, context, self.writer, self.reducer,
                                      self.interlock, ready=self._require_bootstrap,
                                      on_change=self._publish)
        require_execution_engine(engine)
        return cast(EngineLifecycle, engine)

    def _live_transport(self, config: EngineConfig, context: ContextKey) -> Any:
        """Positive native composition path; absent external capabilities remain explicit."""
        from coinfly.operations.live_arming import LiveMandateStore, live_arming_prerequisites
        services = self._live_services
        if services is None:
            raise DomainError("LIVE_NATIVE_COMPOSITION_REQUIRED")
        now = datetime.now(timezone.utc)
        enrollment = services.enrollment_for(config, now)
        enrollment.require_current(config, context, now=now)
        items = live_arming_prerequisites(config, now=now, evidence=enrollment.account,
            local=services.adapter_factory.local_qualifications, credential_bound=enrollment.credential.active,
            dataset_ready=self._dataset_readiness(), kernel_ready=self.kernels.readiness(),
            mandate=LiveMandateStore(self.writer).covering(config, now=now),
            qualification_record=enrollment.qualification_record)
        missing = [item.code for item in items if not item.satisfied]
        if missing:
            raise DomainError("LIVE_PREREQUISITES_UNSATISFIED", ", ".join(missing))
        return services.transport(config, context, now=now, transports=self.coordinator.transports)

    def _realm_for(self, request: RPCRequest) -> str:
        status = self.coordinator.status(self.authority_id)
        if status.effective_context is not None and status.effective_context.environment == Environment.LIVE:
            return "LIVE"
        if status.operation is not None and status.operation.target_context.environment == Environment.LIVE:
            return "LIVE"
        candidate = request.params.get("config")
        if isinstance(candidate, Mapping) and candidate.get("environment") == "LIVE":
            return "LIVE"
        # context.apply carries a worker-owned preview ID. Its candidate is read
        # from the durable preview instead of trusting a client environment field.
        if request.method == "context.apply":
            preview_id = request.params.get("preview_id")
            raw = self.writer.call(lambda connection: connection.execute(
                "SELECT preview_json FROM cf_previews WHERE preview_id=?", (preview_id,)).fetchone())
            if raw is not None and json.loads(raw[0])["config"]["environment"] == "LIVE":
                return "LIVE"
        return "SANDBOX"

    def start(self, *, server: LocalRPCServer | None = None) -> WorkerSupervisor:
        if self._server is not None:
            raise DomainError("WORKER_ALREADY_STARTED")
        policy = (self._live_services.authorization if self._live_services is not None
                  else OwnerOnlySandboxPolicy(realm_resolver=self._realm_for))
        if server is not None:
            server.authorization = policy
        self._server = server or LocalRPCServer(self._socket_path, self.handle, authorization=policy,
                                              cleanup_stale=True).start()
        self._bootstrap_thread = threading.Thread(target=self._run_bootstrap_loop,
                                                   name="coinfly-bootstrap", daemon=False)
        self._bootstrap_thread.start()
        self._bootstrap_started.wait(timeout=5)
        return self

    def _run_bootstrap_loop(self) -> None:
        async def run() -> None:
            self._bootstrap_loop = asyncio.get_running_loop()
            self._bootstrap_stop = asyncio.Event()
            self._bootstrap_started.set()
            queue = self.bootstrap.subscribe()

            async def consume() -> None:
                last_state: str | None = None
                while True:
                    progress = await queue.get()
                    # Presentation must not depend on acquiring a ledger write lock.
                    if self._server is not None:
                        self._server.publish_notification("bootstrap.progress", {
                            **asdict(progress), "stage": progress.state.value, "ready": progress.ready})
                    try:
                        if progress.state.value != last_state:
                            await asyncio.to_thread(self._audit_bootstrap, progress.state.value)
                            last_state = progress.state.value
                        await asyncio.to_thread(self._publish)
                    except sqlite3.Error:
                        logging.getLogger(__name__).warning("Bootstrap ledger publication unavailable")

            consumer = asyncio.create_task(consume())
            try:
                await self.bootstrap.ensure()
                await self._bootstrap_stop.wait()
            finally:
                self.bootstrap.cancel()
                self.bootstrap.unsubscribe(queue)
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

        asyncio.run(run())

    def _audit_bootstrap(self, stage: str) -> None:
        status = self.coordinator.status(self.authority_id)
        context = status.effective_context or ContextKey(
            authority_id=self.authority_id, environment=Environment.SANDBOX,
            product_track=default_config().product_track, broker_profile_id="paper-local", epoch=0)
        scope = status.effective_config.execution_scope[0] if status.effective_config else "paper-book"
        self.writer.call(lambda connection: event_tx(connection, context, scope,
                                                     "BOOTSTRAP_" + stage, "bootstrap", str(uuid4())))

    def _bootstrap_action(self, action: str) -> dict[str, object]:
        loop = self._bootstrap_loop
        if loop is None:
            raise DomainError("BOOTSTRAP_NOT_STARTED")
        if action == "retry":
            asyncio.run_coroutine_threadsafe(self.bootstrap.retry(), loop)
        elif action == "pause":
            loop.call_soon_threadsafe(self.bootstrap.pause)
        elif action == "cancel":
            loop.call_soon_threadsafe(self.bootstrap.cancel)
        else:
            raise DomainError("BOOTSTRAP_ACTION_UNAVAILABLE")
        return {"accepted": True, "action": action}

    def snapshot(self) -> dict[str, Any]:
        # Same ordering as mutations: context interlock, publication, engine.
        # Read the effective context and its registered engine as one handover.
        with self.interlock.lock, self._publication_lock:
            status = self.coordinator.status(self.authority_id)
            config = status.effective_config
            operation = status.operation
            if operation is None and self._last_operation_id is not None:
                operation = self.coordinator.operation(self._last_operation_id)
            proposed = (operation.proposed_config if operation is not None else config) or default_config()
            context = status.effective_context
            engine = self._engines.get(context.identity) if context is not None else None
            progress = self.bootstrap.progress
            bootstrap = {**asdict(progress), "state": progress.state.value,
                         "stage": progress.state.value, "ready": progress.ready}
            slot_rows = [slot.model_dump(mode="json") for slot in self.slots.list(context)] if context else []
            for row in slot_rows:
                row["lifecycle"] = row["engine_state"]
                row["track"] = row["context"]["product_track"]
            alarms = self.writer.call(lambda connection: [dict(row) for row in connection.execute(
                "SELECT alarm_id,reason,acknowledged,context_id,created_at FROM ex_alarms "
                "WHERE authority_id=? ORDER BY created_at DESC LIMIT 100", (self.authority_id,))])
            balance: dict[str, Any] = {
                "environment": context.environment.value if context else "SANDBOX",
                "reporting_currency": config.reporting_currency if config else "USD",
                "coverage": "UNKNOWN_UNIVERSE", "total": None, "settled_assets": None,
                "active_book_available": None, "included_scopes": 0, "expected_scopes": None,
                "label": "NOT INITIALIZED — NO PAPER BOOK" if context is None else "UNVERIFIED — NO ENGINE BOOK",
                "simulated": context is None or context.environment == Environment.SANDBOX,
                "valuation_provenance": "NONE",
            }
            if engine is not None and hasattr(engine, "balance_snapshot"):
                balance = engine.balance_snapshot()
            return {"application_id": __application_id__, "version": __version__,
                    "worker_instance_id": self._instance_id, "runtime": dict(self.runtime), "build": dict(self.build),
                    "retirement": dict(self._retirement) if self._retirement is not None else None,
                    "diagnostics": {"log_file": self._log_file_name()},
                    "ledger_sequence": self.writer.call(lambda connection: int(connection.execute(
                        "SELECT COALESCE(MAX(sequence),0) FROM ex_events").fetchone()[0])),
                    "sequence": self._sequence, "authority_id": self.authority_id,
                    "context_epoch": status.transition_epoch, "config_revision": status.effective_revision,
                    "effective_config": config.model_dump(mode="json") if config else None,
                    "proposed_config": proposed.model_dump(mode="json"),
                    "effective_context": context.model_dump(mode="json") if context else None,
                    "engine_state": status.engine_state.value, "armed": status.armed,
                    "admissions_fenced": status.admissions_fenced,
                    "recovery_required": status.recovery_required,
                    "service_entity": "LOCAL PAPER SIMULATOR" if context and context.environment == Environment.SANDBOX else "UNVERIFIED",
                    "keystore_state": "UNAVAILABLE", "bootstrap": bootstrap, "balance": balance,
                    "slots": slot_rows, "alarms": alarms,
                    "transition": operation.model_dump(mode="json") if operation else None,
                    "release_gates": self._release_checks(),
                    "capabilities": self._capability_matrix(),
                    "automation": self.automation.status() if hasattr(self, "automation") else None,
                    "kernel": self.kernels.describe() if hasattr(self, "kernels") else None,
                    "live_available": self._live_available()}

    def _capability_matrix(self) -> list[dict[str, Any]]:
        """All eight policy × mode × product combinations, resolved for this worker.

        Account evidence enters only through the worker's verified enrollment;
        with none enrolled every LIVE cell explains what is missing.
        """
        from datetime import datetime, timezone

        from coinfly.venues.capability import matrix

        factory = self._live_services.adapter_factory if self._live_services is not None else AdapterFactory()
        rows = []
        for decision in matrix(evidence=self._account_evidence(), now=datetime.now(timezone.utc),
                               local=factory.local_qualifications):
            rows.append({
                "environment": decision.request.environment.value, "region_policy": decision.request.region_policy.value,
                "product_track": decision.request.product_track.value, "product_subtype": decision.subtype.value,
                "category": decision.category.value, "available": decision.available,
                "route": decision.route.value if decision.route else None,
                "service_entity": decision.service_entity, "codes": list(decision.all_blocking_codes),
                "explanation": decision.explanation, "resolver_version": decision.resolver_version,
            })
        return rows

    def _account_evidence(self) -> Any:
        """Read the native enrollment's bounded verified cache, not the keychain."""
        config = self.coordinator.status(self.authority_id).effective_config
        if self._live_services is None or config is None or config.environment is not Environment.LIVE:
            return None
        from datetime import datetime, timezone
        try:
            return self._live_services.enrollment_for(config, datetime.now(timezone.utc)).account
        except DomainError:
            return None

    def _live_available(self) -> bool:
        config = self.coordinator.status(self.authority_id).effective_config
        if self._live_services is None or config is None or config.environment is not Environment.LIVE:
            return False
        from datetime import datetime, timezone
        try:
            return self._live_services.availability(config, now=datetime.now(timezone.utc)).available
        except DomainError:
            return False

    def _log_file_name(self) -> str | None:
        from coinfly.observability.diagnostics import configured_log_names

        names = configured_log_names()
        return names[0] if names else None

    @staticmethod
    def _release_checks() -> list[dict[str, Any]]:
        from coinfly.security.release_gates import live_release_gates
        return [{"code": gate.code, "satisfied": gate.satisfied, "detail": gate.evidence_required}
                for gate in live_release_gates()]

    def _publish(self) -> None:
        if self._closed or not hasattr(self, "slots"):
            return
        with self.interlock.lock, self._publication_lock:
            self._sequence += 1
            current = self.snapshot()
            server = self._server
            if server is not None:
                server.publish_notification("state.snapshot", current)

    @staticmethod
    def _integer(params: Mapping[str, Any], key: str, *, default: int | None = None) -> int:
        value = params.get(key, default)
        if type(value) is not int or value < 0 or value >= 2**63:
            raise DomainError("INVALID_COMMAND_REVISION")
        return value

    @staticmethod
    def _string(params: Mapping[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 160:
            raise DomainError("INVALID_COMMAND_IDENTIFIER")
        return value

    HANDSHAKE_EXEMPT = frozenset({"worker.hello", "worker.shutdown"})

    def handle(self, request: RPCRequest, peer: PeerIdentity) -> Any:
        # Native peer policy has already authenticated this connection; the
        # handshake binds it to a protocol/build identity for every command.
        if request.method == "worker.hello":
            return self._hello(request, peer)
        if peer.connection_id is not None and request.method not in READ_METHODS and request.method not in self.HANDSHAKE_EXEMPT:
            handshake = self._handshakes.get(peer.connection_id)
            if handshake is None:
                raise RPCError("HANDSHAKE_REQUIRED")
            if not handshake["build_match"]:
                raise RPCError("WORKER_BUILD_MISMATCH")
        try:
            return self._handle(request)
        except ValidationError:
            raise RPCError("INVALID_COMMAND") from None

    def _hello(self, request: RPCRequest, peer: PeerIdentity) -> dict[str, Any]:
        """Protocol and build handshake: refuse a different protocol or application; report a different build."""
        params = request.params
        protocol = params.get("protocol_version")
        if type(protocol) is not int or protocol != PROTOCOL_VERSION:
            raise DomainError("PROTOCOL_VERSION_MISMATCH", f"PROTOCOL_VERSION_MISMATCH: worker {PROTOCOL_VERSION}, client {protocol!r}")
        if params.get("application_id") != self.build["application_id"]:
            raise DomainError("APPLICATION_MISMATCH")
        fingerprint = params.get("source_fingerprint")
        build_match = isinstance(fingerprint, str) and fingerprint == self.build["source_fingerprint"]
        record = {"build_match": build_match, "client_version": str(params.get("version", ""))[:40], "connection_id": peer.connection_id}
        if peer.connection_id is not None:
            if len(self._handshakes) >= 256:
                oldest = next(iter(self._handshakes))
                self._handshakes.pop(oldest, None)
            self._handshakes[peer.connection_id] = record
        return {"protocol_version": PROTOCOL_VERSION, "build": dict(self.build), "worker_instance_id": self._instance_id,
                "build_match": build_match, "connection_id": peer.connection_id}

    def _handle(self, request: RPCRequest) -> Any:
        params = request.params
        method = request.method
        if method in {"state.snapshot", "state.subscribe"}:
            return self.snapshot()
        if method == "state.unsubscribe":
            return {"subscribed": False}
        if method == "context.preview":
            config = decode_operator_config(thaw(params.get("config")))
            preview = self.coordinator.preview(config, expected_revision=self._integer(params, "expected_revision"),
                                               expected_epoch=self._integer(params, "expected_epoch", default=request.epoch))
            return {**preview.model_dump(mode="json"), "source": self.snapshot()["effective_config"],
                    "source_drain_remains_live": self._realm_for(request) == "LIVE",
                    "drain_policy": DrainPolicy().model_dump(mode="json"),
                    "bootstrap_ready": self.bootstrap.progress.ready,
                    "target": preview.config.model_dump(mode="json")}
        if method == "context.apply":
            if params.get("confirmation") is not True:
                raise DomainError("CONTEXT_CONFIRMATION_REQUIRED")
            self._require_bootstrap()
            operation = self.coordinator.apply(
                self._string(params, "preview_id"), self._string(params, "config_digest"),
                expected_revision=self._integer(params, "expected_revision"),
                expected_epoch=self._integer(params, "expected_epoch", default=request.epoch),
                request_id=request.request_id,
                drain_policy=DrainPolicy.model_validate_json(json.dumps(thaw(params.get("drain_policy", {})))))
            self._last_operation_id = operation.operation_id
            self._publish()
            return operation.model_dump(mode="json")
        if method == "operation.get":
            if "operation_id" not in params:
                original_id = self._string(params, "request_id")
                def receipt(connection: sqlite3.Connection) -> str | None:
                    row = connection.execute("SELECT operation_id FROM cf_transition_operations "
                        "WHERE authority_id=? AND request_id=?", (self.authority_id, original_id)).fetchone()
                    if row is None:
                        row = connection.execute("SELECT operation_id FROM cf_slot_receipts WHERE request_id=?",
                                                 (original_id,)).fetchone()
                    return str(row[0]) if row is not None and row[0] is not None else None
                operation_id = self.writer.call(receipt)
                if operation_id is None:
                    raise DomainError("OPERATION_UNKNOWN")
            else:
                operation_id = self._string(params, "operation_id")
            try:
                return self.coordinator.operation(operation_id).model_dump(mode="json")
            except DomainError as error:
                if error.code != "OPERATION_UNKNOWN":
                    raise
                return self.slots.operation(operation_id).model_dump(mode="json")
        if method == "operation.retry":
            operation_id = self._string(params, "operation_id")
            try:
                self.coordinator.operation(operation_id)
            except DomainError as error:
                if error.code != "OPERATION_UNKNOWN":
                    raise
                return self.slots.retry(operation_id).model_dump(mode="json")
            return self.coordinator.retry(operation_id).model_dump(mode="json")
        if method == "engine.arm":
            if params.get("confirmation") is not True:
                raise DomainError("ARM_CONFIRMATION_REQUIRED")
            self._require_bootstrap()
            return self.coordinator.arm(self.authority_id,
                                       expected_revision=self._integer(params, "expected_revision"),
                                       expected_epoch=self._integer(params, "expected_epoch", default=request.epoch),
                                       request_id=request.request_id).model_dump(mode="json")
        if method == "safety.pause_entries":
            return self.coordinator.pause_entries(self.authority_id).model_dump(mode="json")
        if method == "safety.stop":
            return self._safety_stop(request)
        if method.startswith("slot."):
            return self._slot_command(request)
        if method == "alarm.acknowledge":
            alarm_id = self._string(params, "alarm_id")
            count = self.writer.call(lambda connection: connection.execute(
                "UPDATE ex_alarms SET acknowledged=1 WHERE alarm_id=? AND authority_id=?",
                (alarm_id, self.authority_id)).rowcount)
            if not count:
                raise DomainError("ALARM_UNKNOWN")
            self._publish()
            return {"acknowledged": True, "fence_cleared": False}
        if method == "bootstrap.status":
            return self.snapshot()["bootstrap"]
        if method.startswith("bootstrap."):
            return self._bootstrap_action(method.split(".")[1])
        if method == "worker.shutdown":
            return self._request_shutdown(force=params.get("force") is True)
        if method.startswith("automation."):
            return self._automation_command(request)
        if method.startswith("live."):
            return self._live_command(request)
        if method == "credentials.status":
            return {"state": "UNAVAILABLE", "reason": "SIGNED_HELPER_REQUIRED"}
        if method == "credentials.enroll":
            raise DomainError("SIGNED_HELPER_REQUIRED")
        if method == "history.open":
            environment = params.get("environment", "SANDBOX")
            if not isinstance(environment, str):
                raise DomainError("INVALID_HISTORY_QUERY")
            query = HistoryQuery(self.authority_id, environment, str(params.get("mode", "ALL_EVENTS")),
                                 scope_id=params.get("scope_id"), instrument=params.get("instrument"),
                                 search=str(params.get("search", "")))
            return self.history.open(query)
        if method == "history.page":
            return self.history.page(self._string(params, "snapshot_id"), cursor=params.get("cursor"),
                                     direction=str(params.get("direction", "next")))
        if method == "history.close":
            self.history.close(self._string(params, "snapshot_id"))
            return {"closed": True}
        raise DomainError("METHOD_NOT_ALLOWED")

    def _live_command(self, request: RPCRequest) -> dict[str, Any]:
        """Live arming prerequisites and the explicit mandate; nothing here arms anything."""
        from datetime import datetime, timezone

        from coinfly.operations.live_arming import (
            LiveMandateStore,
            live_arming_prerequisites,
            mandate_from_params,
            prerequisites_report,
        )
        from coinfly.venues.coinbase.adapter_factory import default_local_qualifications

        now = datetime.now(timezone.utc)
        store = LiveMandateStore(self.writer)
        params = request.params
        if request.method == "live.record_mandate":
            if params.get("confirmation") is not True:
                raise DomainError("LIVE_MANDATE_CONFIRMATION_REQUIRED")
            mandate = store.record(mandate_from_params(params, authority_id=self.authority_id, now=now), now=now)
            self._publish()
            return {"mandate_id": mandate.mandate_id, "digest": mandate.digest, "expires_at": mandate.expires_at.isoformat()}
        if request.method == "live.revoke_mandate":
            # Share the dispatch interlock: once this operation returns, no new
            # entry can start with the revoked authority, even during inference.
            with self.interlock.lock:
                self.coordinator.pause_entries(self.authority_id)
                revoked = store.revoke(self._string(params, "mandate_id"), now=now)
            self._publish()
            return {"revoked": revoked}
        raw = params.get("config")
        config = decode_operator_config(thaw(raw)) if isinstance(raw, Mapping) else self.coordinator.status(self.authority_id).effective_config
        if config is None:
            raise DomainError("NO_EFFECTIVE_CONTEXT")
        enrollment = None
        if self._live_services is not None and config.environment is Environment.LIVE:
            try:
                enrollment = self._live_services.enrollment_for(config, now)
            except DomainError:
                pass
        items = live_arming_prerequisites(
            config, now=now, evidence=enrollment.account if enrollment is not None else None,
            local=self._live_services.adapter_factory.local_qualifications if self._live_services is not None else default_local_qualifications(),
            credential_bound=enrollment is not None and enrollment.credential.active,
            dataset_ready=self._dataset_readiness(), kernel_ready=self.kernels.readiness(),
            qualification_record=enrollment.qualification_record if enrollment is not None else None,
            mandate=store.covering(config, now=now))
        return {**prerequisites_report(items), "config_digest": config.digest, "environment": config.environment.value}

    def _automation_command(self, request: RPCRequest) -> dict[str, Any]:
        from datetime import datetime, timezone

        from coinfly.strategy.definition import StrategyDefinition, research_paper_preset

        params = request.params
        action = request.method.split(".", 1)[1]
        if action == "configure":
            raw_strategy = params.get("strategy")
            preset = params.get("preset")
            if isinstance(raw_strategy, Mapping):
                try:
                    # JSON mode: enums/decimals arrive as strings over IPC (strict models).
                    strategy = StrategyDefinition.model_validate_json(json.dumps(thaw(raw_strategy)))
                except (ValidationError, TypeError, ValueError):
                    raise DomainError("INVALID_STRATEGY_DEFINITION") from None
            elif preset == "malecns-autonomous":
                from coinfly.strategy.autopilot import automatic_strategy
                from coinfly.strategy.definition import DirectionPermission, NeuralSettings
                effective = self.coordinator.status(self.authority_id).effective_config
                if effective is None:
                    raise DomainError("NO_EFFECTIVE_CONTEXT")
                try:
                    neural = NeuralSettings.model_validate_json(json.dumps(thaw(params.get("neural") or {})))
                    strategy = automatic_strategy(effective, capital=Decimal(str(params.get("capital_budget", ""))),
                        neural=neural, max_active_slots=int(str(params.get("max_active_slots", "1"))),
                        directions=DirectionPermission(str(params.get("directions", "LONG_ONLY"))))
                except (ArithmeticError, ValidationError, TypeError, ValueError):
                    raise DomainError("INVALID_STRATEGY_DEFINITION") from None
            elif preset == "research-paper-spot":
                from decimal import InvalidOperation

                try:
                    budget = Decimal(str(params.get("capital_budget", "100")))
                except InvalidOperation:
                    raise DomainError("INVALID_CAPITAL_BUDGET") from None
                strategy = research_paper_preset(capital_budget=budget)
            else:
                raise DomainError("STRATEGY_REQUIRED")
            from decimal import InvalidOperation

            try:
                approved = Decimal(str(params.get("approved_capital", str(strategy.capital_budget))))
            except InvalidOperation:
                raise DomainError("INVALID_CAPITAL_BUDGET") from None
            feed_raw = params.get("feed")
            try:
                feed = FeedSelection.model_validate_json(json.dumps(thaw(feed_raw))) if isinstance(feed_raw, Mapping) else FeedSelection()
                recovery_raw = params.get("recovery")
                recovery = RecoveryPolicy.model_validate_json(json.dumps(thaw(recovery_raw))) if isinstance(recovery_raw, Mapping) else RecoveryPolicy()
                config = AutomationConfig(strategy=strategy, approved_capital=approved, feed=feed, recovery=recovery,
                                          research_mode=params.get("research_mode") is True,
                                          auto_manage_slots=params.get("auto_manage_slots", True) is not False,
                                          pipeline_test_mode=params.get("pipeline_test_mode") is True,
                                          configured_at=datetime.now(timezone.utc), request_id=request.request_id)
            except ValidationError:
                raise DomainError("INVALID_AUTOMATION_CONFIG") from None
            result = self.automation.configure(config)
            self._publish()
            return result
        if action == "enable":
            self._require_bootstrap()
            result = self.automation.enable(expected_revision=self._integer(params, "expected_revision"),
                                            expected_epoch=self._integer(params, "expected_epoch", default=request.epoch),
                                            request_id=request.request_id)
            self._publish()
            return result
        if action == "resume_entries":
            result = self.automation.resume_entries(expected_revision=self._integer(params, "expected_revision"),
                                                    expected_epoch=self._integer(params, "expected_epoch", default=request.epoch),
                                                    request_id=request.request_id)
            self._publish()
            return result
        if action == "pause_entries":
            result = self.automation.pause_entries()
            self._publish()
            return result
        if action == "stop":
            result = self.automation.stop_and_reconcile()
            self._publish()
            return result
        if action == "status":
            return self.automation.status()
        raise DomainError("METHOD_NOT_ALLOWED")

    RETIREMENT_TIMEOUT_SECONDS = 600.0

    def _managed_exposure(self) -> dict[str, Any]:
        """What this worker is currently responsible for: positions, open intents, an enabled automation."""
        status = self.coordinator.status(self.authority_id)
        context = status.effective_context
        health = self.automation._execution_health(context)
        positions: list[tuple[str, str]] = []
        if context is not None:
            positions = self.writer.call(lambda connection: [(str(r[0]), str(r[1])) for r in connection.execute(
                "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (context.identity,)) if Decimal(str(r[1])) != 0])
        automation = self.automation.status()
        return {"positions": positions, "open_intents": int(health["open_intents"]), "unknown_outcomes": int(health["unknown"]),
                "automation_enabled": bool(automation.get("enabled")), "automation_state": automation.get("state")}

    def _request_shutdown(self, *, force: bool = False) -> dict[str, Any]:
        """Retire this worker: never abruptly while it manages positions or open orders.

        Entries are fenced at once. If nothing is managed (no position, no open
        intent, automation not enabled) the launcher stops the process after
        the reply. Otherwise the worker retires gracefully: automation is
        stopped and reconciled (protected drains), and the process exits only
        once every intent is terminal and the book is flat — or the bounded
        wait expires and the worker stays alive, reporting
        ``WORKER_RETIREMENT_BLOCKED``. ``force`` (an explicit operator choice)
        fences and exits immediately; the ledger, fences and unresolved
        obligations are then recovered by the next worker start, nothing is
        deleted.
        """
        self.coordinator.pause_entries(self.authority_id)
        if self._shutdown_request is None:
            raise DomainError("WORKER_SHUTDOWN_UNAVAILABLE")
        exposure = self._managed_exposure()
        managed = bool(exposure["positions"] or exposure["open_intents"] or exposure["automation_enabled"])
        if force or not managed:
            timer = threading.Timer(0.2, self._shutdown_request)
            timer.daemon = True
            timer.start()
            return {"accepted": True, "mode": "FORCED" if force and managed else "IMMEDIATE", "worker_instance_id": self._instance_id,
                    "exposure": exposure}
        if self._retirement is None:
            self._retirement = {"requested_at": datetime.now(timezone.utc).isoformat(), "mode": "GRACEFUL", "exposure": exposure}
            try:
                if exposure["automation_enabled"] or exposure["automation_state"] not in ("STOPPED", "NOT_CONFIGURED"):
                    self.automation.stop_and_reconcile()
            except DomainError as error:
                self._retirement["stop_error"] = error.code
            thread = threading.Thread(target=self._retire_when_flat, name="coinfly-worker-retirement", daemon=True)
            thread.start()
        return {"accepted": True, "mode": "GRACEFUL", "worker_instance_id": self._instance_id, "exposure": exposure,
                "retirement": dict(self._retirement)}

    def _retire_when_flat(self) -> None:
        deadline = time.monotonic() + self.RETIREMENT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                exposure = self._managed_exposure()
            except Exception:
                exposure = {"positions": ["?"], "open_intents": 1, "unknown_outcomes": 0}
            if not exposure["positions"] and not exposure["open_intents"] and not exposure["unknown_outcomes"]:
                if self._retirement is not None:
                    self._retirement["completed_at"] = datetime.now(timezone.utc).isoformat()
                if self._shutdown_request is not None:
                    self._shutdown_request()
                return
            time.sleep(0.5)
        if self._retirement is not None:
            self._retirement["code"] = "WORKER_RETIREMENT_BLOCKED"
            self._retirement["exposure"] = self._managed_exposure()

    def _safety_stop(self, request: RPCRequest) -> dict[str, Any]:
        """Fence entries and request every independent slot's existing safe drain.

        Existing Edit/Delete operations already drain their immutable source. An
        Edit replacement remains stopped, so global Stop can join that work
        without canceling it or issuing a competing close. Individual failures
        remain explicit while the remaining slots still receive their commands.
        """
        self.coordinator.pause_entries(self.authority_id)
        status = self.coordinator.status(self.authority_id)
        if status.operation is not None:
            return self.coordinator.retry(status.operation.operation_id).model_dump(mode="json")
        operations: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if status.effective_context is not None:
            for snapshot_slot in self.slots.list(status.effective_context):
                slot = snapshot_slot
                try:
                    slot = self.slots.get(snapshot_slot.slot_id)
                    operation = self._stop_or_join_slot(slot, request.request_id)
                    operations.append(operation.model_dump(mode="json"))
                except Exception as error:
                    code = error.code if isinstance(error, DomainError) else "SLOT_STOP_COMMAND_FAILED"
                    recorded = self._record_safety_stop_error(slot, request.request_id, code)
                    errors.append({
                        "slot_id": slot.slot_id, "context": slot.context.model_dump(mode="json"),
                        "operation_id": slot.operation_id, "code": code,
                        "engine_state": slot.engine_state.value, "tombstoned": slot.tombstoned,
                        "recorded": recorded,
                    })
        # Each slot command owns its short admission lock. Reassert the pause at
        # completion so an overlapping operator Arm cannot leave this Stop's
        # response claiming a fence that its own command batch has not retained.
        self.coordinator.pause_entries(self.authority_id)
        return {"operations": operations, "errors": errors, "admissions_fenced": True}

    def _stop_or_join_slot(self, slot: SlotRecord, request_id: str) -> SlotOperation:
        if slot.operation_id is not None:
            operation = self.slots.operation(slot.operation_id)
            if operation.slot_id != slot.slot_id or operation.action not in {"STOP", "DELETE", "EDIT"}:
                raise DomainError("SLOT_OPERATION_NOT_SAFETY_DIRECTED")
            if operation.phase == SlotOperationPhase.COMPLETED:
                return operation
            try:
                return self.slots.retry(operation.operation_id)
            except DomainError as error:
                # Completion can commit between observing the slot and asking to
                # join it. Return the durable completion instead of a false error.
                if error.code != "OPERATION_NOT_RETRYABLE":
                    raise
                current = self.slots.operation(operation.operation_id)
                if current.phase != SlotOperationPhase.COMPLETED:
                    raise
                return current
        if slot.tombstoned:
            raise DomainError("TOMBSTONED_SLOT_REQUIRES_RECOVERY")
        return self.slots.stop(
            slot.slot_id, expected_revision=slot.revision, expected_epoch=slot.context.epoch,
            request_id=str(uuid5(NAMESPACE_URL, f"coinfly:{request_id}:{slot.slot_id}")),
        )

    def _record_safety_stop_error(self, slot: SlotRecord, request_id: str, code: str) -> bool:
        evidence_id = str(uuid5(NAMESPACE_URL, f"coinfly:safety-stop:{request_id}:{slot.slot_id}"))
        def record(connection: sqlite3.Connection) -> None:
            alarm_tx(connection, slot.context, slot.slot_id, code, evidence_id)
            existing = connection.execute(
                "SELECT 1 FROM ex_events WHERE authority_id=? AND entity_id=? AND evidence_id=? AND event_kind='SAFETY_STOP_SLOT_FAILED'",
                (slot.context.authority_id, slot.slot_id, evidence_id),
            ).fetchone()
            if existing is None:
                event_tx(connection, slot.context, slot.scope_id, "SAFETY_STOP_SLOT_FAILED", slot.slot_id, evidence_id)
        try:
            self.writer.call(record)
        except Exception:
            # A writer failure cannot be described as a durable incident. The
            # response still reports the unchanged slot and explicit error.
            return False
        return True

    def _slot_command(self, request: RPCRequest) -> dict[str, Any]:
        params = request.params
        status = self.coordinator.status(self.authority_id)
        context = status.effective_context
        if context is None or status.effective_config is None:
            raise DomainError("NO_EFFECTIVE_CONTEXT")
        if request.method == "slot.create":
            self._require_bootstrap()
            slot_id = str(uuid5(NAMESPACE_URL, f"coinfly:{self.authority_id}:{request.request_id}"))
            scope = status.effective_config.execution_scope[0]
            try:
                existing = self.slots.get(slot_id)
            except DomainError as error:
                if error.code != "SLOT_UNKNOWN":
                    raise
            else:
                context, scope = existing.context, existing.scope_id
            if self._integer(params, "expected_epoch", default=request.epoch) != context.epoch:
                raise DomainError("STALE_CONTEXT_EPOCH")
            slot = self.slots.create(slot_id=slot_id, context=context,
                                     scope_id=scope,
                                     instrument=self._string(params, "instrument"),
                                     expected_config_revision=self._integer(params, "expected_revision"),
                                     request_id=request.request_id)
            return slot.model_dump(mode="json")
        slot_id = self._string(params, "slot_id")
        stored = self.slots.get(slot_id)
        if stored.context.authority_id != self.authority_id:
            raise DomainError("AUTHORITY_MISMATCH")
        revision = self._integer(params, "expected_revision")
        epoch = self._integer(params, "expected_epoch", default=request.epoch)
        if request.method == "slot.edit":
            self._require_bootstrap()
            changes = params.get("changes", params)
            return self.slots.edit(slot_id, expected_revision=revision, expected_epoch=epoch,
                                   instrument=self._string(changes, "instrument"),
                                   request_id=request.request_id).model_dump(mode="json")
        if request.method not in {"slot.start", "slot.stop", "slot.delete"}:
            raise DomainError("METHOD_NOT_ALLOWED")
        if request.method == "slot.start":
            return self.slots.start(slot_id, expected_revision=revision, expected_epoch=epoch,
                                   request_id=request.request_id).model_dump(mode="json")
        if request.method == "slot.stop":
            return self.slots.stop(slot_id, expected_revision=revision, expected_epoch=epoch,
                                  request_id=request.request_id).model_dump(mode="json")
        return self.slots.delete(slot_id, expected_revision=revision, expected_epoch=epoch,
                                 request_id=request.request_id).model_dump(mode="json")

    def close(self) -> None:
        if self._closed:
            return
        self.coordinator.pause_entries(self.authority_id)
        self._closed = True
        if self._server is not None:
            self._server.close()
        if self._bootstrap_loop is not None and self._bootstrap_stop is not None:
            self._bootstrap_loop.call_soon_threadsafe(self._bootstrap_stop.set)
        if self._bootstrap_thread is not None:
            self._bootstrap_thread.join(timeout=30)
        if hasattr(self, "automation"):
            self.automation.close()
        self.slots.close()
        self.coordinator.close()
        try:
            self._close_engine_background()
        finally:
            try:
                self.writer.close()
            finally:
                self._ownership.close()

    def _close_engine_background(self) -> None:
        failure: Exception | None = None
        for engine in tuple(self._engines.values()):
            close_background = getattr(engine, "close_background", None)
            if callable(close_background):
                try:
                    close_background()
                except Exception as error:
                    failure = failure or error
        if failure is not None:
            raise failure



def _run_native_worker() -> int:
    from coinfly.operations.qualification import report_storage_failure
    from coinfly.paths import StorageInitializationError, initialize_application_storage
    from coinfly.worker.startup import WorkerStartup

    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    startup: WorkerStartup | None = None
    try:
        from coinfly.observability.diagnostics import configure_worker_log

        application = initialize_application_storage()
        configure_worker_log(application.logs, "signed")
        startup = WorkerStartup().start()
        while not stopped.is_set() and not startup.shutdown_requested.is_set():
            stopped.wait(0.5)
        return 0
    except StorageInitializationError as error:
        return report_storage_failure(error).exit_code
    except (DomainError, RPCError) as error:
        print(f"coinfly: {error.code}", file=sys.stderr)
        return 2
    finally:
        if startup is not None:
            startup.close()


def main() -> int:
    return _run_native_worker()


if __name__ == "__main__":
    raise SystemExit(main())
