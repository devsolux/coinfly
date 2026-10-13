"""Evidence adequacy, paired frozen controls and seed uncertainty for v3 studies.

Three separate questions are kept apart on purpose:

* profitability/risk objectives (``optimization.OBJECTIVE_MAP``) say how a trial
  scored;
* behavior diagnostics (``EvidenceSummary``) say what the policy actually did;
* evidence sufficiency (``EvidenceRequirements``) says whether enough causal
  decisions, rewards and delivered credit exist to support ANY learning claim.

An inactive policy is never punished for holding: it is classified as
``INSUFFICIENT_EVIDENCE`` (no objective values) instead of receiving a favorable
zero-risk score. Thresholds are not universal constants: the experiment either
selects them explicitly or derives them from its own protocol through
``EvidenceRequirements.from_protocol``, and both paths are fingerprinted into the
study contract.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Normal quantile used by the default derivation (two-sided 95% interval).
_Z_95 = 1.959963984540054
# Below this per-pixel sigma, seeded uint8 sensory noise changes practically no
# pixel (P(|N(0,s)| >= 0.5) < 0.1%), so exploration is effectively off.
EFFECTIVE_NOISE_SIGMA = 0.15
EvidenceStatus = Literal["SUFFICIENT", "INSUFFICIENT_EVIDENCE"]
PAIRED_METRICS = ("net_return", "sortino", "maximum_drawdown", "turnover", "order_attempts", "hold_fraction",
                  "completed_round_trips", "credit_loss_fraction", "capacity_rejection_rate",
                  "training_saturation_peak", "compute_seconds", "max_rss_bytes")


@dataclass(frozen=True)
class EvidenceRequirements:
    """Deliberately selected, fingerprinted minimums for a learning claim.

    ``minimum_executed_decisions``: admitted BUY/SELL decisions the paper adapter
    actually executed during the evaluated phase. ``minimum_completed_round_trips``:
    conservative lower bound (min of BUY and SELL fills). ``minimum_nonzero_reward_pulses``:
    training reward windows with a nonzero shaped signal. ``minimum_credit_coverage``:
    fraction of those pulses whose allocations reached at least one retained tag.
    ``minimum_retained_credit_mass``: median decayed allocation mass of delivered
    pulses (1.0 = undecayed full allocation); guards against credit that is
    numerically annihilated by ``credit_tau_seconds`` while every count looks fine.
    """

    minimum_executed_decisions: int
    minimum_completed_round_trips: int
    minimum_nonzero_reward_pulses: int
    minimum_credit_coverage: float
    minimum_retained_credit_mass: float
    derivation: str = "explicit"

    def __post_init__(self) -> None:
        counts = (self.minimum_executed_decisions, self.minimum_completed_round_trips,
                  self.minimum_nonzero_reward_pulses)
        if any(type(value) is not int or value < 1 for value in counts):
            raise ValueError("evidence minimum counts must be positive integers")
        fractions = (self.minimum_credit_coverage, self.minimum_retained_credit_mass)
        if any(isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1 for value in fractions):
            raise ValueError("evidence fractions must lie in (0, 1]")
        if not self.derivation or len(self.derivation) > 200:
            raise ValueError("evidence derivation note required")

    @classmethod
    def from_protocol(cls, *, win_rate_precision: float, credit_coverage: float = 0.95,
                      retained_credit_mass: float = 0.05, rewards_per_decision: float = 1.0) -> EvidenceRequirements:
        """Derive counts from a declared estimation precision, not from a magic number.

        A learning claim compares per-decision outcomes between a learned and a
        frozen controller. Estimating a win rate p to within ``win_rate_precision``
        (half-width, 95% two-sided, worst case p = 0.5) needs
        n >= (z / (2 * precision))**2 executed decisions. Round trips are required
        at half that count because each closes one exposure episode; reward pulses
        at ``rewards_per_decision`` times the decision count.
        """
        if not 0.01 <= win_rate_precision <= 0.5 or not 0 < rewards_per_decision <= 1000:
            raise ValueError("win-rate precision must lie in [0.01, 0.5] and pulses per decision in (0, 1000]")
        decisions = int(math.ceil((_Z_95 / (2 * win_rate_precision)) ** 2))
        return cls(minimum_executed_decisions=decisions,
                   minimum_completed_round_trips=max(1, int(math.ceil(decisions / 2))),
                   minimum_nonzero_reward_pulses=max(1, int(math.ceil(decisions * rewards_per_decision))),
                   minimum_credit_coverage=credit_coverage, minimum_retained_credit_mass=retained_credit_mass,
                   derivation=f"95% win-rate half-width {win_rate_precision:g}; "
                              f"{rewards_per_decision:g} reward pulses per decision")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceSummary:
    """Per-phase behavior/evidence counters; every field is a measurement, none a score."""

    frames: int = 0
    decoded_orders: int = 0            # BUY/SELL decoded by the fixed decoder
    admitted_decisions: int = 0        # admitted by eligibility capacity
    executed_decisions: int = 0        # fills observed in reward windows (BUY + SELL)
    buy_fills: int = 0
    sell_fills: int = 0
    hold_decisions: int = 0
    capacity_denials: int = 0
    execution_side_denials: int = 0    # neural admission denials for other reasons
    exposed_windows: int = 0           # reward windows attributing returns to decisions
    accounted_windows: int = 0
    nonzero_reward_pulses: int = 0
    reward_allocations: int = 0
    delivered_pulses: int = 0          # nonzero pulses whose credit reached a retained tag
    missing_credit_pulses: int = 0
    expired_credit_decisions: int = 0
    unknown_credit_decisions: int = 0
    horizon_violations: int = 0
    sign_disagreements: int = 0        # shaped signal sign != booked net P&L sign
    retained_masses: list[float] = field(default_factory=list)
    credit_delays: list[float] = field(default_factory=list)
    ring_occupancy_peak: int = 0
    expired_tags: int = 0
    turnover_total: float = 0.0
    sigma_first: float | None = None
    sigma_last: float | None = None
    sigma_sum: float = 0.0
    ineffective_noise_frames: int = 0
    action_counts: dict[str, int] = field(default_factory=lambda: {"BUY": 0, "SELL": 0, "HOLD": 0})

    def record_decision(self, side: str, allowed: bool, reason: str | None, *, retained_tags: int,
                        expired_tags: int, sigma: float | None) -> None:
        if side not in ("BUY", "SELL", "HOLD"):
            raise ValueError("invalid decoded side")
        self.frames += 1
        self.action_counts[side] += 1
        if side == "HOLD":
            self.hold_decisions += 1
        else:
            self.decoded_orders += 1
            if allowed:
                self.admitted_decisions += 1
            elif reason == "eligibility_capacity":
                self.capacity_denials += 1
            else:
                self.execution_side_denials += 1
        self.ring_occupancy_peak = max(self.ring_occupancy_peak, int(retained_tags))
        self.expired_tags = max(self.expired_tags, int(expired_tags))
        if sigma is not None:
            if self.sigma_first is None:
                self.sigma_first = float(sigma)
            self.sigma_last = float(sigma)
            self.sigma_sum += float(sigma)
            self.ineffective_noise_frames += int(sigma < EFFECTIVE_NOISE_SIGMA)

    def record_window(self, *, buys: int, sells: int, credits: int, turnover: float) -> None:
        self.accounted_windows += 1
        self.buy_fills += buys
        self.sell_fills += sells
        self.executed_decisions += buys + sells
        self.exposed_windows += int(credits > 0)
        self.turnover_total += float(turnover)

    def record_reward(self, credit: dict[str, Any], *, requested: bool, signal: float, net_return: float,
                      horizon: float) -> None:
        allocations = int(credit.get("credit_allocations", 0))
        self.reward_allocations += allocations
        delay = float(credit.get("maximum_credit_delay_seconds", 0.0))
        if allocations:
            self.credit_delays.append(delay)
        self.horizon_violations += int(delay > horizon)
        self.expired_credit_decisions += int(credit.get("expired_credit_decisions", 0))
        self.unknown_credit_decisions += int(credit.get("unknown_credit_decisions", 0))
        if not requested:
            return
        self.nonzero_reward_pulses += 1
        if (signal > 0) != (net_return > 0) or (signal < 0) != (net_return < 0):
            self.sign_disagreements += 1
        mass = float(credit.get("credit_decay_mass", 0.0))
        missing = bool(credit.get("unassigned_reward") or credit.get("missing_credit_decisions"))
        self.missing_credit_pulses += int(missing)
        if mass > 0:
            self.delivered_pulses += 1
            self.retained_masses.append(mass)

    def report(self) -> dict[str, Any]:
        total = sum(self.action_counts.values())
        entropy = -sum((count / total) * math.log(count / total) for count in self.action_counts.values()
                       if count) if total else 0.0
        masses = sorted(self.retained_masses)
        delays = sorted(self.credit_delays)
        result: dict[str, Any] = {
            "frames": self.frames, "decoded_orders": self.decoded_orders,
            "admitted_decisions": self.admitted_decisions, "executed_decisions": self.executed_decisions,
            "buy_fills": self.buy_fills, "sell_fills": self.sell_fills,
            "completed_round_trips": min(self.buy_fills, self.sell_fills),
            "hold_decisions": self.hold_decisions,
            "hold_fraction": self.hold_decisions / total if total else 1.0,
            "action_counts": dict(self.action_counts), "action_entropy_nats": entropy,
            "capacity_denials": self.capacity_denials, "execution_side_denials": self.execution_side_denials,
            "accounted_windows": self.accounted_windows, "exposed_windows": self.exposed_windows,
            "exposure_fraction": self.exposed_windows / self.accounted_windows if self.accounted_windows else 0.0,
            "turnover_total": self.turnover_total,
            "nonzero_reward_pulses": self.nonzero_reward_pulses, "reward_allocations": self.reward_allocations,
            "delivered_pulses": self.delivered_pulses, "missing_credit_pulses": self.missing_credit_pulses,
            "credit_coverage": (self.delivered_pulses / self.nonzero_reward_pulses
                                if self.nonzero_reward_pulses else 0.0),
            "credit_loss_fraction": (self.missing_credit_pulses / self.nonzero_reward_pulses
                                     if self.nonzero_reward_pulses else 0.0),
            "expired_credit_decisions": self.expired_credit_decisions,
            "unknown_credit_decisions": self.unknown_credit_decisions,
            "credit_horizon_violations": self.horizon_violations,
            "sign_disagreements": self.sign_disagreements,
            "sign_disagreement_fraction": (self.sign_disagreements / self.nonzero_reward_pulses
                                           if self.nonzero_reward_pulses else 0.0),
            "retained_credit_mass": _quantiles(masses),
            "credit_delay_seconds": _quantiles(delays),
            "ring_occupancy_peak": self.ring_occupancy_peak, "expired_tags": self.expired_tags,
            "sensory_noise_sigma": {"first": self.sigma_first, "last": self.sigma_last,
                                    "mean": self.sigma_sum / self.frames if self.frames else None,
                                    "ineffective_frames": self.ineffective_noise_frames},
        }
        return result


def _quantiles(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {"count": len(values), "min": values[0], "median": statistics.median(values), "max": values[-1]}


def assess_evidence(training: dict[str, Any], validation: dict[str, Any],
                    requirements: EvidenceRequirements) -> dict[str, Any]:
    """Classify one fold/seed: learning evidence from training, evaluation evidence from validation.

    Reward pulses, credit coverage and retained mass describe whether anything
    could have been learned; executed decisions and round trips describe whether
    the evaluated policy produced enough causal decisions to be judged at all.
    """
    reasons = []
    if validation["executed_decisions"] < requirements.minimum_executed_decisions:
        reasons.append("validation_executed_decisions")
    if validation["completed_round_trips"] < requirements.minimum_completed_round_trips:
        reasons.append("validation_completed_round_trips")
    if training["nonzero_reward_pulses"] < requirements.minimum_nonzero_reward_pulses:
        reasons.append("training_nonzero_reward_pulses")
    if training["credit_coverage"] < requirements.minimum_credit_coverage:
        reasons.append("training_credit_coverage")
    median_mass = training["retained_credit_mass"]["median"]
    if median_mass is None or median_mass < requirements.minimum_retained_credit_mass:
        reasons.append("training_retained_credit_mass")
    status: EvidenceStatus = "SUFFICIENT" if not reasons else "INSUFFICIENT_EVIDENCE"
    return {"evidence_status": status, "evidence_reasons": reasons,
            "evidence_requirements": requirements.public_dict()}


def paired_deltas(learned: dict[str, float | None], frozen: dict[str, float | None]) -> dict[str, float | None]:
    """learned minus frozen on identical chronological data and seed; None when unmeasured."""
    deltas: dict[str, float | None] = {}
    for key in PAIRED_METRICS:
        a, b = learned.get(key), frozen.get(key)
        deltas[key] = None if a is None or b is None else float(a) - float(b)
    return deltas


def summarize_seeds(deltas: list[dict[str, float | None]]) -> dict[str, dict[str, float | int | None]]:
    """Uncertainty across seeds/folds: mean, sample std, min, max and the count improving."""
    summary: dict[str, dict[str, float | int | None]] = {}
    for key in PAIRED_METRICS:
        floats = [float(value) for item in deltas if (value := item.get(key)) is not None]
        if not floats:
            summary[key] = {"samples": 0, "mean": None, "std": None, "min": None, "max": None, "improving": 0}
            continue
        summary[key] = {"samples": len(floats), "mean": statistics.fmean(floats),
                        "std": statistics.stdev(floats) if len(floats) > 1 else 0.0,
                        "min": min(floats), "max": max(floats),
                        "improving": sum(1 for v in floats if v > 0)}
    return summary
