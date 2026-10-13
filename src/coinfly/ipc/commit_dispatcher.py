"""Publish committed facts only after authoritative risk invalidation succeeds."""

from collections.abc import Callable, Mapping
from typing import Any

from .reactive_cache import ReactiveCache


class CommitDispatcher:
    def __init__(self, cache: ReactiveCache, *,
                 apply_risk: Callable[[Mapping[str, Any]], None],
                 publish: Callable[[str, Mapping[str, Any]], None]) -> None:
        self.cache, self.apply_risk, self.publish = cache, apply_risk, publish

    def committed(self, snapshot: Mapping[str, Any]) -> None:
        """Invoke after the writer commit, on the serialized worker owner."""
        self.apply_risk(snapshot)
        immutable = self.cache.apply_snapshot(snapshot)
        self.publish("state.snapshot", immutable)
