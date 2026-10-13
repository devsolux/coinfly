from __future__ import annotations

from concurrent.futures import Future
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Callable, TypeVar
from unittest import TestCase

from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.execution.dispatcher import CreateResult
from coinfly.execution.fact_reducer import FactReducer
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Fill, IntentKind, OrderIntent, Reservation, Side
from coinfly.storage.writer import Writer

T = TypeVar("T")


def context(epoch: int = 1, environment: Environment = Environment.SANDBOX) -> ContextKey:
    return ContextKey(
        authority_id="owner",
        environment=environment,
        product_track=ProductTrack.SPOT,
        broker_profile_id="paper-local",
        epoch=epoch,
    )


def intent(
    *,
    epoch: int = 1,
    revision: int = 1,
    quantity: str = "2",
    side: Side = Side.BUY,
    environment: Environment = Environment.SANDBOX,
) -> OrderIntent:
    return OrderIntent(
        context=context(epoch, environment),
        scope_id="book",
        profile_version=1,
        signer_version=0,
        slot_id="slot-btc",
        allocation_revision=revision,
        instrument="BTC-USD",
        base_asset="BTC",
        quote_asset="USD",
        side=side,
        kind=IntentKind.ENTRY if side == Side.BUY else IntentKind.EXIT,
        quantity=Decimal(quantity),
        limit_price=Decimal("100"),
    )


def fill_for(
    order: OrderIntent, *, trade: str = "trade-1", quantity: str = "1", price: str = "100", fee: str = "0.1"
) -> Fill:
    return Fill(
        context=order.context,
        scope_id=order.scope_id,
        venue_id="paper",
        account_id="account-book",
        instrument=order.instrument,
        trade_id=trade,
        venue_order_id="venue-" + order.intent_id,
        evidence_id="evidence-" + trade,
        intent_id=order.intent_id,
        base_asset=order.base_asset,
        quote_asset=order.quote_asset,
        side=order.side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_asset="USD",
    )


class Gate:
    def __init__(self) -> None:
        self.lock = RLock()
        self.stopped = False
        self.validations = 0
        self.stop_at_validation: int | None = None

    def validate(self, order: OrderIntent) -> None:
        self.validations += 1
        if self.stopped or self.validations == self.stop_at_validation:
            raise DomainError("CONTEXT_FENCED")

    def provisional_fence(self, authority_id: str, reason: str) -> None:
        self.stopped = True

    def run_at_send_boundary(self, order: OrderIntent, invoke: Callable[[], T]) -> T:
        with self.lock:
            self.validate(order)
            return invoke()


class NonIdempotentGateway:
    def __init__(self) -> None:
        self.calls: list[OrderIntent] = []
        self.pending: Future[CreateResult] = Future()

    def create(self, order: OrderIntent) -> Future[CreateResult]:
        self.calls.append(order)
        return self.pending


class LedgerCase(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "coinfly.sqlite"
        self.writer = Writer(self.path)
        self.store = IntentStore(self.writer)
        self.reducer = FactReducer(self.writer)

    def tearDown(self) -> None:
        self.writer.close()
        self.temporary.cleanup()

    def prepare(self, order: OrderIntent) -> OrderIntent:
        self.store.prepare(order, Reservation(asset="USD", amount=order.quantity * order.limit_price))
        return order
