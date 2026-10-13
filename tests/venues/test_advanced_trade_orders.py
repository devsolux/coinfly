"""Live spot order gateway against a synthetic authenticated transport (no socket, no credential).

Shapes follow the Advanced Trade documentation accessed 2026-09-21. The tests
pin the outcome discipline: ACCEPTED only with an order id, REJECTED_NO_CREATION
only for documented definitive reasons, everything else UNKNOWN and reconciled
from complete venue history by ``client_order_id``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import TestCase

from coinfly.domain.context import DomainError, Environment
from coinfly.execution.dispatcher import Dispatcher
from coinfly.execution.models import Resolution, Submission
from coinfly.execution.reconciler import EvidenceKind
from coinfly.security.credential_realms import CredentialPurpose
from coinfly.venues.coinbase.advanced_trade_orders import (
    AdvancedTradeOrderGateway,
    CancelOutcome,
    CreateClassification,
    classify_create_response,
    limit_order_body,
)
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile
from tests.execution.helpers import Gate, LedgerCase, intent

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.create_response: Mapping[str, Any] | Exception = {"status": 200, "body": {"success": True, "success_response": {"order_id": "venue-1"}}}
        self.cancel_response: Mapping[str, Any] = {"status": 200, "body": {"results": []}}
        self.order_pages: list[Mapping[str, Any]] = [{"orders": [], "has_next": False}]
        self.fill_pages: list[Mapping[str, Any]] = [{"fills": [], "cursor": ""}]

    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.posts.append((path, json.loads(json.dumps(body))))
        if path == "/orders":
            if isinstance(self.create_response, Exception):
                raise self.create_response
            return self.create_response
        return self.cancel_response

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        self.gets.append((path, dict(query or {})))
        pages = self.order_pages if path.endswith("/batch") else self.fill_pages
        index = int((query or {}).get("cursor", "0") or 0)
        return pages[index]


class BodyAndClassificationTests(TestCase):
    def test_limit_order_body_is_exact_and_documented(self) -> None:
        order = intent(environment=Environment.LIVE, quantity="0.00500000")
        body = limit_order_body(order)
        self.assertEqual(body, {"client_order_id": order.intent_id, "product_id": "BTC-USD", "side": "BUY",
                                "order_configuration": {"limit_limit_gtc": {"base_size": "0.005", "limit_price": "100", "post_only": False}}})
        self.assertNotIn("1E", json.dumps(body))

    def test_create_classification_discipline(self) -> None:
        accepted = classify_create_response({"status": 200, "body": {"success": True, "success_response": {"order_id": "abc"}}})
        self.assertEqual(accepted, (CreateClassification.ACCEPTED, "abc", "ACCEPTED"))
        rejected = classify_create_response({"status": 200, "body": {"success": False, "error_response": {
            "error": "INSUFFICIENT_FUND", "message": "Insufficient balance", "new_order_failure_reason": "INSUFFICIENT_FUND"}}})
        self.assertEqual(rejected[0], CreateClassification.REJECTED_NO_CREATION)
        for document in (
            {"status": 200, "body": {"success": False, "error_response": {"error": "SOMETHING_NEW"}}},
            {"status": 200, "body": {"success": True, "success_response": {}}},
            {"status": 503, "body": {}},
            {"status": 401, "body": {"error": "Unauthorized"}},
            {"status": 200, "body": "not json"},
        ):
            self.assertEqual(classify_create_response(document)[0], CreateClassification.UNKNOWN, document)

    def test_execution_profile_is_separate_from_the_read_only_profile(self) -> None:
        execution = endpoint_profile("coinbase-advanced-spot-execution")
        self.assertEqual(execution.validate("POST", "/orders", purpose=CredentialPurpose.EXECUTION),
                         "https://api.coinbase.com/api/v3/brokerage/orders")
        with self.assertRaisesRegex(DomainError, "OBSERVER_MUTATION_FORBIDDEN"):
            execution.validate("POST", "/orders", purpose=CredentialPurpose.OBSERVATION)
        with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
            endpoint_profile("coinbase-advanced-spot").validate("POST", "/orders", purpose=CredentialPurpose.EXECUTION)
        with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
            execution.validate("DELETE", "/orders", purpose=CredentialPurpose.EXECUTION)


class GatewayTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.transport = FakeTransport()
        self.order = self.prepare(intent(environment=Environment.LIVE, quantity="0.5"))
        self.gateway = AdvancedTradeOrderGateway(self.transport, context=self.order.context, scope_id="book", writer=self.writer,
                                                 reducer=self.reducer, clock=lambda: NOW)
        self.gate = Gate()

    def dispatch(self) -> Any:
        return Dispatcher(self.writer, self.gate, self.gateway, reducer=self.reducer, response_timeout=5).dispatch(self.order.intent_id).result(timeout=5)

    def test_accepted_create_is_acknowledged_once(self) -> None:
        record = self.dispatch()
        self.assertEqual(record.submission, Submission.ACKNOWLEDGED)
        self.assertEqual(record.venue_order_id, "venue-1")
        self.assertEqual(len(self.transport.posts), 1)
        path, body = self.transport.posts[0]
        self.assertEqual((path, body["client_order_id"], body["order_configuration"]["limit_limit_gtc"]["base_size"]),
                         ("/orders", self.order.intent_id, "0.5"))
        # A second dispatch of the same intent never POSTs again.
        with self.assertRaises(DomainError):
            self.dispatch()
        self.assertEqual(len(self.transport.posts), 1)

    def test_definitive_rejection_releases_without_creation(self) -> None:
        self.transport.create_response = {"status": 200, "body": {"success": False, "error_response": {
            "error": "INSUFFICIENT_FUND", "message": "x", "new_order_failure_reason": "INSUFFICIENT_FUND"}}}
        record = self.dispatch()
        self.assertEqual(record.submission, Submission.REJECTED)
        self.assertTrue(record.terminal)
        self.assertEqual(record.reservation_remaining, Decimal(0))

    def test_unknown_outcome_is_reconciled_only_from_complete_history(self) -> None:
        self.transport.create_response = DomainError("HTTP_WRITE_OUTCOME_UNKNOWN")
        record = self.dispatch()
        self.assertEqual(record.resolution, Resolution.UNKNOWN)
        self.assertFalse(record.terminal)
        # Incomplete history (has_next with no more pages available) cannot resolve.
        self.transport.order_pages = [{"orders": [{"order_id": "other", "client_order_id": "someone-else"}], "has_next": True, "cursor": "1"},
                                      {"orders": [], "has_next": False}]
        evidence = self.gateway.reconcile_unknown(self.order.intent_id)
        self.assertIs(evidence.kind, EvidenceKind.ABSENT_OR_AMBIGUOUS)
        self.assertTrue(evidence.history_complete)
        self.assertEqual(self.store.get(self.order.intent_id).resolution, Resolution.UNKNOWN)
        self.assertEqual(self.transport.gets[-1][1]["cursor"], "1")
        self.assertEqual(self.transport.gets[-2][1]["product_ids"], "BTC-USD")
        self.assertTrue(self.transport.gets[-2][1]["start_date"].endswith("Z"))
        # Positive identity by client_order_id resolves the intent as acknowledged.
        self.transport.order_pages = [{"orders": [{"order_id": "venue-9", "client_order_id": self.order.intent_id, "status": "OPEN"}],
                                       "has_next": False}]
        evidence = self.gateway.reconcile_unknown(self.order.intent_id)
        self.assertIs(evidence.kind, EvidenceKind.POSITIVE_IDENTITY)
        resolved = self.store.get(self.order.intent_id)
        self.assertEqual((resolved.submission, resolved.venue_order_id, resolved.resolution),
                         (Submission.ACKNOWLEDGED, "venue-9", Resolution.RESOLVED))

    def test_cancel_outcomes_follow_documented_failure_reasons(self) -> None:
        self.transport.cancel_response = {"status": 200, "body": {"results": [
            {"success": True, "order_id": "a"}, {"success": False, "failure_reason": "ORDER_IS_FULLY_FILLED", "order_id": "b"},
            {"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER", "order_id": "c"},
            {"success": False, "failure_reason": "NOT_ALLOWED_TO_CANCEL", "order_id": "d"}]}}
        outcomes = self.gateway.cancel(["a", "b", "c", "d", "e"])
        self.assertEqual(outcomes, {"a": CancelOutcome.CANCELLED, "b": CancelOutcome.ALREADY_TERMINAL, "c": CancelOutcome.UNKNOWN_ORDER,
                                    "d": CancelOutcome.REFUSED, "e": CancelOutcome.UNKNOWN})
        self.assertEqual(self.transport.posts[-1], ("/orders/batch_cancel", {"order_ids": ["a", "b", "c", "d", "e"]}))
        with self.assertRaisesRegex(DomainError, "CANCEL_BATCH_INVALID"):
            self.gateway.cancel([])

    def test_fills_become_exactly_once_facts_for_owned_orders_only(self) -> None:
        self.dispatch()  # acknowledged as venue-1
        self.transport.fill_pages = [{"fills": [
            {"entry_id": "e1", "trade_id": "t1", "order_id": "venue-1", "price": "99.5", "size": "0.2", "commission": "0.01", "side": "BUY",
             "size_in_quote": False, "trade_time": "2026-09-21T12:00:01Z"},
            {"entry_id": "e2", "trade_id": "t2", "order_id": "not-ours", "price": "1", "size": "1", "commission": "0", "side": "BUY"},
            {"entry_id": "e3", "trade_id": "t3", "order_id": "venue-1", "price": "99.5", "size": "5", "commission": "0", "side": "BUY",
             "size_in_quote": True},
        ], "cursor": ""}]
        results = self.gateway.ingest_fills(product_id="BTC-USD", since=NOW - timedelta(minutes=5))
        self.assertEqual([(r.applied, r.incident) for r in results], [(True, None), (False, "FILL_UNITS_UNVERIFIED")])
        record = self.store.get(self.order.intent_id)
        self.assertEqual(record.filled_quantity, Decimal("0.2"))
        again = self.gateway.ingest_fills(product_id="BTC-USD", since=NOW - timedelta(minutes=5))
        self.assertEqual([r.applied for r in again], [False, False])  # idempotent by trade id
        self.assertEqual(self.store.get(self.order.intent_id).filled_quantity, Decimal("0.2"))
        self.assertEqual(self.transport.gets[-1][1]["start_sequence_timestamp"], "2026-09-21T11:55:00Z")
