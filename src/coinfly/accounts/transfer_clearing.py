"""Evidence-bound normalization of asynchronous internal transfer legs."""


from pydantic import Field, model_validator

from coinfly.domain.context import Environment, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal


class InternalTransferEvidence(FrozenModel):
    """The read reconciler proves which selected snapshots include each leg.

    This contract applies only to a confirmed internal transfer between two owned
    canonical assets in the same realm. Unconfirmed deposits never use this path.
    """

    transfer_id: Identifier
    authority_id: Identifier
    environment: Environment
    source_asset_id: Identifier
    destination_asset_id: Identifier
    currency: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    source_observation_id: Identifier
    destination_observation_id: Identifier
    debit_in_source_snapshot: bool
    credit_in_destination_snapshot: bool
    evidence_id: Identifier

    @model_validator(mode="after")
    def distinct_owned_assets(self) -> "InternalTransferEvidence":
        if self.source_asset_id == self.destination_asset_id:
            raise ValueError("transfer legs require distinct canonical assets")
        return self
