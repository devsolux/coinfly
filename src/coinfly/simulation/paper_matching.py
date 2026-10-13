"""Stateful local matcher with explicit synthetic identity and durable snapshots.

Depth is consumed once per supplied snapshot; fills occur after configured latency.
This is a deterministic test model, not an exchange matching certification.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from hashlib import sha256

from pydantic import Field, field_validator, model_validator

from coinfly.domain.context import ContextKey, DomainError, Environment, FrozenModel, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal
from coinfly.domain.products import ProductSubtype
from coinfly.simulation.paper_margin import PaperMarginBook, _bounded, _derived


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class PaperInstrument(FrozenModel):
    instrument_id: str
    base_currency: str
    quote_currency: str
    product_track: ProductTrack
    # Declared contract family and expiry (from the catalog): the paper venue's
    # synthetic funding/settlement schedule follows the declared subtype only.
    # Absent, a perpetual-track instrument is the international no-expiry
    # perpetual (the bare track's meaning under the GLOBAL policy).
    subtype: ProductSubtype | None = None
    expiry: datetime | None = None
    quantity_increment: ExactDecimal = Field(default=Decimal("0.00000001"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    price_increment: ExactDecimal = Field(default=Decimal("0.01"), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    minimum_notional: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    contract_multiplier: ExactDecimal = Field(default=Decimal(1), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)

    @property
    def declared_subtype(self) -> ProductSubtype:
        if self.subtype is not None:
            return self.subtype
        return ProductSubtype.SPOT if self.product_track == ProductTrack.SPOT else ProductSubtype.PERPETUAL_NO_EXPIRY

    @model_validator(mode="after")
    def spot_units(self) -> "PaperInstrument":
        if self.product_track == ProductTrack.SPOT and self.contract_multiplier != 1:
            raise ValueError("spot quantities are base units")
        if self.subtype is not None and self.subtype.track is not self.product_track:
            raise ValueError("paper instrument subtype does not match its track")
        if self.expiry is not None and not self.declared_subtype.has_expiry:
            raise ValueError("only expiring contract families carry an expiry")
        if not self.instrument_id or not self.base_currency or not self.quote_currency:
            raise ValueError("instrument and currency identity required")
        return self


class DepthLevel(FrozenModel):
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


class PaperOrderRequest(FrozenModel):
    client_order_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=160)
    side: Side
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    limit_price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    reduce_only: bool = False


class PaperOrder(FrozenModel):
    order_id: str
    request: PaperOrderRequest
    created_at: datetime
    eligible_at: datetime
    filled_quantity: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    status: PaperOrderStatus

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def instrument_id(self) -> str:
        return self.request.instrument_id

    @property
    def side(self) -> Side:
        return self.request.side

    @property
    def quantity(self) -> Decimal:
        return self.request.quantity

    @property
    @exact_decimal
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def terminal(self) -> bool:
        return self.status in (PaperOrderStatus.CANCELED, PaperOrderStatus.FILLED, PaperOrderStatus.REJECTED)

    @field_validator("created_at", "eligible_at")
    @classmethod
    def aware(cls, stamp: datetime) -> datetime:
        _aware(stamp)
        return stamp.astimezone(timezone.utc)

    @model_validator(mode="after")
    def consistent(self) -> "PaperOrder":
        if self.filled_quantity > self.quantity or self.eligible_at < self.created_at:
            raise ValueError("invalid paper order state")
        if (self.status == PaperOrderStatus.FILLED) != (self.filled_quantity == self.quantity):
            raise ValueError("filled status/quantity mismatch")
        if self.status == PaperOrderStatus.OPEN and self.filled_quantity:
            raise ValueError("open order already has fills")
        if self.status == PaperOrderStatus.PARTIALLY_FILLED and not 0 < self.filled_quantity < self.quantity:
            raise ValueError("invalid partial quantity")
        return self


class PaperFill(FrozenModel):
    trade_id: str
    order_id: str
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False)
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False)
    fee: ExactDecimal = Field(ge=0, allow_inf_nan=False)
    fee_currency: str
    realized_pnl: ExactDecimal
    as_of: datetime


class _Book(FrozenModel):
    instrument_id: str
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    as_of: datetime
    sequence: int = Field(ge=0)


class _FlatRebindProvenance(FrozenModel):
    """Receipt for a worker-verified, flat, same-book context retirement.

    The caller additionally binds its execution scope and durable stop certificate.
    This receipt identifies the retained source snapshot; it is not a venue proof.
    """

    source_context: ContextKey
    target_context: ContextKey
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    settled_balances: tuple[tuple[str, ExactDecimal], ...]
    settled_margin_collateral: ExactDecimal | None


class _PaperState(FrozenModel):
    schema_version: int
    context: ContextKey
    instruments: tuple[PaperInstrument, ...]
    fee_rate: ExactDecimal = Field(ge=0, lt=1, allow_inf_nan=False, max_digits=38, decimal_places=18)
    latency_microseconds: int = Field(ge=0)
    book_max_age_microseconds: int = Field(gt=0)
    balances: tuple[tuple[str, ExactDecimal], ...]
    initial_balances: tuple[tuple[str, ExactDecimal], ...]
    orders: tuple[PaperOrder, ...]
    fills: tuple[PaperFill, ...]
    books: tuple[_Book, ...]
    counter: int = Field(ge=0)
    trade_counter: int = Field(ge=0)
    margin: dict[str, object] | None
    last_time: datetime | None
    id_namespace: str = ""
    flat_rebind_provenance: _FlatRebindProvenance | None = None
    retired_for_rebind: bool = False


class PaperMatcher:
    def __init__(
        self, context: ContextKey, instruments: tuple[PaperInstrument, ...], *,
        initial_balances: dict[str, Decimal], fee_rate: Decimal = Decimal("0.001"),
        latency: timedelta = timedelta(0), margin: PaperMarginBook | None = None,
        book_max_age: timedelta = timedelta(seconds=5),
    ) -> None:
        if context.environment != Environment.SANDBOX:
            raise DomainError("PAPER_REQUIRES_SANDBOX")
        if not instruments or len({item.instrument_id for item in instruments}) != len(instruments):
            raise ValueError("unique synthetic instrument definitions required")
        if any(item.product_track != context.product_track for item in instruments):
            raise DomainError("PAPER_PRODUCT_TRACK_MISMATCH")
        if context.product_track == ProductTrack.PERPETUAL:
            if margin is None or initial_balances:
                raise DomainError("PAPER_MARGIN_BOOK_REQUIRED")
        elif margin is not None:
            raise DomainError("PAPER_MARGIN_IN_SPOT_BOOK")
        _bounded(fee_rate)
        if not 0 <= fee_rate < 1 or latency < timedelta(0) or book_max_age <= timedelta(0):
            raise ValueError("invalid synthetic fee/latency/freshness configuration")
        for currency, value in initial_balances.items():
            _bounded(value)
            if not currency or value < 0:
                raise ValueError("nonnegative funded synthetic balances required")
        self._context = context
        self.instruments = instruments
        self.fee_rate = fee_rate
        self.latency = latency
        self.book_max_age = book_max_age
        self.margin = margin
        self._instruments = {item.instrument_id: item for item in instruments}
        self._balances = dict(initial_balances)
        self._initial_balances = dict(initial_balances)
        self._orders: dict[str, PaperOrder] = {}
        self._clients: dict[str, str] = {}
        self._fills: list[PaperFill] = []
        self._books: dict[str, _Book] = {}
        self._counter = 0
        self._trade_counter = 0
        self._last_time: datetime | None = None
        self._id_namespace = ""
        self._flat_rebind_provenance: _FlatRebindProvenance | None = None
        self._retired_for_rebind = False

    @property
    def context(self) -> ContextKey:
        return self._context

    @property
    def orders(self) -> tuple[PaperOrder, ...]:
        return tuple(self._orders.values())

    @property
    def open_orders(self) -> tuple[PaperOrder, ...]:
        return tuple(order for order in self.orders if not order.terminal)

    @property
    def fills(self) -> tuple[PaperFill, ...]:
        return tuple(self._fills)

    @property
    def balances(self) -> dict[str, Decimal]:
        return dict(self._balances)

    @property
    @exact_decimal
    def available_balances(self) -> dict[str, Decimal]:
        available = dict(self._balances)
        if self.context.product_track == ProductTrack.SPOT:
            for order in self.open_orders:
                instrument = self._instrument(order.instrument_id)
                if order.side == Side.BUY:
                    currency = instrument.quote_currency
                    held = order.remaining_quantity * order.request.limit_price * (1 + self.fee_rate)
                else:
                    currency = instrument.base_currency
                    held = order.remaining_quantity
                available[currency] = available.get(currency, Decimal(0)) - held
        return available

    @exact_decimal
    def submit(self, request: PaperOrderRequest, *, now: datetime) -> PaperOrder:
        self._check_time(now)
        existing_id = self._clients.get(request.client_order_id)
        if existing_id is not None:
            existing = self._orders[existing_id]
            if request != existing.request:
                raise DomainError("PAPER_CLIENT_ID_CONFLICT")
            return existing
        if len(self._orders) >= 100000:
            raise DomainError("PAPER_ORDER_CAPACITY")
        instrument = self._instrument(request.instrument_id)
        if request.quantity % instrument.quantity_increment or request.limit_price % instrument.price_increment:
            raise DomainError("PAPER_INVALID_INCREMENT")
        notional = request.quantity * request.limit_price * instrument.contract_multiplier
        if notional < instrument.minimum_notional:
            raise DomainError("PAPER_BELOW_MINIMUM")
        if self.context.product_track == ProductTrack.SPOT:
            if request.reduce_only and request.side != Side.SELL:
                raise DomainError("PAPER_REDUCE_ONLY_VIOLATION")
            available = self.available_balances
            if request.side == Side.BUY:
                required = notional * (1 + self.fee_rate)
                if available.get(instrument.quote_currency, Decimal(0)) < required:
                    raise DomainError("PAPER_INSUFFICIENT_FUNDS")
            elif available.get(instrument.base_currency, Decimal(0)) < request.quantity:
                raise DomainError("PAPER_INSUFFICIENT_INVENTORY")
        else:
            assert self.margin is not None
            signed = request.quantity if request.side == Side.BUY else -request.quantity
            existing_quantity = self.margin.quantity(request.instrument_id)
            if request.reduce_only and (signed * existing_quantity >= 0 or abs(signed) > abs(existing_quantity)):
                raise DomainError("PAPER_REDUCE_ONLY_VIOLATION")
            if not request.reduce_only:
                snapshot = self.margin.snapshot()
                required = notional * (self.margin.initial_margin_fraction + self.fee_rate)
                required += self._pending_margin()
                if snapshot.available_margin is None or snapshot.available_margin < required:
                    raise DomainError("PAPER_INSUFFICIENT_MARGIN")
        self._counter += 1
        order = PaperOrder(
            order_id=self._order_identifier(self._counter), request=request, created_at=now,
            eligible_at=now + self.latency, filled_quantity=Decimal(0), status=PaperOrderStatus.OPEN,
        )
        self._orders[order.order_id] = order
        self._clients[order.client_order_id] = order.order_id
        return order

    @exact_decimal
    def update_book(
        self, instrument_id: str, *, bids: tuple[DepthLevel, ...], asks: tuple[DepthLevel, ...],
        now: datetime, sequence: int | None = None,
    ) -> tuple[PaperFill, ...]:
        self._check_time(now)
        self._instrument(instrument_id)
        if len(bids) > 1000 or len(asks) > 1000:
            raise DomainError("PAPER_DEPTH_LIMIT")
        if any(left.price <= right.price for left, right in zip(bids, bids[1:])):
            raise DomainError("PAPER_INVALID_BID_DEPTH")
        if any(left.price >= right.price for left, right in zip(asks, asks[1:])):
            raise DomainError("PAPER_INVALID_ASK_DEPTH")
        if bids and asks and bids[0].price >= asks[0].price:
            raise DomainError("PAPER_CROSSED_BOOK")
        old = self._books.get(instrument_id)
        seq = sequence if sequence is not None else (old.sequence + 1 if old else 0)
        if old is not None and seq <= old.sequence:
            raise DomainError("PAPER_STALE_BOOK_SEQUENCE")
        self._books[instrument_id] = _Book(instrument_id=instrument_id, bids=bids, asks=asks, as_of=now, sequence=seq)
        if self.margin is not None and bids and asks:
            self.margin.set_mark(instrument_id, (bids[0].price + asks[0].price) / 2)
        return self.advance(now=now)

    @exact_decimal
    def advance(self, *, now: datetime) -> tuple[PaperFill, ...]:
        self._check_time(now)
        first = len(self._fills)
        for order in self.open_orders:
            if now < order.eligible_at:
                continue
            book = self._books.get(order.instrument_id)
            if book is None or now - book.as_of > self.book_max_age:
                continue
            self._match(order, book, now)
        return tuple(self._fills[first:])

    def cancel(self, order_id: str, *, now: datetime) -> PaperOrder:
        self._check_time(now)
        # Matching/cancel ordering is explicit: callers advance market events before a later cancel.
        order = self.order(order_id)
        if not order.terminal:
            order = order.model_copy(update={"status": PaperOrderStatus.CANCELED})
            self._orders[order_id] = order
        return order

    def order(self, order_id: str) -> PaperOrder:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise DomainError("PAPER_ORDER_NOT_FOUND") from exc

    @exact_decimal
    def _match(self, order: PaperOrder, book: _Book, now: datetime) -> None:
        instrument = self._instrument(order.instrument_id)
        levels = list(book.asks if order.side == Side.BUY else book.bids)
        for index, level in enumerate(levels):
            if order.terminal or order.remaining_quantity <= 0:
                break
            if order.side == Side.BUY and level.price > order.request.limit_price:
                break
            if order.side == Side.SELL and level.price < order.request.limit_price:
                break
            quantity = min(order.remaining_quantity, level.quantity)
            quantity = (quantity / instrument.quantity_increment).to_integral_value(rounding=ROUND_DOWN) * instrument.quantity_increment
            if self.margin is not None and order.request.reduce_only:
                existing = self.margin.quantity(order.instrument_id)
                if (existing >= 0 and order.side == Side.BUY) or (existing <= 0 and order.side == Side.SELL):
                    order = order.model_copy(update={"status": PaperOrderStatus.CANCELED})
                    self._orders[order.order_id] = order
                    break
                quantity = min(quantity, abs(existing))
            if quantity == 0:
                continue
            fee = quantity * level.price * instrument.contract_multiplier * self.fee_rate
            realized = Decimal(0)
            if self.margin is not None:
                try:
                    realized = self.margin.apply_fill(
                        order.instrument_id, quantity if order.side == Side.BUY else -quantity,
                        level.price, multiplier=instrument.contract_multiplier, fee=fee,
                        reduce_only=order.request.reduce_only,
                    )
                except DomainError as exc:
                    if exc.code not in ("PAPER_INSUFFICIENT_MARGIN", "PAPER_REDUCE_ONLY_VIOLATION"):
                        raise
                    order = order.model_copy(update={"status": PaperOrderStatus.CANCELED})
                    self._orders[order.order_id] = order
                    break
            else:
                base = self._balances.get(instrument.base_currency, Decimal(0))
                quote = self._balances.get(instrument.quote_currency, Decimal(0))
                if order.side == Side.BUY:
                    self._balances[instrument.base_currency] = base + quantity
                    self._balances[instrument.quote_currency] = quote - quantity * level.price - fee
                else:
                    self._balances[instrument.base_currency] = base - quantity
                    self._balances[instrument.quote_currency] = quote + quantity * level.price - fee
                if min(self._balances[instrument.base_currency], self._balances[instrument.quote_currency]) < 0:
                    raise DomainError("PAPER_INVENTORY_INVARIANT")
            filled_quantity = order.filled_quantity + quantity
            status = PaperOrderStatus.FILLED if filled_quantity == order.quantity else PaperOrderStatus.PARTIALLY_FILLED
            order = order.model_copy(update={"filled_quantity": filled_quantity, "status": status})
            self._orders[order.order_id] = order
            self._trade_counter += 1
            self._fills.append(PaperFill(
                trade_id=self._trade_identifier(self._trade_counter), order_id=order.order_id,
                client_order_id=order.client_order_id, instrument_id=order.instrument_id,
                side=order.side, quantity=quantity, price=level.price, fee=fee,
                fee_currency=instrument.quote_currency, realized_pnl=realized, as_of=now,
            ))
            remainder = level.quantity - quantity
            levels[index] = level.model_copy(update={"quantity": remainder})
        remaining_levels = tuple(level for level in levels if level.quantity > 0)
        updates = {"asks" if order.side == Side.BUY else "bids": remaining_levels}
        self._books[book.instrument_id] = book.model_copy(update=updates)

    @exact_decimal
    def _pending_margin(self) -> Decimal:
        if self.margin is None:
            return Decimal(0)
        return sum((order.remaining_quantity * order.request.limit_price * self._instrument(order.instrument_id).contract_multiplier
                    * (self.margin.initial_margin_fraction + self.fee_rate)
                    for order in self.open_orders if not order.request.reduce_only), Decimal(0))

    def add_instrument(self, instrument: PaperInstrument) -> bool:
        """Extend the synthetic universe; existing definitions never change."""
        existing = self._instruments.get(instrument.instrument_id)
        if existing is not None:
            if existing != instrument:
                raise DomainError("PAPER_INSTRUMENT_DEFINITION_CHANGED")
            return False
        if instrument.product_track != self.context.product_track:
            raise DomainError("PAPER_PRODUCT_TRACK_MISMATCH")
        if len(self._instruments) >= 200:
            raise DomainError("PAPER_INSTRUMENT_LIMIT")
        self.instruments = (*self.instruments, instrument)
        self._instruments[instrument.instrument_id] = instrument
        return True

    def dump_state(self) -> dict[str, object]:
        state = _PaperState(
            schema_version=1, context=self.context, instruments=self.instruments, fee_rate=self.fee_rate,
            latency_microseconds=self.latency // timedelta(microseconds=1),
            book_max_age_microseconds=self.book_max_age // timedelta(microseconds=1),
            balances=tuple(sorted(self._balances.items())), orders=self.orders, fills=self.fills,
            initial_balances=tuple(sorted(self._initial_balances.items())),
            books=tuple(self._books.values()), counter=self._counter, trade_counter=self._trade_counter,
            margin=self.margin.dump_state() if self.margin else None, last_time=self._last_time,
            id_namespace=self._id_namespace, flat_rebind_provenance=self._flat_rebind_provenance,
            retired_for_rebind=self._retired_for_rebind,
        )
        return state.model_dump(mode="json")

    @exact_decimal
    def rebind_flat(self, target_context: ContextKey) -> "PaperMatcher":
        """Retain exact settled capital after a verified-flat context retirement.

        Callers must enforce the same execution scope and durable stop proof. The
        matcher independently checks realm/profile identity, monotonic epoch, and
        absence of executable orders or nonquote inventory. Source history stays
        in its originating snapshot/ledger; target IDs use a new context namespace.
        External constructor funding bounds are intentionally unchanged.
        """
        self._validate_rebind_context(self.context, target_context)
        if self._retired_for_rebind:
            raise DomainError("PAPER_CONTEXT_REBOUND")
        quote_currencies = {item.quote_currency for item in self.instruments}
        if len(quote_currencies) != 1:
            raise DomainError("PAPER_REBIND_QUOTE_SCOPE_AMBIGUOUS")
        quote = next(iter(quote_currencies))
        if self.open_orders:
            raise DomainError("PAPER_REBIND_OPEN_ORDERS")
        if any(amount != 0 for currency, amount in self._balances.items() if currency != quote):
            raise DomainError("PAPER_REBIND_NONFLAT_INVENTORY")
        if self.margin is not None and self.margin.positions:
            raise DomainError("PAPER_REBIND_NONFLAT_POSITIONS")
        for amount in self._balances.values():
            _derived(amount)
            if amount < 0:
                raise DomainError("PAPER_REBIND_NEGATIVE_SPOT_BALANCE")
        margin: PaperMarginBook | None = None
        if self.margin is not None:
            _derived(self.margin.collateral)
            margin = PaperMarginBook(
                Decimal(0), initial_margin_fraction=self.margin.initial_margin_fraction,
                maintenance_margin_fraction=self.margin.maintenance_margin_fraction,
            )
            margin.collateral = self.margin.collateral
        candidate = PaperMatcher(
            target_context, self.instruments, initial_balances={}, fee_rate=self.fee_rate,
            latency=self.latency, margin=margin, book_max_age=self.book_max_age,
        )
        candidate._balances = dict(self._balances)
        candidate._initial_balances = dict(self._balances)
        candidate._id_namespace = target_context.identity[:24]
        candidate._flat_rebind_provenance = _FlatRebindProvenance(
            source_context=self.context, target_context=target_context,
            source_snapshot_sha256=sha256(json.dumps(self.dump_state(), sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            settled_balances=tuple(sorted(self._balances.items())),
            settled_margin_collateral=self.margin.collateral if self.margin is not None else None,
        )
        self._retired_for_rebind = True
        return candidate

    @classmethod
    def restore(
        cls, context: ContextKey, instruments: tuple[PaperInstrument, ...], state: dict[str, object]
    ) -> "PaperMatcher":
        try:
            return cls._restore(context, instruments, state)
        except (ValueError, ArithmeticError) as error:
            # Worker recovery handles DomainError by fencing execution while the
            # desktop stays available. Do not expose raw financial input errors.
            raise DomainError("INVALID_PAPER_SNAPSHOT") from error

    @classmethod
    @exact_decimal
    def _restore(
        cls, context: ContextKey, instruments: tuple[PaperInstrument, ...], state: dict[str, object]
    ) -> "PaperMatcher":
        parsed = _PaperState.model_validate_json(json.dumps(state))
        if parsed.schema_version != 1 or parsed.context != context:
            raise DomainError("PAPER_SNAPSHOT_CONTEXT_MISMATCH")
        # The persisted instrument set is authoritative for existing history; a
        # caller may only ADD instruments (a growing universe) or pass an
        # identical definition. A changed existing definition is a mismatch.
        persisted = {item.instrument_id: item for item in parsed.instruments}
        for item in instruments:
            if item.instrument_id in persisted and persisted[item.instrument_id] != item:
                raise DomainError("PAPER_SNAPSHOT_CONTEXT_MISMATCH")
        instruments = parsed.instruments + tuple(item for item in instruments if item.instrument_id not in persisted)
        if parsed.last_time is not None:
            _aware(parsed.last_time)
        if (len(dict(parsed.balances)) != len(parsed.balances)
                or len(dict(parsed.initial_balances)) != len(parsed.initial_balances)):
            raise DomainError("INVALID_PAPER_SNAPSHOT")
        margin = PaperMarginBook.restore(parsed.margin) if parsed.margin is not None else None
        provenance = parsed.flat_rebind_provenance
        initial_balances = dict(parsed.initial_balances)
        if provenance is not None:
            cls._validate_rebind_context(provenance.source_context, context)
            if (provenance.target_context != context or provenance.settled_balances != parsed.initial_balances
                    or parsed.id_namespace != context.identity[:24]):
                raise DomainError("PAPER_REBIND_PROVENANCE_MISMATCH")
            quotes = {item.quote_currency for item in instruments}
            if len(quotes) != 1 or any(amount != 0 for currency, amount in parsed.initial_balances if currency not in quotes):
                raise DomainError("PAPER_REBIND_NONFLAT_INVENTORY")
            for amount in initial_balances.values():
                _derived(amount)
                if amount < 0:
                    raise DomainError("PAPER_REBIND_NEGATIVE_SPOT_BALANCE")
            if (margin is None) != (provenance.settled_margin_collateral is None):
                raise DomainError("PAPER_REBIND_PROVENANCE_MISMATCH")
            if provenance.settled_margin_collateral is not None:
                _derived(provenance.settled_margin_collateral)
            # Only this validated, persisted flat-rebind path restores derived
            # quantities beyond the external funding-input precision limit.
            initial_balances = {}
        elif parsed.id_namespace:
            raise DomainError("PAPER_REBIND_PROVENANCE_MISSING")
        matcher = cls(context, instruments, initial_balances=initial_balances, fee_rate=parsed.fee_rate,
                      latency=timedelta(microseconds=parsed.latency_microseconds), margin=margin,
                      book_max_age=timedelta(microseconds=parsed.book_max_age_microseconds))
        matcher._initial_balances = dict(parsed.initial_balances)
        matcher._id_namespace = parsed.id_namespace
        matcher._flat_rebind_provenance = provenance
        matcher._retired_for_rebind = parsed.retired_for_rebind
        for currency, amount in parsed.balances:
            _derived(amount)
            if not amount.is_finite() or amount < 0:
                raise DomainError("INVALID_PAPER_SNAPSHOT")
            matcher._balances[currency] = amount
        for order in parsed.orders:
            matcher._instrument(order.instrument_id)
            if order.order_id in matcher._orders or order.client_order_id in matcher._clients:
                raise DomainError("INVALID_PAPER_SNAPSHOT")
            matcher._orders[order.order_id] = order
            matcher._clients[order.client_order_id] = order.order_id
        seen_fills: set[str] = set()
        filled: dict[str, Decimal] = {}
        reconstructed = dict(parsed.initial_balances)
        previous_fill_time: datetime | None = None
        for fill in parsed.fills:
            order = matcher.order(fill.order_id)
            instrument = matcher._instrument(fill.instrument_id)
            _aware(fill.as_of)
            if (previous_fill_time is not None and fill.as_of < previous_fill_time
                    or parsed.last_time is None or fill.as_of > parsed.last_time):
                raise DomainError("PAPER_SNAPSHOT_FILL_TIME_MISMATCH")
            previous_fill_time = fill.as_of
            if (fill.trade_id in seen_fills or fill.instrument_id != order.instrument_id
                    or fill.side != order.side or fill.client_order_id != order.client_order_id):
                raise DomainError("INVALID_PAPER_SNAPSHOT")
            if (fill.fee != fill.quantity * fill.price * instrument.contract_multiplier * matcher.fee_rate
                    or fill.fee_currency != instrument.quote_currency or fill.as_of < order.eligible_at
                    or (fill.side == Side.BUY and fill.price > order.request.limit_price)
                    or (fill.side == Side.SELL and fill.price < order.request.limit_price)):
                raise DomainError("INVALID_PAPER_SNAPSHOT")
            if context.product_track == ProductTrack.SPOT:
                sign = Decimal(1) if fill.side == Side.BUY else Decimal(-1)
                reconstructed[instrument.base_currency] = reconstructed.get(instrument.base_currency, Decimal(0)) + sign * fill.quantity
                reconstructed[instrument.quote_currency] = reconstructed.get(instrument.quote_currency, Decimal(0)) - sign * fill.quantity * fill.price - fill.fee
            seen_fills.add(fill.trade_id)
            filled[fill.order_id] = filled.get(fill.order_id, Decimal(0)) + fill.quantity
        if any(order.filled_quantity != filled.get(order.order_id, Decimal(0)) for order in matcher.orders):
            raise DomainError("PAPER_SNAPSHOT_FILL_MISMATCH")
        if context.product_track == ProductTrack.SPOT and reconstructed != matcher._balances:
            raise DomainError("PAPER_SNAPSHOT_BALANCE_MISMATCH")
        if margin is not None and any(
            item.instrument_id not in matcher._instruments
            or item.contract_multiplier != matcher._instruments[item.instrument_id].contract_multiplier
            for item in margin.positions
        ):
            raise DomainError("PAPER_SNAPSHOT_POSITION_MISMATCH")
        if parsed.counter != len(parsed.orders) or parsed.trade_counter != len(parsed.fills):
            raise DomainError("INVALID_PAPER_SNAPSHOT")
        if any(order.order_id != matcher._order_identifier(index) for index, order in enumerate(parsed.orders, 1)):
            raise DomainError("INVALID_PAPER_SNAPSHOT")
        if any(fill.trade_id != matcher._trade_identifier(index) for index, fill in enumerate(parsed.fills, 1)):
            raise DomainError("INVALID_PAPER_SNAPSHOT")
        if len({book.instrument_id for book in parsed.books}) != len(parsed.books):
            raise DomainError("INVALID_PAPER_SNAPSHOT")
        matcher._fills = list(parsed.fills)
        # A restored depth snapshot cannot prove current executable liquidity.
        matcher._books = {}
        matcher._counter = parsed.counter
        matcher._trade_counter = parsed.trade_counter
        matcher._last_time = parsed.last_time
        if any(value < 0 for value in matcher.available_balances.values()):
            raise DomainError("PAPER_SNAPSHOT_RESERVATION_MISMATCH")
        return matcher

    @staticmethod
    def _validate_rebind_context(source: ContextKey, target: ContextKey) -> None:
        if (target.authority_id != source.authority_id or target.environment != source.environment
                or target.environment != Environment.SANDBOX or target.product_track != source.product_track
                or target.broker_profile_id != source.broker_profile_id):
            raise DomainError("PAPER_REBIND_REALM_MISMATCH")
        if target.epoch <= source.epoch:
            raise DomainError("PAPER_REBIND_EPOCH_NOT_INCREASING")

    def _order_identifier(self, counter: int) -> str:
        namespace = self._id_namespace + "-" if self._id_namespace else ""
        return f"paper-{namespace}order-{counter}"

    def _trade_identifier(self, counter: int) -> str:
        namespace = self._id_namespace + "-" if self._id_namespace else ""
        return f"paper-{namespace}trade-{counter}"

    def _instrument(self, instrument_id: str) -> PaperInstrument:
        try:
            return self._instruments[instrument_id]
        except KeyError as exc:
            raise DomainError("PAPER_UNKNOWN_INSTRUMENT") from exc

    def _check_time(self, now: datetime) -> None:
        if self._retired_for_rebind:
            raise DomainError("PAPER_CONTEXT_REBOUND")
        _aware(now)
        if self._last_time is not None and now < self._last_time:
            raise DomainError("PAPER_TIME_REGRESSION")
        self._last_time = now


def _aware(stamp: datetime) -> None:
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise DomainError("AWARE_TIMESTAMP_REQUIRED")
