"""Versioned capability resolver: policy × mode × product × route × account evidence.

A dropdown is never eligibility evidence. The resolver binds the requested
execution mode (SANDBOX paper / LIVE), the application policy (GERMANY / GLOBAL),
the verified account jurisdiction and service entity, key permissions, the
product subtype, the broker route that would carry the order and the freshness
of that evidence into one explicit decision. Each closed outcome says *why* it
is closed, in one of six distinguishable categories:

* ``BROKER_RESTRICTED`` — the account's verified service entity, jurisdiction or
  key permissions do not allow this product (nothing local can change that).
* ``ENROLLMENT_MISSING`` — the broker offers it, but the account has not
  completed the authoritative enrollment (derivatives onboarding, trade
  permission, verified profile). The workflow is documented, never invented.
* ``LOCAL_QUALIFICATION_MISSING`` — Coinfly's own release/qualification gates
  are closed (native signing, certified gateway, paper margin model).
* ``IMPLEMENTATION_MISSING`` — the broker route exists but this codebase has no
  adapter for it yet (Deribit-powered Global Derivatives gateway, CBFSE API).
* ``TEMPORARY_FAILURE`` — evidence expired/stale or the service is unavailable;
  a later refresh can change the answer.
* ``POLICY_REJECTED`` — the selected application policy (GERMANY) restricts the
  request even though the account itself might allow it.

GLOBAL means "the jurisdictions the account evidence supports": it can never
widen what the verified account is actually allowed to do.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Iterable

from pydantic import Field

from coinfly.domain.context import Environment, FrozenModel, ProductTrack, RegionPolicy, RunProfile
from coinfly.domain.products import (
    EEA_ELIGIBLE_SUBTYPES,
    ProductSubtype,
    default_subtype,
)
from coinfly.security.release_gates import ReleaseGate, live_release_gates
from coinfly.venues.coinbase.capability_handshake import KeyPermissions
from coinfly.venues.routes import BrokerRoute, route_for

RESOLVER_VERSION = "capability-resolver-v1"

# Service entities named in Coinbase's public EEA documentation. Matching is on
# the normalized entity name only; the evidence channel must have verified it.
EEA_SPOT_ENTITIES = frozenset({"coinbase europe limited", "coinbase luxembourg s.a.", "coinbase germany gmbh"})
EEA_DERIVATIVES_ENTITIES = frozenset({"coinbase financial services europe ltd.", "coinbase financial services europe ltd",
                                      "coinbase financial services europe limited"})
US_DERIVATIVES_ENTITIES = frozenset({"coinbase financial markets, inc.", "coinbase financial markets inc.",
                                     "coinbase financial markets, inc", "coinbase financial markets"})
INTERNATIONAL_DERIVATIVES_ENTITIES = frozenset({"coinbase international exchange", "coinbase bermuda",
                                                "coinbase international exchange (bermuda) ltd"})


class Category(StrEnum):
    SUPPORTED = "SUPPORTED"
    BROKER_RESTRICTED = "BROKER_RESTRICTED"
    ENROLLMENT_MISSING = "ENROLLMENT_MISSING"
    LOCAL_QUALIFICATION_MISSING = "LOCAL_QUALIFICATION_MISSING"
    IMPLEMENTATION_MISSING = "IMPLEMENTATION_MISSING"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    POLICY_REJECTED = "POLICY_REJECTED"


class AccountEvidence(FrozenModel):
    """Worker-owned, verified account facts. Never constructed from UI input.

    ``allowed_subtypes`` comes from a verified broker profile (service entity
    plus product eligibility). ``derivatives_enrolled`` is None when the
    evidence channel did not establish it. ``permissions`` is the documented
    key-permissions response for the credential that would sign orders.
    """

    service_entity: str = Field(min_length=1, max_length=240)
    jurisdiction: str | None = Field(default=None, min_length=2, max_length=2)
    allowed_subtypes: tuple[ProductSubtype, ...] = ()
    derivatives_enrolled: bool | None = None
    permissions: KeyPermissions | None = None
    portfolio_ids: tuple[str, ...] = ()
    evidence_id: str = Field(min_length=1, max_length=160)
    observed_at: datetime
    expires_at: datetime

    @property
    def normalized_entity(self) -> str:
        return " ".join(self.service_entity.casefold().split())

    def is_current(self, now: datetime) -> bool:
        return self.observed_at <= now < self.expires_at

    @property
    def eea_entity(self) -> bool:
        return self.normalized_entity in EEA_SPOT_ENTITIES or self.normalized_entity in EEA_DERIVATIVES_ENTITIES

    @property
    def germany_bound(self) -> bool:
        return self.normalized_entity == "coinbase germany gmbh" or self.jurisdiction == "DE"


class LocalQualifications(FrozenModel):
    """What this build has qualified locally, independent of any account."""

    paper_spot: bool = True
    paper_derivatives: bool = False
    live_spot_adapter: bool = False
    live_futures_adapter: bool = False
    live_global_derivatives_adapter: bool = False


class CapabilityRequest(FrozenModel):
    environment: Environment
    region_policy: RegionPolicy
    product_track: ProductTrack
    product_subtype: ProductSubtype | None = None
    run_profile: RunProfile = RunProfile.PAPER

    @property
    def subtype(self) -> ProductSubtype:
        return self.product_subtype or default_subtype(self.product_track, self.region_policy)


class CapabilityDecision(FrozenModel):
    resolver_version: str = RESOLVER_VERSION
    request: CapabilityRequest
    subtype: ProductSubtype
    category: Category
    route: BrokerRoute | None
    service_entity: str | None
    codes: tuple[str, ...]
    explanation: str
    evidence_fresh: bool
    evidence_id: str | None = None
    # Release gates that are closed regardless of the primary category, so an
    # operator sees every prerequisite at once instead of one per attempt.
    closed_gates: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.category is Category.SUPPORTED

    @property
    def blocking_code(self) -> str | None:
        return self.codes[0] if self.codes else None

    @property
    def all_blocking_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for code in (*self.codes, *self.closed_gates):
            if code not in seen:
                seen.append(code)
        return tuple(seen)


def _closed(request: CapabilityRequest, subtype: ProductSubtype, category: Category, route: BrokerRoute | None,
            codes: Iterable[str], explanation: str, *, entity: str | None = None, fresh: bool = False,
            evidence_id: str | None = None) -> CapabilityDecision:
    return CapabilityDecision(request=request, subtype=subtype, category=category, route=route,
                              service_entity=entity, codes=tuple(codes), explanation=explanation,
                              evidence_fresh=fresh, evidence_id=evidence_id)


def resolve(request: CapabilityRequest, *, evidence: AccountEvidence | None, now: datetime,
            local: LocalQualifications | None = None,
            gates: tuple[ReleaseGate, ...] | None = None) -> CapabilityDecision:
    """Resolve one policy × mode × product combination to an explicit decision."""
    local = local or LocalQualifications()
    subtype = request.subtype
    if request.environment is Environment.SANDBOX:
        return _resolve_paper(request, subtype, local)
    resolved_gates = gates if gates is not None else live_release_gates()
    decision = _resolve_live(request, subtype, evidence=evidence, now=now, local=local, gates=resolved_gates)
    closed = tuple(gate.code for gate in resolved_gates if not gate.satisfied and gate.code not in decision.codes)
    return decision.model_copy(update={"closed_gates": closed})


def _resolve_paper(request: CapabilityRequest, subtype: ProductSubtype, local: LocalQualifications) -> CapabilityDecision:
    route = BrokerRoute.PAPER_LOCAL
    entity = "LOCAL PAPER SIMULATOR"
    if request.run_profile is not RunProfile.PAPER:
        return _closed(request, subtype, Category.LOCAL_QUALIFICATION_MISSING, route,
                       ("RUN_PROFILE_FEED_UNQUALIFIED",),
                       "Only the stateful local paper profile is qualified for simulation.", entity=entity)
    if request.region_policy is RegionPolicy.GERMANY and subtype not in EEA_ELIGIBLE_SUBTYPES:
        return _closed(request, subtype, Category.POLICY_REJECTED, route,
                       ("GERMANY_POLICY_EXCLUDES_INTERNATIONAL_PERPETUALS",),
                       "GERMANY simulates the EEA offering: spot, perpetual-style futures and dated futures. "
                       "International no-expiry perpetuals are outside that offering; choose GLOBAL to simulate them.",
                       entity=entity)
    if subtype is ProductSubtype.SPOT:
        if not local.paper_spot:
            return _closed(request, subtype, Category.LOCAL_QUALIFICATION_MISSING, route,
                           ("PAPER_SPOT_UNQUALIFIED",), "The local paper spot model is not qualified.", entity=entity)
    elif not local.paper_derivatives:
        return _closed(request, subtype, Category.LOCAL_QUALIFICATION_MISSING, route,
                       ("PERPETUAL_MARGIN_UNCERTIFIED",),
                       "The local paper derivatives model (margin, funding, expiry) is not qualified in this build.",
                       entity=entity)
    return CapabilityDecision(request=request, subtype=subtype, category=Category.SUPPORTED, route=route,
                              service_entity=entity, codes=(), evidence_fresh=True,
                              explanation="Simulated locally with synthetic funds; no account or enrollment is required.")


def _resolve_live(request: CapabilityRequest, subtype: ProductSubtype, *, evidence: AccountEvidence | None,
                  now: datetime, local: LocalQualifications, gates: tuple[ReleaseGate, ...]) -> CapabilityDecision:
    route = route_for(subtype, evidence.normalized_entity if evidence is not None else None)
    if request.run_profile is not RunProfile.STANDARD:
        return _closed(request, subtype, Category.POLICY_REJECTED, route, ("LIVE_REQUIRES_STANDARD_PROFILE",),
                       "Live execution uses the standard run profile only.")
    if evidence is None:
        return _closed(request, subtype, Category.ENROLLMENT_MISSING, route,
                       ("REGION_UNVERIFIED", "ACCOUNT_EVIDENCE_MISSING"),
                       "No verified account evidence (service entity, jurisdiction, product eligibility) is enrolled. "
                       "Complete the documented enrollment workflow; a stored credential or a successful read "
                       "authentication is not eligibility evidence.")
    entity = evidence.service_entity
    if not evidence.is_current(now):
        return _closed(request, subtype, Category.TEMPORARY_FAILURE, route, ("ACCOUNT_EVIDENCE_STALE",),
                       "The verified account evidence has expired; refresh it before requesting live capability.",
                       entity=entity, evidence_id=evidence.evidence_id)
    # Account restrictions come before anything the application could qualify.
    if subtype is ProductSubtype.PERPETUAL_NO_EXPIRY and (evidence.germany_bound or evidence.eea_entity):
        return _closed(request, subtype, Category.BROKER_RESTRICTED, route,
                       ("GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN",),
                       "An EEA/Germany-bound account cannot trade international no-expiry perpetuals; "
                       "selecting GLOBAL does not change the account's service entity.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if subtype not in evidence.allowed_subtypes:
        if subtype.is_derivative and evidence.derivatives_enrolled is False:
            return _closed(request, subtype, Category.ENROLLMENT_MISSING, route,
                           ("DERIVATIVES_ENROLLMENT_MISSING",),
                           "The account has not completed the broker's derivatives onboarding "
                           "(application, appropriateness assessment, identity verification).",
                           entity=entity, fresh=True, evidence_id=evidence.evidence_id)
        return _closed(request, subtype, Category.BROKER_RESTRICTED, route, ("PRODUCT_INELIGIBLE",),
                       "The verified service entity does not offer this contract model to this account.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if request.region_policy is RegionPolicy.GERMANY:
        if subtype not in EEA_ELIGIBLE_SUBTYPES:
            return _closed(request, subtype, Category.POLICY_REJECTED, route,
                           ("GERMANY_POLICY_EXCLUDES_INTERNATIONAL_PERPETUALS",),
                           "The GERMANY policy permits only the EEA contract families.",
                           entity=entity, fresh=True, evidence_id=evidence.evidence_id)
        if not evidence.eea_entity and evidence.jurisdiction not in (None, "DE"):
            return _closed(request, subtype, Category.POLICY_REJECTED, route, ("GERMANY_POLICY_REQUIRES_EEA_ACCOUNT",),
                           "The GERMANY policy applies to accounts served by a Coinbase EEA entity.",
                           entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    permissions = evidence.permissions
    if permissions is None:
        return _closed(request, subtype, Category.ENROLLMENT_MISSING, route, ("KEY_PERMISSIONS_UNVERIFIED",),
                       "The signing credential's permissions have not been verified.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if not permissions.can_view or not permissions.can_trade:
        return _closed(request, subtype, Category.ENROLLMENT_MISSING, route, ("TRADE_PERMISSION_REQUIRED",),
                       "The credential lacks View+Trade permission; create a trade-enabled key without transfer rights.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if permissions.can_transfer:
        return _closed(request, subtype, Category.ENROLLMENT_MISSING, route, ("KEY_PERMISSIONS_EXCESSIVE_OR_MISSING",),
                       "Execution keys must not carry transfer permission.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    closed_gates = tuple(gate.code for gate in gates if not gate.satisfied)
    if closed_gates:
        return _closed(request, subtype, Category.LOCAL_QUALIFICATION_MISSING, route, closed_gates,
                       "Coinfly's release gates for live execution are not satisfied on this installation.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if route is BrokerRoute.COINBASE_ADVANCED_SPOT and not local.live_spot_adapter:
        return _closed(request, subtype, Category.IMPLEMENTATION_MISSING, route, ("LIVE_SPOT_ADAPTER_UNQUALIFIED",),
                       "The Advanced Trade spot execution adapter is not qualified in this build.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if route is BrokerRoute.COINBASE_ADVANCED_FUTURES:
        if evidence.normalized_entity in EEA_DERIVATIVES_ENTITIES:
            return _closed(request, subtype, Category.IMPLEMENTATION_MISSING, route, ("CBFSE_API_ROUTE_UNDOCUMENTED",),
                           "Coinbase's public API reference documents CFM (US) futures endpoints; an EEA (CBFSE) "
                           "futures API route is not documented, so no adapter can be qualified for it.",
                           entity=entity, fresh=True, evidence_id=evidence.evidence_id)
        if not local.live_futures_adapter:
            return _closed(request, subtype, Category.IMPLEMENTATION_MISSING, route, ("FUTURES_ROUTE_NOT_IMPLEMENTED",),
                           "No qualified futures execution adapter exists in this build.",
                           entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    if route is BrokerRoute.COINBASE_GLOBAL_DERIVATIVES and not local.live_global_derivatives_adapter:
        return _closed(request, subtype, Category.IMPLEMENTATION_MISSING, route,
                       ("GLOBAL_DERIVATIVES_GATEWAY_NOT_IMPLEMENTED", "INTX_CUTOVER_STATUS_UNVERIFIED"),
                       "The Deribit-powered Global Derivatives gateway (JSON-RPC 2.0, drb.coinbase.com) has no "
                       "adapter here, and the legacy INTX route retires at a hard cutover whose date is unpublished.",
                       entity=entity, fresh=True, evidence_id=evidence.evidence_id)
    return CapabilityDecision(request=request, subtype=subtype, category=Category.SUPPORTED, route=route,
                              service_entity=entity, codes=(), evidence_fresh=True, evidence_id=evidence.evidence_id,
                              explanation="Account evidence, permissions, policy, route and local qualification agree.")


def matrix(*, evidence: AccountEvidence | None, now: datetime, local: LocalQualifications | None = None,
           gates: tuple[ReleaseGate, ...] | None = None) -> tuple[CapabilityDecision, ...]:
    """All eight policy × mode × product-track combinations (default subtypes)."""
    decisions = []
    for environment in (Environment.SANDBOX, Environment.LIVE):
        for policy in (RegionPolicy.GERMANY, RegionPolicy.GLOBAL):
            for track in (ProductTrack.SPOT, ProductTrack.PERPETUAL):
                profile = RunProfile.PAPER if environment is Environment.SANDBOX else RunProfile.STANDARD
                request = CapabilityRequest(environment=environment, region_policy=policy, product_track=track,
                                            run_profile=profile)
                decisions.append(resolve(request, evidence=evidence, now=now, local=local, gates=gates))
    return tuple(decisions)
