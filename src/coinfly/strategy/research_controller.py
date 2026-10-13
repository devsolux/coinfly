"""RESEARCH_LEARNING: the experimental full-graph controller on the worker's causal core.

One slot, one chronological session, one shared brain: the worker's exact
sensory frames become audited frames, the paper ledger's reward windows
(``strategy.reward_ledger``) drive the declared experimental plasticity through
``TrainingController.run_frame`` — the preserved ``prepare_experimental_frame →
propagation/decoding → finalize_experimental_frame`` sequence — and every
delayed reward is credited to the originating admitted decisions by their
decision time. Expired or unknown credits are reported by the plasticity engine
(``expired_credit_decisions``, ``missing_credit_decisions``); nothing is
re-timestamped or dropped to make them fit.

Fenced disposal: the experimental session (eligibility ring, traces, reward
history, causal cursors) is snapshotted before every observation, so a result
the automation discards restores the session *and* the brain to the last
committed generation; learning never advances on discarded work.

This module imports the research stack lazily: it is used only when a strategy
declares ``neural.mode = RESEARCH_LEARNING`` in research mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.strategy.controllers import (
    FULL_GRAPH_EDGES,
    FULL_GRAPH_NEURONS,
    ControllerUnavailable,
    Decision,
)
from coinfly.strategy.definition import ControllerProfile, TrainingSettings
from coinfly.strategy.neural_slots import DEFAULT_NEURAL_STATE_BUDGET_BYTES, SharedGraphSlotStates
from coinfly.strategy.sensory import SensoryFrame

TRAINING_DIAGNOSTIC_KEYS = ("left_hz", "right_hz", "difference_hz", "gate_spikes", "total_spikes", "reward_spikes",
                            "aversive_spikes", "brain_ms", "spike_sha256", "stimulus", "stimulus_ms")


@dataclass
class SessionSnapshot:
    """Everything of the experimental session outside the brain arrays."""

    bank_values: np.ndarray
    bank_timestamps: np.ndarray
    bank_slots: dict[int, int]
    bank_dates: list[datetime | None]
    bank_scalars: dict[str, Any]
    plasticity_arrays: dict[str, np.ndarray]
    plasticity_scalars: dict[str, Any]
    risk_history: np.ndarray
    risk_scalars: dict[str, Any]
    session_scalars: dict[str, Any]
    controller_scalars: dict[str, Any]

    @property
    def nbytes(self) -> int:
        return int(self.bank_values.nbytes + self.bank_timestamps.nbytes + self.risk_history.nbytes
                   + sum(array.nbytes for array in self.plasticity_arrays.values()))


def snapshot_session(controller: Any) -> SessionSnapshot:
    plasticity, bank = controller.plasticity, controller.plasticity.bank
    risk, session = controller.risk_reward, controller.session
    return SessionSnapshot(
        bank_values=bank._values.copy(), bank_timestamps=bank._timestamps.copy(), bank_slots=dict(bank._slots),
        bank_dates=list(bank._dates),
        bank_scalars={key: getattr(bank, key) for key in ("_head", "_size", "_last_decision", "_clock_us", "_reserved",
                                                          "expired_tags", "capacity_rejections")},
        plasticity_arrays={key: getattr(plasticity, key).copy() for key in ("pre_trace", "post_trace", "tag", "credit",
                                                                            "_dopamine", "_dan", "_bin_metrics")},
        plasticity_scalars={"credit_metrics": dict(plasticity.credit_metrics), "admission_allowed": plasticity.admission_allowed,
                            "reinforcement_gain": plasticity.reinforcement_gain, "_frame_at": plasticity._frame_at,
                            "_frame_finalized": plasticity._frame_finalized},
        risk_history=risk.history.copy(),
        risk_scalars={"count": risk.count, "cursor": risk.cursor, "last_end": risk.last_end, "metrics": dict(risk.metrics)},
        session_scalars={"_last_observation": session._last_observation, "_last_reward_end": session._last_reward_end,
                         "leakage_rejections": session.leakage_rejections, "actions": dict(session.actions)},
        controller_scalars={"_signal": controller._signal, "_frames": controller._frames, "_failed": controller._failed,
                            "s": controller.s},
    )


def restore_session(controller: Any, snapshot: SessionSnapshot) -> None:
    plasticity, bank = controller.plasticity, controller.plasticity.bank
    risk, session = controller.risk_reward, controller.session
    np.copyto(bank._values, snapshot.bank_values)
    np.copyto(bank._timestamps, snapshot.bank_timestamps)
    bank._slots = dict(snapshot.bank_slots)
    bank._dates = list(snapshot.bank_dates)
    for key, value in snapshot.bank_scalars.items():
        setattr(bank, key, value)
    for key, value in snapshot.plasticity_arrays.items():
        np.copyto(getattr(plasticity, key), value)
    plasticity.credit_metrics = dict(snapshot.plasticity_scalars["credit_metrics"])
    plasticity.admission_allowed = snapshot.plasticity_scalars["admission_allowed"]
    plasticity.reinforcement_gain = snapshot.plasticity_scalars["reinforcement_gain"]
    plasticity._frame_at = snapshot.plasticity_scalars["_frame_at"]
    plasticity._frame_finalized = snapshot.plasticity_scalars["_frame_finalized"]
    np.copyto(risk.history, snapshot.risk_history)
    risk.count, risk.cursor = snapshot.risk_scalars["count"], snapshot.risk_scalars["cursor"]
    risk.last_end, risk.metrics = snapshot.risk_scalars["last_end"], dict(snapshot.risk_scalars["metrics"])
    session._last_observation = snapshot.session_scalars["_last_observation"]
    session._last_reward_end = snapshot.session_scalars["_last_reward_end"]
    session.leakage_rejections = snapshot.session_scalars["leakage_rejections"]
    session.actions = dict(snapshot.session_scalars["actions"])
    controller._signal, controller._frames = snapshot.controller_scalars["_signal"], snapshot.controller_scalars["_frames"]
    controller._failed, controller.s = snapshot.controller_scalars["_failed"], snapshot.controller_scalars["s"]


def training_profile(settings: TrainingSettings) -> Any:
    """Construct the declared research profile; every override is validated by the research dataclasses."""
    from coinfly.research.neural.experimental_plasticity import PlasticityConfig
    from coinfly.research.training import TrainingProfile

    overrides: dict[str, Any] = {str(key): value for key, value in settings.plasticity.items()}
    try:
        plasticity = replace(PlasticityConfig(), **overrides)
        return TrainingProfile(profile_id=settings.profile_id, implementation_revision=settings.implementation_revision,
                               seed=settings.seed, reward_credit_horizon_seconds=settings.reward_credit_horizon_seconds,
                               plasticity=plasticity)
    except (TypeError, ValueError) as error:
        raise DomainError("TRAINING_PROFILE_INVALID", f"TRAINING_PROFILE_INVALID: {error}") from None


def risk_reward_config(settings: TrainingSettings) -> Any:
    from coinfly.research.risk_reward import RiskRewardConfig

    overrides: dict[str, Any] = {str(key): value for key, value in settings.risk_reward.items()}
    try:
        return replace(RiskRewardConfig(interval_seconds=settings.reward_interval_seconds), **overrides)
    except (TypeError, ValueError) as error:
        raise DomainError("RISK_REWARD_CONFIG_INVALID", f"RISK_REWARD_CONFIG_INVALID: {error}") from None


class ResearchLearningController:
    """DecisionController over a ``TrainingController``: one slot, rewards from the ledger."""

    profile = ControllerProfile.PRESERVED_MALECNS_V1
    mode = "RESEARCH_LEARNING"

    def __init__(self, training_controller: Any, *, settings: TrainingSettings, state_budget_bytes: int | None = None) -> None:
        brain = training_controller.brain
        neurons, edges = int(getattr(brain, "n", 0)), int(len(getattr(brain, "post", ())))
        if (neurons, edges) != (FULL_GRAPH_NEURONS, FULL_GRAPH_EDGES):
            raise ControllerUnavailable("GRAPH_IDENTITY_MISMATCH",
                                        missing=(f"neurons={neurons}/{FULL_GRAPH_NEURONS}", f"edges={edges}/{FULL_GRAPH_EDGES}"))
        if not training_controller.s.learning or brain.weights_frozen:
            raise DomainError("RESEARCH_CONTROLLER_NOT_LEARNING")
        self._controller = training_controller
        self.settings = settings
        self._states = SharedGraphSlotStates(brain, True, state_budget_bytes or DEFAULT_NEURAL_STATE_BUDGET_BYTES)
        self._slot: str | None = None
        self._snapshot: SessionSnapshot | None = None
        self._closed = False
        self.discarded = 0
        self.last_training: dict[str, Any] = {}

    @classmethod
    def build(cls, *, dataset: Any, kernel: Any, settings: TrainingSettings, state_budget_bytes: int | None = None) -> ResearchLearningController:
        from coinfly.research.assets import NeuralPaths
        from coinfly.research.profile import NeuralProfile
        from coinfly.research.training import TrainingController

        profile = training_profile(settings)
        reward = risk_reward_config(settings)
        controller = TrainingController(NeuralProfile(learning=True), NeuralPaths(dataset), kernel, profile=profile, reward_config=reward)
        return cls(controller, settings=settings, state_budget_bytes=state_budget_bytes)

    # ------------------------------------------------------------------ slots
    def admit_slot(self, slot_key: str) -> None:
        if self._slot is not None and self._slot != slot_key:
            raise DomainError("RESEARCH_SINGLE_SLOT", "RESEARCH_SINGLE_SLOT: one chronological session serves one instrument")
        self._slot = slot_key
        self._states.admit(slot_key)

    def release_slot(self, slot_key: str) -> None:
        self._states.release(slot_key)
        if self._slot == slot_key:
            self._slot = None

    def resource_usage(self) -> dict[str, Any]:
        usage = self._states.usage()
        usage["session_snapshot_bytes"] = self._snapshot.nbytes if self._snapshot is not None else 0
        usage["eligibility_buffer_bytes"] = int(self._controller.plasticity.buffer_nbytes)
        return usage

    def commit(self, slot_key: str) -> None:
        self._states.commit(slot_key)
        self._snapshot = None

    def discard(self, slot_key: str) -> None:
        """Restore the brain AND the experimental session: discarded work never learned."""
        self._states.discard(slot_key)
        if self._snapshot is not None:
            restore_session(self._controller, self._snapshot)
            self._snapshot = None
        self.discarded += 1

    # ------------------------------------------------------------------ provenance
    def provenance(self, slot_key: str) -> dict[str, Any]:
        from dataclasses import asdict

        controller = self._controller
        brain = controller.brain
        build = dict(getattr(brain, "build", {}))
        dataset = getattr(getattr(brain, "paths", None), "dataset", None)
        profile = controller.profile
        return {
            "controller": self.profile.value, "mode": self.mode, "pipeline_fixture": False,
            "graph": {"neurons": int(getattr(brain, "n", 0)), "edges": int(len(getattr(brain, "post", ()))),
                      "dataset_digest": getattr(dataset, "digest", None),
                      "initial_weight_sha256": getattr(brain, "initial_weight_sha256", None)},
            "kernel": {"binary_sha256": build.get("binary_sha256"), "source_sha256": build.get("source_sha256"),
                       "signed_identity": build.get("signed_identity"), "model": build.get("model")},
            "checkpoint": {"kind": "FRESH_QUALIFIED_GRAPH", "checkpoint_id": None},
            "learning": True, "neural_profile": asdict(controller.s),
            "training_profile": asdict(profile), "risk_reward": asdict(controller.risk_reward.config),
            "state_generation": self._states.generation(slot_key),
            "native_frame_counter": {"cursor": int(getattr(brain, "cursor", 0)), "sim_ms": float(getattr(brain, "sim_ms", 0.0)),
                                     "total_spikes": int(getattr(brain, "total_spikes", 0))},
            "session": {"frames": int(controller._frames), "discarded_observations": self.discarded,
                        "retained_tags": len(controller.plasticity.bank), "expired_tags": controller.plasticity.bank.expired_tags},
        }

    # ------------------------------------------------------------------ observation
    def observe(self, frame: SensoryFrame, *, reinforcement: str, decision_id: str, slot_key: str,
                outcome: Any = None) -> Decision:
        from coinfly.research.frames import AuditedFrame
        from coinfly.research.reward import ObservationEvidence
        from coinfly.research.vectorization import TensorBatch

        if self._closed:
            raise DomainError("CONTROLLER_CLOSED")
        if slot_key != self._slot:
            raise DomainError("NEURAL_SLOT_NOT_ADMITTED")
        self._states.activate(slot_key)
        self._snapshot = snapshot_session(self._controller)
        evidence = ObservationEvidence(decision_at=frame.decision_at, features_event_through=frame.observed_through,
                                       features_available_through=min(frame.available_through, frame.decision_at),
                                       preprocessing_fitted_through=frame.fitted_through, partition="train")
        values = frame.rgb[:, :, 0].astype(np.float64) / 127.5 - 1.0
        audited = AuditedFrame(rgb=frame.rgb, tensor=TensorBatch(values=values, ticks=frame.ticks, scales=()),
                               evidence=evidence, sha256=frame.sha256)
        started = time.perf_counter()
        result = self._controller.run_frame(audited, outcome)
        elapsed = time.perf_counter() - started
        side = result["side"]
        if side not in ("BUY", "SELL", "HOLD"):
            raise DomainError("DECODER_OUTPUT_INVALID")
        admission = result.get("execution_admission", {"allowed": True, "reason": "not_applicable"})
        training = dict(result.get("training", {}))
        training.pop("plasticity", None)
        self.last_training = training
        diagnostics = {key: result[key] for key in TRAINING_DIAGNOSTIC_KEYS if key in result}
        reward = result.get("reward")
        provenance = {**self.provenance(slot_key), "training": training, "reward": reward, "causality": result.get("causality"),
                      "exploration": result.get("exploration"),
                      "reward_window": None if outcome is None else {"start_at": outcome.start_at.isoformat(), "end_at": outcome.end_at.isoformat(),
                                                                    "available_at": outcome.available_at.isoformat(),
                                                                    "executions": len(outcome.executions), "credits": len(outcome.credits)}}
        return Decision(decision_id=decision_id, instrument=frame.instrument, side=side, decided_at=frame.decision_at,
                        controller_profile=self.profile.value, frame_sha256=frame.sha256, observed_through=frame.observed_through,
                        reinforcement=str(result.get("stimulus", "none")), pipeline_fixture=False,
                        admission_allowed=bool(admission.get("allowed", True)),
                        admission_reason=str(admission.get("reason") or "not_applicable"),
                        compute_seconds=elapsed, diagnostics=diagnostics, provenance=provenance)

    def close(self) -> None:
        self._closed = True


__all__ = ["ResearchLearningController", "SessionSnapshot", "restore_session", "snapshot_session", "training_profile",
           "risk_reward_config"]
