"""Explicit source enrollment and proven cross-API economic identities."""

from datetime import datetime, timedelta
from typing import Protocol

from pydantic import Field, model_validator

from coinfly.accounts.inventory import AccountKind, AccountPage
from coinfly.domain.context import DomainError, Environment, FrozenModel, Identifier


class AccountsReader(Protocol):
    async def read_page(self, cursor: str | None) -> AccountPage: ...


class SourceScope(FrozenModel):
    source_id: Identifier
    scope_id: Identifier
    authority_id: Identifier
    environment: Environment
    portfolio_ids: tuple[Identifier, ...] = Field(min_length=1)
    service_entity: str = Field(min_length=1, max_length=240)
    source_api: str = Field(min_length=1, max_length=240)
    readable_kinds: tuple[AccountKind, ...] = Field(min_length=1)
    freshness_budget: timedelta = timedelta(seconds=30)
    supported: bool = True
    accessible: bool = True
    discovery_complete: bool = False
    evidence_id: Identifier

    @model_validator(mode="after")
    def bounded_freshness(self) -> "SourceScope":
        if self.freshness_budget <= timedelta(0):
            raise ValueError("positive source freshness budget required")
        return self


class AliasBinding(FrozenModel):
    """Only certified account IDs establish identity; amounts are never keys."""

    source_id: Identifier
    scope_id: Identifier
    account_id: Identifier
    currency: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    canonical_asset_id: Identifier
    evidence_id: Identifier
    priority: int = Field(default=0, ge=0)
    summary_component_ids: tuple[Identifier, ...] = ()


class UniverseEvidence(FrozenModel):
    authority_id: Identifier
    environment: Environment
    expected_scopes: tuple[Identifier, ...]
    evidence_id: Identifier
    verified_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def valid_window(self) -> "UniverseEvidence":
        for stamp in (self.verified_at, self.expires_at):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("aware universe evidence time required")
        if self.expires_at <= self.verified_at or len(set(self.expected_scopes)) != len(self.expected_scopes):
            raise ValueError("invalid universe evidence")
        return self


class SourceRegistry:
    def __init__(
        self, authority_id: str, environment: Environment, *,
        expected_scopes: tuple[str, ...] = (), universe_evidence: UniverseEvidence | None = None,
    ) -> None:
        if len(set(expected_scopes)) != len(expected_scopes):
            raise ValueError("duplicate expected scope")
        self._authority_id = authority_id
        self._environment = environment
        self.expected_scopes = frozenset(expected_scopes)
        self.universe_evidence = universe_evidence
        if universe_evidence is not None:
            if universe_evidence.authority_id != authority_id or universe_evidence.environment != environment:
                raise DomainError("OBSERVATION_REALM_MISMATCH")
            if set(universe_evidence.expected_scopes) != set(expected_scopes):
                raise DomainError("UNIVERSE_EVIDENCE_SCOPE_MISMATCH")
        self._sources: dict[str, SourceScope] = {}
        self._readers: dict[str, AccountsReader] = {}
        self._aliases: dict[tuple[str, str, str, str], AliasBinding] = {}

    @property
    def authority_id(self) -> str:
        return self._authority_id

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def sources(self) -> tuple[SourceScope, ...]:
        return tuple(self._sources[key] for key in sorted(self._sources))

    def register(self, scope: SourceScope, reader: AccountsReader | None = None) -> None:
        if scope.authority_id != self.authority_id or scope.environment != self.environment:
            raise DomainError("OBSERVATION_REALM_MISMATCH")
        if scope.scope_id in self._sources:
            raise DomainError("OBSERVATION_SCOPE_ALREADY_REGISTERED")
        self._sources[scope.scope_id] = scope
        if reader is not None:
            self._readers[scope.scope_id] = reader

    def bind_alias(self, binding: AliasBinding) -> None:
        scope = self.scope(binding.scope_id)
        if scope.source_id != binding.source_id:
            raise DomainError("ALIAS_SOURCE_MISMATCH")
        key = (binding.source_id, binding.scope_id, binding.account_id, binding.currency)
        if key in self._aliases and self._aliases[key] != binding:
            raise DomainError("ALIAS_IDENTITY_CONFLICT")
        if any(item.canonical_asset_id == binding.canonical_asset_id and item.currency != binding.currency
               for item in self._aliases.values()):
            raise DomainError("CANONICAL_ASSET_CURRENCY_CONFLICT")
        self._aliases[key] = binding

    def alias(self, source_id: str, scope_id: str, account_id: str, currency: str) -> AliasBinding | None:
        return self._aliases.get((source_id, scope_id, account_id, currency))

    def scope(self, scope_id: str) -> SourceScope:
        try:
            return self._sources[scope_id]
        except KeyError as exc:
            raise DomainError("UNENROLLED_OBSERVATION_SCOPE") from exc

    def reader(self, scope_id: str) -> AccountsReader | None:
        return self._readers.get(scope_id)

    def universe_is_verified(self, now: datetime) -> bool:
        evidence = self.universe_evidence
        return (evidence is not None and evidence.verified_at <= now < evidence.expires_at
                and set(self._sources) == set(evidence.expected_scopes))
