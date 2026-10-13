"""Only these nonsensitive fields may cross the GUI configuration boundary."""

from decimal import Decimal
from hashlib import sha256
from typing import Annotated

from pydantic import Field, model_validator

from coinfly.domain.context import (
    Environment,
    FrozenModel,
    Identifier,
    ProductTrack,
    RegionPolicy,
    RunProfile,
)
from coinfly.domain.decimal_math import ExactDecimal
from coinfly.domain.products import ProductSubtype, default_subtype


class RiskLimits(FrozenModel):
    max_order_notional: ExactDecimal = Field(default=Decimal("10"), gt=0, allow_inf_nan=False)
    max_gross_exposure: ExactDecimal = Field(default=Decimal("1"), gt=0, allow_inf_nan=False)
    max_daily_loss: ExactDecimal = Field(default=Decimal("20"), gt=0, allow_inf_nan=False)
    max_drawdown_fraction: ExactDecimal = Field(default=Decimal("0.20"), gt=0, lt=1, allow_inf_nan=False)
    max_active_episodes: int = Field(default=4, ge=1, le=100)


class PaperFunding(FrozenModel):
    """Explicit, visible synthetic funding for a NEW paper book.

    Applied exactly once when a paper book is created (the SIMULATION_SEED
    journal event). Changing it later never reseeds an existing book: the
    funded amount is part of that book's own ledger history.
    """

    currency: str = Field(default="USD", pattern=r"^[A-Z0-9]{2,12}$")
    amount: ExactDecimal = Field(default=Decimal("100"), gt=0, le=Decimal("1000000000"), allow_inf_nan=False,
                                 max_digits=38, decimal_places=18)


class EngineConfig(FrozenModel):
    authority_id: Identifier
    environment: Environment = Environment.SANDBOX
    product_track: ProductTrack = ProductTrack.SPOT
    # Versioned contract model within the track (see coinfly.domain.products).
    # None keeps the policy default so persisted configurations stay readable.
    product_subtype: ProductSubtype | None = None
    region_policy: RegionPolicy = RegionPolicy.GERMANY
    run_profile: RunProfile = RunProfile.PAPER
    execution_scope: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    broker_profile_id: Identifier = "paper-local"
    endpoint_profile_id: Identifier = "paper-local"
    credential_binding_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    reporting_currency: str = Field(default="USD", pattern=r"^[A-Z0-9]{2,12}$")
    limits: RiskLimits = Field(default_factory=RiskLimits)
    paper_funding: PaperFunding = Field(default_factory=PaperFunding)

    @model_validator(mode="after")
    def validate_realms(self) -> "EngineConfig":
        if len(set(self.execution_scope)) != len(self.execution_scope):
            raise ValueError("execution scope contains duplicate portfolios")
        if self.product_subtype is not None and self.product_subtype.track != self.product_track:
            raise ValueError("product subtype does not belong to the selected product track")
        # Regional admission is decided by the versioned capability resolver
        # (coinfly.venues.capability) from account evidence and route
        # qualification, never by an unconditional rule in the configuration.
        if self.environment == Environment.LIVE:
            if self.run_profile != RunProfile.STANDARD:
                raise ValueError("LIVE requires STANDARD run profile")
            if not self.credential_binding_id or len(self.execution_scope) != 1:
                raise ValueError("LIVE requires an opaque credential and one execution portfolio")
            if self.endpoint_profile_id in ("paper-local", "coinbase-static-sandbox", "coinbase-public-market"):
                raise ValueError("LIVE cannot use a simulation or public-data endpoint")
        else:
            if self.run_profile == RunProfile.STANDARD:
                raise ValueError("SANDBOX requires an explicit simulation run profile")
            if self.paper_funding.currency != self.reporting_currency:
                raise ValueError("paper funding must be denominated in the reporting currency")
            if self.credential_binding_id is not None:
                raise ValueError("SANDBOX has no execution credential binding")
            expected = "coinbase-static-sandbox" if self.run_profile == RunProfile.CONTRACT_TEST else "paper-local"
            if self.endpoint_profile_id != expected:
                raise ValueError("SANDBOX endpoint does not match run profile")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def effective_subtype(self) -> ProductSubtype:
        return self.product_subtype or default_subtype(self.product_track, self.region_policy)


class ConfigurationRevision(FrozenModel):
    revision: int = Field(ge=0)
    config: EngineConfig


def default_config() -> EngineConfig:
    return EngineConfig(authority_id="local-paper", execution_scope=("paper-book",))
