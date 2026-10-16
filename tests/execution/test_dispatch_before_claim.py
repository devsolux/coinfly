from __future__ import annotations

from decimal import Decimal

from coinfly.domain.context import DomainError
from coinfly.execution.dispatcher import Dispatcher
from coinfly.execution.models import DispatchState, OrderIntent, Resolution, Submission
from coinfly.execution.recovery import assert_context_closed_tx
from tests.execution.helpers import Gate, LedgerCase, NonIdempotentGateway, intent


class DenyingPermit:
    def acquire(self, order: OrderIntent) -> None:
        raise DomainError("RATE_LIMIT_DENIED")


class DispatchBeforeClaimTests(LedgerCase):
    """A refusal before the send marker proves nothing was sent: reject the intent and free its reservation."""

    def assert_rejected_without_send(self, order: OrderIntent, gateway: NonIdempotentGateway) -> None:
        record = self.store.get(order.intent_id)
        self.assertEqual(record.submission, Submission.REJECTED)
        self.assertEqual(record.dispatch, DispatchState.NOT_DISPATCHED)
        self.assertEqual(record.resolution, Resolution.RESOLVED)
        self.assertTrue(record.terminal)
        self.assertEqual(record.reservation_remaining, Decimal(0))
        self.assertFalse(gateway.calls)
        self.writer.call(lambda connection: assert_context_closed_tx(connection, order.context))

    def test_permit_refusal_rejects_the_unsent_intent_and_releases_its_reservation(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        with self.assertRaisesRegex(DomainError, "RATE_LIMIT_DENIED"):
            Dispatcher(self.writer, Gate(), gateway, permit=DenyingPermit()).dispatch(order.intent_id)
        self.assert_rejected_without_send(order, gateway)

    def test_first_gate_refusal_rejects_the_unsent_intent_and_releases_its_reservation(self) -> None:
        order = self.prepare(intent())
        gate = Gate()
        gate.stop_at_validation = 1
        gateway = NonIdempotentGateway()
        with self.assertRaisesRegex(DomainError, "CONTEXT_FENCED"):
            Dispatcher(self.writer, gate, gateway).dispatch(order.intent_id)
        self.assert_rejected_without_send(order, gateway)

    def test_send_boundary_refusal_before_the_marker_rejects_the_unsent_intent(self) -> None:
        order = self.prepare(intent())
        gate = Gate()
        gate.stop_at_validation = 2  # the validation inside run_at_send_boundary, before the marker is written
        gateway = NonIdempotentGateway()
        with self.assertRaisesRegex(DomainError, "CONTEXT_FENCED"):
            Dispatcher(self.writer, gate, gateway).dispatch(order.intent_id)
        self.assert_rejected_without_send(order, gateway)

    def test_refused_claim_of_an_already_started_intent_changes_nothing(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        first = Dispatcher(self.writer, Gate(), gateway, response_timeout=100).dispatch(order.intent_id)
        gateway.pending.set_exception(ConnectionError())
        self.assertEqual(first.result(timeout=2).resolution, Resolution.UNKNOWN)
        before = self.store.get(order.intent_id)
        with self.assertRaisesRegex(DomainError, "DISPATCH_ALREADY_CLAIMED_OR_RESOLVED"):
            Dispatcher(self.writer, Gate(), gateway).dispatch(order.intent_id)
        after = self.store.get(order.intent_id)
        self.assertEqual(after, before)
        self.assertEqual(after.resolution, Resolution.UNKNOWN)
        self.assertEqual(after.reservation_remaining, Decimal(200))
        self.assertEqual(len(gateway.calls), 1)
