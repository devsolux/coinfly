"""Immutable documented endpoints. No profile allows an arbitrary base URL."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from coinfly.domain.context import DomainError, Environment, ProductTrack
from coinfly.security.credential_realms import CredentialPurpose


@dataclass(frozen=True, slots=True)
class EndpointProfile:
    profile_id: str
    environment: Environment
    product: ProductTrack | None
    origin: str | None
    prefix: str
    authenticated: bool
    routes: frozenset[tuple[str, str]]

    def validate(self, method: str, path: str, *, purpose: CredentialPurpose | None) -> str:
        if not isinstance(method, str) or not isinstance(path, str):
            raise DomainError("ENDPOINT_ROUTE_FORBIDDEN")
        # Match an exact relative route; encoded traversal/query/authority never
        # reaches URL concatenation. Query parameters use a separate allowlist.
        if (method, path) not in self.routes or any(char in path for char in ("?", "#", "%", "\\")):
            raise DomainError("ENDPOINT_ROUTE_FORBIDDEN")
        if self.origin is None:
            raise DomainError("PAPER_HAS_NO_NETWORK_ENDPOINT")
        parsed = urlsplit(self.origin)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.path:
            raise DomainError("ENDPOINT_PROFILE_INVALID")
        if self.authenticated and purpose not in (CredentialPurpose.EXECUTION, CredentialPurpose.OBSERVATION):
            raise DomainError("ENDPOINT_PURPOSE_MISMATCH")
        if not self.authenticated and purpose is not None:
            raise DomainError("UNAUTHENTICATED_ENDPOINT_FORBIDS_CREDENTIALS")
        if purpose == CredentialPurpose.OBSERVATION and method != "GET":
            raise DomainError("OBSERVER_MUTATION_FORBIDDEN")
        return self.origin + self.prefix + path


_READ_ROUTES = frozenset(
    {
        ("GET", "/accounts"),
        ("GET", "/key_permissions"),
        ("GET", "/orders/historical/batch"),
        ("GET", "/orders/historical/fills"),
    }
)

ENDPOINT_PROFILES: Mapping[str, EndpointProfile] = MappingProxyType(
    {
        "paper-local": EndpointProfile(
            "paper-local", Environment.SANDBOX, None, None, "", False, frozenset()
        ),
        "coinbase-static-sandbox": EndpointProfile(
            "coinbase-static-sandbox",
            Environment.SANDBOX,
            ProductTrack.SPOT,
            "https://api-sandbox.coinbase.com",
            "/api/v3/brokerage",
            False,
            frozenset({("GET", "/accounts"), ("GET", "/orders/historical/batch")}),
        ),
        "coinbase-advanced-spot": EndpointProfile(
            "coinbase-advanced-spot",
            Environment.LIVE,
            ProductTrack.SPOT,
            "https://api.coinbase.com",
            "/api/v3/brokerage",
            True,
            _READ_ROUTES,
        ),
        # Execution profile for the qualified live spot adapter
        # (coinfly.venues.coinbase.advanced_trade_orders). Documented 2026-09-21:
        # POST /orders (client_order_id idempotency), POST /orders/batch_cancel,
        # GET /orders/historical/batch (cursor/has_next), GET /orders/historical/fills.
        # A LIVE configuration must name this profile explicitly; the read-only
        # "coinbase-advanced-spot" profile can never place an order.
        "coinbase-advanced-spot-execution": EndpointProfile(
            "coinbase-advanced-spot-execution",
            Environment.LIVE,
            ProductTrack.SPOT,
            "https://api.coinbase.com",
            "/api/v3/brokerage",
            True,
            _READ_ROUTES | frozenset({("POST", "/orders"), ("POST", "/orders/batch_cancel")}),
        ),
        # Public, unauthenticated market data (products and order book) used by
        # the paper worker's COINBASE_PUBLIC_* feeds. GET-only, never a credential.
        # Documented 2026-09-21: docs.cdp.coinbase.com Advanced Trade REST
        # "Public endpoints": /market/products, /market/product_book.
        "coinbase-public-market": EndpointProfile(
            "coinbase-public-market",
            Environment.SANDBOX,
            None,
            "https://api.coinbase.com",
            "/api/v3/brokerage/market",
            False,
            frozenset({("GET", "/products"), ("GET", "/product_book")}),
        ),
    }
)


def endpoint_profile(profile_id: str) -> EndpointProfile:
    try:
        return ENDPOINT_PROFILES[profile_id]
    except KeyError:
        # No fabricated perpetual host: only an active certified gateway may add one.
        raise DomainError("ENDPOINT_PROFILE_UNCERTIFIED") from None


def require_registered(profile: EndpointProfile) -> None:
    if ENDPOINT_PROFILES.get(profile.profile_id) != profile:
        raise DomainError("ENDPOINT_PROFILE_UNCERTIFIED")
