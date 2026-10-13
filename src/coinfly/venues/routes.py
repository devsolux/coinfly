"""Broker routes: which documented gateway would carry an order for a subtype.

Route identity is separate from endpoint profiles: a route names the protocol
family and its documentation status; an endpoint profile pins concrete hosts.
Access dates and verified facts are recorded in
``docs/venues/coinbase-route-verification.md``.
"""

from __future__ import annotations

from enum import StrEnum

from coinfly.domain.products import ProductSubtype


class BrokerRoute(StrEnum):
    PAPER_LOCAL = "PAPER_LOCAL"
    # REST https://api.coinbase.com/api/v3/brokerage — ES256 request JWT,
    # decimal strings, client_order_id idempotency. Spot products.
    COINBASE_ADVANCED_SPOT = "COINBASE_ADVANCED_SPOT"
    # Same REST surface with product_type=FUTURE: CFM (US) venue is documented
    # (/cfm/* endpoints); the EEA CBFSE venue has no documented API route.
    COINBASE_ADVANCED_FUTURES = "COINBASE_ADVANCED_FUTURES"
    # Deribit-powered gateway https://drb.coinbase.com/api/v2 — JSON-RPC 2.0,
    # public/auth token exchange with a CDP JWT, JSON numbers, labels are not
    # idempotency keys. Replaces the legacy INTX perpetual endpoints at a hard
    # cutover ("coming soon" as of 21 September 2026).
    COINBASE_GLOBAL_DERIVATIVES = "COINBASE_GLOBAL_DERIVATIVES"
    # Legacy INTX perpetual endpoints under /api/v3/brokerage/intx — deprecated,
    # retiring at cutover; never selected for new integrations.
    COINBASE_INTX_LEGACY = "COINBASE_INTX_LEGACY"


ROUTE_DOCUMENTATION_STATUS: dict[BrokerRoute, str] = {
    BrokerRoute.PAPER_LOCAL: "local simulator; no network",
    BrokerRoute.COINBASE_ADVANCED_SPOT: "documented; reviewed 2026-09-21",
    BrokerRoute.COINBASE_ADVANCED_FUTURES: "documented for CFM (US); EEA/CBFSE API route undocumented; reviewed 2026-09-21",
    BrokerRoute.COINBASE_GLOBAL_DERIVATIVES: "documented; hard cutover date unpublished; reviewed 2026-09-21",
    BrokerRoute.COINBASE_INTX_LEGACY: "deprecated; retires at cutover; reviewed 2026-09-21",
}


def route_for(subtype: ProductSubtype, service_entity: str | None) -> BrokerRoute:
    """The route Coinbase documents for a contract model (independent of eligibility)."""
    if subtype is ProductSubtype.SPOT:
        return BrokerRoute.COINBASE_ADVANCED_SPOT
    if subtype is ProductSubtype.PERPETUAL_NO_EXPIRY:
        return BrokerRoute.COINBASE_GLOBAL_DERIVATIVES
    return BrokerRoute.COINBASE_ADVANCED_FUTURES
