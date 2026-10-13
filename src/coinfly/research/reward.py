"""Causal, cost-aware reinforcement for the experimental fixed neural decoder.

There is no autograd policy or reward-driven action selection. This wrapper maps
an explicitly denominated accounting interval to the existing fixed DAN pulses.
"""

import math
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, localcontext
from threading import RLock
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from coinfly.domain.context import DomainError, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.research.neural.controller import FlyController

Amount = Annotated[ExactDecimal, Field(allow_inf_nan=False, max_digits=200, decimal_places=100)]


class TimedEvidence(FrozenModel):
    @field_validator("*", mode="after")
    @classmethod
    def aware(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("aware evidence timestamps required")
        return value


class TakerFees(TimedEvidence):
    venue: Literal["coinbase-exchange", "coinbase-advanced"]
    # Explicit account/market tier snapshot; no universal or silently stale rate.
    rate: ExactDecimal = Field(ge=0, lt=1, allow_inf_nan=False, max_digits=38, decimal_places=18)
    effective_at: datetime
    observed_at: datetime
    expires_at: datetime
    evidence_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def interval(self) -> "TakerFees":
        if not self.effective_at <= self.observed_at < self.expires_at:
            raise ValueError("invalid fee evidence interval")
        return self


class ExecutionCost(TimedEvidence):
    fill_id: str = Field(min_length=1, max_length=160)
    currency: str = Field(min_length=1, max_length=20)
    side: Literal["BUY", "SELL"]
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    multiplier: ExactDecimal = Field(default=Decimal(1), gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    reference_price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    fill_price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    decision_at: datetime
    reference_at: datetime
    reference_available_at: datetime
    submitted_at: datetime
    filled_at: datetime
    available_at: datetime
    fees: TakerFees
    actual_fee: ExactDecimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=200, decimal_places=100)

    @model_validator(mode="after")
    def causal(self) -> "ExecutionCost":
        if not (self.reference_at <= self.reference_available_at <= self.decision_at
                <= self.submitted_at <= self.filled_at <= self.available_at):
            raise ValueError("noncausal execution evidence")
        if not self.fees.observed_at <= self.submitted_at < self.fees.expires_at:
            raise ValueError("fee tier must be valid at order submission")
        return self


class DecisionCredit(TimedEvidence):
    """Causal exposure attribution from the paper ledger, not inferred from P&L sign."""

    decision_at: datetime
    fraction: ExactDecimal = Field(gt=0, le=1, allow_inf_nan=False, max_digits=38, decimal_places=18)


class RewardWindow(TimedEvidence):
    start_at: datetime
    end_at: datetime
    available_at: datetime
    currency: str = Field(min_length=1, max_length=20)
    # All prices, fees, P&L and capital must be in this one quote currency.
    pnl: Amount
    capital: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    basis: Literal["gross_reference", "net_execution"]
    executions: tuple[ExecutionCost, ...] = Field(default=(), max_length=100000)
    credits: tuple[DecisionCredit, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def complete_interval(self) -> "RewardWindow":
        if not self.start_at < self.end_at <= self.available_at:
            raise ValueError("invalid reward interval")
        if len({item.fill_id for item in self.executions}) != len(self.executions):
            raise ValueError("duplicate reward fill")
        if (len({item.decision_at for item in self.credits}) != len(self.credits)
                or any(item.decision_at >= self.end_at for item in self.credits)):
            raise ValueError("duplicate or future reward credit")
        with localcontext(financial_context(100)):
            if sum((item.fraction for item in self.credits), Decimal(0)) > 1:
                raise ValueError("reward credit fractions exceed one")
        for item in self.executions:
            if item.currency != self.currency:
                raise ValueError("reward costs require the same quote currency")
            if not (self.start_at < item.filled_at <= self.end_at and item.available_at <= self.available_at):
                raise ValueError("execution outside reward interval")
            if self.basis == "net_execution" and item.actual_fee is None:
                raise ValueError("net execution accounting requires booked fees")
        return self


class RewardResult(FrozenModel):
    net_pnl: Amount
    fees: Amount
    execution_shortfall: Amount
    normalized_reward: ExactDecimal = Field(ge=-1, le=1, allow_inf_nan=False)
    stimulus: Literal["none", "reward", "aversive"]
    maximum_latency_ms: Amount
    estimated_fee_count: int


def execution_notional(window: RewardWindow) -> Decimal:
    """Gross executed turnover, both sides, including terminal liquidations."""
    with localcontext(financial_context(512)):
        return sum((item.quantity * item.multiplier * item.fill_price for item in window.executions), Decimal(0))


def evaluate_reward(window: RewardWindow) -> RewardResult:
    """Subtract costs once; actual prices include spread/impact/latency slippage.

    gross_reference P&L uses decision-time reference prices; net_execution P&L
    already uses actual fills and booked fees. Neither includes deposits/withdrawals.
    Positive shortfall is adverse; price improvement remains negative, not clipped.
    For replay, the caller must supply execution prices from latency-delayed depth.
    """
    with localcontext(financial_context(512)):
        fees = shortfall = latency = Decimal(0)
        estimated = 0
        for item in window.executions:
            units = item.quantity * item.multiplier
            fees += (item.actual_fee if item.actual_fee is not None
                     else units * item.fill_price * item.fees.rate)
            estimated += item.actual_fee is None
            direction = Decimal(1) if item.side == "BUY" else Decimal(-1)
            shortfall += direction * units * (item.fill_price - item.reference_price)
            elapsed = item.filled_at - item.submitted_at
            milliseconds = (Decimal(elapsed.days * 86400 + elapsed.seconds) * 1000
                            + Decimal(elapsed.microseconds) / 1000)
            latency = max(latency, milliseconds)
        net = window.pnl - fees - shortfall if window.basis == "gross_reference" else window.pnl
        with localcontext(financial_context(40)):
            normalized = max(Decimal(-1), min(Decimal(1), net / window.capital))
        return RewardResult(net_pnl=net, fees=fees, execution_shortfall=shortfall,
                            normalized_reward=normalized, maximum_latency_ms=latency,
                            estimated_fee_count=estimated,
                            stimulus="reward" if net > 0 else "aversive" if net < 0 else "none")


class ObservationEvidence(TimedEvidence):
    decision_at: datetime
    features_event_through: datetime
    features_available_through: datetime
    preprocessing_fitted_through: datetime
    partition: Literal["train", "evaluation"]
    evaluation_start: datetime | None = None


class CostAwareController:
    """One chronological experiment session, serialized with bounded diagnostics.

    Evidence comes from the experiment/data owner; timestamps cannot certify the
    provenance of arbitrary RGB pixels. A new wrapper is required after restore;
    replay the experiment's causal cursor with its matching neural checkpoint.
    """

    def __init__(self, controller: FlyController,
                 evaluator: Callable[[RewardWindow], RewardResult] = evaluate_reward) -> None:
        self.controller = controller
        self.evaluator = evaluator
        # Share the real controller's reentrant lock. An experimental controller
        # can enter through its audited-frame boundary before calling this
        # wrapper; separate locks would create an inverse acquisition order.
        self._lock = getattr(controller, "_lock", None) or RLock()
        self._last_observation: datetime | None = None
        self._last_reward_end: datetime | None = None
        self.leakage_rejections = 0
        self.actions = {"BUY": 0, "SELL": 0, "HOLD": 0}

    def observe(self, rgb: Any, evidence: ObservationEvidence,
                outcome: RewardWindow | None = None) -> dict[str, Any]:
        with self._lock:
            decision = evidence.decision_at
            if (evidence.features_event_through > evidence.features_available_through
                    or evidence.features_available_through > decision
                    or evidence.preprocessing_fitted_through > decision
                    or (self._last_observation is not None and decision <= self._last_observation)
                    or (evidence.partition == "evaluation" and self.controller.s.learning)
                    or (evidence.partition == "evaluation" and (evidence.evaluation_start is None
                        or not evidence.preprocessing_fitted_through < evidence.evaluation_start <= decision))
                    or (outcome is not None and (outcome.available_at > decision
                        or (self._last_reward_end is not None and outcome.start_at < self._last_reward_end)))):
                self.leakage_rejections += 1
                raise DomainError("RESEARCH_INFORMATION_LEAKAGE")
            reward = self.evaluator(outcome) if outcome is not None else None
            result = self.controller.observe(rgb, reward.stimulus if reward else "none")
            self._last_observation = decision
            if outcome is not None:
                self._last_reward_end = outcome.end_at
            self.actions[result["side"]] += 1
            total = sum(self.actions.values())
            entropy = -sum((count / total) * math.log(count / total)
                           for count in self.actions.values() if count)
            delay = decision - outcome.end_at if outcome is not None else None
            delay_us = ((delay.days * 86400 + delay.seconds) * 1000000 + delay.microseconds
                        if delay is not None else None)
            return {**result, "reward": reward.model_dump(mode="json") if reward else None,
                    "causality": {"leakage_rejections": self.leakage_rejections,
                                  "decision_at": decision.isoformat(), "partition": evidence.partition,
                                  "reward_delay_microseconds": delay_us},
                    "exploration": {"strategy": "retained-neural-dynamics-fixed-decoder",
                                    "action_counts": dict(self.actions), "action_entropy_nats": entropy,
                                    "hold_fraction": self.actions["HOLD"] / total,
                                    "policy_gradients": "not_applicable"}}
