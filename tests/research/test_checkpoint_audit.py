"""Small array-only fixtures exercise checkpoint validation without native loading."""

import json
from dataclasses import asdict
from threading import RLock

import numpy as np
import pytest

from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.neural.controller import FlyController
from coinfly.research.profile import NeuralProfile
from tests.research import test_preserved_profile as preserved


class SyntheticMemory(MemoryBrain):
    def __init__(self):
        # No constructor/load bypass is exposed by production code. This small
        # test double has no graph attestation, native kernel or trading methods.
        self.n, self.dt = 4, .1
        self.ids = np.arange(4, dtype=np.int64)
        self.ptr = np.array([0, 1, 2, 2, 2], dtype=np.int64)
        self.post = np.array([2, 3], dtype=np.int32)
        self.weight = np.ones(2, dtype=np.float32)
        self.circuit = {"edges": np.array([0], dtype=np.int64)}
        self.baseline_plastic = np.ones(1, dtype=np.float32)
        arrays = preserved.NativeCheckpointTests().valid_arrays()
        arrays.update({name: np.zeros(1) for name in ("rate_kc", "rate_dan", "memory_u", "memory_w")})
        self.fields = list(arrays)
        for name, value in arrays.items():
            setattr(self, name, value)
        self.eta, self.cursor, self.sim_ms = .001, 0, 0.
        self.total_spikes, self.weights_frozen = 0, False
        self.build = {"fixture": "SYNTHETIC_NOT_MALECNS"}

    def configuration_signature(self):
        return {"fixture": "SYNTHETIC_NOT_MALECNS"}


def rewrite(path, mutate):
    with np.load(path, allow_pickle=False) as archive:
        arrays = {key: archive[key].copy() for key in archive.files}
    mutate(arrays)
    np.savez(path, **arrays)


@pytest.mark.parametrize("field,value", [("memory_u", 10.), ("memory_w", .5), ("rate_kc", -1.)])
def test_restore_rejects_inconsistent_memory_without_partial_mutation(tmp_path, field, value):
    brain = SyntheticMemory()
    path = tmp_path / "synthetic.npz"
    brain.checkpoint(path)
    rewrite(path, lambda arrays: arrays[field].fill(value))
    before = {name: getattr(brain, name).copy() for name in ("weight", *brain.fields)}
    with pytest.raises(ValueError, match="Checkpoint"):
        brain.restore(path)
    for name, expected in before.items():
        np.testing.assert_array_equal(getattr(brain, name), expected)


def test_checkpoint_round_trip_and_fixed_weight_provenance(tmp_path):
    brain = SyntheticMemory()
    brain.memory_u[:] = -.25
    brain.memory_w[:] = -.125
    brain.weight[0] = .875
    path = tmp_path / "synthetic.npz"
    brain.checkpoint(path)
    restored = SyntheticMemory()
    restored.restore(path)
    for name in ("weight", *brain.fields):
        np.testing.assert_array_equal(getattr(brain, name), getattr(restored, name))
    rewrite(path, lambda arrays: arrays["weight"].__setitem__(1, .9))
    with pytest.raises(ValueError, match="fixed synaptic"):
        restored.restore(path)


@pytest.mark.parametrize("fingerprint", [
    "74246bea3958a6e5e59bc81adbbd67a59ab713b59af6a2b9a29fa9f47fab77a7",
    "0" * 64,
    None,
])
def test_checkpoint_requires_exact_current_rule_provenance(tmp_path, monkeypatch, fingerprint):
    brain = SyntheticMemory()
    signature = {"fixture": "SYNTHETIC_NOT_MALECNS",
                 "rule_sha256": "7aa56b82e670303f7b2da7109ee8a6865c504b78d68d3c2e735c62a848c763e9"}
    monkeypatch.setattr(brain, "configuration_signature", lambda: dict(signature))
    path = tmp_path / "synthetic.npz"
    brain.checkpoint(path)
    brain.plasticity_diagnostics = {"bins": 10}
    brain.restore(path)
    assert brain.plasticity_diagnostics == {}

    def change_rule(arrays):
        metadata = json.loads(str(arrays["metadata"]))
        metadata["configuration_sha256"]["rule_sha256"] = fingerprint
        arrays["metadata"] = np.array(json.dumps(metadata))
    rewrite(path, change_rule)
    before = {name: getattr(brain, name).copy() for name in ("weight", *brain.fields)}
    brain.plasticity_diagnostics = {"bins": 11}
    with pytest.raises(ValueError, match="provenance"):
        brain.restore(path)
    assert brain.plasticity_diagnostics == {"bins": 11}
    for name, expected in before.items():
        np.testing.assert_array_equal(getattr(brain, name), expected)


def test_controller_checkpoint_binds_learning_and_decoder_profile(tmp_path):
    controller = FlyController.__new__(FlyController)
    controller.s, controller.brain = NeuralProfile(), SyntheticMemory()
    controller._lock, controller._failed = RLock(), False
    path = tmp_path / "synthetic.npz"
    controller.save(path)
    controller.restore(path)
    for changed in (NeuralProfile(learning=False), NeuralProfile(decoder_threshold_hz=3),
                    NeuralProfile(pulse_current=21), NeuralProfile(neural_ms=600)):
        controller.s = changed
        with pytest.raises(ValueError, match="provenance"):
            controller.restore(path)


def test_controller_rejects_mismatched_frozen_flag_and_unbound_checkpoint(tmp_path):
    brain = SyntheticMemory()
    path = tmp_path / "synthetic.npz"
    profile = asdict(NeuralProfile())
    brain.checkpoint(path, controller_profile=profile)
    def freeze(arrays):
        metadata = json.loads(str(arrays["metadata"]))
        metadata["weights_frozen"] = True
        arrays["metadata"] = np.array(json.dumps(metadata))
    rewrite(path, freeze)
    with pytest.raises(ValueError, match="learning mode"):
        brain.restore(path, controller_profile=profile)
    brain.checkpoint(path)
    with pytest.raises(ValueError, match="provenance"):
        brain.restore(path, controller_profile=profile)


@pytest.mark.parametrize("field", ["neural_ms", "neural_bin_ms", "pulse_ms"])
def test_profile_cannot_round_a_positive_duration_to_zero_ticks(field):
    with pytest.raises(ValueError):
        NeuralProfile(**{field: 1e-10})


def test_concurrent_checkpoints_use_distinct_temporary_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    original = np.savez_compressed
    barrier = Barrier(2)
    names = []
    def simultaneous(handle, **values):
        names.append(handle.name)
        barrier.wait(timeout=3)
        return original(handle, **values)
    monkeypatch.setattr(np, "savez_compressed", simultaneous)
    paths = [tmp_path / "same.npz", tmp_path / "same.checkpoint"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(SyntheticMemory().checkpoint, path) for path in paths]
        for future in pending:
            future.result(timeout=3)
    assert len(set(names)) == 2
    assert not list(tmp_path.glob("*.partial"))
    for path in paths:
        SyntheticMemory().restore(path)


def test_failed_checkpoint_keeps_last_good_archive_and_removes_partial(tmp_path, monkeypatch):
    brain = SyntheticMemory()
    path = tmp_path / "synthetic.npz"
    brain.checkpoint(path)
    previous = path.read_bytes()
    def fail(*_args, **_kwargs):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(np, "savez_compressed", fail)
    with pytest.raises(OSError):
        brain.checkpoint(path)
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob("*.partial"))
