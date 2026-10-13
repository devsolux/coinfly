"""Context-owned network resources, retired before a replacement can initialize."""

import threading
from typing import Protocol

from coinfly.domain.context import ContextKey, DomainError


class RetirableTransport(Protocol):
    def retire(self) -> None: ...


class ContextTransports:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resources: dict[ContextKey, list[RetirableTransport]] = {}
        self._retired: set[ContextKey] = set()

    def begin(self, context: ContextKey) -> None:
        """Coordinator-only: create fresh handles for an unarmed target/retry.

        Old transport objects remain permanently retired. Failed closes must
        resolve before a new set of handles can be registered.
        """
        with self._lock:
            if self._resources.get(context):
                raise DomainError("NETWORK_RETIREMENT_UNPROVEN")
            self._retired.discard(context)

    def register(self, context: ContextKey, transport: RetirableTransport) -> None:
        with self._lock:
            if context in self._retired:
                transport.retire()
                raise DomainError("NETWORK_CONTEXT_RETIRED")
            self._resources.setdefault(context, []).append(transport)

    def retire(self, context: ContextKey) -> None:
        with self._lock:
            self._retired.add(context)
            failed = []
            for transport in self._resources.get(context, ()):
                try:
                    transport.retire()
                except Exception:
                    failed.append(transport)
            if failed:
                self._resources[context] = failed
                raise DomainError("NETWORK_RETIREMENT_UNPROVEN")
            self._resources.pop(context, None)
