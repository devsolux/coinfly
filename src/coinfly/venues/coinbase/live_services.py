"""Native-release composition inputs; never deserialized from UI or environment.

The account/evidence provider returns its already authenticated bounded cache;
network enrollment/refresh is an explicit service, not a UI snapshot side effect.
This module connects existing qualified primitives instead of manufacturing
publisher roots, legal eligibility, credential references or signing authority.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from coinfly.config.models import EngineConfig
from coinfly.domain.context import ContextKey
from coinfly.ipc.auth import SignedHelperPolicy
from coinfly.network.context_transports import ContextTransports
from coinfly.security.credential_realms import CredentialPurpose, CredentialReference
from coinfly.security.signing_gateway import SigningGateway
from coinfly.venues.capability import AccountEvidence

from .adapter_factory import AdapterAvailability, AdapterFactory
from .capability_handshake import PermissionEvidence
from .http_reader import BoundHttpsMutator


@dataclass(frozen=True)
class ExecutionEnrollment:
    account: AccountEvidence
    region_evidence: bytes
    credential: CredentialReference
    permissions: PermissionEvidence
    qualification_record: str | None = None

    def require_current(self, config: EngineConfig, context: ContextKey, *, now: datetime) -> None:
        self.credential.metadata.require_match(context, portfolio_id=config.execution_scope[0],
            purpose=CredentialPurpose.EXECUTION, version=self.credential.metadata.version)
        self.permissions.require_current(self.credential.metadata, now=now)


@dataclass(frozen=True)
class QualifiedLiveServices:
    adapter_factory: AdapterFactory
    enrollment_for: Callable[[EngineConfig, datetime], ExecutionEnrollment]
    signer_for: Callable[[EngineConfig, ContextKey, ExecutionEnrollment], SigningGateway]
    authorization: SignedHelperPolicy

    def availability(self, config: EngineConfig, *, now: datetime) -> AdapterAvailability:
        enrollment = self.enrollment_for(config, now)
        return self.adapter_factory.availability(config, evidence=enrollment.account, now=now)

    def transport(self, config: EngineConfig, context: ContextKey, *, now: datetime,
                  transports: ContextTransports) -> BoundHttpsMutator:
        enrollment = self.enrollment_for(config, now)
        enrollment.require_current(config, context, now=now)
        signer = self.signer_for(config, context, enrollment)
        try:
            return self.adapter_factory.create_live(config=config, context=context,
                evidence=enrollment.region_evidence, credential=enrollment.credential,
                permissions=enrollment.permissions, signer=signer, account_evidence=enrollment.account,
                transports=transports, now=now)
        except BaseException:
            signer.retire()
            raise
