"""Documented key permissions, independent of jurisdiction and products."""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Mapping, Protocol

from pydantic import Field

from coinfly.domain.context import DomainError, FrozenModel, Identifier
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose


class KeyPermissions(FrozenModel):
    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_uuid: Identifier
    portfolio_type: str = Field(min_length=1, max_length=64)


class PermissionEvidence(FrozenModel):
    binding_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    credential_version: int = Field(ge=1)
    permissions: KeyPermissions
    observed_at: datetime
    expires_at: datetime
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def require_current(self, metadata: CredentialMetadata, *, now: datetime) -> None:
        if self.observed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise DomainError("PERMISSIONS_TIMESTAMP_INVALID")
        if not self.observed_at <= now < self.expires_at:
            raise DomainError("PERMISSIONS_STALE")
        if self.binding_id != metadata.binding_id or self.credential_version != metadata.version:
            raise DomainError("PERMISSIONS_CREDENTIAL_MISMATCH")
        validate_least_privilege(self.permissions, metadata)


class PermissionReader(Protocol):
    def read_key_permissions(self) -> Mapping[str, Any]: ...


def validate_least_privilege(permissions: KeyPermissions, metadata: CredentialMetadata) -> None:
    if not permissions.can_view or permissions.can_transfer:
        raise DomainError("KEY_PERMISSIONS_EXCESSIVE_OR_MISSING")
    if permissions.portfolio_uuid != metadata.portfolio_id:
        raise DomainError("PORTFOLIO_SCOPE_UNVERIFIED")
    if metadata.purpose == CredentialPurpose.EXECUTION:
        if not permissions.can_trade:
            raise DomainError("TRADE_PERMISSION_REQUIRED")
    elif metadata.purpose == CredentialPurpose.OBSERVATION:
        if permissions.can_trade:
            raise DomainError("OBSERVER_TRADE_PERMISSION_FORBIDDEN")
    else:
        raise DomainError("COINBASE_CREDENTIAL_PURPOSE_FORBIDDEN")


class CapabilityHandshake:
    def __init__(self, reader: PermissionReader, *, ttl: timedelta = timedelta(minutes=2)) -> None:
        if not timedelta(seconds=1) <= ttl <= timedelta(minutes=5):
            raise DomainError("PERMISSIONS_TTL_INVALID")
        self._reader = reader
        self._ttl = ttl

    def verify(self, metadata: CredentialMetadata, *, now: datetime) -> PermissionEvidence:
        if now.tzinfo is None:
            raise DomainError("PERMISSIONS_TIMESTAMP_INVALID")
        try:
            response = KeyPermissions.model_validate(dict(self._reader.read_key_permissions()))
        except Exception:
            raise DomainError("KEY_PERMISSIONS_UNVERIFIED") from None
        validate_least_privilege(response, metadata)
        return PermissionEvidence(
            binding_id=metadata.binding_id,
            credential_version=metadata.version,
            permissions=response,
            observed_at=now,
            expires_at=now + self._ttl,
            response_sha256=sha256(response.model_dump_json().encode()).hexdigest(),
        )
