"""Declared decoder-to-signed-weight mapping for derivatives: fixed-decoder-derivatives-step-v1.

This is deliberately NOT the spot mapping (``fixed-decoder-spot-step-v1``) and
does not touch it. Semantics, all explicit and per instrument:

* Weights are signed fractions of the strategy's equity, ``-1 <= w <= 1``;
  positive is long, negative is short. Gross exposure ``sum(|w|)`` is bounded by
  the permitted leverage.
* BUY on a flat or long instrument opens/increases the long by at most ``step``,
  using only unallocated gross room. BUY on a short instrument reduces the short
  by at most ``step`` and stops at flat (a reduce/close), unless ``allow_reversal``
  permits the remainder of the step to open a long in the same decision.
* SELL is the mirror image: it reduces a long toward flat, or opens/increases a
  short only when short direction is permitted. SELL therefore never means "close
  long AND open short" unless reversal is explicitly permitted.
* HOLD and an exhausted permitted change emit no proposal.
* Contract size is not a mapping concern: the execution planner converts the
  signed weight into contracts using the instrument's mark and contract
  multiplier; margin admission runs separately and may reject the proposal.

It inspects no prices, balances, P&L, margin, signer state or broker routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from coinfly.domain.context import ProductTrack
from coinfly.domain.decimal_math import exact_decimal
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.definition import DirectionPermission

MAPPING_VERSION = "fixed-decoder-derivatives-step-v1"


@dataclass(frozen=True)
class FixedDecoderDerivativesAdapter:
    instrument_ids: tuple[str, ...]
    directions: DirectionPermission
    max_gross: Decimal = Decimal("1")
    max_per_instrument: Decimal = Decimal("1")
    step: Decimal = Decimal("0.10")
    allow_reversal: bool = False
    version: str = MAPPING_VERSION

    def __post_init__(self) -> None:
        if (not self.instrument_ids or len(set(self.instrument_ids)) != len(self.instrument_ids)
                or not isinstance(self.step, Decimal) or not self.step.is_finite() or not Decimal(0) < self.step <= Decimal(1)
                or not Decimal(0) < self.max_per_instrument <= self.max_gross or self.max_gross > Decimal("10")
                or self.version != MAPPING_VERSION):
            raise ValueError("invalid declared derivatives weight mapping")

    def _validate_previous(self, previous_targets: tuple[InstrumentWeight, ...]) -> dict[str, Decimal]:
        previous = {entry.instrument_id: entry.weight for entry in previous_targets}
        if (len(previous) != len(previous_targets) or set(previous) != set(self.instrument_ids)
                or any(not weight.is_finite() or abs(weight) > self.max_per_instrument for weight in previous.values())
                or sum((abs(weight) for weight in previous.values()), Decimal(0)) > self.max_gross):
            raise ValueError("valid full previous signed target vector required")
        return previous

    @exact_decimal
    def propose(self, *, side: Literal["BUY", "SELL", "HOLD"], instrument_id: str,
                previous_targets: tuple[InstrumentWeight, ...], product_track: ProductTrack,
                decision_id: str, snapshot_id: str, allocation_revision: int,
                expires_at: datetime) -> TargetWeights | None:
        if product_track is not ProductTrack.PERPETUAL:
            raise ValueError("derivatives mapping applies to the derivatives track only")
        if side not in {"BUY", "SELL", "HOLD"} or instrument_id not in self.instrument_ids:
            raise ValueError("invalid fixed neural decoder output")
        previous = self._validate_previous(previous_targets)
        if side == "HOLD":
            return None
        current = previous[instrument_id]
        gross_room = self.max_gross - sum((abs(weight) for weight in previous.values()), Decimal(0))
        direction = Decimal(1) if side == "BUY" else Decimal(-1)
        target = current
        remaining = self.step
        if current * direction < 0:
            # Reduce/close the opposite exposure first; stop at flat unless reversal is permitted.
            reduction = min(remaining, abs(current))
            target = current + direction * reduction
            remaining -= reduction
            if not self.allow_reversal:
                remaining = Decimal(0)
        if remaining > 0:
            permitted = (self.directions in (DirectionPermission.BOTH, DirectionPermission.LONG_ONLY) if direction > 0
                         else self.directions in (DirectionPermission.BOTH, DirectionPermission.SHORT_ONLY))
            if permitted:
                room = min(remaining, self.max_per_instrument - abs(target), gross_room + abs(current) - abs(target))
                if room > 0:
                    target = target + direction * room
        if target == current:
            return None
        weights = dict(previous)
        weights[instrument_id] = target
        return TargetWeights(
            affected_instruments=(instrument_id,),
            decision_id=decision_id, model_id="coinfly-dual-compartment-v1", model_version=self.version,
            snapshot_id=snapshot_id, allocation_revision=allocation_revision, expires_at=expires_at,
            weights=tuple(InstrumentWeight(instrument_id=name, weight=weights[name]) for name in self.instrument_ids),
        )
