"""Account-bound legal eligibility, separate from API key permissions.

Coinbase key_permissions has no legal-entity or residency fields. There is no
certified regional evidence integration supplied with this source release.
"""

from datetime import datetime
from typing import Protocol

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, RegionPolicy, VerifiedBrokerProfile
from coinfly.domain.products import ProductSubtype
from coinfly.security.release_gates import CERTIFIED_REGION_CHANNELS


class BrokerEvidenceChannel(Protocol):
    """External, independently certified authenticated broker evidence channel.

    Implementations must verify issuer identity, signature/authenticated origin,
    revocation, account identity, service-specific eligibility and expiry. This
    protocol describes an integration requirement; it does not invent an API.
    """

    @property
    def certification_id(self) -> str: ...
    def verify(self, evidence: bytes, *, authority_id: str, now: datetime) -> VerifiedBrokerProfile: ...


class RegionAttestor:
    def __init__(self, channel: BrokerEvidenceChannel | None = None) -> None:
        self._channel = channel

    def attest(self, evidence: bytes, *, config: EngineConfig, now: datetime) -> VerifiedBrokerProfile:
        if config.environment != Environment.LIVE:
            raise DomainError("REGION_ATTESTATION_LIVE_ONLY")
        channel = self._channel
        if channel is None or channel.certification_id not in CERTIFIED_REGION_CHANNELS:
            raise DomainError("REGION_UNVERIFIED")
        if not evidence or len(evidence) > 64 * 1024:
            raise DomainError("REGION_EVIDENCE_INVALID")
        profile = channel.verify(evidence, authority_id=config.authority_id, now=now)
        enforce_region_policy(config, profile, now=now)
        return profile


def enforce_region_policy(config: EngineConfig, profile: VerifiedBrokerProfile, *, now: datetime) -> None:
    """Restrictions only; calling this is not evidence authentication."""
    if not profile.is_current(now) or profile.expires_at <= profile.issued_at:
        raise DomainError("REGION_EVIDENCE_STALE")
    if (
        config.authority_id != profile.authority_id
        or config.environment != profile.environment
        or config.broker_profile_id != profile.profile_id
        or not set(config.execution_scope).issubset(profile.portfolio_ids)
    ):
        raise DomainError("REGION_PROFILE_MISMATCH")
    if config.product_track not in profile.allowed_products:
        raise DomainError("PRODUCT_INELIGIBLE")
    subtype = config.effective_subtype
    if subtype is ProductSubtype.PERPETUAL_NO_EXPIRY:
        # International no-expiry perpetuals are not part of the EEA offering.
        # GLOBAL cannot change a verified Germany-bound service entity.
        if " ".join(profile.service_entity.casefold().split()) == "coinbase germany gmbh":
            raise DomainError("GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN")
        if config.region_policy == RegionPolicy.GERMANY:
            raise DomainError("GERMANY_POLICY_EXCLUDES_INTERNATIONAL_PERPETUALS")

