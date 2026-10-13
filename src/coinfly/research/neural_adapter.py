"""Declared decoder-to-weight mapping; no market heuristic or execution authority.

The fixed decoder emits BUY/SELL/HOLD. Mapping those observations to
weights is a separate engineered profile, not a
claim of learned allocation. It is intentionally qualified for SPOT only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from coinfly.domain.context import ProductTrack
from coinfly.domain.decimal_math import exact_decimal
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights


@dataclass(frozen=True)
class FixedDecoderWeightAdapter:
    instrument_ids: tuple[str, ...]
    step: Decimal = Decimal("0.10")
    version: str = "fixed-decoder-spot-step-v1"

    def __post_init__(self) -> None:
        if (not self.instrument_ids or len(set(self.instrument_ids)) != len(self.instrument_ids)
                or not isinstance(self.step, Decimal) or not self.step.is_finite()
                or not Decimal(0) < self.step <= Decimal(1)
                or self.version != "fixed-decoder-spot-step-v1"):
            raise ValueError("invalid declared neural weight mapping")

    @exact_decimal
    def propose(self, *, side: Literal["BUY", "SELL", "HOLD"], instrument_id: str,
                previous_targets: tuple[InstrumentWeight, ...], product_track: ProductTrack,
                decision_id: str, snapshot_id: str, allocation_revision: int,
                expires_at: datetime) -> TargetWeights | None:
        if product_track != ProductTrack.SPOT:
            raise ValueError("perpetual neural mapping has no qualified profile")
        if side not in {"BUY", "SELL", "HOLD"} or instrument_id not in self.instrument_ids:
            raise ValueError("invalid fixed neural decoder output")
        previous = {entry.instrument_id: entry.weight for entry in previous_targets}
        if (len(previous) != len(previous_targets) or set(previous) != set(self.instrument_ids)
                or any(not weight.is_finite() or not Decimal(0) <= weight <= Decimal(1)
                       for weight in previous.values()) or sum(previous.values()) > Decimal(1)):
            raise ValueError("valid full previous strategy target vector required")
        if side == "HOLD":
            return None
        if side == "BUY":
            # Use only unallocated strategy weight. Never sell a different asset
            # to manufacture room for this neural direction.
            change = min(self.step, Decimal(1) - sum(previous.values()))
            if change == 0:
                return None
            previous[instrument_id] += change
        else:
            change = min(self.step, previous[instrument_id])
            if change == 0:
                return None
            previous[instrument_id] -= change
        return TargetWeights(
            affected_instruments=(instrument_id,),
            decision_id=decision_id, model_id="coinfly-dual-compartment-v1",
            model_version=self.version, snapshot_id=snapshot_id,
            allocation_revision=allocation_revision, expires_at=expires_at,
            weights=tuple(InstrumentWeight(instrument_id=name, weight=previous[name])
                          for name in self.instrument_ids),
        )
