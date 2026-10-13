"""Immutable versioned state; missed deltas demand a complete snapshot."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from .protocol import RPCError, freeze, thaw


class ResnapshotRequired(RPCError):
    def __init__(self) -> None:
        super().__init__("RESNAPSHOT_REQUIRED")


class ReactiveCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Mapping[str, Any] | None = None

    def snapshot(self) -> Mapping[str, Any] | None:
        with self._lock:
            return self._snapshot

    def apply_snapshot(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        self._validate(snapshot)
        with self._lock:
            current = self._snapshot
            if (current is not None and snapshot.get("worker_instance_id") == current.get("worker_instance_id")
                    and (snapshot["sequence"] < current["sequence"] or snapshot["context_epoch"] < current["context_epoch"])):
                raise RPCError("STALE_SNAPSHOT")
            self._snapshot = freeze(snapshot)
            return self._snapshot

    def apply_delta(self, delta: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            current = self._snapshot
            if current is None or delta.get("base_sequence") != current["sequence"]:
                raise ResnapshotRequired()
            if delta.get("context_epoch") != current["context_epoch"]:
                raise ResnapshotRequired()
            if type(delta.get("sequence")) is not int or delta["sequence"] <= current["sequence"]:
                raise RPCError("STALE_DELTA")
            changes = delta.get("changes")
            if not isinstance(changes, Mapping) or {"sequence", "context_epoch"} & set(changes):
                raise RPCError("INVALID_DELTA")
            data = thaw(current)
            data.update(thaw(changes))
            data["sequence"] = delta["sequence"]
            self._snapshot = freeze(data)
            return self._snapshot

    @staticmethod
    def _validate(snapshot: Mapping[str, Any]) -> None:
        if any(type(snapshot.get(key)) is not int or snapshot[key] < 0 for key in ("sequence", "context_epoch")):
            raise RPCError("INVALID_SNAPSHOT_REVISION")
