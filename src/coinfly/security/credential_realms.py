"""Opaque vault references and exact realm/purpose matching at every use."""

from enum import StrEnum
from hashlib import sha256

from pydantic import Field

from coinfly.domain.context import ContextKey, DomainError, Environment, FrozenModel, Identifier


class CredentialPurpose(StrEnum):
    EXECUTION = "EXECUTION"
    OBSERVATION = "OBSERVATION"
    MODEL_PROVIDER = "MODEL_PROVIDER"
    TEST = "TEST"


class CredentialMetadata(FrozenModel):
    binding_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    environment: Environment
    verified_profile_id: Identifier
    authority_id: Identifier
    portfolio_id: Identifier
    purpose: CredentialPurpose
    version: int = Field(ge=1)

    @property
    def digest(self) -> bytes:
        return sha256(self.model_dump_json().encode("utf-8")).digest()

    def require_match(
        self,
        context: ContextKey,
        *,
        portfolio_id: str,
        purpose: CredentialPurpose,
        version: int,
    ) -> None:
        actual = (
            self.authority_id,
            self.environment,
            self.verified_profile_id,
            self.portfolio_id,
            self.purpose,
            self.version,
        )
        expected = (
            context.authority_id,
            context.environment,
            context.broker_profile_id,
            portfolio_id,
            purpose,
            version,
        )
        if actual != expected:
            raise DomainError("CREDENTIAL_REALM_MISMATCH")
        if self.environment == Environment.SANDBOX and purpose != CredentialPurpose.TEST:
            raise DomainError("SANDBOX_SIGNER_FORBIDDEN")


class CredentialReference(FrozenModel):
    """The only credential object allowed in persistence/IPC receipts."""

    metadata: CredentialMetadata
    active: bool = False
