"""One create initiation under the shared transition gate, then asynchronous facts."""

from __future__ import annotations

from concurrent.futures import Future, InvalidStateError
from enum import StrEnum
from threading import Timer
from typing import Callable, Protocol, TypeVar

from pydantic import model_validator

from coinfly.domain.context import ContextKey, FrozenModel, Identifier
from coinfly.execution.evidence import fence_authority_tx
from coinfly.execution.fact_reducer import FactReducer
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import IntentRecord, OrderIntent
from coinfly.execution.outbox import claim_dispatch_tx
from coinfly.storage.writer import Writer

T = TypeVar("T")


class DispatchGate(Protocol):
    """Checks authority, realm, slot, risk and permission epochs in the network owner."""

    def validate(self, intent: OrderIntent) -> None: ...

    def provisional_fence(self, authority_id: str, reason: str) -> None:
        """Fail closed in memory before a potentially failed durable write."""
        ...

    def run_at_send_boundary(self, intent: OrderIntent, invoke: Callable[[], T]) -> T:
        """Serialize validation/send initiation with transition fencing."""
        ...


class CreateOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED_NO_CREATION = "REJECTED_NO_CREATION"


class CreateResult(FrozenModel):
    context: ContextKey
    outcome: CreateOutcome
    evidence_id: Identifier
    venue_order_id: Identifier | None = None

    @model_validator(mode="after")
    def acceptance_has_identity(self) -> CreateResult:
        if self.outcome == CreateOutcome.ACCEPTED and self.venue_order_id is None:
            raise ValueError("positive acceptance requires venue order identity")
        if self.outcome == CreateOutcome.REJECTED_NO_CREATION and self.venue_order_id is not None:
            raise ValueError("non-creation cannot carry an accepted venue order")
        return self


class CreateGateway(Protocol):
    def create(self, intent: OrderIntent) -> Future[CreateResult]:
        """Initiate at most once per intent and return promptly; transport replay is disabled.

        The outcome may be UNKNOWN (initiated, result unobserved); that is neither
        a creation nor a rejection and is resolved only from venue evidence.
        """
        ...


class RatePermit(Protocol):
    def acquire(self, intent: OrderIntent) -> None: ...


class Dispatcher:
    def __init__(
        self,
        writer: Writer,
        gate: DispatchGate,
        gateway: CreateGateway,
        *,
        reducer: FactReducer | None = None,
        permit: RatePermit | None = None,
        response_timeout: float = 30.0,
    ) -> None:
        if response_timeout <= 0:
            raise ValueError("positive response timeout required")
        self.writer = writer
        self.gate = gate
        self.gateway = gateway
        self.reducer = reducer or FactReducer(writer, provisional_fence=gate.provisional_fence)
        self.permit = permit
        self.response_timeout = response_timeout
        self.intents = IntentStore(writer)

    def dispatch(self, intent_id: str) -> Future[IntentRecord]:
        """Called in a worker actor, never from Tk. The result is durable state."""
        intent = self.intents.get(intent_id).intent
        if self.permit is not None:
            self.permit.acquire(intent)
        self.gate.validate(intent)
        invoked = False
        claimed = False

        def initiate() -> Future[CreateResult]:
            nonlocal invoked, claimed
            self.writer.call(lambda connection: claim_dispatch_tx(connection, intent_id, intent.payload_hash))
            claimed = True
            self.gate.validate(intent)  # after the marker, at the actual send boundary
            invoked = True
            return self.gateway.create(intent)

        output: Future[IntentRecord] = Future()
        try:
            pending = self.gate.run_at_send_boundary(intent, initiate)
        except Exception:
            if invoked:
                try:
                    self._deliver(output, self.reducer.unknown(intent_id, "create-initiation-ambiguous"))
                except Exception:
                    self.gate.provisional_fence(intent.context.authority_id, "EXECUTION_PERSISTENCE_FAILED")
                    raise
            elif claimed:
                self._deliver(
                    output,
                    self.reducer.reject_unsent(intent_id, "final-gate-proven-no-send"),
                )
            else:
                raise
            return output

        def timed_out() -> None:
            try:
                self._deliver(output, self.reducer.unknown(intent_id, "create-response-deadline"))
            except Exception as error:
                self.gate.provisional_fence(intent.context.authority_id, "EXECUTION_PERSISTENCE_FAILED")
                self._fail(output, error)

        timer = Timer(self.response_timeout, timed_out)
        timer.daemon = True
        timer.start()

        def completed(result_future: Future[CreateResult]) -> None:
            timer.cancel()
            try:
                try:
                    result = result_future.result()
                except Exception:
                    self._deliver(output, self.reducer.unknown(intent_id, "create-response-ambiguous"))
                    return
                if result.context != intent.context:
                    self.writer.call(
                        lambda connection: fence_authority_tx(
                            connection, intent.context, "CREATE_RESPONSE_REALM_MISMATCH", result.evidence_id
                        )
                    )
                    self._deliver(output, self.reducer.unknown(intent_id, "create-response-realm-mismatch"))
                    return
                if result.outcome == CreateOutcome.ACCEPTED:
                    assert result.venue_order_id is not None
                    state = self.reducer.acknowledge(intent_id, result.venue_order_id, result.evidence_id)
                else:
                    state = self.reducer.reject(intent_id, result.evidence_id, proven_no_creation=True)
                self._deliver(output, state)
            except Exception as error:
                self.gate.provisional_fence(intent.context.authority_id, "EXECUTION_PERSISTENCE_FAILED")
                self._fail(output, error)

        pending.add_done_callback(completed)
        return output

    @staticmethod
    def _deliver(future: Future[IntentRecord], result: IntentRecord) -> None:
        try:
            future.set_result(result)
        except InvalidStateError:
            # A timeout can deliver UNKNOWN first; late facts are still committed.
            pass

    @staticmethod
    def _fail(future: Future[IntentRecord], error: Exception) -> None:
        try:
            future.set_exception(error)
        except InvalidStateError:
            pass
