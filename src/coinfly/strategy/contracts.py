"""The complete model output schema. Context and money are deliberately absent."""

from datetime import datetime, timezone

from pydantic import Field, field_validator, model_validator

from coinfly.domain.context import FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal


class InstrumentWeight(FrozenModel):
    instrument_id: Identifier
    weight: ExactDecimal = Field(allow_inf_nan=False, max_digits=38, decimal_places=18)


class TargetWeights(FrozenModel):
    decision_id: Identifier
    model_id: Identifier
    model_version: Identifier
    snapshot_id: Identifier
    allocation_revision: int = Field(ge=0)
    expires_at: datetime
    weights: tuple[InstrumentWeight, ...] = Field(min_length=1, max_length=10000)
    # A full vector communicates the desired allocation; it is NOT authority to
    # rebalance every instrument. The trusted mapping declares the action scope.
    affected_instruments: tuple[Identifier, ...] = Field(min_length=1, max_length=10000)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware expiry required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_instruments(self) -> "TargetWeights":
        if len({item.instrument_id for item in self.weights}) != len(self.weights):
            raise ValueError("duplicate instrument")
        scope = set(self.affected_instruments)
        if len(scope) != len(self.affected_instruments):
            raise ValueError("duplicate affected instrument")
        if not scope.issubset(item.instrument_id for item in self.weights):
            raise ValueError("affected instrument missing from target vector")
        return self
