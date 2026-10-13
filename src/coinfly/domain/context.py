"""Account authority and execution realm identity; never inferred from UI locale."""

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class Environment(StrEnum):
    SANDBOX = "SANDBOX"
    LIVE = "LIVE"


class ProductTrack(StrEnum):
    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"


class RegionPolicy(StrEnum):
    GLOBAL = "GLOBAL"
    GERMANY = "GERMANY"


class RunProfile(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    REPLAY = "REPLAY"
    CONTRACT_TEST = "CONTRACT_TEST"
    STANDARD = "STANDARD"


class EngineState(StrEnum):
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class ContextKey(FrozenModel):
    authority_id: Identifier
    environment: Environment
    product_track: ProductTrack
    broker_profile_id: Identifier
    epoch: int = Field(ge=0)

    @property
    def identity(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class VerifiedBrokerProfile(FrozenModel):
    """Worker-owned evidence. This type is excluded from operator configuration RPCs."""

    profile_id: Identifier
    authority_id: Identifier
    environment: Environment
    service_entity: str = Field(min_length=1, max_length=240)
    portfolio_ids: tuple[Identifier, ...] = Field(min_length=1)
    allowed_products: tuple[ProductTrack, ...] = Field(min_length=1)
    evidence_id: Identifier
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_version: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime
    residency: str | None = Field(default=None, max_length=80)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware evidence timestamp required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def coherent_evidence(self) -> "VerifiedBrokerProfile":
        if self.expires_at <= self.issued_at:
            raise ValueError("evidence expiry must follow issuance")
        if len(set(self.portfolio_ids)) != len(self.portfolio_ids):
            raise ValueError("evidence contains duplicate portfolios")
        if len(set(self.allowed_products)) != len(self.allowed_products):
            raise ValueError("evidence contains duplicate products")
        return self

    def is_current(self, now: datetime) -> bool:
        return self.issued_at <= now < self.expires_at


class DomainError(RuntimeError):
    """A stable nonsensitive error code suitable for IPC and audit receipts."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)
