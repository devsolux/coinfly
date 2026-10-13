from __future__ import annotations

from decimal import Decimal

from coinfly.accounting.journal import Journal
from coinfly.domain.context import DomainError
from coinfly.execution.dispatcher import CreateOutcome, CreateResult, Dispatcher
from coinfly.execution.evidence import acknowledge_alarm_tx
from coinfly.execution.models import Resolution, Submission
from coinfly.execution.outbox import claim_dispatch_tx
from coinfly.execution.reconciler import EvidenceKind, Reconciler, ReconciliationEvidence
from coinfly.execution.recovery import assert_context_closed_tx
from tests.execution.helpers import Gate, LedgerCase, NonIdempotentGateway, context, fill_for, intent


class OutboxTests(LedgerCase):
    def test_early_fill_atomically_acknowledges_and_timeout_does_not_regress(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        dispatched = Dispatcher(self.writer, Gate(), gateway, response_timeout=100).dispatch(order.intent_id)
        self.reducer.apply_fill(fill_for(order))
        gateway.pending.set_exception(TimeoutError("network unavailable"))
        result = dispatched.result(timeout=2)
        self.assertEqual(result.submission, Submission.ACKNOWLEDGED)
        self.assertEqual(result.resolution, Resolution.RESOLVED)
        self.assertEqual(result.filled_quantity, Decimal(1))
        self.assertEqual(result.reservation_remaining, Decimal(100))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_entries").fetchone()[0]
            ),
            6,
        )
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute(
                    "SELECT COUNT(*) FROM ex_slot_fences WHERE active=1"
                ).fetchone()[0]
            ),
            0,
        )

    def test_real_unknown_is_sticky_and_reservation_survives_recovery(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        dispatched = Dispatcher(self.writer, Gate(), gateway, response_timeout=100).dispatch(order.intent_id)
        gateway.pending.set_exception(ConnectionError())
        result = dispatched.result(timeout=2)
        self.assertEqual(result.resolution, Resolution.UNKNOWN)
        self.assertEqual(result.reservation_remaining, Decimal(200))
        alarm_id = self.writer.call(
            lambda connection: str(connection.execute("SELECT alarm_id FROM ex_alarms").fetchone()[0])
        )
        self.writer.call(lambda connection: acknowledge_alarm_tx(connection, alarm_id))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT active FROM ex_slot_fences").fetchone()[0]
            ),
            1,
        )
        with self.assertRaises(DomainError):
            self.writer.call(lambda connection: assert_context_closed_tx(connection, order.context))
        Reconciler(self.writer).recover()
        self.assertEqual(len(gateway.calls), 1)
        with self.assertRaises(DomainError):
            Dispatcher(self.writer, Gate(), gateway).dispatch(order.intent_id)

    def test_duplicate_fill_across_epochs_is_one_financial_fact(self) -> None:
        order = self.prepare(intent())
        first = fill_for(order)
        self.assertTrue(self.reducer.apply_fill(first).applied)
        duplicate = first.model_copy(update={"context": context(2), "evidence_id": "another-source"})
        self.assertFalse(self.reducer.apply_fill(duplicate).applied)
        self.assertEqual(self.store.get(order.intent_id).filled_quantity, Decimal(1))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_fills").fetchone()[0]
            ),
            1,
        )
        Journal(self.writer).verify()

    def test_equivalent_decimal_source_scales_are_duplicate_facts(self) -> None:
        order = self.prepare(intent())
        first = fill_for(order)
        self.reducer.apply_fill(first)
        duplicate = first.model_copy(
            update={"quantity": Decimal("1.000"), "price": Decimal("100.00"), "fee": Decimal("0.1000")}
        )
        result = self.reducer.apply_fill(duplicate)
        self.assertFalse(result.applied)
        self.assertIsNone(result.incident)

    def test_acceptance_after_rejection_restores_potential_exposure(self) -> None:
        order = self.prepare(intent())
        self.reducer.reject(order.intent_id, "rejected", proven_no_creation=True)
        result = self.reducer.acknowledge(order.intent_id, "actually-created", "contradictory-proof")
        self.assertEqual(result.submission, Submission.ACKNOWLEDGED)
        self.assertFalse(result.terminal)
        self.assertEqual(result.reservation_remaining, Decimal(200))

    def test_contradictory_response_cannot_erase_fill(self) -> None:
        order = self.prepare(intent())
        self.reducer.apply_fill(fill_for(order))
        result = self.reducer.reject(order.intent_id, "late-rejection", proven_no_creation=True)
        self.assertEqual(result.submission, Submission.ACKNOWLEDGED)
        self.assertEqual(result.filled_quantity, Decimal(1))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute(
                    "SELECT COUNT(*) FROM ex_authority_fences WHERE active=1"
                ).fetchone()[0]
            ),
            1,
        )

    def test_final_gate_after_marker_prevents_send(self) -> None:
        order = self.prepare(intent())
        gate = Gate()
        gate.stop_at_validation = 3
        gateway = NonIdempotentGateway()
        result = Dispatcher(self.writer, gate, gateway).dispatch(order.intent_id).result(timeout=2)
        self.assertEqual(result.submission, Submission.REJECTED)
        self.assertEqual(result.reservation_remaining, Decimal(0))
        self.assertFalse(gateway.calls)

    def test_private_acceptance_during_final_gate_is_not_a_rejection_incident(self) -> None:
        from coinfly.execution.models import OrderIntent

        order = self.prepare(intent())
        reducer = self.reducer

        class EarlyGate(Gate):
            def validate(self, candidate: OrderIntent) -> None:
                super().validate(candidate)
                if self.validations == 3:
                    reducer.apply_fill(fill_for(candidate))
                    raise DomainError("RISK_BOOK_BEHIND_NEW_FILL")

        gateway = NonIdempotentGateway()
        result = Dispatcher(self.writer, EarlyGate(), gateway).dispatch(order.intent_id).result(timeout=2)
        self.assertEqual(result.submission, Submission.ACKNOWLEDGED)
        self.assertFalse(gateway.calls)
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_authority_fences").fetchone()[
                    0
                ]
            ),
            0,
        )

    def test_deadline_unknown_still_accepts_late_positive_identity(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        dispatched = Dispatcher(self.writer, Gate(), gateway, response_timeout=0.02).dispatch(order.intent_id)
        self.assertEqual(dispatched.result(timeout=2).resolution, Resolution.UNKNOWN)
        gateway.pending.set_result(
            CreateResult(
                context=order.context,
                outcome=CreateOutcome.ACCEPTED,
                evidence_id="late-proof",
                venue_order_id="late-order",
            )
        )
        self.assertEqual(self.store.get(order.intent_id).submission, Submission.ACKNOWLEDGED)
        self.assertEqual(len(gateway.calls), 1)

    def test_success_after_early_fill_preserves_quantity(self) -> None:
        order = self.prepare(intent())
        gateway = NonIdempotentGateway()
        dispatched = Dispatcher(self.writer, Gate(), gateway).dispatch(order.intent_id)
        event = fill_for(order)
        self.reducer.apply_fill(event)
        gateway.pending.set_result(
            CreateResult(
                context=order.context,
                outcome=CreateOutcome.ACCEPTED,
                evidence_id="response",
                venue_order_id=event.venue_order_id,
            )
        )
        self.assertEqual(dispatched.result(timeout=2).filled_quantity, Decimal(1))

    def test_empty_complete_history_does_not_prove_rejection(self) -> None:
        order = self.prepare(intent())
        self.writer.call(
            lambda connection: claim_dispatch_tx(connection, order.intent_id, order.payload_hash)
        )
        result = Reconciler(self.writer).reconcile(
            ReconciliationEvidence(
                evidence_id="empty-history",
                context=order.context,
                intent_id=order.intent_id,
                kind=EvidenceKind.ABSENT_OR_AMBIGUOUS,
                source_digest="a" * 64,
                history_complete=True,
            )
        )
        self.assertEqual(result.resolution, Resolution.UNKNOWN)
        self.assertEqual(result.reservation_remaining, Decimal(200))

    def test_unknown_callback_precedes_persistence(self) -> None:
        order = self.prepare(intent())
        self.writer.call(
            lambda connection: claim_dispatch_tx(connection, order.intent_id, order.payload_hash)
        )
        seen: list[Resolution] = []
        from coinfly.execution.fact_reducer import FactReducer

        reducer = FactReducer(
            self.writer,
            provisional_fence=lambda authority, reason: seen.append(
                self.store.get(order.intent_id).resolution
            ),
        )
        reducer.unknown(order.intent_id, "network-fault")
        self.assertEqual(seen, [Resolution.NONE])

    def test_unattributed_fill_uses_suspense_and_fences(self) -> None:
        order = intent()
        event = fill_for(order).model_copy(update={"intent_id": None})
        result = self.reducer.apply_fill(event)
        self.assertEqual(result.incident, "UNATTRIBUTED_FILL")
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_positions").fetchone()[0]
            ),
            0,
        )
        self.assertEqual(
            Journal(self.writer).balance(order.context, "book", "venue_suspense:inventory", "BTC"), Decimal(1)
        )

    def test_cross_realm_fill_cannot_be_applied_to_intent(self) -> None:
        from coinfly.domain.context import Environment

        order = self.prepare(intent())
        event = fill_for(order).model_copy(update={"context": context(environment=Environment.LIVE)})
        with self.assertRaises(DomainError):
            self.reducer.apply_fill(event)
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_fills").fetchone()[0]
            ),
            0,
        )
