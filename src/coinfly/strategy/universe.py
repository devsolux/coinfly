"""Deterministic, auditable instrument selection separate from neural decisions.

Selection uses only contemporaneously available catalog and observation data
plus the strategy's transparent filters. It is reproducible from the recorded
inputs and never consults P&L, model output or any profit prediction: it is not
a second policy. Every candidate gets a reason, including every rejection with
the unmet threshold, so an empty universe is explained rather than hidden.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import Field

from coinfly.domain.context import FrozenModel, Identifier
from coinfly.market.observations import InstrumentCatalogEntry, MarketObservation
from coinfly.strategy.definition import SelectionOrder, StrategyDefinition


class SelectionReason(FrozenModel):
    instrument: Identifier
    eligible: bool
    rank: int | None = Field(default=None, ge=0)
    codes: tuple[str, ...] = ()
    thresholds: tuple[tuple[str, str], ...] = ()
    observed: tuple[tuple[str, str], ...] = ()

    @property
    def detail(self) -> str:
        parts = [f"{key}={value}" for key, value in self.observed]
        limits = [f"{key}<{value}" if key.startswith("max") else f"{key}>={value}" for key, value in self.thresholds]
        return ", ".join(self.codes) + (" (" + "; ".join(parts + limits) + ")" if parts or limits else "")


class UniverseSelection(FrozenModel):
    strategy_identity: str
    selected: tuple[Identifier, ...]
    reasons: tuple[SelectionReason, ...]
    selected_at: datetime
    inputs_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def rejected(self) -> tuple[SelectionReason, ...]:
        return tuple(reason for reason in self.reasons if not reason.eligible)

    def as_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy_identity, "selected": list(self.selected),
                "selected_at": self.selected_at.isoformat(), "inputs_digest": self.inputs_digest,
                "reasons": [{"instrument": r.instrument, "eligible": r.eligible, "rank": r.rank,
                             "codes": list(r.codes), "detail": r.detail} for r in self.reasons]}


def _inputs_digest(entries: tuple[InstrumentCatalogEntry, ...], observations: dict[str, MarketObservation | None],
                   definition: StrategyDefinition, now: datetime) -> str:
    digest = sha256()
    digest.update(definition.digest.encode())
    digest.update(now.isoformat().encode())
    for entry in sorted(entries, key=lambda item: item.instrument_id):
        digest.update(entry.model_dump_json().encode())
        observation = observations.get(entry.instrument_id)
        digest.update(observation.model_dump_json().encode() if observation is not None else b"none")
    return digest.hexdigest()


def slot_cap_notional(definition: StrategyDefinition, capital: Decimal | None = None) -> Decimal:
    """Largest notional one slot can ever hold: sizing capital x per-slot fraction.

    Unlevered, like the admission check (``strategy.capital.require_intent_tx``):
    leverage never expands the approved cash authority of a slot. ``capital``
    is the sizing capital (approved capital within the budget and, on paper,
    the book's funding); absent, the strategy budget.
    """
    base = definition.capital_budget if capital is None else min(capital, definition.capital_budget)
    return base * definition.per_slot_max_exposure_fraction


def minimum_order_notional(entry: InstrumentCatalogEntry, price: Decimal | None) -> Decimal | None:
    """Notional of the smallest order the venue accepts (one lot, or the quote minimum), at ``price``."""
    if price is None or price <= 0:
        return None
    lot = max(entry.base_min_size, entry.base_increment)
    return max(lot * price * entry.contract_multiplier, entry.quote_min_size)


def _static_codes(definition: StrategyDefinition, entry: InstrumentCatalogEntry, price: Decimal | None,
                  thresholds: list[tuple[str, str]], observed: list[tuple[str, str]],
                  max_order_notional: Decimal | None = None, capital: Decimal | None = None) -> list[str]:
    """Eligibility that needs no order book: identity, permissions, tradability, size feasibility."""
    filters = definition.filters
    codes: list[str] = []
    permitted = set(definition.permitted_instruments)
    if entry.instrument_id in definition.excluded_instruments:
        codes.append("EXCLUDED_BY_OPERATOR")
    if permitted and entry.instrument_id not in permitted:
        codes.append("NOT_PERMITTED")
    if entry.subtype is not definition.product_subtype:
        codes.append("SUBTYPE_MISMATCH")
        observed.append(("subtype", entry.subtype.value))
    if entry.quote_currency not in definition.quote_currencies:
        codes.append("QUOTE_NOT_PERMITTED")
        observed.append(("quote", entry.quote_currency))
    if filters.require_tradable and not entry.tradable:
        codes.append("NOT_TRADABLE")
        observed.append(("status", entry.status if not entry.trading_disabled else "trading_disabled"))
    if filters.min_volume_24h is not None:
        thresholds.append(("min_volume_24h", str(filters.min_volume_24h)))
        if entry.volume_24h is None or entry.volume_24h < filters.min_volume_24h:
            codes.append("VOLUME_BELOW_MINIMUM")
            observed.append(("volume_24h", str(entry.volume_24h) if entry.volume_24h is not None else "unknown"))
    # Feasibility, not preference: an instrument whose smallest accepted order
    # is larger than everything one slot may ever hold can never receive an
    # order from the step mapping, so owning a slot for it would only look like
    # a strategy that "does nothing". Shown with both numbers.
    minimum = minimum_order_notional(entry, price)
    cap = slot_cap_notional(definition, capital)
    if minimum is not None and minimum > cap:
        codes.append("MIN_ORDER_ABOVE_SLOT_CAP")
        observed.append(("min_order_notional", f"{minimum:.2f}"))
        thresholds.append(("max_slot_notional", f"{cap:.2f}"))
    sizing = definition.capital_budget if capital is None else min(capital, definition.capital_budget)
    turnover = sizing * definition.execution.max_turnover_fraction
    if minimum is not None and minimum >= turnover:
        # The exact turnover guard admits strictly less than this per
        # accounting window; a smaller quantity than one lot does not exist.
        codes.append("MIN_ORDER_ABOVE_TURNOVER_LIMIT")
        if ("min_order_notional", f"{minimum:.2f}") not in observed:
            observed.append(("min_order_notional", f"{minimum:.2f}"))
        thresholds.append(("max_window_turnover", f"{turnover:.2f}"))
    if minimum is not None and max_order_notional is not None and minimum > max_order_notional:
        # The context's risk limit (Settings) admits no order this large.
        codes.append("MIN_ORDER_ABOVE_ORDER_LIMIT")
        if ("min_order_notional", f"{minimum:.2f}") not in observed:
            observed.append(("min_order_notional", f"{minimum:.2f}"))
        thresholds.append(("max_order_notional", f"{max_order_notional:.2f}"))
    return codes


def _order_key(definition: StrategyDefinition, entry: InstrumentCatalogEntry) -> tuple[Any, ...]:
    filters = definition.filters
    if filters.order is SelectionOrder.PRIORITY_LIST:
        position = filters.priority.index(entry.instrument_id) if entry.instrument_id in filters.priority else len(filters.priority)
        return (position, entry.instrument_id)
    if filters.order is SelectionOrder.INSTRUMENT_ID:
        return (entry.instrument_id,)
    # Quote-currency volume: base volumes (coins, contracts) are not comparable
    # across instruments. Deterministic tie-break by id.
    return (-entry.ranking_volume, entry.instrument_id)


def plan_subscription(definition: StrategyDefinition, catalog: tuple[InstrumentCatalogEntry, ...], *, capacity: int,
                      keep: tuple[str, ...] = (), max_order_notional: Decimal | None = None,
                      capital: Decimal | None = None) -> tuple[str, ...]:
    """Which instruments the market feed should carry: kept instruments first, then ranked candidates.

    ``keep`` holds instruments the context must keep marking (owned slots, open
    positions). Candidates pass every book-independent eligibility check and
    are ranked like the universe; the list is capped at the feed's capacity.
    Selection itself still happens on observed books (``select_universe``).
    """
    known = {entry.instrument_id: entry for entry in catalog}
    chosen: list[str] = [name for name in dict.fromkeys(keep) if name in known][:max(0, capacity)]
    ranked = []
    for entry in catalog:
        if entry.instrument_id in chosen:
            continue
        if _static_codes(definition, entry, entry.reference_price, [], [], max_order_notional, capital):
            continue
        ranked.append((_order_key(definition, entry), entry.instrument_id))
    ranked.sort()
    for _, name in ranked:
        if len(chosen) >= capacity:
            break
        chosen.append(name)
    return tuple(chosen)


def select_universe(definition: StrategyDefinition, catalog: tuple[InstrumentCatalogEntry, ...],
                    observations: dict[str, MarketObservation | None], *, now: datetime,
                    max_order_notional: Decimal | None = None, capital: Decimal | None = None) -> UniverseSelection:
    """Rank eligible instruments and cap at ``max_active_slots``; explain the rest.

    ``max_order_notional`` is the effective context's per-order risk limit and
    ``capital`` the sizing capital (see ``slot_cap_notional``); an instrument
    whose smallest accepted order cannot fit them is reported, not owned.
    """
    filters = definition.filters
    max_age = timedelta(seconds=definition.market_data.max_observation_age_seconds)
    reasons: list[SelectionReason] = []
    eligible: list[tuple[tuple[Any, ...], str]] = []
    for entry in sorted(catalog, key=lambda item: item.instrument_id):
        thresholds: list[tuple[str, str]] = []
        observed: list[tuple[str, str]] = []
        observation = observations.get(entry.instrument_id)
        mid = observation.mid if observation is not None else None
        codes = _static_codes(definition, entry, mid if mid is not None else entry.reference_price, thresholds, observed,
                              max_order_notional, capital)
        if observation is None or not observation.two_sided:
            codes.append("NO_TWO_SIDED_OBSERVATION")
        else:
            age = observation.age(now)
            if age < timedelta(0) or age > max_age:
                codes.append("OBSERVATION_STALE")
                observed.append(("observation_age_seconds", f"{age.total_seconds():.1f}"))
                thresholds.append(("max_observation_age_seconds", str(definition.market_data.max_observation_age_seconds)))
            if len(observation.bids) < definition.market_data.min_depth_levels or len(observation.asks) < definition.market_data.min_depth_levels:
                codes.append("DEPTH_BELOW_MINIMUM")
                thresholds.append(("min_depth_levels", str(definition.market_data.min_depth_levels)))
            spread = observation.spread_bps
            if spread is None or spread > filters.max_spread_bps:
                codes.append("SPREAD_ABOVE_MAXIMUM")
                observed.append(("spread_bps", f"{spread:.2f}" if spread is not None else "unknown"))
                thresholds.append(("max_spread_bps", str(filters.max_spread_bps)))
            # Contracts: displayed size x price x contract size is the notional.
            notional = min(observation.top_notional("BID"), observation.top_notional("ASK")) * entry.contract_multiplier
            if notional < filters.min_top_of_book_notional:
                codes.append("TOP_OF_BOOK_BELOW_MINIMUM")
                observed.append(("top_of_book_notional", f"{notional:.2f}"))
                thresholds.append(("min_top_of_book_notional", str(filters.min_top_of_book_notional)))
        if codes:
            reasons.append(SelectionReason(instrument=entry.instrument_id, eligible=False, codes=tuple(codes),
                                           thresholds=tuple(thresholds), observed=tuple(observed)))
            continue
        eligible.append((_order_key(definition, entry), entry.instrument_id))
    eligible.sort()
    selected = tuple(name for _, name in eligible[: definition.max_active_slots])
    for rank, (_, name) in enumerate(eligible):
        if rank < definition.max_active_slots:
            reasons.append(SelectionReason(instrument=name, eligible=True, rank=rank, codes=("ELIGIBLE",)))
        else:
            reasons.append(SelectionReason(instrument=name, eligible=False, rank=rank, codes=("BEYOND_MAX_ACTIVE_SLOTS",),
                                           thresholds=(("max_active_slots", str(definition.max_active_slots)),)))
    reasons.sort(key=lambda reason: reason.instrument)
    return UniverseSelection(strategy_identity=definition.identity, selected=selected, reasons=tuple(reasons),
                             selected_at=now, inputs_digest=_inputs_digest(catalog, observations, definition, now))
