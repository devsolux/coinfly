"""Bounded immutable handoff: I/O threads enqueue, Tk's main thread renders."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from coinfly.ipc.protocol import freeze


@dataclass(frozen=True, slots=True)
class RenderEvent:
    kind: str
    payload: Mapping[str, Any]


class RenderQueue:
    """Coalesce snapshots; preserve bounded command/alarm events.

    An overflow becomes a visible resnapshot/disconnected condition. Silent
    receipt or sticky-alarm loss would otherwise give the operator false state.
    """

    def __init__(self, capacity: int = 64) -> None:
        if capacity < 2:
            raise ValueError("Render capacity must be at least two")
        self.capacity = capacity
        self._events: deque[RenderEvent] = deque()
        self._latest: RenderEvent | None = None
        self._overflow = False
        self._lock = threading.Lock()

    def put(self, kind: str, payload: Mapping[str, Any]) -> bool:
        event = RenderEvent(kind, freeze(payload))
        with self._lock:
            if kind == "snapshot":
                if (self._latest is None or payload.get("worker_instance_id") != self._latest.payload.get("worker_instance_id")
                        or payload.get("sequence", -1) >= self._latest.payload.get("sequence", -1)):
                    self._latest = event
                return True
            if len(self._events) >= self.capacity:
                self._overflow = True
                return False
            self._events.append(event)
            return True

    def take(self, limit: int = 8) -> tuple[RenderEvent, ...]:
        with self._lock:
            result: list[RenderEvent] = []
            if self._overflow:
                self._overflow = False
                result.append(RenderEvent("resnapshot_required", freeze({"code": "UI_QUEUE_OVERFLOW"})))
            while self._events and len(result) < limit:
                result.append(self._events.popleft())
            if self._latest is not None and len(result) < limit:
                result.append(self._latest)
                self._latest = None
            return tuple(result)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events) + int(self._latest is not None) + int(self._overflow)


class TkScheduler(Protocol):
    def after(self, milliseconds: int, callback: Callable[[], None]) -> Any: ...


class RenderScheduler:
    def __init__(self, root: TkScheduler, queue: RenderQueue, render: Callable[[RenderEvent], None], *,
                 interval_ms: int = 16, callback_budget_ms: float = 8.0) -> None:
        self.root, self.queue, self.render = root, queue, render
        self.interval_ms, self.callback_budget_ms = interval_ms, callback_budget_ms
        self._main_thread = threading.get_ident()
        self._running = False

    def start(self) -> None:
        self._running = True
        self.root.after(self.interval_ms, self._tick)

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        if threading.get_ident() != self._main_thread:
            raise RuntimeError("Tk rendering must run on the main thread")
        if not self._running:
            return
        started = time.monotonic()
        # One callback per event, bounded batch. Visual certification measures
        # real host latency; this scheduling policy is not a performance claim.
        for _ in range(8):
            events = self.queue.take(limit=1)
            if not events:
                break
            self.render(events[0])
            if (time.monotonic() - started) * 1000 >= self.callback_budget_ms:
                break
        self.root.after(self.interval_ms, self._tick)
