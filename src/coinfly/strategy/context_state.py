"""Ephemeral decision memory has no financial state or cross-context promotion."""

from coinfly.domain.context import ContextKey, DomainError


class ContextDecisionState:
    def __init__(self, context: ContextKey) -> None:
        self.context = context
        self._values: dict[str, tuple[float, ...]] = {}
        self._retired = False

    def put(self, key: str, values: tuple[float, ...], *, context: ContextKey) -> None:
        import math

        if self._retired or context != self.context:
            raise DomainError("STALE_CONTEXT")
        if len(values) > 1000000 or any(not math.isfinite(value) for value in values):
            raise DomainError("INVALID_DECISION_STATE")
        self._values[key] = values

    def get(self, key: str, *, context: ContextKey) -> tuple[float, ...] | None:
        if self._retired or context != self.context:
            raise DomainError("STALE_CONTEXT")
        return self._values.get(key)

    def retire(self, *, stopped_context: ContextKey) -> None:
        if stopped_context != self.context:
            raise DomainError("STALE_CONTEXT")
        self._values.clear()
        self._retired = True
