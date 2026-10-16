"""Purged chronological full-graph studies, with an untouched final holdout.

The qualified host supplies market data and a PAPER environment. No synthetic
default, exchange client, native attestation bypass, or scripted action selector.
"""

import hashlib
import math
import sqlite3
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from optuna import TrialPruned

from coinfly.domain.decimal_math import financial_context
from coinfly.research.evidence import (
    EvidenceRequirements,
    EvidenceSummary,
    assess_evidence,
    paired_deltas,
    summarize_seeds,
)
from coinfly.research.frames import AuditedFrame
from coinfly.research.neural.experimental_plasticity import EligibilityCapacityError, PlasticityConfig
from coinfly.research.reward import RewardWindow, evaluate_reward, execution_notional
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.training import TrainingController, TrainingProfile
from coinfly.research.turnover import TURNOVER_LIMIT, turnover_admissible

SATURATION_LIMIT = 0.01
ABSOLUTE_SATURATION_TIME_LIMIT = 0.15
CAPACITY_REJECTION_THRESHOLD = 0.10
CAPACITY_PENALTY_WEIGHT = 1.0
MISSING_CREDIT_ALERT_FRACTION = 0.01
OBJECTIVE_MAP = (
    ("capacity_penalized_net_sortino", "maximize"),
    ("validation_maximum_drawdown", "minimize"),
    ("training_near_bound_edge_time_occupancy", "minimize"),
    ("training_passive_erasure_rate", "minimize"),
)
# Shared by the sampler and schema-4 contract. Conditional age also covers H/tau.
REINFORCEMENT_COORDINATES = (
    ("positive_return_threshold", .0001, .01, True),
    ("positive_gain", 1., 2., False),
    ("fee_shock_gain", .25, 1., False),
    ("credit_tau_seconds", .5, 150., True),
    ("maximum_credit_age_seconds", 5., 150., True),
)
PLASTICITY_CUMULATIVE_KEYS = (
    "bins", "observed_neural_seconds", "near_saturation_peak", "near_saturation_seconds",
    "absolute_saturation_seconds", "absolute_saturation_edge_seconds", "severe_saturation_seconds",
    "homeostatic_erasure_integral", "maximum_bin_homeostatic_loss",
)


def implementation_files() -> tuple[str, ...]:
    """Every module that can change study behavior, plus the attested kernel source."""
    root = Path(__file__).parent
    names = sorted(str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*.py")
                   if "__pycache__" not in path.parts)
    return (*names, "neural/kernel.cpp")


def implementation_fingerprint() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in implementation_files():
        digest.update(name.encode())
        digest.update(hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class StudyProtocol:
    """Versioned study contract. v2 is frozen; behavior changes require a new profile."""

    profile_id: str
    implementation_revision: int
    schema: int
    default_study_name: str
    evidence: EvidenceRequirements | None = None
    paired_control: bool = False
    prune_on_horizon_violation: bool = False

    def __post_init__(self) -> None:
        if self.profile_id == V2_PROFILE_ID:
            if self.evidence is not None or self.paired_control or self.prune_on_horizon_violation:
                raise ValueError("the frozen v2 contract cannot carry v3 evidence semantics")
        elif self.profile_id == V3_PROFILE_ID:
            if self.evidence is None:
                raise ValueError("v3 requires explicitly selected evidence requirements")
        else:
            raise ValueError("unknown experimental profile")

    def public_dict(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, "implementation_revision": self.implementation_revision,
                "schema": self.schema, "evidence": None if self.evidence is None else self.evidence.public_dict(),
                "paired_control": self.paired_control,
                "prune_on_horizon_violation": self.prune_on_horizon_violation}


V2_PROFILE_ID = "malecns-risk-adjusted-experiment-v2"
V3_PROFILE_ID = "malecns-risk-adjusted-experiment-v3"
V2_PROTOCOL = StudyProtocol(V2_PROFILE_ID, 4, 4, "malecns-risk-v2-capacity")


def v3_protocol(evidence: EvidenceRequirements, *, paired_control: bool = True) -> StudyProtocol:
    return StudyProtocol(V3_PROFILE_ID, 5, 5, "malecns-risk-v3-evidence", evidence=evidence,
                         paired_control=paired_control, prune_on_horizon_violation=True)


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime


def walk_forward(start: datetime, holdout_start: datetime, *, folds: int,
                 minimum_train: timedelta, validation: timedelta, purge: timedelta) -> tuple[Fold, ...]:
    """Embargo >= feature lookback, reward horizon and maximum report delay."""
    if (start.tzinfo is None or holdout_start.tzinfo is None or not start < holdout_start
            or type(folds) is not int or folds < 1 or minimum_train <= timedelta(0)
            or validation <= timedelta(0) or purge <= timedelta(0)):
        raise ValueError("invalid chronological split")
    result = []
    first_validation = start + minimum_train + purge
    for index in range(folds):
        val_start = first_validation + index * validation
        val_end = val_start + validation
        if val_end > holdout_start - purge:
            raise ValueError("development folds overlap the purged final holdout")
        result.append(Fold(start, val_start - purge, val_start, val_end))
    return tuple(result)


class PaperEnvironment(Protocol):
    """Trusted replay adapter: no account, live endpoints, or replacement trades.

    Fresh capital and a flat book at each phase start. Mark all inventory each
    interval, and liquidate/charge terminal inventory in finish(). Include every
    fill, fee and funding cost. Feature fitting uses only the training prefix.
    stream() advances only after execute()/reject() receives the decoded action.
    Credit names originating admitted BUY/SELL decisions, including subsequent
    inventory returns; HOLD frames and capacity-denied orders have no tag.
    execute() must use turnover_admissible with reserved worst-case gross notional
    before each order. A rejected action remains rejected; no replacement trade.
    """

    execution_realm: Literal["paper"]

    def stream(self) -> Iterable[tuple[AuditedFrame, RewardWindow | None]]: ...
    def execute(self, side: str, decision_at: datetime) -> None: ...
    def reject(self, side: str, decision_at: datetime, reason: str) -> None:
        """Record the rejected decoded action and advance replay without an order."""
        ...
    def finish(self) -> RewardWindow | None: ...


@dataclass(frozen=True)
class StudyResources:
    controller_factory: Callable[[TrainingProfile, RiskRewardConfig], TrainingController]
    environment_factory: Callable[[Fold, Literal["train", "evaluation"], int], PaperEnvironment]
    # Immutable host/data/preprocessing/fee/latency provenance, recorded with study.
    evidence_id: str
    dataset_id: str = ""
    # Declared from development execution timing, never estimated from holdout.
    # Includes exposure attribution age, reward batching and report delivery.
    reward_credit_horizon_seconds: float | None = None


def required_credit_horizon(resources: StudyResources, reward: RiskRewardConfig) -> float:
    horizon = resources.reward_credit_horizon_seconds
    if horizon is None or isinstance(horizon, bool) or not math.isfinite(horizon) or horizon <= 0:
        raise ValueError("host must declare a finite decision-to-reward credit horizon")
    required = max(horizon, reward.interval_seconds)
    if required > 150:
        raise ValueError("declared reward timing cannot fit the 150-second credit envelope")
    return required


def _interval_micros(start: datetime, end: datetime) -> tuple[int, int]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("an aware chronological interval is required")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    def micros(value: datetime) -> int:
        elapsed = value - epoch
        return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
    return micros(start), micros(end)


def assert_development_unsealed(storage: Path, dataset_id: str,
                                intervals: Iterable[tuple[datetime, datetime]]) -> None:
    """Consumed holdouts cannot become another study's development data."""
    if not dataset_id:
        raise ValueError("stable dataset identity required")
    bounds = [_interval_micros(start, end) for start, end in intervals]
    with closing(sqlite3.connect(storage)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                  "AND name='coinfly_holdouts'").fetchone():
            return
        for begin, finish in bounds:
            if connection.execute("SELECT 1 FROM coinfly_holdouts WHERE dataset_id=? "
                                  "AND start_us < ? AND end_us > ? LIMIT 1",
                                  (dataset_id, finish, begin)).fetchone():
                raise ValueError("development data overlaps a sealed dataset holdout")


def claim_holdout(storage: Path, dataset_id: str, start: datetime, end: datetime,
                  study: str, trial: int) -> None:
    """Atomic cross-study exclusion of overlapping holdouts in this study database."""
    if not dataset_id:
        raise ValueError("stable dataset identity required")
    begin, finish = _interval_micros(start, end)
    with closing(sqlite3.connect(storage)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS coinfly_holdouts "
                           "(dataset_id TEXT, start_us INTEGER, end_us INTEGER, study TEXT, trial INTEGER)")
        exists = connection.execute("SELECT 1 FROM coinfly_holdouts WHERE dataset_id=? "
                                    "AND start_us < ? AND end_us > ? LIMIT 1", (dataset_id, finish, begin)).fetchone()
        if exists:
            raise ValueError("this dataset holdout overlaps an already claimed interval")
        connection.execute("INSERT INTO coinfly_holdouts VALUES (?, ?, ?, ?, ?)",
                           (dataset_id, begin, finish, study, trial))


def performance(returns: list[float], *, floor: float = 0.0001) -> dict[str, float | int]:
    """Unannualized Sortino and compounded MDD from UNSHAPED net equity returns."""
    if len(returns) < 2 or not 0 < floor <= 1:
        raise ValueError("at least two equal-interval returns and a positive floor required")
    values = np.asarray(returns, dtype=np.float64)
    if not np.isfinite(values).all() or np.any(values <= -1) or np.any(values > 1e100):
        raise ValueError("invalid or insolvent validation return")
    downside = float(np.sqrt(np.mean(np.minimum(values, 0) ** 2)))
    mean = float(values.mean())
    # Log equity includes initial equity in the running peak; the first loss
    # therefore contributes to drawdown. Do not reset the peak at fold tail.
    log_equity = np.cumsum(np.log1p(values))
    peaks = np.maximum.accumulate(np.maximum(log_equity, 0))
    drawdown = float(np.max(-np.expm1(log_equity - peaks)))
    log_return = float(log_equity[-1])
    if log_return > 700:
        raise ValueError("unrealistic compounded equity overflow")
    return {"samples": len(returns), "sortino": mean / max(downside, floor),
            "maximum_drawdown": drawdown, "net_return": math.expm1(log_return),
            "downside_samples": int(np.count_nonzero(values < 0)), "mean_return": mean}


@dataclass
class ExecutionDiagnostics:
    order_attempts: int = 0
    capacity_order_rejections: int = 0
    other_order_rejections: int = 0
    hold_decisions: int = 0
    reward_pulses_requested: int = 0
    missing_credit_pulses: int = 0
    credit_horizon_violations: int = 0
    maximum_observed_credit_delay_seconds: float = 0.

    def record_order(self, side: str, allowed: bool, reason: str | None) -> None:
        if (side not in ("BUY", "SELL", "HOLD") or type(allowed) is not bool
                or (allowed and reason is not None) or (not allowed and not reason)):
            raise ValueError("invalid decoded action/admission diagnostic")
        if side == "HOLD":
            self.hold_decisions += 1
            return
        self.order_attempts += 1
        if not allowed:
            if reason == "eligibility_capacity":
                self.capacity_order_rejections += 1
            else:
                self.other_order_rejections += 1

    def record_reward(self, *, missing: bool, maximum_delay: float, horizon: float,
                      requested: bool) -> None:
        if not math.isfinite(maximum_delay) or maximum_delay < 0 or not math.isfinite(horizon) or horizon <= 0:
            raise ValueError("invalid credit-delay diagnostic")
        self.maximum_observed_credit_delay_seconds = max(self.maximum_observed_credit_delay_seconds, maximum_delay)
        self.credit_horizon_violations += int(maximum_delay > horizon)
        if requested:
            self.reward_pulses_requested += 1
            self.missing_credit_pulses += int(missing)

    def report(self) -> dict[str, float | int | bool]:
        return {**asdict(self),
                "capacity_order_rejection_rate": self.capacity_order_rejections / self.order_attempts if self.order_attempts else 0.,
                "missing_credit_fraction": self.missing_credit_pulses / self.reward_pulses_requested if self.reward_pulses_requested else 0.,
                # Exact comparison at the contract fraction; suppressed pulses stay in the denominator.
                "missing_credit_alert": _alert(self.missing_credit_pulses, self.reward_pulses_requested)}


def _alert(missing: int, requested: int) -> bool:
    numerator, denominator = Decimal(str(MISSING_CREDIT_ALERT_FRACTION)).as_integer_ratio()
    return missing * denominator > requested * numerator


def capacity_penalty(rejection_rate: float) -> float:
    """C1-continuous additive fitness cost; never relabel this as booked return."""
    if not math.isfinite(rejection_rate) or not 0 <= rejection_rate <= 1:
        raise ValueError("bounded capacity order-rejection rate required")
    excess = max(rejection_rate - CAPACITY_REJECTION_THRESHOLD, 0.) / (1 - CAPACITY_REJECTION_THRESHOLD)
    return CAPACITY_PENALTY_WEIGHT * excess * excess


class AbsoluteSaturationError(ValueError):
    def __init__(self, audit: dict[str, Any]) -> None:
        self.audit = dict(audit)
        super().__init__("absolute-saturation epoch time exceeds 15%")


def validate_epoch_constraints(audit: dict[str, Any]) -> None:
    """Called on completed epochs, not transient prefixes that may recover."""
    fraction = audit["absolute_saturation_time_fraction"]
    if (isinstance(fraction, bool) or not isinstance(fraction, (float, int))
            or not math.isfinite(fraction) or not 0 <= fraction <= 1):
        raise ValueError("invalid absolute-saturation epoch fraction")
    if fraction > ABSOLUTE_SATURATION_TIME_LIMIT:
        raise AbsoluteSaturationError(audit)


@dataclass
class PhaseResult:
    returns: list[float]
    turnover_peak: Decimal = Decimal(0)
    turnover_violations: int = 0
    unassigned_credit_windows: int = 0
    capacity_rejections: int = 0
    execution: ExecutionDiagnostics = field(default_factory=ExecutionDiagnostics)
    evidence: EvidenceSummary = field(default_factory=EvidenceSummary)
    compute_seconds: float = 0.0


def analyze_plasticity(diagnostics: dict[str, float | int], *, learning: bool,
                      maximum_loss_per_second: float = .02) -> dict[str, float]:
    """Strict cumulative epoch audit; omitted/nonfinite telemetry is not zero."""
    if not learning:
        return {"training_saturation_peak": 0., "training_saturation_mean": 0.,
                "absolute_saturation_time_fraction": 0., "absolute_saturation_edge_fraction": 0.,
                "severe_saturation_time_fraction": 0., "homeostatic_erasure_rate": 0.}
    if any(k not in diagnostics or isinstance(diagnostics[k], bool)
           or not math.isfinite(diagnostics[k]) or diagnostics[k] < 0 for k in PLASTICITY_CUMULATIVE_KEYS):
        raise ValueError("incomplete/nonfinite plasticity epoch diagnostics")
    seconds = float(diagnostics["observed_neural_seconds"])
    if (seconds <= 0 or type(diagnostics["bins"]) is not int or diagnostics["bins"] < 1
            or not 0 < maximum_loss_per_second <= .1):
        raise ValueError("nonempty bounded plasticity epoch required")
    for key in ("near_saturation_seconds", "absolute_saturation_seconds",
                "absolute_saturation_edge_seconds", "severe_saturation_seconds"):
        if diagnostics[key] > seconds + 1e-8:
            raise ValueError("plasticity occupancy exceeds epoch duration")
    rate_limit = -math.log1p(-maximum_loss_per_second)
    if (diagnostics["near_saturation_peak"] > 1
            or diagnostics["homeostatic_erasure_integral"] > rate_limit * seconds + 1e-8
            or diagnostics["maximum_bin_homeostatic_loss"] > -math.expm1(-rate_limit * .0100001) + 1e-12):
        raise ValueError("homeostatic erasure exceeded its declared time budget")
    return {"training_saturation_peak": float(diagnostics["near_saturation_peak"]),
            "training_saturation_mean": min(1., diagnostics["near_saturation_seconds"] / seconds),
            "absolute_saturation_time_fraction": min(1., diagnostics["absolute_saturation_seconds"] / seconds),
            "absolute_saturation_edge_fraction": min(1., diagnostics["absolute_saturation_edge_seconds"] / seconds),
            "severe_saturation_time_fraction": min(1., diagnostics["severe_saturation_seconds"] / seconds),
            "homeostatic_erasure_rate": diagnostics["homeostatic_erasure_integral"] / seconds}


@dataclass
class PlasticityEpochAudit:
    """Bounded snapshot detects counter resets hidden by an increasing clock."""

    maximum_loss_per_second: float = .02
    previous: dict[str, float | int] = field(default_factory=dict)

    def observe(self, diagnostics: dict[str, float | int]) -> None:
        analyze_plasticity(diagnostics, learning=True, maximum_loss_per_second=self.maximum_loss_per_second)
        for key in PLASTICITY_CUMULATIVE_KEYS:
            if diagnostics[key] < self.previous.get(key, 0):
                raise ValueError(f"plasticity cumulative counter reset: {key}")
        seconds = diagnostics["observed_neural_seconds"] - self.previous.get("observed_neural_seconds", 0)
        bins = diagnostics["bins"] - self.previous.get("bins", 0)
        if seconds <= 0 or bins <= 0 or seconds > bins * .0100001 + 1e-8:
            raise ValueError("plasticity epoch telemetry stopped or reset during training")
        for key in ("near_saturation_seconds", "absolute_saturation_seconds",
                    "absolute_saturation_edge_seconds", "severe_saturation_seconds"):
            if diagnostics[key] - self.previous.get(key, 0) > seconds + 1e-8:
                raise ValueError("plasticity occupancy exceeds frame duration")
        integral = diagnostics["homeostatic_erasure_integral"] - self.previous.get("homeostatic_erasure_integral", 0)
        if integral > -math.log1p(-self.maximum_loss_per_second) * seconds + 1e-8:
            raise ValueError("passive erasure exceeds frame budget")
        self.previous = {key: diagnostics[key] for key in PLASTICITY_CUMULATIVE_KEYS}

    def finish(self) -> dict[str, float]:
        audit = analyze_plasticity(self.previous, learning=True,
                                  maximum_loss_per_second=self.maximum_loss_per_second)
        validate_epoch_constraints(audit)
        return audit


def _phase(controller: TrainingController, environment: PaperEnvironment, *, start: datetime,
           end: datetime, partition: Literal["train", "evaluation"], max_frames: int) -> PhaseResult:
    if environment.execution_realm != "paper":
        raise ValueError("only paper experiments are accepted")
    if not callable(getattr(environment, "reject", None)):
        raise ValueError("paper adapter must implement explicit admission rejection")
    cursor = start
    last_equity: Decimal | None = None
    currency: str | None = None
    phase = PhaseResult([])
    epoch = PlasticityEpochAudit(controller.profile.plasticity.maximum_homeostatic_loss_per_second)

    def account(window: RewardWindow) -> None:
        nonlocal cursor, last_equity, currency
        if (window.start_at != cursor or window.end_at > end
                or abs((window.end_at - window.start_at).total_seconds()
                       - controller.risk_reward.config.interval_seconds) > 1e-6
                or (last_equity is not None and window.capital != last_equity)
                or (currency is not None and currency != window.currency)
                or window.basis != "net_execution"):
            raise ValueError("incomplete/noncontiguous booked net-equity validation ledger")
        result = evaluate_reward(window)
        notional = execution_notional(window)
        phase.turnover_violations += int(not turnover_admissible(window.capital, Decimal(0), notional))
        with localcontext(financial_context(512)):
            value = float(result.net_pnl / window.capital)
            last_equity = window.capital + result.net_pnl
            phase.turnover_peak = max(phase.turnover_peak, notional / window.capital)
            turnover = float(notional / window.capital)
        if not math.isfinite(value) or value <= -1:
            raise ValueError("insolvent/nonfinite paper account")
        phase.returns.append(value)
        phase.evidence.record_window(buys=sum(1 for item in window.executions if item.side == "BUY"),
                                     sells=sum(1 for item in window.executions if item.side == "SELL"),
                                     credits=len(window.credits), turnover=turnover)
        cursor, currency = window.end_at, window.currency

    for count, (frame, outcome) in enumerate(environment.stream(), 1):
        evidence = frame.evidence
        if (count > max_frames or not start <= evidence.decision_at < end
                or evidence.partition != partition
                or (partition == "evaluation" and evidence.evaluation_start != start)):
            raise ValueError("frame budget or chronological phase boundary violated")
        if outcome is not None:
            account(outcome)
        result = controller.run_frame(frame, outcome)
        if partition == "train" and controller.s.learning:
            epoch.observe(controller.brain.plasticity_diagnostics)
        horizon = max(controller.profile.reward_credit_horizon_seconds, controller.risk_reward.config.interval_seconds)
        if partition == "train" and controller.s.learning and outcome is not None:
            credit = result["training"]["eligibility"]
            missing = bool(credit["unassigned_reward"] or credit["missing_credit_decisions"])
            risk = result["training"]["risk"]
            requested = float(risk["signal"]) != 0
            phase.execution.record_reward(missing=missing, maximum_delay=float(credit["maximum_credit_delay_seconds"]),
                horizon=horizon, requested=requested)
            phase.unassigned_credit_windows += int(missing and requested)
            phase.evidence.record_reward(dict(credit), requested=requested, signal=float(risk["signal"]),
                                         net_return=float(risk.get("net_return", 0.0)), horizon=horizon)
        admission = result["execution_admission"]
        phase.execution.record_order(result["side"], admission["allowed"], admission["reason"])
        training = result.get("training", {})
        eligibility = training.get("eligibility", {})
        phase.evidence.record_decision(result["side"], admission["allowed"], admission["reason"],
                                       retained_tags=int(eligibility.get("retained_tags", 0)),
                                       expired_tags=int(eligibility.get("expired_tags", 0)),
                                       sigma=float(training["sensory_noise_sigma"])
                                       if "sensory_noise_sigma" in training else None)
        phase.compute_seconds += float(result.get("compute_seconds", 0.0))
        if admission["allowed"]:
            environment.execute(result["side"], evidence.decision_at)
        else:
            phase.capacity_rejections += int(admission["reason"] == "eligibility_capacity" and result["side"] != "HOLD")
            environment.reject(result["side"], evidence.decision_at, admission["reason"])
    tail = environment.finish()
    if tail is not None:
        account(tail)
    if cursor != end:
        raise ValueError("missing accounting intervals or terminal liquidation costs")
    if partition == "train" and controller.s.learning:
        epoch.finish()  # Hard prune only the completed epoch, before any validation.
    return phase


def _learning(controller: TrainingController) -> bool:
    return controller.s.learning


def evaluate_fold(resources: StudyResources, fold: Fold, profile: TrainingProfile,
                  reward: RiskRewardConfig, *, max_frames: int = 1000000,
                  learning: bool = True) -> dict[str, Any]:
    if profile.reward_credit_horizon_seconds < required_credit_horizon(resources, reward):
        raise ValueError("profile credit horizon does not cover the declared reward timing")
    controller = resources.controller_factory(profile, reward)
    if (not isinstance(controller, TrainingController) or controller.profile != profile
            or controller.risk_reward.config != reward or not _learning(controller)
            or controller.brain.n != 166700 or len(controller.brain.post) != 25582938
            or controller.brain.cursor != 0):
        raise ValueError("fresh qualified full MaleCNS TrainingController required")
    controller.brain.paths.dataset.assert_current()
    graph_before = {name: hashlib.sha256(getattr(controller.brain, name).tobytes()).hexdigest()
                    for name in ("ids", "ptr", "post")}
    if not learning:
        controller.freeze_learning()
    train_environment = resources.environment_factory(fold, "train", profile.seed)
    train = _phase(controller, train_environment, start=fold.train_start, end=fold.train_end,
                   partition="train", max_frames=max_frames)
    plasticity = dict(controller.brain.plasticity_diagnostics)
    audit = analyze_plasticity(plasticity, learning=learning,
        maximum_loss_per_second=profile.plasticity.maximum_homeostatic_loss_per_second)
    validate_epoch_constraints(audit)
    training_actions = dict(controller.session.actions)  # begin_evaluation replaces the session
    controller.begin_evaluation()
    before = hashlib.sha256(controller.brain.weight.tobytes()).hexdigest()
    environment = resources.environment_factory(fold, "evaluation", profile.seed)
    validation = _phase(controller, environment, start=fold.validation_start, end=fold.validation_end,
                        partition="evaluation", max_frames=max_frames)
    after = hashlib.sha256(controller.brain.weight.tobytes()).hexdigest()
    if before != after or controller.s.learning or not controller.brain.weights_frozen:
        raise ValueError("validation changed learned weights")
    graph_after = {name: hashlib.sha256(getattr(controller.brain, name).tobytes()).hexdigest()
                   for name in ("ids", "ptr", "post")}
    if graph_before != graph_after:
        raise ValueError("retained graph identity changed during the experiment")
    return {**performance(validation.returns), **audit, "seed": profile.seed, "learned": learning,
            "profile_id": profile.profile_id, "evidence_id": resources.evidence_id,
            "turnover_peak": str(max(train.turnover_peak, validation.turnover_peak)),
            "turnover_violations": train.turnover_violations + validation.turnover_violations,
            "capacity_rejections": train.capacity_rejections,
            "execution_diagnostics": train.execution.report(),
            "validation_execution_diagnostics": validation.execution.report(),
            "training_evidence": train.evidence.report(), "validation_evidence": validation.evidence.report(),
            "unassigned_credit_windows": train.unassigned_credit_windows,
            "weight_sha256": after, "graph_identity": graph_after, "actions": dict(controller.session.actions),
            "training_actions": training_actions,
            "compute_seconds": train.compute_seconds + validation.compute_seconds,
            "max_rss_bytes": _max_rss_bytes(),
            "plasticity": plasticity,
            "native_build": dict(controller.brain.build)}


def _max_rss_bytes() -> int | None:
    """Process peak RSS in bytes (Linux reports KiB, macOS bytes); None where unmeasured."""
    if sys.platform == "win32":
        return None  # no POSIX resource module: reported as unmeasured rather than a guess
    else:
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, AttributeError, OSError, ValueError):
            return None
        return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def comparable_metrics(evaluation: dict[str, Any]) -> dict[str, float | None]:
    """Flat, unit-consistent metrics for learned-versus-frozen paired deltas."""
    validation = evaluation.get("validation_evidence", {})
    training = evaluation.get("training_evidence", {})
    execution = evaluation.get("execution_diagnostics", {})

    def number(value: Any) -> float | None:
        return None if value is None else float(value)

    return {"net_return": number(evaluation.get("net_return")), "sortino": number(evaluation.get("sortino")),
            "maximum_drawdown": number(evaluation.get("maximum_drawdown")),
            "turnover": number(evaluation.get("turnover_peak")),
            "order_attempts": number(validation.get("decoded_orders")),
            "hold_fraction": number(validation.get("hold_fraction")),
            "completed_round_trips": number(validation.get("completed_round_trips")),
            "credit_loss_fraction": number(training.get("credit_loss_fraction")),
            "capacity_rejection_rate": number(execution.get("capacity_order_rejection_rate")),
            "training_saturation_peak": number(evaluation.get("training_saturation_peak")),
            "compute_seconds": number(evaluation.get("compute_seconds")),
            "max_rss_bytes": number(evaluation.get("max_rss_bytes"))}


def trial_constraints(trial: Any) -> tuple[float, ...]:
    """Fail closed for incompatible or unmeasured trials; all constraints must be <= 0."""
    constraints = getattr(trial, "constraints", {})
    keys = ("turnover", "saturation", "credit", "absolute_saturation_time")
    if (not isinstance(constraints, dict) or set(constraints) != set(keys)
            or any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in constraints.values())):
        return (1., 1., 1., 1.)
    return tuple(float(constraints[key]) for key in keys)


def objective(trial: Any, resources: StudyResources, folds: tuple[Fold, ...], seeds: tuple[int, ...],
              fixed_reward: RiskRewardConfig, *, protocol: StudyProtocol = V2_PROTOCOL) -> tuple[float, float, float, float]:
    """Capacity-penalized Sortino up; MDD, saturation and passive erasure down.

    Fees, slippage assumptions, timing, feature definitions and decoder thresholds
    are fixed outside this search. A fresh graph is trained for every fold/seed.
    Under a v3 protocol, insufficient evidence prunes the trial (no objective
    values) and a paired frozen control is recorded for every fold/seed.
    """
    if Decimal(str(fixed_reward.turnover_budget)) != TURNOVER_LIMIT:
        raise ValueError("the study's 5% turnover budget cannot be tuned or relaxed")
    horizon = required_credit_horizon(resources, fixed_reward)
    coordinates: dict[str, float] = {}
    for name, low, high, logarithmic in REINFORCEMENT_COORDINATES:
        if name == "maximum_credit_age_seconds":
            low = max(low, coordinates["credit_tau_seconds"], horizon)
        coordinates[name] = trial.suggest_float(name, low, high, log=logarithmic)
    profile = TrainingProfile(
        profile_id=protocol.profile_id, implementation_revision=protocol.implementation_revision,
        reward_credit_horizon_seconds=horizon,
        sensory_noise=trial.suggest_float("sensory_noise", 0, 8),
        exploration_decay_frames=trial.suggest_int("exploration_decay_frames", 1000, 100000, log=True),
        plasticity_delta_limit=trial.suggest_float("plasticity_delta_limit", 1e-5, 1e-2, log=True),
        volatility_target=trial.suggest_float("volatility_target", 1e-4, 1e-2, log=True),
        plasticity=PlasticityConfig(credit_tau_seconds=coordinates["credit_tau_seconds"],
            maximum_credit_age_seconds=coordinates["maximum_credit_age_seconds"],
            positive_return_threshold=coordinates["positive_return_threshold"],
            positive_gain=coordinates["positive_gain"], fee_shock_gain=coordinates["fee_shock_gain"],
            neural_trace_seconds=trial.suggest_float("neural_trace_seconds", .02, 5., log=True),
            dan_threshold_hz=trial.suggest_float("dan_threshold_hz", 0., 25.),
            eta_scale=trial.suggest_float("eta_scale", .1, 2., log=True),
            homeostasis_per_second=trial.suggest_float("homeostasis_per_second", 1e-5, .01, log=True),
            saturation_decay_per_second=trial.suggest_float("saturation_decay_per_second", .001, .2, log=True)))
    reward = replace(fixed_reward, window=trial.suggest_categorical("risk_window", [64, 128, 256, 512, 1024]),
                     sortino_weight=trial.suggest_float("alpha", 0.25, 0.95),
                     signal_scale=trial.suggest_float("signal_scale", 1, 16, log=True),
                     turnover_penalty=trial.suggest_float("turnover_penalty", 0.05, 5, log=True))
    trial.set_user_attr("profile", asdict(profile))
    trial.set_user_attr("reward", asdict(reward))
    trial.set_user_attr("protocol", protocol.public_dict())
    try:
        evaluations, controls = [], []
        for fold in folds:
            for seed in seeds:
                evaluation = evaluate_fold(resources, fold, replace(profile, seed=seed), reward)
                validate_epoch_constraints(evaluation)
                evaluations.append(evaluation)
                if protocol.paired_control:
                    # Same chronological data, same seed, learning disabled: the
                    # frozen control shares everything but plasticity/exploration.
                    controls.append(evaluate_fold(resources, fold, replace(profile, seed=seed), reward,
                                                  learning=False))
    except EligibilityCapacityError as error:
        trial.set_user_attr("capacity_rejection", str(error))
        raise TrialPruned(str(error)) from error
    except AbsoluteSaturationError as error:
        trial.set_user_attr("absolute_saturation_audit", error.audit)
        trial.set_user_attr("pruning_reason", str(error))
        raise TrialPruned(str(error)) from error
    if not evaluations:
        raise ValueError("nonempty chronological folds and seeds required")
    trial.set_user_attr("evaluations", evaluations)
    if protocol.paired_control:
        deltas = [paired_deltas(comparable_metrics(learned), comparable_metrics(frozen))
                  for learned, frozen in zip(evaluations, controls)]
        trial.set_user_attr("paired_control", {"frozen_controls": controls, "deltas": deltas,
                                               "seed_summary": summarize_seeds(deltas)})
    if protocol.prune_on_horizon_violation and any(
            item["execution_diagnostics"]["credit_horizon_violations"] for item in evaluations):
        # Reward delivered beyond the declared horizon means the study learned from
        # a truncated subset of its own credit; fail explicitly, record nothing scored.
        trial.set_user_attr("pruning_reason", "REWARD_DELIVERY_BEYOND_DECLARED_HORIZON")
        raise TrialPruned("reward delivery exceeded the declared credit horizon")
    if protocol.evidence is not None:
        assessments = [assess_evidence(item.get("training_evidence", {}), item.get("validation_evidence", {}),
                                       protocol.evidence) for item in evaluations]
        trial.set_user_attr("evidence", assessments)
        if any(item["evidence_status"] != "SUFFICIENT" for item in assessments):
            # Not an activity bonus: HOLD is allowed, but a trial with too few
            # causal decisions/rewards cannot be called a learned strategy.
            trial.set_user_attr("pruning_reason", "INSUFFICIENT_EVIDENCE")
            raise TrialPruned("insufficient causal evidence for a learning claim")
    raw_sortino = float(np.median([item["sortino"] for item in evaluations]))
    rejection_rate = max(float(item["execution_diagnostics"]["capacity_order_rejection_rate"]) for item in evaluations)
    penalty = capacity_penalty(rejection_rate)
    fitness = raw_sortino - penalty
    trial.set_user_attr("fitness", {"raw_net_sortino": raw_sortino, "capacity_order_rejection_rate": rejection_rate,
                                   "capacity_penalty": penalty, "penalized_net_sortino": fitness})
    trial.set_constraint("turnover", float(max(item["turnover_violations"] for item in evaluations)))
    trial.set_constraint("saturation", max(float(item["training_saturation_peak"]) for item in evaluations) - SATURATION_LIMIT)
    trial.set_constraint("credit", float(max(item["unassigned_credit_windows"]
        + item["execution_diagnostics"]["credit_horizon_violations"] for item in evaluations)))
    trial.set_constraint("absolute_saturation_time", max(float(item["absolute_saturation_time_fraction"])
                         for item in evaluations) - ABSOLUTE_SATURATION_TIME_LIMIT)
    return (fitness,
            max(float(item["maximum_drawdown"]) for item in evaluations),
            float(np.mean([item["training_saturation_mean"] for item in evaluations])),
            max(float(item["homeostatic_erasure_rate"]) for item in evaluations))
