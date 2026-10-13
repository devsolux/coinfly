"""Live Coinbase Advanced spot execution engine behind the common ``ExecutionEngine`` interface.

The automation plans exactly as it does on the paper venue; only the venue
differs. This engine binds the documented spot order gateway
(``AdvancedTradeOrderGateway`` over a signed, keychain-backed transport) to the
worker's ledger: intents are admitted by the same ``AdmissionActor``,
dispatched once by the same ``Dispatcher``, acknowledged / rejected / UNKNOWN
by the same ``FactReducer``, and the book the planner sees is rebuilt from the
venue's own account and history routes (``sync``), never from a local
simulation.

What this engine guarantees, precisely:

* **At most one initiation per intent.** The dispatcher claims the intent,
  the signed transport initiates one ``POST /orders`` and never retries; the
  venue's ``client_order_id`` (= intent id) is documented to return the
  existing order for a reused id. A failure after initiation is UNKNOWN —
  neither a creation nor a rejection — and is resolved only from the venue's
  order history by client id (``reconcile_unknown``); elapsed time never
  resolves it. Nothing here is "exactly-once delivery": the guarantee is one
  local attempt plus a documented venue-side dedup key.
* **Positive identity only.** ACCEPTED needs a venue order id; REJECTED only
  for documented definitive reasons.
* **Local authorization is not the broker's.** The signed transport binds the
  request body digest locally (the worker refuses to send a body it did not
  sign for); the broker verifies its own JWT claims (method, host, path,
  expiry). The two are different evidence and are named as such.
* **Continuous sync.** ``sync(now)`` reads balances (``GET /accounts``),
  order states (``GET /orders/historical/batch``) and fills
  (``GET /orders/historical/fills``) for every instrument this context
  trades, reduces them into the ledger, and resolves UNKNOWN intents. The
  admission book is authoritative only when the last sync is fresh.
* **Gated.** ``arm`` needs an unexpired live mandate covering the
  configuration and a transport (an EXECUTION credential in the OS keychain);
  the capability resolver and the arming prerequisites (all listed together)
  are enforced before this engine is even constructed. Spot only: no
  derivative label inherits anything from here.

This module is exercised with a fake transport in isolated tests. No real
order has been placed through it; the recorded qualification run remains an
external prerequisite (see ``docs/runbooks/live-arming.md``).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from uuid import uuid4

from coinfly.config.models import EngineConfig
from coinfly.control.slot_service import SlotRecord, SlotStopProof
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.execution.dispatcher import Dispatcher
from coinfly.execution.fact_reducer import FactReducer, acknowledge_tx, terminal_tx
from coinfly.execution.intent_causes import ensure_schema as ensure_intent_causes_schema
from coinfly.execution.intent_causes import record_cause_tx
from coinfly.execution.intent_store import get_tx
from coinfly.execution.models import IntentKind, OrderIntent, Side
from coinfly.execution.reconciler import EvidenceKind
from coinfly.execution.recovery import (
    assert_context_closed_tx,
    assert_slot_closed_tx,
    context_fact_sequence_tx,
    slot_fact_sequence_tx,
)
from coinfly.execution.weight_planner import InstrumentContract, PlannedDelta, plan_weights
from coinfly.risk.admission_actor import ActiveBookSnapshot, AdmissionActor, AvailableAsset, ExecutionMark
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.stop_certificate import RetirementEvidence, ScopeWatermark, StopCertificate
from coinfly.simulation.paper_matching import DepthLevel, PaperInstrument
from coinfly.storage.writer import Writer
from coinfly.strategy.target_coordinator import BoundTarget
from coinfly.venues.coinbase.advanced_trade_orders import (
    OPEN_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
    AdvancedTradeOrderGateway,
    AuthenticatedTransport,
    CancelOutcome,
)

MARK_MAX_AGE = timedelta(seconds=5)
SYNC_MAX_AGE = timedelta(seconds=30)


class LiveSpotEngine:
    """``ExecutionEngine`` for one LIVE spot context on Coinbase Advanced Trade."""

    def __init__(self, config: EngineConfig, context: ContextKey, writer: Writer, reducer: FactReducer,
                 interlock: ContextInterlock, *, transport_factory: Callable[[], AuthenticatedTransport],
                 mandate_check: Callable[[EngineConfig, datetime], bool], instruments: tuple[PaperInstrument, ...] = (),
                 clock: Callable[[], datetime] | None = None, on_change: Callable[[], None] | None = None) -> None:
        if config.environment is not Environment.LIVE or context.environment is not Environment.LIVE:
            raise DomainError("LIVE_ENGINE_REQUIRES_LIVE_CONTEXT")
        if context.product_track is not ProductTrack.SPOT or config.product_track is not ProductTrack.SPOT:
            raise DomainError("LIVE_ENGINE_SPOT_ONLY")
        if len(config.execution_scope) != 1:
            raise DomainError("LIVE_ENGINE_SINGLE_PORTFOLIO")
        self.config, self.context = config, context
        self.scope = config.execution_scope[0]
        self.writer, self.reducer, self.interlock = writer, reducer, interlock
        self._transport_factory = transport_factory
        self._mandate_check = mandate_check
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._on_change = on_change
        self.admission = AdmissionActor(writer, interlock, clock=self._clock, on_change=on_change)
        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._account_complete = False
        self._external_activity = False
        from coinfly.worker.maintenance import MaintenanceLoop
        self._maintenance = MaintenanceLoop(self.sync, self._maintenance_failed, clock=self._clock)
        self._instruments: dict[str, PaperInstrument] = {item.instrument_id: item for item in instruments}
        self._depth: dict[str, tuple[tuple[DepthLevel, ...], tuple[DepthLevel, ...], datetime]] = {}
        self._balances: dict[str, tuple[Decimal, Decimal]] = {}  # asset -> (available, hold)
        self._last_sync: datetime | None = None
        self._sync_report: dict[str, Any] = {}
        self._gateway: AdvancedTradeOrderGateway | None = None
        self._armed = False
        self._quiesced = False
        self._retired = False
        self._initialized = False
        self._enabled_slots: set[str] = set()
        self._exit_children = 0
        self.writer.call(ensure_intent_causes_schema)

    # ------------------------------------------------------------------ lifecycle
    def initialize(self, config: EngineConfig, context: ContextKey) -> None:
        if config != self.config or context != self.context:
            raise DomainError("LIVE_CONTEXT_MISMATCH")
        self._initialized = True

    def arm(self) -> None:
        with self._lock:
            if self._retired:
                raise DomainError("LIVE_CONTEXT_RETIRED")
            if not self._initialized:
                raise DomainError("LIVE_ENGINE_NOT_INITIALIZED")
            if not self._mandate_check(self.config, self._clock()):
                raise DomainError("LIVE_MANDATE_REQUIRED")
            if self._gateway is None:
                transport = self._transport_factory()  # signer + keychain-backed credential; raises without one
                self._gateway = AdvancedTradeOrderGateway(transport, context=self.context, scope_id=self.scope, writer=self.writer,
                                                          reducer=self.reducer, clock=self._clock)
        from coinfly.operations.live_arming import LiveMandateStore
        LiveMandateStore(self.writer).bind_context(self.config, self.context.identity, now=self._clock())
        try:
            self.sync(self._clock())
            with self._lock:
                if self._retired:
                    raise DomainError("LIVE_CONTEXT_RETIRED")
                self._armed, self._quiesced = True, False
            self._maintenance.start()
        except BaseException:
            with self._lock:
                self._armed = False
            raise

    def _maintenance_failed(self, code: str) -> None:
        with self._lock:
            self._account_complete = False
            self._sync_report = {"code": code, "status": "FAILED"}
        if self._on_change is not None:
            self._on_change()

    def recover_existing(self) -> None:
        """Resume reconciliation only. Recovery never silently rearms entries."""
        self.quiesce()
        with self._lock:
            if self._gateway is None:
                self._gateway = AdvancedTradeOrderGateway(self._transport_factory(), context=self.context,
                    scope_id=self.scope, writer=self.writer, reducer=self.reducer, clock=self._clock)
        self.sync(self._clock())
        self._maintenance.start()

    def close_background(self) -> None:
        self.quiesce()
        self._maintenance.stop(timeout=0)
        gateway = self._gateway
        if gateway is not None:
            retire = getattr(gateway._transport, "retire", None)
            if callable(retire):
                retire()
        if not self._maintenance.stop(timeout=5):
            raise DomainError("NETWORK_RETIREMENT_UNPROVEN")

    def quiesce(self) -> None:
        with self._lock:
            self._quiesced, self._armed = True, False
            self._enabled_slots.clear()

    def retire(self) -> RetirementEvidence:
        with self._lock:
            self._quiesced, self._armed, self._retired = True, False, True
            self._enabled_slots.clear()
            gateway = self._gateway
        # Native transports cancel sockets/signing handles before the maintenance
        # join. Never certify a close merely by losing a Python reference.
        self._maintenance.stop(timeout=0)
        if gateway is not None:
            retire = getattr(gateway._transport, "retire", None)
            if not callable(retire):
                raise DomainError("NETWORK_RETIREMENT_UNPROVEN")
            retire()
        if not self._maintenance.stop(timeout=5):
            raise DomainError("NETWORK_RETIREMENT_UNPROVEN")
        with self._lock:
            self._gateway = None
        return RetirementEvidence(context=self.context, evidence_id=str(uuid4()), mutation_transports_retired=True,
                                  signing_handles_retired=True, all_tasks_joined_or_fenced=True, grants_revoked=True)

    def reset_transient(self) -> None:
        with self._lock:
            if not self._retired:
                raise DomainError("RESET_REQUIRES_RETIREMENT")
            self._depth.clear()
            self._enabled_slots.clear()

    # ------------------------------------------------------------------ instruments and marks
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(self._instruments)

    def ensure_instruments(self, instruments: tuple[PaperInstrument, ...]) -> tuple[str, ...]:
        added = []
        with self._lock:
            for item in instruments:
                existing = self._instruments.get(item.instrument_id)
                if existing is not None:
                    if existing != item:
                        raise DomainError("LIVE_INSTRUMENT_DEFINITION_CHANGED")
                    continue
                if item.product_track is not ProductTrack.SPOT:
                    raise DomainError("LIVE_ENGINE_SPOT_ONLY")
                self._instruments[item.instrument_id] = item
                added.append(item.instrument_id)
        return tuple(added)

    def instrument_contract(self, instrument_id: str) -> PaperInstrument:
        contract = self._instruments.get(instrument_id)
        if contract is None:
            raise DomainError("LIVE_INSTRUMENT_UNKNOWN")
        return contract

    def update_market(self, instrument: str, *, bids: tuple[DepthLevel, ...], asks: tuple[DepthLevel, ...], now: datetime) -> tuple[Any, ...]:
        """Marks only: a live venue fills orders itself; fills arrive through ``sync``."""
        with self._lock:
            if instrument in self._instruments:
                self._depth[instrument] = (bids, asks, now)
        return ()

    def settle(self, now: datetime) -> tuple[dict[str, Any], ...]:
        return ()  # spot: no funding, no expiry

    # ------------------------------------------------------------------ venue sync
    def _require_gateway(self) -> AdvancedTradeOrderGateway:
        if self._gateway is None:
            raise DomainError("LIVE_ENGINE_UNARMED")
        return self._gateway

    def sync(self, now: datetime, *, lookback: timedelta = timedelta(hours=24)) -> dict[str, Any]:
        """Balances, order states, fills and UNKNOWN resolution from the venue's own routes."""
        with self._sync_lock:
            try:
                return self._sync_once(now, lookback=lookback)
            except BaseException:
                with self._lock:
                    self._account_complete = False
                raise

    def _sync_once(self, now: datetime, *, lookback: timedelta) -> dict[str, Any]:
        from coinfly.venues.coinbase.execution_accounts import collect_accounts
        gateway = self._require_gateway()
        account_snapshot = collect_accounts(gateway._transport, self.scope)
        balances = {asset: (available, held) for asset, available, held in account_snapshot.balances}
        report: dict[str, Any] = {"fills_applied": 0, "fills_refused": 0, "orders_terminal": 0, "unknown_resolved": 0,
                                  "unknown_open": 0, "instruments": []}
        oldest = self.writer.call(lambda connection: connection.execute(
            "SELECT MIN(i.created_at) FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
            "WHERE i.context_id=? AND o.terminal=0", (self.context.identity,)).fetchone()[0])
        since = min(now - lookback, datetime.fromisoformat(oldest) - timedelta(minutes=1)) if oldest else now - lookback
        known_ids = self.writer.call(lambda connection: {str(row[0]) for row in connection.execute(
            "SELECT intent_id FROM ex_intents WHERE authority_id=? AND environment='LIVE' AND scope_id=?",
            (self.context.authority_id, self.scope))})
        all_open, complete, _ = gateway.list_open_orders()
        if not complete:
            raise DomainError("ORDER_HISTORY_INCOMPLETE")
        external_activity = any(item.get("client_order_id") not in known_ids for item in all_open)
        report["order_scope"] = "CREDENTIAL_PORTFOLIO_RETAIL_ADVANCED"
        report["external_open_orders"] = sum(item.get("client_order_id") not in known_ids for item in all_open)
        open_before = self._open_intent_ids()
        retained_instruments = self.writer.call(lambda connection: {str(row[0]) for row in connection.execute(
            "SELECT DISTINCT json_extract(payload_json,'$.instrument') FROM ex_intents WHERE context_id=?", (self.context.identity,))})
        for instrument in sorted(set(self._instruments) | retained_instruments):
            orders, complete, _ = gateway.list_orders(product_id=instrument, since=since)
            if not complete:
                raise DomainError("ORDER_HISTORY_INCOMPLETE")
            external_activity = external_activity or any(str(item.get("status")) in OPEN_ORDER_STATUSES
                and item.get("client_order_id") not in known_ids for item in orders)
            by_client = {str(item.get("client_order_id")): item for item in orders if isinstance(item.get("client_order_id"), str)}
            fills = gateway.ingest_fills(product_id=instrument, since=since)
            report["fills_applied"] += sum(1 for item in fills if item.applied)
            report["fills_refused"] += sum(1 for item in fills if not item.applied and item.incident)
            self._apply_order_states(by_client)
            report["instruments"].append({"instrument": instrument, "history_complete": complete, "orders": len(orders), "fills": len(fills)})
        # Intents that became terminal during this sync, whether the fill facts
        # completed them or the venue's documented terminal status did.
        report["orders_terminal"] = len(open_before - self._open_intent_ids())
        unknown_ids = self.writer.call(lambda connection: [str(r[0]) for r in connection.execute(
            "SELECT i.intent_id FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND o.resolution='UNKNOWN' AND o.terminal=0",
            (self.context.identity,))])
        for intent_id in unknown_ids:
            evidence = gateway.reconcile_unknown(intent_id, lookback=max(lookback, now - since))
            # Only positive identity (or a documented non-creation guarantee, which
            # this venue's history route does not provide) resolves an UNKNOWN create.
            # Absence from history is not a rejection: the intent stays fenced.
            if evidence.kind in (EvidenceKind.POSITIVE_IDENTITY, EvidenceKind.AUTHORITATIVE_NONCREATION):
                report["unknown_resolved"] += 1
            else:
                report["unknown_open"] += 1
        if report["fills_refused"]:
            raise DomainError("LIVE_FILL_FACTS_UNRESOLVED")
        with self._lock:
            if self._retired:
                raise DomainError("LIVE_CONTEXT_RETIRED")
            self._balances = balances
            self._account_complete = True
            self._external_activity = external_activity
            self._last_sync = now
            self._sync_report = {**report, "synced_at": now.isoformat()}
        if self._on_change is not None:
            self._on_change()
        return dict(self._sync_report)

    def _open_intent_ids(self) -> frozenset[str]:
        return frozenset(self.writer.call(lambda connection: [str(r[0]) for r in connection.execute(
            "SELECT i.intent_id FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND o.terminal=0",
            (self.context.identity,))]))

    def _apply_order_states(self, by_client: dict[str, Mapping[str, Any]]) -> int:
        """Acknowledge venue order ids and mark documented terminal statuses.

        A terminal status is booked only once the ledger holds the fills the venue
        reports for that order (``filled_size``); until then the intent stays open
        and the next sync completes it. UNKNOWN never becomes a rejection here: the
        venue's history is no non-creation guarantee.
        """
        def save(connection: sqlite3.Connection) -> int:
            count = 0
            rows = connection.execute("SELECT i.intent_id, o.submission, o.terminal, o.filled_quantity, o.resolution "
                                      "FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=?",
                                      (self.context.identity,)).fetchall()
            for row in rows:
                intent_id = str(row[0])
                order = by_client.get(intent_id)
                if order is None or int(row[2]):
                    continue
                if row[4] == "UNKNOWN":
                    continue  # identity is the reconciler's call (reconcile_unknown), never inferred here
                order_id, status = order.get("order_id"), str(order.get("status", ""))
                if isinstance(order_id, str) and order_id and row[1] != "ACKNOWLEDGED":
                    acknowledge_tx(connection, intent_id, order_id, f"cb-history-ack-{intent_id}")
                if status in TERMINAL_ORDER_STATUSES:
                    reported = order.get("filled_size")
                    booked = Decimal(str(row[3] or "0"))
                    try:
                        fills_complete = isinstance(reported, str) and Decimal(reported) == booked
                    except ArithmeticError:
                        fills_complete = False
                    if not fills_complete:
                        continue  # fills lag the order status (or the size is unreadable): the next sync books them first
                    terminal_tx(connection, intent_id, f"cb-history-terminal-{intent_id}-{status}", complete_fills=True)
                    count += 1
                elif status not in OPEN_ORDER_STATUSES and status:
                    pass  # undocumented status: leave the intent as it is; nothing is inferred
            return count
        return int(self.writer.call(save))

    # ------------------------------------------------------------------ admission book
    def observe_execution_book(self) -> ActiveBookSnapshot:
        now = self._clock()
        with self._lock:
            if self._last_sync is None:
                raise DomainError("LIVE_ACCOUNT_NOT_SYNCED")
            fresh = self._account_complete and timedelta(0) <= now - self._last_sync <= SYNC_MAX_AGE
            if not fresh:
                raise DomainError("LIVE_ACCOUNT_SYNC_STALE", f"LIVE_ACCOUNT_SYNC_STALE: last sync {self._last_sync.isoformat()}")
            marks = tuple(ExecutionMark(instrument=instrument, price=(bids[0].price + asks[0].price) / 2)
                          for instrument, (bids, asks, stamp) in self._depth.items()
                          if bids and asks and timedelta(0) <= now - stamp <= MARK_MAX_AGE)
            mark_map = {item.instrument: item.price for item in marks}
            positions = self.writer.call(lambda connection: {str(r[0]): Decimal(r[1]) for r in connection.execute(
                "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (self.context.identity,))})
            cash_available, cash_hold = self._balances.get(self.config.reporting_currency, (Decimal(0), Decimal(0)))
            equity = cash_available + cash_hold
            for instrument, quantity in positions.items():
                if quantity and instrument not in mark_map:
                    raise DomainError("POSITION_MARK_UNVERIFIED")
                equity += quantity * mark_map.get(instrument, Decimal(0))
            available = tuple(AvailableAsset(asset=asset, amount=amount) for asset, (amount, _) in self._balances.items())
            sequence = self.writer.call(lambda connection: context_fact_sequence_tx(connection, self.context))
            book = ActiveBookSnapshot(context=self.context, scope_id=self.scope, evidence_id=str(uuid4()), observed_at=now,
                                      expires_at=now + timedelta(seconds=5), fact_sequence=sequence,
                                      reporting_currency=self.config.reporting_currency, funded_equity=equity,
                                      eligible_collateral=cash_available, available_assets=available, marks=marks,
                                      reservations_in_remote_holds=(), complete_execution_scope=self._account_complete,
                                      authoritative_account_evidence=fresh, conflicting_external_activity=self._external_activity)
        # Loss history for the portfolio book and for the account ("authority")
        # book the live admission actor also checks. With one portfolio in scope
        # the account routes this engine reads ARE that portfolio's accounts, so
        # both books see the same equity; a multi-portfolio account is out of scope.
        for book_id in (self.scope, "authority"):
            self.admission.drawdown.record(self.context, book_id, equity=equity, evidence_id=f"{book.evidence_id}:{book_id}", observed_at=now,
                                           currency=self.config.reporting_currency)
        self.admission.observe(book)
        return book

    # ------------------------------------------------------------------ targets and orders
    def submit_target(self, target: BoundTarget, *, constraint: Callable[[PlannedDelta, ActiveBookSnapshot], PlannedDelta] | None = None,
                      cause: dict[str, Any] | None = None) -> tuple[str, ...]:
        if target.context != self.context or not self._armed or self._quiesced:
            raise DomainError("STALE_OR_UNARMED_TARGET")
        gateway = self._require_gateway()
        book = self.observe_execution_book()
        actual = self.writer.call(lambda connection: {str(row[0]): Decimal(row[1]) for row in connection.execute(
            "SELECT instrument,quantity FROM ex_positions WHERE context_id=?", (self.context.identity,))})

        def pending(connection: sqlite3.Connection) -> dict[str, Decimal]:
            result: dict[str, Decimal] = {}
            for row in connection.execute("SELECT i.payload_json,o.filled_quantity FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
                                          "WHERE i.context_id=? AND o.terminal=0", (self.context.identity,)):
                intent = OrderIntent.model_validate_json(row[0])
                quantity = (intent.quantity - Decimal(row[1])) * (1 if intent.side == Side.BUY else -1)
                result[intent.instrument] = result.get(intent.instrument, Decimal(0)) + quantity
            return result
        contracts = tuple(InstrumentContract(instrument=item.instrument_id, quantity_increment=item.quantity_increment,
                                             price_increment=item.price_increment, minimum_quantity=item.quantity_increment,
                                             minimum_notional=item.minimum_notional) for item in self._instruments.values())
        from coinfly.strategy.capital import planning_book_tx

        planning_book, owned_actual = self.writer.call(
            lambda connection: planning_book_tx(connection, book, target.strategy_identity))
        if owned_actual is not None:
            actual = owned_actual
        planned = plan_weights(target=target, book=planning_book, contracts=contracts, actual_quantities=actual,
                               pending_signed_quantities=self.writer.call(pending), now=self._clock())
        ids: list[str] = []
        for child, delta in enumerate(planned):
            if self._clock() >= target.proposal.expires_at:
                raise DomainError("TARGET_EXPIRED")
            # Each admission is judged against a book at the current fact sequence:
            # the previous child's acknowledgement or fill is a new fact.
            current_book = self.observe_execution_book() if child else book
            if constraint is not None:
                allocated_book, _ = self.writer.call(
                    lambda connection: planning_book_tx(connection, current_book, target.strategy_identity))
                constrained = constraint(delta, allocated_book)
                if (constrained.instrument != delta.instrument or constrained.side != delta.side or constrained.kind != delta.kind
                        or constrained.quantity > delta.quantity or constrained.quantity <= 0):
                    raise DomainError("EXECUTION_CONSTRAINT_INVALID")
                delta = constrained
            contract = self._instruments[delta.instrument]
            intent = OrderIntent(context=self.context, scope_id=self.scope, profile_version=1, signer_version=1,
                                 slot_id=self._slot_for(delta.instrument), allocation_revision=target.proposal.allocation_revision,
                                 instrument=delta.instrument, base_asset=contract.base_currency, quote_asset=contract.quote_currency,
                                 side=delta.side, kind=delta.kind, quantity=delta.quantity, limit_price=delta.limit_price,
                                 contract_multiplier=delta.contract_multiplier, child_index=child)
            record = self.admission.admit(intent)
            if cause is not None:
                self._record_cause(record.intent.intent_id, delta.kind, cause)
            Dispatcher(self.writer, self.interlock, gateway, reducer=self.reducer).dispatch(record.intent.intent_id)
            ids.append(record.intent.intent_id)
        return tuple(ids)

    def _record_cause(self, intent_id: str, kind: IntentKind, cause: dict[str, Any]) -> None:
        def save(connection: sqlite3.Connection) -> None:
            record_cause_tx(connection, intent_id=intent_id, context_id=self.context.identity,
                            cause="DECODED_DECISION_EXIT" if kind is IntentKind.EXIT else "DECODED_DECISION_ENTRY",
                            origin="AUTOMATION", decision_id=str(cause["decision_id"]), controller_profile=cause.get("controller_profile"),
                            pipeline_fixture=bool(cause.get("pipeline_fixture", False)))
        self.writer.call(save)

    def _slot_for(self, instrument: str) -> str:
        row = self.writer.call(lambda connection: connection.execute(
            "SELECT slot_id FROM cf_slots WHERE context_id=? AND instrument=? AND tombstoned=0", (self.context.identity, instrument)).fetchone())
        if row is None:
            raise DomainError("SLOT_UNKNOWN")
        return str(row[0])

    def cancel_intent(self, intent_id: str) -> dict[str, Any]:
        """Cancel one resting live order; the venue's documented outcome is reported, never assumed."""
        gateway = self._require_gateway()
        record = self.writer.call(lambda connection: get_tx(connection, intent_id))
        if record.terminal:
            return {"cancelled": False, "reason": "ORDER_NOT_OPEN"}
        if record.venue_order_id is None:
            return {"cancelled": False, "reason": "VENUE_ORDER_ID_UNKNOWN"}
        outcome = gateway.cancel([record.venue_order_id])[record.venue_order_id]
        depth = self._depth.get(record.intent.instrument)
        crossed = False
        if depth is not None and depth[0] and depth[1]:
            bids, asks, _ = depth
            crossed = asks[0].price <= record.intent.limit_price if record.intent.side is Side.BUY else bids[0].price >= record.intent.limit_price
        if outcome is CancelOutcome.CANCELLED:
            # The cancellation acknowledgement is not a complete fill history.
            # Retain reservations until sync verifies terminal state and quantity.
            self.sync(self._clock())
            current = self.writer.call(lambda connection: get_tx(connection, intent_id))
            return {"cancelled": current.terminal, "cancel_requested": True,
                    "reason": "CANCELLED" if current.terminal else "CANCEL_AWAITING_RECONCILIATION",
                    "filled_quantity": str(current.filled_quantity),
                    "remaining_quantity": str(current.intent.quantity - current.filled_quantity),
                    "limit_crossed_at_cancel": crossed, "instrument": current.intent.instrument}
        if outcome is CancelOutcome.ALREADY_TERMINAL:
            return {"cancelled": False, "reason": "ALREADY_TERMINAL"}  # the next sync books the fill/terminal state
        return {"cancelled": False, "reason": outcome.value}

    # ------------------------------------------------------------------ drain and proofs
    def drain(self, policy: DrainPolicy) -> None:
        with self._drain_lock:
            self._cancel_open(None)
            self._close_residuals(policy, None)
            self.writer.call(lambda connection: assert_context_closed_tx(connection, self.context))

    def _cancel_open(self, slot: SlotRecord | None) -> None:
        gateway = self._require_gateway()
        rows = self.writer.call(lambda connection: [(str(r[0]), r[1], str(r[2])) for r in connection.execute(
            "SELECT i.intent_id, o.venue_order_id, i.payload_json FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
            "WHERE i.context_id=? AND o.terminal=0", (self.context.identity,))])
        targets = []
        for intent_id, venue_order_id, payload in rows:
            intent = OrderIntent.model_validate_json(payload)
            if slot is not None and intent.instrument != slot.instrument:
                continue
            if venue_order_id:
                targets.append((intent_id, str(venue_order_id)))
        if not targets:
            return
        gateway.cancel([venue_id for _, venue_id in targets])
        # Never mark a cancellation terminal from its acknowledgement alone.
        # Sync books late fills and checks the venue's reported filled size.
        self.sync(self._clock())

    def _close_residuals(self, policy: DrainPolicy, slot: SlotRecord | None) -> None:
        deadline = self._clock() + timedelta(seconds=policy.timeout_seconds)
        gateway = self._require_gateway()
        for _ in range(100):
            now = self._clock()
            if now >= deadline:
                raise DomainError("DRAIN_DEADLINE_EXCEEDED")
            self.sync(now)
            positions = self.writer.call(lambda connection: {str(r[0]): Decimal(r[1]) for r in connection.execute(
                "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (self.context.identity,))})
            residual = next(((name, quantity) for name, quantity in positions.items() if quantity != 0
                             and (slot is None or name == slot.instrument)), None)
            if residual is None:
                return
            instrument, holding = residual
            if holding < 0:
                raise DomainError("DRAIN_REDUCING_SIDE_UNPROVEN")  # spot cannot be short; the ledger is inconsistent
            contract = self._instruments[instrument]
            depth = self._depth.get(instrument)
            if depth is None or not depth[0] or now - depth[2] > MARK_MAX_AGE:
                raise DomainError("DRAIN_FRESH_MARKET_REQUIRED")
            bids = depth[0]
            price = (bids[0].price * (1 - policy.max_slippage_fraction) / contract.price_increment).to_integral_value(rounding=ROUND_UP) * contract.price_increment
            budget = sum((level.quantity for level in bids if level.price >= price), Decimal(0)) * policy.max_participation_fraction
            quantity = min(holding, budget, self.config.limits.max_order_notional / price)
            quantity = (quantity / contract.quantity_increment).to_integral_value(rounding=ROUND_DOWN) * contract.quantity_increment
            if quantity <= 0 or quantity * price < contract.minimum_notional:
                raise DomainError("DRAIN_DUST_OR_LIQUIDITY_BLOCKED")
            self._exit_children += 1
            intent = OrderIntent(context=self.context, scope_id=self.scope, profile_version=1, signer_version=1,
                                 slot_id=self._slot_for(instrument), allocation_revision=0, instrument=instrument,
                                 base_asset=contract.base_currency, quote_asset=contract.quote_currency, side=Side.SELL,
                                 kind=IntentKind.EXIT, quantity=quantity, limit_price=price, contract_multiplier=Decimal(1),
                                 child_index=self._exit_children)
            self.observe_execution_book()  # the sync above changed the facts; admission needs the current book
            record = self.admission.admit(intent)
            self.writer.call(lambda connection: record_cause_tx(connection, intent_id=record.intent.intent_id, context_id=self.context.identity,
                                                                cause="PROTECTIVE_DRAIN", origin="SLOT_DRAIN" if slot is not None else "CONTEXT_DRAIN"))
            Dispatcher(self.writer, self.interlock, gateway, reducer=self.reducer).dispatch(record.intent.intent_id)
            self.sync(self._clock())
            remaining = self.writer.call(lambda connection: get_tx(connection, record.intent.intent_id))
            if not remaining.terminal:
                raise DomainError("DRAIN_AWAITING_LIQUIDITY")
        raise DomainError("DRAIN_OPERATION_BUDGET_EXCEEDED")

    def _certificate(self, slot: SlotRecord | None) -> StopCertificate:
        now = self._clock()
        with self._lock:
            if (not self._account_complete or self._last_sync is None
                    or not timedelta(0) <= now - self._last_sync <= SYNC_MAX_AGE
                    or self._external_activity):
                raise DomainError("STOP_PROOF_ACCOUNT_EVIDENCE_UNAVAILABLE")
        open_ids = self.writer.call(lambda connection: tuple(str(r[0]) for r in connection.execute(
            "SELECT i.intent_id FROM ex_intents i JOIN ex_outbox o USING(intent_id) WHERE i.context_id=? AND o.terminal=0", (self.context.identity,))))
        if open_ids:
            raise DomainError("STOP_PROOF_OUTSTANDING_VENUE_ORDER")
        positions = self.writer.call(lambda connection: {str(r[0]): Decimal(r[1]) for r in connection.execute(
            "SELECT instrument, quantity FROM ex_positions WHERE context_id=?", (self.context.identity,))})
        if any(quantity != 0 for name, quantity in positions.items() if slot is None or name == slot.instrument):
            raise DomainError("STOP_PROOF_POSITION_NONZERO")
        def evidence(connection: sqlite3.Connection) -> int:
            if slot is None:
                assert_context_closed_tx(connection, self.context)
                return context_fact_sequence_tx(connection, self.context)
            assert_slot_closed_tx(connection, self.context, slot.slot_id)
            return slot_fact_sequence_tx(connection, self.context, slot.slot_id)
        sequence = self.writer.call(evidence)
        synced = self._last_sync or now
        return StopCertificate(proof_id=str(uuid4()), context=self.context, execution_scope=(self.scope,), issued_at=now,
                               expires_at=now + timedelta(seconds=30), fact_sequence=sequence,
                               watermarks=(ScopeWatermark(scope_id=self.scope, evidence_id=f"cb-sync-{sequence}", account_observed_at=synced,
                                                          account_stream_sequence=sequence, applied_stream_sequence=sequence,
                                                          history_complete=True, account_complete=True),),
                               residual_positions=(), outstanding_order_ids=(), unresolved_create_ids=(), unapplied_fill_count=0,
                               all_delegated_positions_covered=True, all_related_orders_terminal=True,
                               dispatch_tasks_fenced=self._quiesced or (slot is not None and slot.slot_id not in self._enabled_slots),
                               task_fence_evidence_id=f"live-fenced-{self.context.epoch}")

    def reconcile(self) -> StopCertificate:
        return self._certificate(None)

    # ------------------------------------------------------------------ slots
    def start_slot(self, slot: SlotRecord) -> None:
        if slot.context != self.context or slot.instrument not in self._instruments:
            raise DomainError("LIVE_INSTRUMENT_UNAVAILABLE")
        with self._lock:
            self._enabled_slots.add(slot.slot_id)

    def quiesce_slot(self, slot: SlotRecord) -> None:
        with self._lock:
            self._enabled_slots.discard(slot.slot_id)

    def drain_slot(self, slot: SlotRecord, policy: DrainPolicy) -> None:
        with self._drain_lock:
            self._cancel_open(slot)
            self._close_residuals(policy, slot)

    def reconcile_slot(self, slot: SlotRecord) -> SlotStopProof:
        return SlotStopProof(slot_id=slot.slot_id, certificate=self._certificate(slot))

    # ------------------------------------------------------------------ reporting
    def balance_snapshot(self) -> dict[str, Any]:
        with self._lock:
            cash_available, cash_hold = self._balances.get(self.config.reporting_currency, (Decimal(0), Decimal(0)))
            synced = self._last_sync
            verified = (self._account_complete and synced is not None
                        and timedelta(0) <= self._clock() - synced <= SYNC_MAX_AGE)
        return {"environment": "LIVE", "simulated": False, "reporting_currency": self.config.reporting_currency,
                "coverage": "VERIFIED_CASH_SUBTOTAL" if verified else "UNKNOWN_UNIVERSE",
                "total": str(cash_available + cash_hold) if verified else None,
                "settled_assets": str(cash_available) if verified else None,
                "active_book_available": str(cash_available) if verified else None,
                "included_scopes": 1, "expected_scopes": 1, "label": "LIVE — COINBASE ADVANCED SPOT (portfolio scope)",
                "observation_age_seconds": (self._clock() - synced).total_seconds() if synced is not None else None,
                "valuation_provenance": "COINBASE_ACCOUNTS_ROUTE", "sync": dict(self._sync_report)}
