"""Exact non-signing tool schemas; model input has no context or balance fields."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from coinfly.domain.context import FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal

ALLOWED_TOOLS = ("get_cached_features", "get_cached_order_book", "submit_target_weights")


class FeatureQuery(FrozenModel):
    snapshot_id: Identifier
    instruments: tuple[Identifier, ...] = Field(min_length=1, max_length=200)


class BookQuery(FrozenModel):
    snapshot_id: Identifier
    instrument_id: Identifier
    depth: int = Field(default=10, ge=1, le=50)


class FeatureValue(FrozenModel):
    instrument_id: Identifier
    name: Literal[
        "return_1m", "volatility_5m", "spread_bps", "imbalance", "volume_1m", "momentum", "net_opportunity"
    ]
    value: ExactDecimal = Field(allow_inf_nan=False)


class CachedFeatures(FrozenModel):
    snapshot_id: Identifier
    as_of: datetime
    values: tuple[FeatureValue, ...] = Field(max_length=2000)


class BookLevel(FrozenModel):
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False)
    quantity: ExactDecimal = Field(ge=0, allow_inf_nan=False)


class CachedBook(FrozenModel):
    snapshot_id: Identifier
    instrument_id: Identifier
    as_of: datetime
    bids: tuple[BookLevel, ...] = Field(max_length=50)
    asks: tuple[BookLevel, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def sorted_book(self) -> "CachedBook":
        if self.bids != tuple(sorted(self.bids, key=lambda level: level.price, reverse=True)):
            raise ValueError("bids must be sorted")
        if self.asks != tuple(sorted(self.asks, key=lambda level: level.price)):
            raise ValueError("asks must be sorted")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed order book")
        return self


class ProposalReceipt(FrozenModel):
    decision_id: Identifier
    receipt_id: Identifier
    status: str = "PROPOSED"
