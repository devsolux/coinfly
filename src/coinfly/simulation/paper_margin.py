"""Explicitly synthetic linear perpetual margin; not a certified venue model."""

import json
from collections import deque
from decimal import Decimal, localcontext

from pydantic import Field

from coinfly.domain.context import DomainError, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal, financial_context


class PaperPosition(FrozenModel):
    instrument_id: str
    quantity: ExactDecimal = Field(allow_inf_nan=False, max_digits=38, decimal_places=18)
    entry_price: ExactDecimal = Field(gt=0, allow_inf_nan=False)
    contract_multiplier: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


class PaperMarginSnapshot(FrozenModel):
    collateral: ExactDecimal
    unrealized_pnl: ExactDecimal | None
    equity: ExactDecimal | None
    initial_margin: ExactDecimal | None
    maintenance_margin: ExactDecimal | None
    available_margin: ExactDecimal | None
    maintenance_breach: bool | None
    positions: tuple[PaperPosition, ...]


class PaperMarginBook:
    def __init__(
        self, collateral: Decimal, *, initial_margin_fraction: Decimal = Decimal("0.1"),
        maintenance_margin_fraction: Decimal = Decimal("0.05"),
    ) -> None:
        for value in (collateral, initial_margin_fraction, maintenance_margin_fraction):
            _bounded(value)
        if collateral < 0 or not 0 < maintenance_margin_fraction <= initial_margin_fraction <= 1:
            raise ValueError("invalid synthetic margin configuration")
        self.collateral = collateral
        self.initial_margin_fraction = initial_margin_fraction
        self.maintenance_margin_fraction = maintenance_margin_fraction
        self._positions: dict[str, PaperPosition] = {}
        self._lots: dict[str, tuple[PaperPosition, ...]] = {}
        self._marks: dict[str, Decimal] = {}
        self._funding: dict[str, tuple[str, Decimal]] = {}

    @property
    def positions(self) -> tuple[PaperPosition, ...]:
        return tuple(self._positions[key] for key in sorted(self._positions))

    def quantity(self, instrument_id: str) -> Decimal:
        item = self._positions.get(instrument_id)
        return item.quantity if item else Decimal(0)

    def set_mark(self, instrument_id: str, price: Decimal) -> None:
        _derived(price)
        if price <= 0:
            raise DomainError("INVALID_PAPER_MARK")
        self._marks[instrument_id] = price

    @exact_decimal
    def snapshot(self) -> PaperMarginSnapshot:
        with localcontext(financial_context()) as context:
            context.prec = 512
            if any(item.instrument_id not in self._marks for item in self.positions):
                return PaperMarginSnapshot(
                    collateral=self.collateral, unrealized_pnl=None, equity=None,
                    initial_margin=None, maintenance_margin=None, available_margin=None,
                    maintenance_breach=None, positions=self.positions,
                )
            upl = sum(((self._marks[item.instrument_id] - item.entry_price) * item.quantity * item.contract_multiplier
                       for lots in self._lots.values() for item in lots), Decimal(0))
            notional = sum((abs(item.quantity) * item.contract_multiplier * self._marks[item.instrument_id]
                            for item in self.positions), Decimal(0))
            equity = self.collateral + upl
            initial = notional * self.initial_margin_fraction
            maintenance = notional * self.maintenance_margin_fraction
            return PaperMarginSnapshot(
                collateral=self.collateral, unrealized_pnl=upl, equity=equity,
                initial_margin=initial, maintenance_margin=maintenance,
                available_margin=equity - initial, maintenance_breach=bool(self.positions) and equity <= maintenance,
                positions=self.positions,
            )

    @exact_decimal
    def apply_fill(
        self, instrument_id: str, signed_quantity: Decimal, price: Decimal,
        *, multiplier: Decimal = Decimal(1), fee: Decimal = Decimal(0), reduce_only: bool = False,
    ) -> Decimal:
        for value in (signed_quantity, price, multiplier):
            _bounded(value)
        _derived(fee)
        if signed_quantity == 0 or price <= 0 or multiplier <= 0 or fee < 0:
            raise DomainError("INVALID_PAPER_FILL")
        with localcontext(financial_context()) as context:
            context.prec = 512
            old = self._positions.get(instrument_id)
            old_quantity = old.quantity if old else Decimal(0)
            if old is not None and old.contract_multiplier != multiplier:
                raise DomainError("PAPER_CONTRACT_MULTIPLIER_MISMATCH")
            reducing = old_quantity * signed_quantity < 0 and abs(signed_quantity) <= abs(old_quantity)
            if reduce_only and not reducing:
                raise DomainError("PAPER_REDUCE_ONLY_VIOLATION")
            # Settlement uses original finite lot prices. A display average
            # such as 302 / 3 cannot be an exact monetary cost basis.
            old_lots = self._lots.get(instrument_id, ())
            lots, realized = _settle_lots(old_lots, instrument_id, signed_quantity, price, multiplier)
            candidate = _summarize_lots(lots) if lots else None
            old_collateral = self.collateral
            old_mark = self._marks.get(instrument_id)
            self.collateral += realized - fee
            if candidate is None:
                self._positions.pop(instrument_id, None)
                self._lots.pop(instrument_id, None)
            else:
                self._positions[instrument_id] = candidate
                self._lots[instrument_id] = tuple(lots)
            self._marks.setdefault(instrument_id, price)
            try:
                snapshot = self.snapshot()
                if not reducing and (snapshot.available_margin is None or snapshot.available_margin < 0):
                    raise DomainError("PAPER_INSUFFICIENT_MARGIN")
            except Exception:
                self.collateral = old_collateral
                if old is None:
                    self._positions.pop(instrument_id, None)
                    self._lots.pop(instrument_id, None)
                else:
                    self._positions[instrument_id] = old
                    self._lots[instrument_id] = old_lots
                if old_mark is None:
                    self._marks.pop(instrument_id, None)
                else:
                    self._marks[instrument_id] = old_mark
                raise
            return realized

    @exact_decimal
    def apply_funding(self, event_id: str, instrument_id: str, signed_payment: Decimal) -> bool:
        _bounded(signed_payment)
        if not event_id:
            raise ValueError("stable funding event ID required")
        prior = self._funding.get(event_id)
        if prior is not None:
            if prior != (instrument_id, signed_payment):
                raise DomainError("PAPER_FUNDING_ID_CONFLICT")
            return False
        if instrument_id not in self._positions:
            raise DomainError("PAPER_FUNDING_WITHOUT_POSITION")
        with localcontext(financial_context()) as context:
            context.prec = 512
            self.collateral += signed_payment
        self._funding[event_id] = (instrument_id, signed_payment)
        return True

    def dump_state(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "collateral": str(self.collateral), "initial_margin_fraction": str(self.initial_margin_fraction),
            "maintenance_margin_fraction": str(self.maintenance_margin_fraction),
            "positions": [item.model_dump(mode="json") for item in self.positions],
            "lots": [lot.model_dump(mode="json") for key in sorted(self._lots) for lot in self._lots[key]],
            "marks": {key: str(value) for key, value in self._marks.items()},
            "funding": {key: [instrument, str(payment)] for key, (instrument, payment) in self._funding.items()},
        }

    @classmethod
    @exact_decimal
    def restore(cls, state: dict[str, object]) -> "PaperMarginBook":
        expected = {"schema_version", "lots", "collateral", "initial_margin_fraction",
                    "maintenance_margin_fraction", "positions", "marks", "funding"}
        if (set(state) != expected or type(state["schema_version"]) is not int
                or state["schema_version"] != 2):
            raise DomainError("INVALID_PAPER_MARGIN_STATE")
        # Losses may leave negative collateral. Restore through a zero seed, then validate the exact snapshot.
        collateral = _state_decimal(state["collateral"])
        _derived(collateral)
        book = cls(Decimal(0), initial_margin_fraction=_state_decimal(state["initial_margin_fraction"]),
                   maintenance_margin_fraction=_state_decimal(state["maintenance_margin_fraction"]))
        book.collateral = collateral
        positions = state["positions"]
        marks = state["marks"]
        funding = state["funding"]
        if not isinstance(positions, list) or not isinstance(marks, dict) or not isinstance(funding, dict):
            raise DomainError("INVALID_PAPER_MARGIN_STATE")
        for value in positions:
            position = PaperPosition.model_validate_json(json.dumps(value))
            _derived(position.entry_price)
            if position.quantity == 0 or position.instrument_id in book._positions:
                raise DomainError("INVALID_PAPER_MARGIN_STATE")
            book._positions[position.instrument_id] = position
        stored_lots = state["lots"]
        if not isinstance(stored_lots, list):
            raise DomainError("INVALID_PAPER_MARGIN_STATE")
        grouped: dict[str, list[PaperPosition]] = {}
        for value in stored_lots:
            lot = PaperPosition.model_validate_json(json.dumps(value))
            _bounded(lot.entry_price)
            aggregate = book._positions.get(lot.instrument_id)
            if (aggregate is None or aggregate.quantity * lot.quantity <= 0
                    or aggregate.contract_multiplier != lot.contract_multiplier):
                raise DomainError("PAPER_MARGIN_LOT_MISMATCH")
            grouped.setdefault(lot.instrument_id, []).append(lot)
        book._lots = {key: tuple(lots) for key, lots in grouped.items()}
        if ({key: _summarize_lots(lots) for key, lots in book._lots.items()} != book._positions):
            raise DomainError("PAPER_MARGIN_LOT_MISMATCH")
        for instrument, price in marks.items():
            book.set_mark(str(instrument), _state_decimal(price))
        for key, pair in funding.items():
            if not isinstance(pair, list) or len(pair) != 2:
                raise DomainError("INVALID_PAPER_MARGIN_STATE")
            payment = _state_decimal(pair[1])
            _bounded(payment)
            book._funding[str(key)] = (str(pair[0]), payment)
        return book


def _settle_lots(
    old: tuple[PaperPosition, ...], instrument: str, quantity: Decimal, price: Decimal, multiplier: Decimal,
) -> tuple[tuple[PaperPosition, ...], Decimal]:
    """Exact FIFO inventory transition."""
    lots = deque(old)
    realized = _consume_fifo(lots, instrument, quantity, price, multiplier)
    return tuple(lots), realized


def _consume_fifo(
    lots: deque[PaperPosition], instrument: str, quantity: Decimal, price: Decimal, multiplier: Decimal,
) -> Decimal:
    """Consume each closed lot once using its original cost basis."""
    realized = Decimal(0)
    remaining = quantity
    while lots and remaining and lots[0].quantity * remaining < 0:
        lot = lots.popleft()
        direction = Decimal(1) if lot.quantity > 0 else Decimal(-1)
        closed = min(abs(lot.quantity), abs(remaining))
        realized += (price - lot.entry_price) * closed * multiplier * direction
        residual = lot.quantity - closed * direction
        remaining += closed * direction
        if residual:
            lots.appendleft(lot.model_copy(update={"quantity": residual}))
    if remaining:
        lots.append(PaperPosition(instrument_id=instrument, quantity=remaining,
                                  entry_price=price, contract_multiplier=multiplier))
    return realized


def _summarize_lots(lots: tuple[PaperPosition, ...]) -> PaperPosition:
    with localcontext(financial_context(512)):
        quantity = sum((lot.quantity for lot in lots), Decimal(0))
        cost = sum((lot.quantity * lot.entry_price for lot in lots), Decimal(0))
        with localcontext(financial_context(100)):
            average = cost / quantity
    return PaperPosition(instrument_id=lots[0].instrument_id, quantity=quantity, entry_price=average,
                         contract_multiplier=lots[0].contract_multiplier)


def _state_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise DomainError("INVALID_PAPER_MARGIN_STATE")
    try:
        return Decimal(value)
    except ArithmeticError:
        raise DomainError("INVALID_PAPER_MARGIN_STATE") from None


def _bounded(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainError("INVALID_DECIMAL")
    parts = value.as_tuple()
    if len(parts.digits) > 38 or not isinstance(parts.exponent, int) or parts.exponent < -18 or parts.exponent > 18:
        raise DomainError("DECIMAL_PRECISION_LIMIT")


def _derived(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainError("INVALID_DECIMAL")
    parts = value.as_tuple()
    if len(parts.digits) > 100 or not isinstance(parts.exponent, int) or not -100 <= parts.exponent <= 100:
        raise DomainError("DECIMAL_PRECISION_LIMIT")
