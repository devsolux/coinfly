"""Exact paper research admission guard; rejection never chooses another trade."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from threading import RLock
from typing import Literal

from pydantic import Field, model_validator

from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.research.fee_presets import preset_fees
from coinfly.research.reward import TakerFees, TimedEvidence

TURNOVER_LIMIT = Decimal("0.05")


def turnover_admissible(capital: Decimal, spent_and_reserved: Decimal, proposed: Decimal) -> bool:
    """Strict per-accounting-interval gross notional < 5% of starting equity.

    The paper adapter must reserve conservative fill notional BEFORE execution,
    include partial fills and open intents, and release reservations only on a
    known outcome. An unknown outcome retains its reservation. Terminal exits
    count too. This guard never shrinks an order or substitutes an action.
    """
    values = (capital, spent_and_reserved, proposed)
    if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values) or capital <= 0:
        raise ValueError("finite exact positive capital/nonnegative turnover required")
    for value in values:
        parts = value.as_tuple()
        if len(parts.digits) > 200 or not isinstance(parts.exponent, int) or not -100 <= parts.exponent <= 100:
            raise ValueError("turnover arithmetic budget exceeded")
    with localcontext(financial_context(512)):
        return spent_and_reserved + proposed < capital * TURNOVER_LIMIT


def _aware(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware reservation clock required")


@dataclass(frozen=True)
class ReservationQuote:
    """A proven execution-price ceiling, not a favorable current mid-price.

    Gross turnover excludes fees. Cash/margin admission must additionally fund
    fee_reserve, using the paper adapter's instrument/side-specific limits.
    """

    quantity: Decimal
    multiplier: Decimal
    price_ceiling: Decimal
    fees: TakerFees
    quoted_at: datetime

    def __post_init__(self) -> None:
        _aware(self.quoted_at)
        for value in (self.quantity, self.multiplier, self.price_ceiling):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("positive exact reservation quantity/price/multiplier required")
            parts = value.as_tuple()
            if len(parts.digits) > 38 or not isinstance(parts.exponent, int) or not -18 <= parts.exponent <= 18:
                raise ValueError("reservation quote exceeds accounting input bounds")
        if not self.fees.observed_at <= self.quoted_at < self.fees.expires_at:
            raise ValueError("fee evidence must be available and valid at reservation")

    @property
    def notional(self) -> Decimal:
        with localcontext(financial_context(512)):
            return self.quantity * self.multiplier * self.price_ceiling

    @property
    def fee_reserve(self) -> Decimal:
        with localcontext(financial_context(512)):
            return self.notional * self.fees.rate


def quote_reservation(*, quantity: Decimal, price_ceiling: Decimal, preset_id: str,
                      trailing_volume_usd: Decimal, observed_at: datetime, expires_at: datetime,
                      now: datetime, multiplier: Decimal = Decimal(1)) -> ReservationQuote:
    """Recalculate a dated fee scenario from volume already known at now."""
    fees = preset_fees(preset_id, trailing_volume_usd=trailing_volume_usd,
                       observed_at=observed_at, expires_at=expires_at)
    return ReservationQuote(quantity, multiplier, price_ceiling, fees, now)


class ReservationReport(TimedEvidence):
    """Authoritative cumulative paper status; terminal evidence includes all fills."""

    order_id: str = Field(min_length=1, max_length=160)
    sequence: int = Field(ge=0, strict=True)
    status: Literal["open", "partial", "filled", "canceled", "rejected", "unknown"]
    event_at: datetime
    available_at: datetime
    filled_quantity: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=200, decimal_places=100)
    filled_notional: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=200, decimal_places=100)
    actual_fees: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=200, decimal_places=100)

    @model_validator(mode="after")
    def causal(self) -> "ReservationReport":
        if (self.event_at > self.available_at or (self.filled_quantity == 0) != (self.filled_notional == 0)
                or (self.filled_quantity == 0 and self.actual_fees != 0)):
            raise ValueError("inconsistent cumulative paper report")
        return self


@dataclass(frozen=True)
class _HeldOrder:
    quote: ReservationQuote
    admitted_at: datetime
    report: ReservationReport | None = None

    @property
    def terminal(self) -> bool:
        return self.report is not None and self.report.status in ("filled", "canceled", "rejected")

    @property
    def remaining_notional(self) -> Decimal:
        if self.terminal:
            return Decimal(0)
        with localcontext(financial_context(512)):
            filled = self.report.filled_quantity if self.report else Decimal(0)
            return (self.quote.quantity - filled) * self.quote.multiplier * self.quote.price_ceiling


class PaperTurnoverReservations:
    """Serialized exact research reservation ledger for ONE accounting interval.

    reserve() must precede paper submission; reconcile() consumes only causal,
    conclusive status. A timeout, request to cancel, missing order, or lower quote
    is never release evidence. Unknown outcomes stop new admission. This in-memory
    helper is not a replacement for the desktop's durable execution journal.
    Reuse across intervals is forbidden: finish/settle every order first.
    """

    def __init__(self, capital: Decimal, start: datetime, end: datetime) -> None:
        _aware(start)
        _aware(end)
        if start >= end or not turnover_admissible(capital, Decimal(0), Decimal(0)):
            raise ValueError("positive capital and chronological interval required")
        self.capital, self.start, self.end = capital, start, end
        self._orders: dict[str, _HeldOrder] = {}
        self._spent = Decimal(0)
        self._reserved_notional = self._reserved_fees = self._actual_fees = Decimal(0)
        self._pending: set[str] = set()
        self._unknown: set[str] = set()
        self._clock = start
        self._lock = RLock()
        self._rejections = {"unknown_outcome": 0, "turnover_budget": 0}

    @property
    def rejection_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._rejections)

    def _time(self, now: datetime) -> None:
        _aware(now)
        if now < self._clock or now < self.start:
            raise ValueError("reservation clock cannot move backwards")

    def totals(self) -> dict[str, Decimal]:
        with self._lock:
            return {"spent_notional": self._spent,
                    "reserved_notional": self._reserved_notional,
                    "reserved_fees": self._reserved_fees, "actual_fees": self._actual_fees}

    def reserve(self, order_id: str, quote: ReservationQuote, *, now: datetime) -> bool:
        with self._lock, localcontext(financial_context(512)):
            self._time(now)
            if (not isinstance(order_id, str) or not 1 <= len(order_id) <= 160
                    or order_id in self._orders or len(self._orders) >= 100000):
                raise ValueError("unique bounded paper intent identity required")
            if not self.start <= quote.quoted_at <= now < min(self.end, quote.fees.expires_at):
                raise ValueError("reservation or fee quote outside the current interval")
            if self._unknown:
                self._rejections["unknown_outcome"] += 1
                self._clock = now
                return False
            allowed = turnover_admissible(self.capital, self._spent + self._reserved_notional, quote.notional)
            self._clock = now
            if allowed:
                self._orders[order_id] = _HeldOrder(quote, now)
                self._pending.add(order_id)
                self._reserved_notional += quote.notional
                self._reserved_fees += quote.fee_reserve
            else:
                self._rejections["turnover_budget"] += 1
            return allowed

    def reconcile(self, report: ReservationReport, *, now: datetime) -> None:
        with self._lock, localcontext(financial_context(512)):
            self._time(now)
            order = self._orders[report.order_id]
            old = order.report
            if old is not None and report.sequence <= old.sequence:
                if report == old:
                    return
                raise ValueError("stale/conflicting cumulative reservation report")
            if (report.event_at < order.admitted_at or report.available_at > now
                    or (old is not None and (report.event_at < old.event_at or report.available_at < old.available_at))):
                raise ValueError("future or out-of-interval reservation evidence")
            prior_qty = old.filled_quantity if old else Decimal(0)
            prior_notional = old.filled_notional if old else Decimal(0)
            prior_fee = old.actual_fees if old else Decimal(0)
            quantity, notional = report.filled_quantity - prior_qty, report.filled_notional - prior_notional
            # Terminal acknowledgement may arrive in a later interval. It can
            # release the unchanged remainder, but new fills belong to their
            # actual execution interval and require host carry-forward support.
            if quantity > 0 and report.event_at > self.end:
                raise ValueError("new fill belongs to a later accounting interval")
            if (quantity < 0 or notional < 0 or report.actual_fees < prior_fee
                    or report.filled_quantity > order.quote.quantity
                    or notional > quantity * order.quote.multiplier * order.quote.price_ceiling
                    or (quantity > 0 and notional <= 0)
                    or (order.terminal and old is not None and (quantity != 0 or report.status != old.status))):
                raise ValueError("invalid cumulative fill or violated execution-price ceiling")
            if ((report.status == "filled" and report.filled_quantity != order.quote.quantity)
                    or (report.status in ("open", "rejected") and report.filled_quantity != 0)
                    or (report.status == "partial" and not 0 < report.filled_quantity < order.quote.quantity)):
                raise ValueError("reservation status/quantity mismatch")
            updated = replace(order, report=report)
            remaining_change = updated.remaining_notional - order.remaining_notional
            self._orders[report.order_id] = updated
            self._spent += notional
            self._reserved_notional += remaining_change
            self._reserved_fees += remaining_change * order.quote.fees.rate
            self._actual_fees += report.actual_fees - prior_fee
            if updated.terminal:
                self._pending.discard(report.order_id)
            if report.status == "unknown":
                self._unknown.add(report.order_id)
            else:
                self._unknown.discard(report.order_id)
            self._clock = now

    def refresh_fees(self, order_id: str, fees: TakerFees, *, now: datetime) -> None:
        """Refresh only fee funding; never shrink an executable price ceiling."""
        with self._lock, localcontext(financial_context(512)):
            self._time(now)
            order = self._orders[order_id]
            if order.terminal or (order.report and order.report.status == "unknown"):
                raise ValueError("fee refresh requires a known open paper order")
            if (not fees.observed_at <= now < min(self.end, fees.expires_at) or fees.venue != order.quote.fees.venue
                    or fees.observed_at < order.quote.fees.observed_at):
                raise ValueError("fresh causal fee evidence for the same venue required")
            # Keep submission timestamp and original executable ceiling. Actual
            # fees already reported never change when later tiers improve.
            self._orders[order_id] = replace(order, quote=ReservationQuote(
                order.quote.quantity, order.quote.multiplier, order.quote.price_ceiling, fees, now))
            self._reserved_fees += order.remaining_notional * (fees.rate - order.quote.fees.rate)
            self._clock = now

    def close(self, *, now: datetime) -> dict[str, Decimal]:
        """Require complete terminal reports before a host opens the next interval."""
        with self._lock:
            self._time(now)
            if now < self.end or self._pending:
                raise ValueError("interval cannot close with unresolved paper reservations")
            self._clock = now
            return self.totals()
