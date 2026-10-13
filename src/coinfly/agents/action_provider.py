"""Scoped, bounded cache queries plus weight proposals through trusted runtime."""

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from pydantic import ValidationError

from coinfly.agents.tool_contracts import (
    ALLOWED_TOOLS,
    BookQuery,
    CachedBook,
    CachedFeatures,
    FeatureQuery,
    ProposalReceipt,
)
from coinfly.domain.context import ContextKey, DomainError
from coinfly.strategy.contracts import TargetWeights


class ModelCache(Protocol):
    def features(self, query: FeatureQuery) -> CachedFeatures: ...
    def order_book(self, query: BookQuery) -> CachedBook: ...


class ScopedProposalSink(Protocol):
    def submit(self, proposal: TargetWeights, *, expected_epoch: int) -> str: ...


class ActionProvider:
    """Trusted IPC-side tools. Does not import AgentKit default wallet actions.

    The cache and sink are worker-supplied capabilities. A model receives JSON
    tool schemas only; the context and these Python objects are never model input.
    """

    def __init__(
        self,
        *,
        context: ContextKey,
        cache: ModelCache,
        sink: ScopedProposalSink,
        context_is_current: Callable[[ContextKey], bool],
        deadline: datetime,
        max_calls: int = 32,
        max_age: timedelta = timedelta(seconds=10),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if (
            not 1 <= max_calls <= 128
            or deadline.tzinfo is None
            or not timedelta(0) < max_age <= timedelta(minutes=1)
        ):
            raise DomainError("AGENT_RUN_BUDGET_INVALID")
        self._context = context
        self._cache = cache
        self._sink = sink
        self._is_current = context_is_current
        self._deadline = deadline
        self._remaining = max_calls
        self._max_age = max_age
        self._clock = clock
        self._retired = False
        self._lock = threading.RLock()

    @property
    def tool_names(self) -> tuple[str, ...]:
        return ALLOWED_TOOLS

    def _admit(self, name: str, payload: str) -> datetime:
        if name not in ALLOWED_TOOLS:
            raise DomainError("AGENT_TOOL_FORBIDDEN")
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > 64 * 1024:
            raise DomainError("AGENT_PAYLOAD_TOO_LARGE")
        now = self._clock()
        if self._retired or not self._is_current(self._context):
            raise DomainError("AGENT_CONTEXT_RETIRED")
        if now >= self._deadline or self._remaining <= 0:
            raise DomainError("AGENT_RUN_EXPIRED")
        self._remaining -= 1
        return now

    def _fresh(self, as_of: datetime, now: datetime) -> None:
        if as_of.tzinfo is None or not timedelta(0) <= now - as_of <= self._max_age:
            raise DomainError("AGENT_CACHE_STALE")

    def invoke(self, name: str, payload_json: str) -> str:
        with self._lock:
            now = self._admit(name, payload_json)
            try:
                if name == "get_cached_features":
                    query = FeatureQuery.model_validate_json(payload_json)
                    features = self._cache.features(query)
                    if features.snapshot_id != query.snapshot_id or not {
                        item.instrument_id for item in features.values
                    }.issubset(query.instruments):
                        raise DomainError("AGENT_CACHE_SCOPE_MISMATCH")
                    self._fresh(features.as_of, now)
                    result = features.model_dump_json()
                elif name == "get_cached_order_book":
                    book_query = BookQuery.model_validate_json(payload_json)
                    book = self._cache.order_book(book_query)
                    if (
                        book.snapshot_id != book_query.snapshot_id
                        or book.instrument_id != book_query.instrument_id
                        or max(len(book.bids), len(book.asks)) > book_query.depth
                    ):
                        raise DomainError("AGENT_CACHE_SCOPE_MISMATCH")
                    self._fresh(book.as_of, now)
                    result = book.model_dump_json()
                else:
                    proposal = TargetWeights.model_validate_json(payload_json)
                    if proposal.expires_at <= now:
                        raise DomainError("AGENT_PROPOSAL_EXPIRED")
                    receipt_id = self._sink.submit(proposal, expected_epoch=self._context.epoch)
                    result = ProposalReceipt(
                        decision_id=proposal.decision_id, receipt_id=receipt_id
                    ).model_dump_json()
            except ValidationError:
                raise DomainError("AGENT_TOOL_INPUT_INVALID") from None
            # Slow callbacks cannot produce a valid late response after retirement.
            if self._retired or not self._is_current(self._context) or self._clock() >= self._deadline:
                raise DomainError("AGENT_CONTEXT_RETIRED_OR_EXPIRED")
            if len(result.encode("utf-8")) > 128 * 1024:
                raise DomainError("AGENT_RESPONSE_TOO_LARGE")
            return result

    def retire(self) -> None:
        with self._lock:
            self._retired = True
