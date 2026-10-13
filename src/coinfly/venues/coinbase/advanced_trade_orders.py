"""Coinbase Advanced Trade spot order gateway: create, cancel, list, fills, reconcile.

Documented contract (Advanced Trade REST, accessed 2026-09-21):

* ``POST /orders`` — body ``client_order_id`` (unique per order; a reused id is
  NOT created again and the venue returns the existing order), ``product_id``,
  ``side`` BUY|SELL, ``order_configuration`` (here ``limit_limit_gtc`` with
  ``base_size``/``limit_price`` strings, ``post_only`` false). Response
  ``success`` true → ``success_response.order_id``; false → ``error_response``
  with ``error``/``message`` and ``new_order_failure_reason``.
* ``POST /orders/batch_cancel`` — ``order_ids``; response ``results[]`` with
  ``success``, ``failure_reason`` (UNKNOWN_CANCEL_ORDER, ORDER_IS_FULLY_FILLED,
  DUPLICATE_CANCEL_REQUEST, NOT_ALLOWED_TO_CANCEL, ...), ``order_id``.
* ``GET /orders/historical/batch`` — ``product_ids``, ``order_status``,
  ``start_date``, ``limit``, ``cursor``; response ``orders[]`` (``order_id``,
  ``client_order_id``, ``status`` OPEN|FILLED|CANCELLED|EXPIRED|FAILED|
  UNKNOWN_ORDER_STATUS|QUEUED|CANCEL_QUEUED|PENDING, ``filled_size``,
  ``size_in_quote``), ``has_next``, ``cursor``.
* ``GET /orders/historical/fills`` — ``product_ids``, ``order_ids``,
  ``start_sequence_timestamp``, ``limit``, ``cursor``; response ``fills[]``
  (``entry_id``, ``trade_id``, ``order_id``, ``trade_time``, ``price``,
  ``size``, ``commission``, ``side``, ``size_in_quote``), ``cursor``.

Outcome discipline: a create returns ACCEPTED only on a positive order identity,
REJECTED_NO_CREATION only for a documented definitive failure reason, and every
other case (timeouts, transport errors, undocumented reasons, HTTP 5xx) raises
so the dispatcher records UNKNOWN. UNKNOWN is resolved exclusively from the
venue's own order history: a complete page walk that finds the
``client_order_id`` is positive identity; anything else stays ambiguous, and
elapsed time never upgrades evidence. Numerics are exact Decimals from strings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from coinfly.domain.context import ContextKey, DomainError
from coinfly.execution.dispatcher import CreateOutcome, CreateResult
from coinfly.execution.fact_reducer import FactReducer, FillResult
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Fill, OrderIntent, Side
from coinfly.execution.reconciler import EvidenceKind, Reconciler, ReconciliationEvidence
from coinfly.storage.writer import Writer

VENUE_ID = "coinbase-advanced-trade"
MAX_PAGES = 200
PAGE_LIMIT = 100

# new_order_failure_reason values documented as definitive "not created"
# outcomes. Anything not listed here is treated as UNKNOWN and reconciled from
# order history — an undocumented reason never proves non-creation.
DEFINITIVE_NONCREATION_REASONS = frozenset({
    "INSUFFICIENT_FUND", "INSUFFICIENT_FUNDS_FOR_FUTURES", "INVALID_PRICE_PRECISION", "INVALID_SIZE_PRECISION",
    "INVALID_LIMIT_PRICE_POST_ONLY", "INVALID_LIMIT_PRICE", "INVALID_ORDER_CONFIGURATION", "INVALID_SIDE",
    "INVALID_PRODUCT_TYPE", "INELIGIBLE_PAIR", "ORDER_ENTRY_DISABLED", "UNSUPPORTED_ORDER_CONFIGURATION",
    "INVALID_MARGIN_TYPE", "INVALID_QUOTE_SIZE_PRECISION", "INVALID_REQUEST", "INVALID_LEVERAGE",
})
TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELLED", "EXPIRED", "FAILED"})
OPEN_ORDER_STATUSES = frozenset({"OPEN", "PENDING", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED"})


class AuthenticatedTransport(Protocol):
    """GET/POST on the execution endpoint profile; POST raises HTTP_WRITE_OUTCOME_UNKNOWN after initiation."""

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]: ...
    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CreateClassification(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED_NO_CREATION = "REJECTED_NO_CREATION"
    UNKNOWN = "UNKNOWN"


class CancelOutcome(StrEnum):
    CANCELLED = "CANCELLED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    REFUSED = "REFUSED"
    UNKNOWN = "UNKNOWN"


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}") from None
    if not number.is_finite():
        raise DomainError("VENUE_NUMBER_INVALID", f"VENUE_NUMBER_INVALID: {field}")
    return number


def limit_order_body(intent: OrderIntent) -> dict[str, Any]:
    """The documented POST /orders body for a GTC limit order; exact strings, no floats."""
    if intent.contract_multiplier != 1:
        raise DomainError("SPOT_CONTRACT_MULTIPLIER_INVALID")
    return {
        "client_order_id": intent.intent_id,
        "product_id": intent.instrument,
        "side": intent.side.value,
        "order_configuration": {
            "limit_limit_gtc": {"base_size": _plain(intent.quantity), "limit_price": _plain(intent.limit_price), "post_only": False},
        },
    }


def classify_create_response(document: Mapping[str, Any]) -> tuple[CreateClassification, str | None, str]:
    """Map the venue's create response onto ACCEPTED / REJECTED_NO_CREATION / UNKNOWN."""
    status = document.get("status")
    body = document.get("body")
    if not isinstance(body, Mapping):
        return CreateClassification.UNKNOWN, None, "RESPONSE_NOT_JSON"
    if status is not None and (not isinstance(status, int) or status >= 500):
        return CreateClassification.UNKNOWN, None, f"HTTP_{status}"
    if body.get("success") is True:
        success = body.get("success_response")
        order_id = success.get("order_id") if isinstance(success, Mapping) else None
        if isinstance(order_id, str) and order_id:
            return CreateClassification.ACCEPTED, order_id, "ACCEPTED"
        return CreateClassification.UNKNOWN, None, "ACCEPTED_WITHOUT_ORDER_ID"
    error = body.get("error_response")
    if body.get("success") is False and isinstance(error, Mapping):
        reason = str(error.get("new_order_failure_reason") or error.get("error") or "")
        if reason in DEFINITIVE_NONCREATION_REASONS:
            return CreateClassification.REJECTED_NO_CREATION, None, reason
        return CreateClassification.UNKNOWN, None, reason or "UNDOCUMENTED_FAILURE"
    if isinstance(status, int) and 400 <= status < 500 and "error" in body:
        # Authentication/permission problems: nothing was created, but the
        # venue does not say so for the order itself; keep it reconcilable.
        return CreateClassification.UNKNOWN, None, str(body.get("error"))[:60]
    return CreateClassification.UNKNOWN, None, "RESPONSE_SHAPE_UNRECOGNIZED"


def _document_digest(document: object) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, default=str).encode()).hexdigest()


class AdvancedTradeOrderGateway:
    """CreateGateway + cancel/list/fills for ONE live spot context.

    Nothing here decides *whether* to trade: the admission actor and dispatcher
    already persisted and authorized the intent. The gateway only carries the
    documented request, classifies the documented response, and turns venue
    history into facts the reducer applies idempotently (each venue fill id
    and each terminal evidence id is booked at most once).
    """

    def __init__(self, transport: AuthenticatedTransport, *, context: ContextKey, scope_id: str, writer: Writer,
                 reducer: FactReducer | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self._transport = transport
        self.context = context
        self.scope_id = scope_id
        self.writer = writer
        self.reducer = reducer or FactReducer(writer)
        self.intents = IntentStore(writer)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconciler = Reconciler(writer, self.reducer)

    # ---------------------------------------------------------------- create
    def create(self, intent: OrderIntent) -> Future[CreateResult]:
        """Called by the Dispatcher inside the send interlock: at most one POST per intent.

        ``client_order_id`` is the intent id, which the venue documents as its
        dedup key; the outcome is ACCEPTED (order id), REJECTED_NO_CREATION
        (documented definitive reason) or UNKNOWN (initiated, unobserved).
        """
        output: Future[CreateResult] = Future()
        if intent.context != self.context or intent.scope_id != self.scope_id:
            raise DomainError("ADAPTER_CONTEXT_MISMATCH")
        body = limit_order_body(intent)
        try:
            document = self._transport.post("/orders", body)
        except DomainError as error:
            if error.code == "HTTP_WRITE_OUTCOME_UNKNOWN":
                output.set_exception(error)  # dispatcher → UNKNOWN; reconciliation resolves it
                return output
            raise  # never initiated: the dispatcher records a proven non-send
        classification, order_id, reason = classify_create_response(document)
        evidence_id = f"cb-create-{intent.intent_id}-{_document_digest(document)[:16]}"
        if classification is CreateClassification.ACCEPTED:
            output.set_result(CreateResult(context=self.context, outcome=CreateOutcome.ACCEPTED, venue_order_id=order_id,
                                           evidence_id=evidence_id))
        elif classification is CreateClassification.REJECTED_NO_CREATION:
            output.set_result(CreateResult(context=self.context, outcome=CreateOutcome.REJECTED_NO_CREATION, evidence_id=evidence_id))
        else:
            output.set_exception(DomainError("CREATE_OUTCOME_UNKNOWN", f"CREATE_OUTCOME_UNKNOWN: {reason}"))
        return output

    # ---------------------------------------------------------------- cancel
    def cancel(self, venue_order_ids: Sequence[str]) -> dict[str, CancelOutcome]:
        ids = list(dict.fromkeys(venue_order_ids))
        if not ids or len(ids) > 100:
            raise DomainError("CANCEL_BATCH_INVALID")
        try:
            document = self._transport.post("/orders/batch_cancel", {"order_ids": ids})
        except DomainError as error:
            if error.code == "HTTP_WRITE_OUTCOME_UNKNOWN":
                return {order_id: CancelOutcome.UNKNOWN for order_id in ids}
            raise
        body = document.get("body") if isinstance(document, Mapping) else None
        results = body.get("results") if isinstance(body, Mapping) else None
        outcomes = {order_id: CancelOutcome.UNKNOWN for order_id in ids}
        if not isinstance(results, list):
            return outcomes
        for item in results:
            if not isinstance(item, Mapping) or item.get("order_id") not in outcomes:
                continue
            if item.get("success") is True:
                outcomes[str(item["order_id"])] = CancelOutcome.CANCELLED
                continue
            reason = str(item.get("failure_reason", ""))
            if reason in ("ORDER_IS_FULLY_FILLED", "DUPLICATE_CANCEL_REQUEST"):
                outcomes[str(item["order_id"])] = CancelOutcome.ALREADY_TERMINAL
            elif reason == "UNKNOWN_CANCEL_ORDER":
                outcomes[str(item["order_id"])] = CancelOutcome.UNKNOWN_ORDER
            elif reason in ("NOT_ALLOWED_TO_CANCEL", "COMMANDER_REJECTED_CANCEL_ORDER", "INVALID_CANCEL_REQUEST",
                            "INVALID_CANCEL_PRODUCT_ID", "INVALID_CANCEL_FCM_TRADING_SESSION"):
                outcomes[str(item["order_id"])] = CancelOutcome.REFUSED
        return outcomes

    # ---------------------------------------------------------------- history
    def _walk(self, path: str, key: str, query: dict[str, str]) -> tuple[list[Mapping[str, Any]], bool, str]:
        """Page through a history route; returns (items, complete, digest)."""
        items: list[Mapping[str, Any]] = []
        digest = hashlib.sha256()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            page_query = dict(query)
            page_query["limit"] = str(PAGE_LIMIT)
            if cursor:
                page_query["cursor"] = cursor
            page = self._transport.get(path, query=page_query)
            digest.update(json.dumps(page, sort_keys=True, default=str).encode())
            found = page.get(key)
            if not isinstance(found, list):
                raise DomainError("HTTP_RESPONSE_INVALID")
            if any(not isinstance(item, Mapping) for item in found):
                raise DomainError("HTTP_RESPONSE_INVALID")
            items.extend(found)
            next_cursor = page.get("cursor")
            has_next = page.get("has_next")
            if has_next is False or (has_next is None and not next_cursor):
                return items, True, digest.hexdigest()
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise DomainError("HISTORY_PAGINATION_INCOMPLETE")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return items, False, digest.hexdigest()

    def list_orders(self, *, product_id: str, since: datetime, statuses: Sequence[str] = ()) -> tuple[list[Mapping[str, Any]], bool, str]:
        query = {"product_ids": product_id, "start_date": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        if statuses:
            query["order_status"] = ",".join(statuses)
        return self._walk("/orders/historical/batch", "orders", query)

    def list_open_orders(self) -> tuple[list[Mapping[str, Any]], bool, str]:
        """All products in the credential-bound portfolio, without a date cutoff.

        Query each documented status individually; a comma-encoded multi-value
        query is not assumed to have the same semantics on every API version.
        This route covers RETAIL_ADVANCED orders, not unobserved account services.
        Missing or different portfolio identity is never silently adopted.
        """
        found: dict[str, Mapping[str, Any]] = {}
        digest = hashlib.sha256()
        for status in sorted(OPEN_ORDER_STATUSES | {"UNKNOWN_ORDER_STATUS"}):
            rows, complete, page_digest = self._walk(
                "/orders/historical/batch", "orders", {"order_status": status}
            )
            digest.update(page_digest.encode("ascii"))
            if not complete:
                raise DomainError("ORDER_HISTORY_INCOMPLETE")
            for row in rows:
                order_id = row.get("order_id")
                if not isinstance(order_id, str) or not order_id:
                    raise DomainError("ORDER_IDENTITY_MISSING")
                if row.get("retail_portfolio_id") != self.scope_id:
                    raise DomainError("ORDER_PORTFOLIO_SCOPE_UNVERIFIED")
                # Repeated status queries can observe a state transition. Retain
                # its latest observation; never treat disappearance as closure.
                found[order_id] = row
        return list(found.values()), True, digest.hexdigest()

    def reconcile_unknown(self, intent_id: str, *, lookback: timedelta = timedelta(hours=1)) -> ReconciliationEvidence:
        """Resolve an UNKNOWN create from venue history by client_order_id; never by elapsed time."""
        record = self.intents.get(intent_id)
        if record.intent.context != self.context:
            raise DomainError("ADAPTER_CONTEXT_MISMATCH")
        orders, complete, digest = self.list_orders(product_id=record.intent.instrument, since=self._clock() - lookback)
        match = next((order for order in orders if order.get("client_order_id") == intent_id), None)
        if match is not None and isinstance(match.get("order_id"), str) and match["order_id"]:
            evidence = ReconciliationEvidence(evidence_id=f"cb-history-{intent_id}-{digest[:16]}", context=self.context,
                                              intent_id=intent_id, kind=EvidenceKind.POSITIVE_IDENTITY, source_digest=digest,
                                              history_complete=complete, venue_order_id=str(match["order_id"]))
        else:
            # A complete history without the client id is still not a documented
            # non-creation guarantee: the venue's idempotent client_order_id is the
            # only authoritative probe, and that is a mutation the operator must
            # approve. The intent stays ambiguous and fenced.
            evidence = ReconciliationEvidence(evidence_id=f"cb-history-{intent_id}-{digest[:16]}", context=self.context,
                                              intent_id=intent_id, kind=EvidenceKind.ABSENT_OR_AMBIGUOUS, source_digest=digest,
                                              history_complete=complete)
        self.reconciler.reconcile(evidence)
        return evidence

    def order_status(self, venue_order_id: str, *, product_id: str, since: datetime) -> Mapping[str, Any] | None:
        orders, _, _ = self.list_orders(product_id=product_id, since=since)
        return next((order for order in orders if order.get("order_id") == venue_order_id), None)

    # ---------------------------------------------------------------- fills
    def _intent_for_order(self, venue_order_id: str) -> str | None:
        def read(connection: Any) -> str | None:
            row = connection.execute(
                "SELECT i.intent_id FROM ex_outbox o JOIN ex_intents i USING(intent_id) WHERE o.venue_order_id=? AND i.context_id=?",
                (venue_order_id, self.context.identity)).fetchone()
            return str(row[0]) if row is not None else None
        result: str | None = self.writer.call(read)
        return result

    def ingest_fills(self, *, product_id: str, since: datetime) -> tuple[FillResult, ...]:
        """Turn venue fills into Fill facts for intents this context owns; each venue fill id is booked at most once."""
        query = {"product_ids": product_id,
                 "start_sequence_timestamp": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        fills, complete, digest = self._walk("/orders/historical/fills", "fills", query)
        if not complete:
            raise DomainError("FILL_HISTORY_INCOMPLETE")
        known: dict[str, OrderIntent] = {}
        results: list[FillResult] = []
        for item in fills:
            order_id = item.get("order_id")
            trade_id = item.get("trade_id") or item.get("entry_id")
            if not isinstance(order_id, str) or not isinstance(trade_id, str) or not trade_id:
                continue
            intent = known.get(order_id)
            if intent is None:
                intent_id = self._intent_for_order(order_id)
                if intent_id is None:
                    continue  # not ours (another portfolio/client); never attributed by guesswork
                intent = self.intents.get(intent_id).intent
                known[order_id] = intent
            if item.get("size_in_quote") is True:
                results.append(FillResult(f"{order_id}:{trade_id}", False, None, 0, "FILL_UNITS_UNVERIFIED"))
                continue
            side = str(item.get("side", "")).upper()
            if side != intent.side.value:
                results.append(FillResult(f"{order_id}:{trade_id}", False, None, 0, "CONFLICTING_FILL_FACT"))
                continue
            fill = Fill(context=self.context, scope_id=self.scope_id, venue_id=VENUE_ID, account_id=self.scope_id,
                        instrument=intent.instrument, trade_id=trade_id, venue_order_id=order_id,
                        evidence_id=f"cb-fill-{trade_id}-{digest[:12]}", intent_id=intent.intent_id,
                        base_asset=intent.base_asset, quote_asset=intent.quote_asset, side=Side(side),
                        quantity=_decimal(item.get("size"), field="size"), price=_decimal(item.get("price"), field="price"),
                        fee=_decimal(item.get("commission"), field="commission"), fee_asset=intent.quote_asset)
            results.append(self.reducer.apply_fill(fill))
        return tuple(results)
