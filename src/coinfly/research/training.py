"""Explicit experimental full-graph training, bounded plasticity, fixed decoding.

There is no differentiable policy, PPO surrogate loss, critic, or action override.
An experiment is a chronological single session. Restart trials from a fresh
qualified controller; neural-only checkpoints cannot resume its evidence ledger.
"""

import hashlib
import math
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from coinfly.research.assets import NeuralPaths, VerifiedNativeKernel
from coinfly.research.frames import AuditedFrame
from coinfly.research.neural.controller import FlyController
from coinfly.research.neural.experimental_plasticity import ExperimentalPlasticity, PlasticityConfig
from coinfly.research.profile import NeuralProfile
from coinfly.research.reward import CostAwareController, RewardWindow
from coinfly.research.risk_reward import RiskAdjustedReward, RiskRewardConfig

# v2 is the frozen scientific contract (revision 4). v3 keeps the identical
# reward, plasticity and decoder path but runs under the evidence-gated study
# protocol (revision 5); see coinfly.research.optimization.StudyProtocol.
PROFILE_REVISIONS = {"malecns-risk-adjusted-experiment-v2": 4, "malecns-risk-adjusted-experiment-v3": 5}


@dataclass(frozen=True)
class TrainingProfile:
    profile_id: str = "malecns-risk-adjusted-experiment-v2"
    implementation_revision: int = 4
    reward_credit_horizon_seconds: float = 1.0
    seed: int = 1729
    sensory_noise: float = 2.0
    exploration_decay_frames: int = 10000
    plasticity_delta_limit: float = 0.001
    volatility_target: float = 0.001
    plasticity: PlasticityConfig = PlasticityConfig()

    def __post_init__(self) -> None:
        if (PROFILE_REVISIONS.get(self.profile_id) != self.implementation_revision
                or type(self.implementation_revision) is not int
                or type(self.seed) is not int
                or not isinstance(self.plasticity, PlasticityConfig)
                or not 0 <= self.seed < 2**63 or type(self.exploration_decay_frames) is not int
                or not 1 <= self.exploration_decay_frames <= 1000000000):
            raise ValueError("invalid experimental profile or seed")
        if (any(isinstance(v, bool) or not math.isfinite(v)
                for v in (self.sensory_noise, self.plasticity_delta_limit, self.volatility_target,
                          self.reward_credit_horizon_seconds))
                or not 0 < self.reward_credit_horizon_seconds <= self.plasticity.maximum_credit_age_seconds
                or not 0 <= self.sensory_noise <= 32 or not 1e-12 <= self.plasticity_delta_limit <= 0.1
                or not 1e-12 <= self.volatility_target <= 1):
            raise ValueError("invalid experimental exploration/trust region")


class TrainingController(FlyController):
    """Experimental Controller; baseline FlyController behavior is unchanged.

    Only run_frame/run are accepted for training. They require owned audited RGB
    and causal completed rewards. Execution remains a separate paper adapter.
    """

    def __init__(self, settings: NeuralProfile, paths: NeuralPaths, kernel: VerifiedNativeKernel,
                 profile: TrainingProfile = TrainingProfile(),
                 reward_config: RiskRewardConfig = RiskRewardConfig()) -> None:
        super().__init__(settings, paths, kernel)
        self._initialize_training(profile, reward_config)

    def _initialize_training(self, profile: TrainingProfile, reward_config: RiskRewardConfig) -> None:
        if reward_config.interval_seconds > profile.plasticity.maximum_credit_age_seconds:
            raise ValueError("accounting window exceeds the retained credit horizon")
        self.profile = profile
        self.plasticity = ExperimentalPlasticity(self.brain, profile.plasticity)
        self.risk_reward = RiskAdjustedReward(reward_config)
        self.session = CostAwareController(self, self.risk_reward)
        self._inside_session = False
        self._evaluation_started = False
        self._signal = 0.0
        self._frames = 0
        self._decision_at: datetime | None = None
        self._outcome: RewardWindow | None = None

    def observe(self, rgb: Any, reinforcement: str) -> dict[str, Any]:
        with self._lock:
            if not self._inside_session:
                raise RuntimeError("experimental observations require run_frame causal evidence")
            return super().observe(rgb, reinforcement)

    def _observe(self, rgb: Any, reinforcement: str) -> dict[str, Any]:
        assert self._decision_at is not None
        self._signal = float(self.risk_reward.metrics.get("signal", 0)) if reinforcement != "none" else 0.0
        if self.s.learning:
            metrics = self.risk_reward.metrics if self._outcome is not None else {}
            self.brain.prepare_experimental_frame(self.plasticity, self._decision_at,
                self._outcome.credits if self._outcome else (), has_reward=self._outcome is not None,
                signal=self._signal, net_return=float(metrics.get("net_return", 0)),
                stressed_return=float(metrics.get("stressed_return", 0)),
                booked_fee_return=float(metrics.get("booked_fee_return", 0)))
        original_hash = hashlib.sha256(rgb.tobytes()).hexdigest()
        sigma = (self.profile.sensory_noise / math.sqrt(1 + self._frames / self.profile.exploration_decay_frames)
                 if self.s.learning else 0.0)
        if sigma:
            rng = np.random.default_rng(np.random.SeedSequence([self.profile.seed, self._frames]))
            rgb = np.rint(np.clip(rgb.astype(np.float64) + rng.normal(0, sigma, rgb.shape), 0, 255)).astype(np.uint8)
            rgb.flags.writeable = False
        result = super()._observe(rgb, reinforcement)
        order_decision = result["side"] in ("BUY", "SELL")
        admission = self.brain.finalize_experimental_frame(self.plasticity, self._decision_at,
                                                           result["side"], learning=self.s.learning)
        self._frames += 1
        return {**result, "audited_input_sha256": original_hash,
                "execution_admission": admission,
                "training": {"profile": self.profile.profile_id, "sensory_noise_sigma": sigma,
                             "plasticity_delta_limit": self._step_options()["plasticity_delta_limit"],
                             "risk": dict(self.risk_reward.metrics), "frames": self._frames,
                             "eligibility": {**self.plasticity.credit_metrics,
                                             "retained_tags": len(self.plasticity.bank),
                                             "expired_tags": self.plasticity.bank.expired_tags,
                                             "capacity_checks_blocked": self.plasticity.bank.capacity_rejections,
                                             "tag_retained": int(self.s.learning and order_decision and admission["allowed"] is True)},
                             "plasticity": dict(self.brain.plasticity_diagnostics)}}

    def _pulse_ticks(self, reinforcement: str) -> int:
        if (not self.s.learning or reinforcement == "none" or self._signal == 0
                or not np.any(self.plasticity.credit)):
            return 0
        # Magnitude changes pulse duration only, bounded by the declared maximum.
        return max(1, round(super()._pulse_ticks(reinforcement) * abs(self._signal)))

    def _step_options(self) -> dict[str, Any]:
        volatility = float(self.risk_reward.metrics.get("volatility", 0))
        limit = self.profile.plasticity_delta_limit / (1 + volatility / self.profile.volatility_target)
        return {"plasticity_delta_limit": limit, "experimental_plasticity": self.plasticity}

    def _checkpoint_profile(self) -> dict[str, Any]:
        return {**super()._checkpoint_profile(), "experiment": asdict(self.profile),
                "reward": asdict(self.risk_reward.config), "session_resume": False}

    def restore(self, path: Path) -> None:
        raise RuntimeError("neural-only checkpoint cannot resume experimental risk/evidence state; replay the trial")

    def begin_evaluation(self) -> None:
        """Freeze weights/noise/pulses; start independent validation accounting."""
        with self._lock:
            if self._evaluation_started:
                raise RuntimeError("evaluation has already started; do not reset evidence cursors")
            self.freeze_learning()
            self.plasticity.clear_credit()
            self.risk_reward = RiskAdjustedReward(self.risk_reward.config)
            self.session = CostAwareController(self, self.risk_reward)
            self._evaluation_started = True

    def run_frame(self, frame: AuditedFrame, outcome: RewardWindow | None = None) -> dict[str, Any]:
        with self._lock:
            if self._failed:
                raise RuntimeError("failed experimental session; replay from a fresh controller")
            if frame.evidence.partition != ("evaluation" if self._evaluation_started else "train"):
                raise ValueError("frame partition does not match the experimental phase")
            rgb = frame.owned_rgb()
            self._inside_session = True
            self._decision_at, self._outcome = frame.evidence.decision_at, outcome
            try:
                result = self.session.observe(rgb, frame.evidence, outcome)
                result["exploration"]["strategy"] = "seeded-sensory-noise-fixed-decoder"
                return result
            finally:
                self._inside_session = False
                self._decision_at, self._outcome = None, None

    def run(self, stream: Iterable[tuple[AuditedFrame, RewardWindow | None]]) -> Iterator[dict[str, Any]]:
        """Pull one causal frame, propagate, yield the unchanged decoded action.

        A trusted paper environment consumes that action, advances to the next
        decision, and yields only accounting already available then. This loop
        cannot manufacture fills or choose replacement trades after rejection.
        """
        for frame, outcome in stream:
            yield self.run_frame(frame, outcome)
