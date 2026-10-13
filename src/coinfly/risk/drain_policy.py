"""Bounded drain instructions; a deadline never authorizes a wider loss bound."""

from decimal import Decimal

from pydantic import Field

from coinfly.domain.context import FrozenModel
from coinfly.domain.decimal_math import ExactDecimal


class DrainPolicy(FrozenModel):
    max_slippage_fraction: ExactDecimal = Field(default=Decimal("0.005"), ge=0, lt=1, allow_inf_nan=False)
    max_participation_fraction: ExactDecimal = Field(default=Decimal("0.10"), gt=0, le=1, allow_inf_nan=False)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    require_reduce_only: bool = True
