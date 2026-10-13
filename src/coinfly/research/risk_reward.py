"""Bounded net Sharpe/Sortino reinforcement; exact accounting stays in reward.py.

JIT handles non-differentiable, normalized research math only. No float result
from this module may be booked as money or used as an execution quantity.
"""

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

import numpy as np
from numba import njit

from coinfly.domain.decimal_math import financial_context
from coinfly.research.reward import RewardResult, RewardWindow, evaluate_reward, execution_notional


@njit(cache=True, nogil=True, fastmath=False)
def latency_residual(notionals: Any, adverse: Any, seconds: Any, sides: Any,
                     buy_bps: float, sell_bps: float, cap_bps: float) -> float:
    """Additional stress above already observed adverse implementation shortfall.

    This is an assumption, not an estimate of latency's causal contribution.
    All monetary inputs have already been divided by starting equity exactly.
    """
    cost = 0.0
    for i in range(len(notionals)):
        rate = buy_bps if sides[i] == 1 else sell_bps
        predicted = notionals[i] * min(rate * seconds[i], cap_bps) / 10000.0
        cost += max(predicted - max(adverse[i], 0.0), 0.0)
    return cost


@njit(cache=True, nogil=True, fastmath=False)
def risk_signal(history: Any, count: int, current: float, floor: float,
                sortino_weight: float, signal_scale: float, turnover: float,
                turnover_budget: float, turnover_penalty: float) -> Any:
    mean = 0.0
    m2 = 0.0
    downside = 0.0
    for i in range(count):
        value = history[i]
        delta = value - mean
        mean += delta / (i + 1)
        m2 += delta * (value - mean)
        downside += min(value, 0.0) ** 2
    volatility = math.sqrt(max(m2 / max(count - 1, 1), 0.0))
    down = math.sqrt(downside / max(count, 1))
    vol_denom, down_denom = max(volatility, floor), max(down, floor)
    # Per-step risk contribution rather than a cumulative ratio reward that can
    # reward inactivity indefinitely. The floor prevents warm-up explosions.
    signal = ((1.0 - sortino_weight) * current / vol_denom
              + sortino_weight * current / down_denom) / signal_scale
    # Regularize every unit of turnover so this parameter remains identifiable
    # among trials below the hard execution budget. Excess adds another charge;
    # neither term is booked as a fee or replaces the exact admission guard.
    penalty = turnover_penalty * (turnover + max(turnover - turnover_budget, 0.0))
    shaped = signal - penalty
    if not math.isfinite(shaped):
        # Clipping a NaN would silently yield +1 (maximum reward); fail instead.
        raise ValueError("nonfinite shaped reinforcement signal")
    return (max(-1.0, min(1.0, shaped)), mean / vol_denom,
            mean / down_denom, volatility, down, penalty)


@dataclass(frozen=True)
class RiskRewardConfig:
    window: int = 256
    interval_seconds: float = 1.0
    downside_floor: float = 0.0001
    sortino_weight: float = 0.75
    signal_scale: float = 4.0
    turnover_budget: float = 0.05
    turnover_penalty: float = 0.1
    # Explicit asymmetric stress scenario. Calibrate using training executions;
    # do not tune these downward in a profit-seeking hyperparameter study.
    buy_latency_bps_per_second: float = 1.0
    sell_latency_bps_per_second: float = 1.5
    latency_cap_bps: float = 20.0

    def __post_init__(self) -> None:
        if type(self.window) is not int or not 2 <= self.window <= 65536:
            raise ValueError("bounded rolling risk window required")
        floats = [value for key, value in asdict(self).items() if key != "window"]
        if any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in floats):
            raise ValueError("finite nonnegative risk parameters required")
        if (not 1e-6 <= self.interval_seconds <= 86400 or not 1e-12 <= self.downside_floor <= 1
                or not 0 <= self.sortino_weight <= 1 or not 1e-6 <= self.signal_scale <= 1e6
                or self.latency_cap_bps > 10000
                or max(self.turnover_budget, self.turnover_penalty, self.buy_latency_bps_per_second,
                       self.sell_latency_bps_per_second) > 1e6):
            raise ValueError("risk parameters outside declared bounds")


class RiskAdjustedReward:
    """Single-session, constant-space risk state; windows must be contiguous.

    Ratio diagnostics are per interval, unannualized, and use a zero target
    return. Exact net_pnl is retained unchanged; shaped scores are not returns.
    """

    def __init__(self, config: RiskRewardConfig = RiskRewardConfig()) -> None:
        self.config = config
        self.history = np.zeros(config.window, dtype=np.float64)
        self.count = self.cursor = 0
        self.last_end: datetime | None = None
        self.metrics: dict[str, float | int] = {}

    def __call__(self, outcome: RewardWindow) -> RewardResult:
        cfg = self.config
        if (abs((outcome.end_at - outcome.start_at).total_seconds() - cfg.interval_seconds) > 1e-6
                or (self.last_end is not None and outcome.start_at != self.last_end)):
            raise ValueError("risk returns require contiguous equal-duration accounting windows")
        exact = evaluate_reward(outcome)
        with localcontext(financial_context(512)):
            net = float(exact.net_pnl / outcome.capital)
            fee_return = float(exact.fees / outcome.capital)
            booked_fee_return = float(sum((item.actual_fee for item in outcome.executions
                if item.actual_fee is not None), Decimal(0)) / outcome.capital)
            notional, shortfall, latency, sides = [], [], [], []
            for item in outcome.executions:
                side = 1 if item.side == "BUY" else -1
                units = item.quantity * item.multiplier
                notional.append(float(units * item.reference_price / outcome.capital))
                shortfall.append(float(side * units * (item.fill_price - item.reference_price) / outcome.capital))
                latency.append((item.filled_at - item.submitted_at).total_seconds())
                sides.append(side)
        arrays = [np.asarray(values, dtype=np.float64) for values in (notional, shortfall, latency, sides)]
        if (not all(math.isfinite(v) for v in (net, fee_return, booked_fee_return))
                or any(not np.isfinite(a).all() for a in arrays)):
            raise ValueError("nonfinite normalized accounting input")
        if net <= -1:
            raise ValueError("insolvent research account: stop the episode")
        residual = float(latency_residual(arrays[0], arrays[1], arrays[2], arrays[3], cfg.buy_latency_bps_per_second,
                                         cfg.sell_latency_bps_per_second, cfg.latency_cap_bps))
        stressed = net - residual
        with localcontext(financial_context(512)):
            turnover = float(execution_notional(outcome) / outcome.capital)
        if not all(math.isfinite(v) and abs(v) <= 1e100 for v in (stressed, turnover, residual)):
            raise ValueError("research ratios outside finite arithmetic budget")
        # Every operation that can reject input precedes committing risk state.
        next_history = self.history.copy()
        next_history[self.cursor] = stressed
        count = min(self.count + 1, cfg.window)
        signal, sharpe, sortino, volatility, downside, penalty = risk_signal(
            next_history, count, stressed, cfg.downside_floor, cfg.sortino_weight,
            cfg.signal_scale, turnover, cfg.turnover_budget, cfg.turnover_penalty)
        self.history[:] = next_history
        self.cursor = (self.cursor + 1) % cfg.window
        self.count, self.last_end = count, outcome.end_at
        self.metrics = {"net_return": net, "fee_return": fee_return,
                        "booked_fee_return": booked_fee_return,
                        "stressed_return": stressed, "latency_residual_return": residual,
                        "turnover": turnover, "turnover_penalty": float(penalty), "signal": float(signal),
                        "rolling_sharpe": float(sharpe), "rolling_sortino": float(sortino),
                        "volatility": float(volatility), "downside_deviation": float(downside),
                        "risk_samples": count}
        return exact.model_copy(update={"normalized_reward": Decimal(str(signal)),
                                        "stimulus": "reward" if signal > 0 else "aversive" if signal < 0 else "none"})
