"""The one execution interface the automation dispatches through — paper or live.

``AutomationController`` never talks to a venue: it plans against
``observe_execution_book`` and hands bound targets to ``submit_target`` with the
strategy's execution constraint, its declared maintenance-utilization ceiling
and the decoded decision's cause. The local paper engine and the live spot
engine both implement this protocol; the worker refuses to bind an engine
that does not (``EXECUTION_ENGINE_INTERFACE_INCOMPLETE``), so a route can never
be "wired" by duck typing a subset.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from coinfly.control.context_coordinator import EngineLifecycle
from coinfly.domain.context import DomainError

REQUIRED_METHODS: tuple[str, ...] = (
    # lifecycle (EngineLifecycle)
    "initialize", "quiesce", "drain", "reconcile", "retire", "reset_transient", "arm",
    # market ingestion and marks
    "instrument_ids", "ensure_instruments", "instrument_contract", "update_market",
    # admission authority and planning
    "observe_execution_book", "submit_target", "cancel_intent",
    # scheduled venue-side events (funding, settlement); spot returns ()
    "settle",
    # per-slot protected lifecycle used by the slot service
    "start_slot", "quiesce_slot", "drain_slot", "reconcile_slot",
)


@runtime_checkable
class ExecutionEngine(EngineLifecycle, Protocol):
    def instrument_ids(self) -> tuple[str, ...]: ...
    def ensure_instruments(self, instruments: tuple[Any, ...]) -> tuple[str, ...]: ...
    def instrument_contract(self, instrument_id: str) -> Any: ...
    def update_market(self, instrument: str, *, bids: tuple[Any, ...], asks: tuple[Any, ...], now: datetime) -> tuple[Any, ...]: ...
    def observe_execution_book(self) -> Any: ...
    def submit_target(self, target: Any, *, constraint: Callable[[Any, Any], Any] | None = None,
                      cause: dict[str, Any] | None = None, max_maintenance_utilization: Decimal | None = None) -> tuple[str, ...]: ...
    def cancel_intent(self, intent_id: str) -> dict[str, Any]: ...
    def settle(self, now: datetime) -> tuple[dict[str, Any], ...]: ...
    def start_slot(self, slot: Any) -> None: ...
    def quiesce_slot(self, slot: Any) -> None: ...
    def drain_slot(self, slot: Any, policy: Any) -> None: ...
    def reconcile_slot(self, slot: Any) -> Any: ...


def execution_engine_gaps(engine: object) -> tuple[str, ...]:
    return tuple(name for name in REQUIRED_METHODS if not callable(getattr(engine, name, None)))


def require_execution_engine(engine: object) -> None:
    gaps = execution_engine_gaps(engine)
    if gaps:
        raise DomainError("EXECUTION_ENGINE_INTERFACE_INCOMPLETE", "EXECUTION_ENGINE_INTERFACE_INCOMPLETE: " + ", ".join(gaps))
