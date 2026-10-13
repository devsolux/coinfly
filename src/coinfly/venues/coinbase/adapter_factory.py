"""Construction gates; a source checkout never silently enables live orders."""

from dataclasses import dataclass
from datetime import datetime, timezone

from coinfly.config.models import EngineConfig
from coinfly.domain.context import ContextKey, DomainError, Environment, VerifiedBrokerProfile
from coinfly.network.context_transports import ContextTransports
from coinfly.security.credential_realms import CredentialPurpose, CredentialReference
from coinfly.security.release_gates import live_release_gates
from coinfly.security.signing_gateway import SigningGateway
from coinfly.venues.capability import (
    AccountEvidence,
    CapabilityDecision,
    CapabilityRequest,
    LocalQualifications,
    resolve,
)
from coinfly.venues.coinbase.capability_handshake import PermissionEvidence
from coinfly.venues.coinbase.endpoint_profiles import EndpointProfile, endpoint_profile, require_registered
from coinfly.venues.coinbase.http_reader import BoundHttpsMutator
from coinfly.venues.coinbase.region_attestor import RegionAttestor, enforce_region_policy


@dataclass(frozen=True, slots=True)
class AdapterAvailability:
    available: bool
    blocking_codes: tuple[str, ...]
    decision: CapabilityDecision | None = None

    @property
    def category(self) -> str:
        return self.decision.category.value if self.decision is not None else ("SUPPORTED" if self.available else "UNKNOWN")


@dataclass(frozen=True, slots=True)
class VerifiedAdapterBinding:
    context: ContextKey
    config: EngineConfig
    profile: VerifiedBrokerProfile
    endpoint: EndpointProfile
    credential: CredentialReference
    permissions: PermissionEvidence

    def require_current(self, *, now: datetime) -> None:
        if (
            self.context.authority_id != self.config.authority_id
            or self.context.environment != self.config.environment
            or self.context.product_track != self.config.product_track
            or self.context.broker_profile_id != self.config.broker_profile_id
        ):
            raise DomainError("ADAPTER_CONTEXT_MISMATCH")
        require_registered(self.endpoint)
        if (
            self.endpoint.profile_id != self.config.endpoint_profile_id
            or self.endpoint.environment != self.context.environment
            or self.endpoint.product != self.context.product_track
        ):
            raise DomainError("ADAPTER_ENDPOINT_MISMATCH")
        enforce_region_policy(self.config, self.profile, now=now)
        if (
            not self.credential.active
            or self.config.credential_binding_id != self.credential.metadata.binding_id
        ):
            raise DomainError("CREDENTIAL_INACTIVE")
        self.credential.metadata.require_match(
            self.context,
            portfolio_id=self.config.execution_scope[0],
            purpose=CredentialPurpose.EXECUTION,
            version=self.credential.metadata.version,
        )
        self.permissions.require_current(self.credential.metadata, now=now)


def default_local_qualifications() -> LocalQualifications:
    """What this build qualifies locally. Updated only with the matching tests."""
    return LocalQualifications(paper_spot=True, paper_derivatives=True, live_spot_adapter=False,
                               live_futures_adapter=False, live_global_derivatives_adapter=False)


class AdapterFactory:
    def __init__(self, attestor: RegionAttestor | None = None,
                 local_qualifications: LocalQualifications | None = None) -> None:
        self._attestor = attestor or RegionAttestor()
        self.local_qualifications = local_qualifications or default_local_qualifications()

    def availability(self, config: EngineConfig, *, evidence: AccountEvidence | None = None,
                     now: datetime | None = None,
                     local: LocalQualifications | None = None) -> AdapterAvailability:
        """Resolve through the versioned capability resolver; never a bare gate list."""
        decision = resolve(
            CapabilityRequest(environment=config.environment, region_policy=config.region_policy,
                              product_track=config.product_track, product_subtype=config.product_subtype,
                              run_profile=config.run_profile),
            evidence=evidence, now=now or datetime.now(timezone.utc), local=local or self.local_qualifications,
            gates=live_release_gates(),
        )
        if config.environment == Environment.LIVE:
            codes = decision.all_blocking_codes or (() if decision.available else ("LIVE_ADAPTER_NOT_IMPLEMENTED",))
            return AdapterAvailability(decision.available, codes, decision)
        profile = endpoint_profile(config.endpoint_profile_id)
        if profile.environment != Environment.SANDBOX:
            return AdapterAvailability(False, ("ADAPTER_ENDPOINT_MISMATCH",), decision)
        return AdapterAvailability(decision.available, decision.codes, decision)

    def create_live(
        self,
        *,
        config: EngineConfig,
        context: ContextKey,
        evidence: bytes,
        credential: CredentialReference,
        permissions: PermissionEvidence,
        now: datetime,
        signer: SigningGateway,
        account_evidence: AccountEvidence,
        transports: ContextTransports,
    ) -> BoundHttpsMutator:
        """Compose a real scoped transport only from independently verified inputs.

        No signer/credential is minted from setup status or an RPC payload. The
        packaged native composition owns these capabilities and must refresh
        evidence before its leases expire. Unsigned source gates stay closed.
        """
        if config.environment != Environment.LIVE:
            raise DomainError("LIVE_FACTORY_REALM_MISMATCH")
        available = self.availability(config, evidence=account_evidence, now=now)
        if not available.available:
            raise DomainError(available.blocking_codes[0])
        profile = self._attestor.attest(evidence, config=config, now=now)
        binding = VerifiedAdapterBinding(
            context, config, profile, endpoint_profile(config.endpoint_profile_id), credential, permissions
        )
        binding.require_current(now=now)
        signer.require_context(context)
        signer.require_endpoint(binding.endpoint)
        return BoundHttpsMutator(binding.endpoint, signer=signer, context=context, transports=transports)
