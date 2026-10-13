"""Decision controllers behind one narrow interface: frame in, decoded side out.

* ``PreservedMaleCNSController`` wraps the unchanged ``FlyController`` (full
  graph, fixed DNp20/DNpe017 decoder, anatomical dopamine routes, preserved
  propagator). It exists only when a verified dataset AND a verified native
  kernel are available; nothing substitutes for it.
* ``FixtureSequenceController`` replays scripted decisions. It establishes
  pipeline mechanics in tests and is refused outside SANDBOX; its diagnostics
  say ``pipeline_fixture`` so no report can mistake it for the neural model.
* ``ControllerUnavailable`` is the honest answer when prerequisites are
  missing: it never decodes and reports exactly which capability is absent.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from coinfly.domain.context import DomainError, Environment, FrozenModel, Identifier
from coinfly.strategy.definition import ControllerMode, ControllerProfile, NeuralSettings
from coinfly.strategy.sensory import SensoryFrame

Side = Literal["BUY", "SELL", "HOLD"]


class Decision(FrozenModel):
    decision_id: Identifier
    instrument: Identifier
    side: Side
    decided_at: datetime
    controller_profile: str
    frame_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_through: datetime
    reinforcement: str = "none"
    pipeline_fixture: bool = False
    admission_allowed: bool = True
    admission_reason: str = "not_applicable"
    compute_seconds: float = Field(ge=0)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    # Provenance of the decision: graph/array identity, kernel identity,
    # controller mode and checkpoint (or explicit untrained baseline), native
    # frame counters and the slot's neural-state generation.
    provenance: dict[str, Any] = Field(default_factory=dict)


class DecisionController(Protocol):
    """Frame in, decoded side out; state changes become durable only on ``commit``.

    ``observe`` may mutate the slot's working neural state; the caller decides,
    after revalidating its generation tokens, whether that state is committed
    (``commit``) or fenced off (``discard`` restores the last committed state).
    """

    profile: ControllerProfile

    def observe(self, frame: SensoryFrame, *, reinforcement: str, decision_id: str, slot_key: str,
                outcome: Any = None) -> Decision: ...
    def commit(self, slot_key: str) -> None: ...
    def discard(self, slot_key: str) -> None: ...
    def admit_slot(self, slot_key: str) -> None: ...
    def release_slot(self, slot_key: str) -> None: ...
    def resource_usage(self) -> dict[str, Any]: ...
    def provenance(self, slot_key: str) -> dict[str, Any]: ...
    def close(self) -> None: ...


class ControllerUnavailable(DomainError):
    """Raised by factories when a controller's prerequisites are absent."""

    def __init__(self, code: str, *, missing: Sequence[str]) -> None:
        super().__init__(code)
        self.missing = tuple(missing)


class FixtureSequenceController:
    """Scripted decisions for pipeline tests. Never a policy; never live."""

    profile = ControllerProfile.FIXTURE_SEQUENCE_V1

    def __init__(self, sides: Sequence[Side], *, environment: Environment, repeat_last: bool = True) -> None:
        if environment is not Environment.SANDBOX:
            raise DomainError("FIXTURE_CONTROLLER_SANDBOX_ONLY")
        if not sides or any(side not in ("BUY", "SELL", "HOLD") for side in sides):
            raise ValueError("scripted sides required")
        self._sides = tuple(sides)
        self._repeat_last = repeat_last
        self._cursor = 0
        self.observed: list[Decision] = []

    def admit_slot(self, slot_key: str) -> None:
        return None

    def release_slot(self, slot_key: str) -> None:
        return None

    def commit(self, slot_key: str) -> None:
        return None

    def discard(self, slot_key: str) -> None:
        return None

    def resource_usage(self) -> dict[str, Any]:
        return {"per_slot_state_bytes": 0, "budget_bytes": 0, "capacity_slots": 10**6, "admitted_slots": 0,
                "used_bytes": 0, "shared_graph": False, "graph_cloned_per_slot": False, "plastic_state_per_slot": False,
                "pipeline_fixture": True}

    def provenance(self, slot_key: str) -> dict[str, Any]:
        return {"controller": self.profile.value, "mode": "PIPELINE_FIXTURE", "pipeline_fixture": True,
                "graph": None, "kernel": None, "checkpoint": None, "scripted_index": self._cursor}

    def observe(self, frame: SensoryFrame, *, reinforcement: str, decision_id: str, slot_key: str,
                outcome: Any = None) -> Decision:
        if outcome is not None:
            raise DomainError("REWARD_NOT_APPLICABLE")
        if self._cursor < len(self._sides):
            side = self._sides[self._cursor]
        elif self._repeat_last:
            side = "HOLD"
        else:
            raise DomainError("FIXTURE_SEQUENCE_EXHAUSTED")
        self._cursor += 1
        decision = Decision(decision_id=decision_id, instrument=frame.instrument, side=side, decided_at=frame.decision_at,
                            controller_profile=self.profile.value, frame_sha256=frame.sha256,
                            observed_through=frame.observed_through, reinforcement=reinforcement, pipeline_fixture=True,
                            compute_seconds=0.0, diagnostics={"scripted_index": self._cursor - 1},
                            provenance=self.provenance(slot_key))
        self.observed.append(decision)
        return decision

    def close(self) -> None:
        return None


# The preserved full MaleCNS graph. Any other size is not "the model with a
# different setting": it is a different graph, and the controller refuses it.
FULL_GRAPH_NEURONS = 166_700
FULL_GRAPH_EDGES = 25_582_938


class PreservedMaleCNSController:
    """The preserved controller; decodes only, learns only in research mode."""

    profile = ControllerProfile.PRESERVED_MALECNS_V1

    def __init__(self, fly_controller: Any, *, learning: bool, state_budget_bytes: int | None = None,
                 mode: str = "BASELINE_INFERENCE", checkpoint_identity: dict[str, Any] | None = None) -> None:
        from coinfly.strategy.neural_slots import DEFAULT_NEURAL_STATE_BUDGET_BYTES, SharedGraphSlotStates

        brain = fly_controller.brain
        neurons, edges = int(getattr(brain, "n", 0)), int(len(getattr(brain, "post", ())))
        if (neurons, edges) != (FULL_GRAPH_NEURONS, FULL_GRAPH_EDGES):
            raise ControllerUnavailable("GRAPH_IDENTITY_MISMATCH",
                                        missing=(f"neurons={neurons}/{FULL_GRAPH_NEURONS}", f"edges={edges}/{FULL_GRAPH_EDGES}"))
        self._controller = fly_controller
        self._learning = learning
        self._closed = False
        # Explicit provenance: an untrained baseline says so; a checkpoint names itself.
        self.mode = mode
        self.checkpoint_identity: dict[str, Any] = checkpoint_identity or {"kind": "UNTRAINED_BASELINE", "checkpoint_id": None}
        self._states = SharedGraphSlotStates(fly_controller.brain, learning,
                                             state_budget_bytes or DEFAULT_NEURAL_STATE_BUDGET_BYTES)

    @classmethod
    def build(cls, *, dataset: Any, kernel: Any, state_budget_bytes: int | None = None,
              neural: NeuralSettings | None = None, checkpoints: Path | None = None) -> PreservedMaleCNSController:
        """Baseline inference or a named frozen checkpoint; never learning (that is ``ResearchLearningController``)."""
        from coinfly.research.assets import NeuralPaths
        from coinfly.research.neural.controller import FlyController
        from coinfly.research.profile import NeuralProfile

        settings = neural or NeuralSettings()
        if settings.mode is ControllerMode.RESEARCH_LEARNING:
            raise DomainError("RESEARCH_MODE_CONTROLLER_REQUIRED")
        # The scientific parameters are the preserved profile, untouched; the
        # weights are frozen in both inference modes.
        controller = FlyController(NeuralProfile(learning=False), NeuralPaths(dataset), kernel)
        identity: dict[str, Any] = {"kind": "UNTRAINED_BASELINE", "checkpoint_id": None,
                                    "initial_weight_sha256": getattr(controller.brain, "initial_weight_sha256", None)}
        if settings.mode is ControllerMode.FROZEN_CHECKPOINT:
            from coinfly.strategy.checkpoints import checkpoint_path, restore_frozen_checkpoint

            if checkpoints is None:
                raise DomainError("CHECKPOINT_DIRECTORY_UNAVAILABLE")
            assert settings.checkpoint_id is not None
            path = checkpoint_path(checkpoints, settings.checkpoint_id)
            identity = restore_frozen_checkpoint(controller, path, expected_training_profile_id=settings.expected_training_profile_id)
        return cls(controller, learning=False, state_budget_bytes=state_budget_bytes, mode=settings.mode.value, checkpoint_identity=identity)

    def admit_slot(self, slot_key: str) -> None:
        self._states.admit(slot_key)

    def release_slot(self, slot_key: str) -> None:
        self._states.release(slot_key)

    def resource_usage(self) -> dict[str, Any]:
        return self._states.usage()

    def commit(self, slot_key: str) -> None:
        self._states.commit(slot_key)

    def discard(self, slot_key: str) -> None:
        """Fenced disposal: the working state of a discarded observation never becomes the slot's state."""
        self._states.discard(slot_key)

    def provenance(self, slot_key: str) -> dict[str, Any]:
        brain = self._controller.brain
        build = dict(getattr(brain, "build", {}))
        dataset = getattr(getattr(brain, "paths", None), "dataset", None)
        return {
            "controller": self.profile.value, "mode": self.mode, "pipeline_fixture": False,
            "graph": {"neurons": int(getattr(brain, "n", 0)), "edges": int(len(getattr(brain, "post", ()))),
                      "dataset_digest": getattr(dataset, "digest", None),
                      "initial_weight_sha256": getattr(brain, "initial_weight_sha256", None)},
            "kernel": {"binary_sha256": build.get("binary_sha256"), "source_sha256": build.get("source_sha256"),
                       "signed_identity": build.get("signed_identity"), "model": build.get("model")},
            "checkpoint": self.checkpoint_identity, "learning": self._learning,
            "neural_profile": dict(getattr(self._controller, "s").__dict__) if hasattr(self._controller, "s") else None,
            "state_generation": self._states.generation(slot_key),
            "native_frame_counter": {"cursor": int(getattr(brain, "cursor", 0)), "sim_ms": float(getattr(brain, "sim_ms", 0.0)),
                                     "total_spikes": int(getattr(brain, "total_spikes", 0))},
        }

    def observe(self, frame: SensoryFrame, *, reinforcement: str, decision_id: str, slot_key: str,
                outcome: Any = None) -> Decision:
        if self._closed:
            raise DomainError("CONTROLLER_CLOSED")
        if outcome is not None:
            raise DomainError("REWARD_NOT_APPLICABLE", "REWARD_NOT_APPLICABLE: inference modes never learn from rewards")
        self._states.activate(slot_key)
        started = time.perf_counter()
        result = self._controller.observe(frame.owned_rgb(), reinforcement if self._learning else "none")
        elapsed = time.perf_counter() - started
        # Not committed here: the automation revalidates its generation tokens
        # first and then calls commit() or discard().
        side = result["side"]
        if side not in ("BUY", "SELL", "HOLD"):
            raise DomainError("DECODER_OUTPUT_INVALID")
        admission = result.get("execution_admission", {"allowed": True, "reason": "not_applicable"})
        diagnostics = {key: result[key] for key in ("left_hz", "right_hz", "difference_hz", "gate_spikes", "total_spikes",
                                                     "reward_spikes", "aversive_spikes", "brain_ms", "spike_sha256")
                       if key in result}
        return Decision(decision_id=decision_id, instrument=frame.instrument, side=side, decided_at=frame.decision_at,
                        controller_profile=self.profile.value, frame_sha256=frame.sha256, observed_through=frame.observed_through,
                        reinforcement=reinforcement if self._learning else "none", pipeline_fixture=False,
                        admission_allowed=bool(admission.get("allowed", True)), admission_reason=str(admission.get("reason", "not_applicable")),
                        compute_seconds=elapsed, diagnostics=diagnostics, provenance=self.provenance(slot_key))

    def close(self) -> None:
        self._closed = True


def build_controller(profile: ControllerProfile, *, environment: Environment, dataset: Any | None, kernel: Any | None,
                     learning: bool, fixture_sides: Sequence[Side] | None = None, neural: NeuralSettings | None = None,
                     checkpoints: Path | None = None, research_mode: bool = False) -> DecisionController:
    """Construct the declared controller or raise ControllerUnavailable with the missing pieces.

    The mode is the strategy's declaration (``neural.mode``): baseline
    inference, a named frozen checkpoint, or research learning under a
    declared training profile. Nothing is chosen for the operator.
    """
    if profile is ControllerProfile.FIXTURE_SEQUENCE_V1:
        if fixture_sides is None:
            raise ControllerUnavailable("FIXTURE_SEQUENCE_REQUIRED", missing=("fixture_sides",))
        return FixtureSequenceController(fixture_sides, environment=environment)
    settings = neural or NeuralSettings()
    missing = []
    if dataset is None:
        missing.append("DATASET_NOT_READY")
    if kernel is None:
        missing.append("NATIVE_KERNEL_UNAVAILABLE")
    if settings.mode is ControllerMode.RESEARCH_LEARNING:
        if not learning or not research_mode:
            missing.append("RESEARCH_MODE_REQUIRED")
        if environment is not Environment.SANDBOX:
            missing.append("RESEARCH_LEARNING_SANDBOX_ONLY")
    elif learning:
        missing.append("LEARNING_REQUIRES_RESEARCH_LEARNING_MODE")
    if missing:
        raise ControllerUnavailable(missing[0], missing=missing)
    if settings.mode is ControllerMode.RESEARCH_LEARNING:
        from coinfly.strategy.research_controller import ResearchLearningController

        assert settings.training is not None
        return ResearchLearningController.build(dataset=dataset, kernel=kernel, settings=settings.training)
    return PreservedMaleCNSController.build(dataset=dataset, kernel=kernel, neural=settings, checkpoints=checkpoints)
