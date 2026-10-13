"""FROZEN_CHECKPOINT loading policy: named, provenance-checked, frozen, the declared experiment only.

The brain here is a stub that records what ``restore`` was asked to verify;
the real provenance checks (graph digests, build, parameters, configuration
signature, array layout) live in ``MemoryBrain.restore`` and are covered by
``tests/research/test_checkpoint_audit.py`` on the qualified host.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import TestCase

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.research.profile import NeuralProfile
from coinfly.strategy.checkpoints import checkpoint_path, inspect_checkpoint, restore_frozen_checkpoint
from coinfly.strategy.definition import (
    ControllerMode,
    NeuralSettings,
    StrategyDefinition,
    research_paper_preset,
)


class StubBrain:
    def __init__(self) -> None:
        self.fields = ["v", "g"]
        self.v = np.zeros(4)
        self.g = np.zeros(4)
        self.weight = np.ones(8, dtype=np.float32)
        self.circuit = {"edges": np.array([1, 2])}
        self.weights_frozen = False
        self.restored_with: dict[str, Any] | None = None
        self.refuse = False

    def restore(self, path: Path, *, controller_profile: dict[str, Any] | None = None) -> None:
        if self.refuse:
            raise ValueError("Checkpoint provenance mismatch")
        self.restored_with = controller_profile
        self.weights_frozen = True


def write_checkpoint(path: Path, brain: StubBrain, metadata: dict[str, Any]) -> None:
    np.savez_compressed(path, metadata=json.dumps(metadata), weight=brain.weight, v=brain.v, g=brain.g)


def frozen_profile(**experiment: Any) -> dict[str, Any]:
    profile = asdict(NeuralProfile(learning=False))
    if experiment:
        profile["experiment"] = {"profile_id": "malecns-risk-adjusted-experiment-v2", "implementation_revision": 4, **experiment}
        profile["reward"] = {"interval_seconds": 1.0}
    return profile


class CheckpointPolicyTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.brain = StubBrain()
        self.controller = SimpleNamespace(brain=self.brain, s=NeuralProfile(learning=False), _failed=True)

    def metadata(self, *, frozen: bool = True, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cursor": 10, "total_spikes": 5, "weights_frozen": frozen,
                "controller_profile": profile if profile is not None else frozen_profile(seed=1)}

    def test_named_frozen_experiment_checkpoint_restores_with_its_own_declared_profile(self) -> None:
        path = self.directory / "eval-v2.npz"
        write_checkpoint(path, self.brain, self.metadata())
        identity = restore_frozen_checkpoint(self.controller, checkpoint_path(self.directory, "eval-v2"),
                                             expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        self.assertEqual(self.brain.restored_with, frozen_profile(seed=1))
        self.assertEqual((identity["kind"], identity["checkpoint_id"], identity["training_profile_id"], identity["implementation_revision"]),
                         ("FROZEN_CHECKPOINT", "eval-v2.npz", "malecns-risk-adjusted-experiment-v2", 4))
        self.assertEqual((identity["cursor"], identity["total_spikes"]), (10, 5))
        self.assertEqual(len(identity["file_sha256"]), 64)
        self.assertFalse(self.controller._failed)

    def test_wrong_experiment_is_refused_never_loaded_silently(self) -> None:
        path = self.directory / "eval-v2.npz"
        write_checkpoint(path, self.brain, self.metadata())
        with self.assertRaises(DomainError) as refused:
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v3")
        self.assertEqual(refused.exception.code, "CHECKPOINT_PROFILE_MISMATCH")
        self.assertIsNone(self.brain.restored_with)
        # A plain baseline checkpoint (no experiment) needs the strategy to say so (None).
        write_checkpoint(path, self.brain, self.metadata(profile=frozen_profile()))
        with self.assertRaises(DomainError):
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        identity = restore_frozen_checkpoint(self.controller, path, expected_training_profile_id=None)
        self.assertIsNone(identity["training_profile_id"])

    def test_learning_or_unfrozen_checkpoints_are_refused(self) -> None:
        path = self.directory / "train.npz"
        learning = {**frozen_profile(seed=1), "learning": True}
        write_checkpoint(path, self.brain, self.metadata(frozen=False, profile=learning))
        with self.assertRaises(DomainError) as refused:
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        self.assertEqual(refused.exception.code, "CHECKPOINT_NEURAL_PROFILE_MISMATCH")
        neural_mismatch = {**frozen_profile(seed=1), "neural_ms": 250.0}
        write_checkpoint(path, self.brain, self.metadata(profile=neural_mismatch))
        with self.assertRaises(DomainError) as refused:
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        self.assertEqual(refused.exception.code, "CHECKPOINT_NEURAL_PROFILE_MISMATCH")
        write_checkpoint(path, self.brain, self.metadata(frozen=False))
        with self.assertRaises(DomainError) as refused:
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        self.assertEqual(refused.exception.code, "CHECKPOINT_NOT_FROZEN")

    def test_brain_provenance_refusal_is_surfaced(self) -> None:
        path = self.directory / "other-graph.npz"
        write_checkpoint(path, self.brain, self.metadata())
        self.brain.refuse = True
        with self.assertRaises(DomainError) as refused:
            restore_frozen_checkpoint(self.controller, path, expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        self.assertEqual(refused.exception.code, "CHECKPOINT_PROVENANCE_MISMATCH")

    def test_checkpoint_ids_are_file_names_only_and_archives_are_validated(self) -> None:
        with self.assertRaises(DomainError) as refused:
            checkpoint_path(self.directory, "../escape")
        self.assertEqual(refused.exception.code, "CHECKPOINT_ID_INVALID")
        with self.assertRaises(DomainError) as missing:
            checkpoint_path(self.directory, "absent")
        self.assertEqual(missing.exception.code, "CHECKPOINT_NOT_FOUND")
        path = self.directory / "wrong-layout.npz"
        np.savez_compressed(path, metadata=json.dumps(self.metadata()), weight=np.ones(3, dtype=np.float32), v=self.brain.v, g=self.brain.g)
        with self.assertRaises(DomainError) as invalid:
            inspect_checkpoint(path, self.brain)
        self.assertEqual(invalid.exception.code, "CHECKPOINT_INVALID")


class NeuralSettingsSchemaTests(TestCase):
    def test_modes_declare_exactly_their_inputs(self) -> None:
        NeuralSettings()
        NeuralSettings(mode=ControllerMode.FROZEN_CHECKPOINT, checkpoint_id="eval-v2",
                       expected_training_profile_id="malecns-risk-adjusted-experiment-v2")
        with self.assertRaises(ValueError):
            NeuralSettings(mode=ControllerMode.FROZEN_CHECKPOINT)
        with self.assertRaises(ValueError):
            NeuralSettings(checkpoint_id="eval-v2")
        with self.assertRaises(ValueError):
            NeuralSettings(mode=ControllerMode.RESEARCH_LEARNING)

    def test_learning_flag_is_the_research_learning_mode(self) -> None:
        def load(document: dict[str, Any]) -> StrategyDefinition:
            return StrategyDefinition.model_validate_json(json.dumps(document))

        preset = research_paper_preset()
        base = preset.model_dump(mode="json")
        self.assertIs(preset.neural.mode, ControllerMode.BASELINE_INFERENCE)
        self.assertFalse(preset.learning_enabled)
        with self.assertRaises(ValueError):  # learning without the declared mode: nothing learns silently
            load({**base, "learning_enabled": True})
        research = load({**base, "learning_enabled": True, "max_active_slots": 1,
                         "neural": {"mode": "RESEARCH_LEARNING", "training": {"profile_id": "malecns-risk-adjusted-experiment-v2",
                                                                              "implementation_revision": 4, "reward_interval_seconds": 60}}})
        self.assertIs(research.neural.mode, ControllerMode.RESEARCH_LEARNING)
        document = research.model_dump(mode="json")
        with self.assertRaises(ValueError):  # two slots would be two sessions
            load({**document, "max_active_slots": 2})
        with self.assertRaises(ValueError):  # reward windows are the accounting windows
            load({**document, "execution": {**document["execution"], "accounting_interval_seconds": 30}})
        with self.assertRaises(ValueError):  # a schema-3 mode declaration on the fixture controller
            load({**document, "controller_profile": "fixture-sequence-v1", "fixture_sides": ["HOLD"]})
