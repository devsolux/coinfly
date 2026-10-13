"""Versioned product subtypes: one label per genuinely different contract model.

``ProductTrack`` (SPOT/PERPETUAL) is the coarse execution track used by context
identity, ledgers and journals. A subtype refines a track without relabeling one
contract as another:

* ``SPOT`` — base/quote exchange of the asset itself.
* ``PERPETUAL_NO_EXPIRY`` — international no-expiry perpetual swaps (the
  Coinbase Global Derivatives / legacy INTX product family, e.g.
  ``BTC_USDC-PERPETUAL``), funding-settled, no expiry date.
* ``PERPETUAL_STYLE_FUTURE`` — regulated long-dated futures with a real expiry
  (five-year contracts, hourly funding, daily settlement) offered through
  Coinbase Financial Markets (US) or Coinbase Financial Services Europe (EEA,
  including Germany). Not the same instrument as a no-expiry perpetual.
* ``DATED_FUTURE`` — monthly/quarterly expiring futures with cash settlement.

The subtype schema is versioned so persisted configurations and paper books can
be checked for compatibility instead of being silently reinterpreted.
"""

from __future__ import annotations

from enum import StrEnum

from coinfly.domain.context import ProductTrack, RegionPolicy

PRODUCT_SUBTYPE_SCHEMA_VERSION = 1


class ProductSubtype(StrEnum):
    SPOT = "SPOT"
    PERPETUAL_NO_EXPIRY = "PERPETUAL_NO_EXPIRY"
    PERPETUAL_STYLE_FUTURE = "PERPETUAL_STYLE_FUTURE"
    DATED_FUTURE = "DATED_FUTURE"

    @property
    def track(self) -> ProductTrack:
        return ProductTrack.SPOT if self is ProductSubtype.SPOT else ProductTrack.PERPETUAL

    @property
    def has_expiry(self) -> bool:
        return self in (ProductSubtype.PERPETUAL_STYLE_FUTURE, ProductSubtype.DATED_FUTURE)

    @property
    def funding_settled(self) -> bool:
        return self in (ProductSubtype.PERPETUAL_NO_EXPIRY, ProductSubtype.PERPETUAL_STYLE_FUTURE)

    @property
    def is_derivative(self) -> bool:
        return self is not ProductSubtype.SPOT


DERIVATIVE_SUBTYPES: tuple[ProductSubtype, ...] = (
    ProductSubtype.PERPETUAL_NO_EXPIRY, ProductSubtype.PERPETUAL_STYLE_FUTURE, ProductSubtype.DATED_FUTURE,
)

# Contract families that an EEA (MiFID) retail account can legally be offered by
# Coinbase Financial Services Europe per the 9 March 2026 announcement. The
# international no-expiry perpetual family is not part of that offering.
EEA_ELIGIBLE_SUBTYPES: frozenset[ProductSubtype] = frozenset({
    ProductSubtype.SPOT, ProductSubtype.PERPETUAL_STYLE_FUTURE, ProductSubtype.DATED_FUTURE,
})


def default_subtype(track: ProductTrack, policy: RegionPolicy) -> ProductSubtype:
    """The subtype a bare track selection means under each application policy."""
    if track is ProductTrack.SPOT:
        return ProductSubtype.SPOT
    if policy is RegionPolicy.GERMANY:
        return ProductSubtype.PERPETUAL_STYLE_FUTURE
    return ProductSubtype.PERPETUAL_NO_EXPIRY


def paper_scope_for(subtype: ProductSubtype) -> str:
    """Separate synthetic books per contract model; a book never changes model."""
    return {
        ProductSubtype.SPOT: "paper-book",
        ProductSubtype.PERPETUAL_NO_EXPIRY: "paper-perpetual-book",
        ProductSubtype.PERPETUAL_STYLE_FUTURE: "paper-perpetual-style-book",
        ProductSubtype.DATED_FUTURE: "paper-dated-futures-book",
    }[subtype]
