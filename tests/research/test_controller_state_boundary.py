"""Synthetic controller fixtures; no MaleCNS assets or native kernel loaded."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from types import SimpleNamespace

import numpy as np
import pytest

from coinfly.research.neural.controller import FlyController
from coinfly.research.profile import NeuralProfile


class SyntheticBrain:
    n = 1
    dt = .1

    def __init__(self):
        self.counts = np.zeros(1, dtype=np.int32)
        self.circuit = {name: np.array([0]) for name in ("reward", "aversive", "kc")}
        self.sim_ms = 0.
        self.frames = []
        self.first_bin = None
        self.fail = False
        self.saved = []

    def rgb_step(self, rgb, interval, **_kwargs):
        self.frames.append(rgb.copy())
        if self.first_bin is not None and len(self.frames) == 1:
            self.first_bin()
        self.sim_ms += interval
        if self.fail and len(self.frames) == 2:
            raise ValueError("synthetic partial neural failure")
        return np.ones(1, dtype=np.int32), 0.

    def memory(self):
        return {"synthetic": True}

    def checkpoint(self, path, **_kwargs):
        self.saved.append(self.sim_ms)

    def restore(self, path, **_kwargs):
        self.sim_ms = 0.
        self.frames.clear()
        self.fail = False


def controller():
    result = FlyController.__new__(FlyController)
    result.s = NeuralProfile(neural_ms=30., neural_bin_ms=10., pulse_ms=10.)
    result._lock, result._failed = RLock(), False
    result.brain = SyntheticBrain()
    result.decoder = SimpleNamespace(decode=lambda *_: {"side": "HOLD"})
    return result


def test_observation_owns_one_frame_across_bins_and_provenance_hash():
    model = controller()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    expected_hash = hashlib.sha256(frame.tobytes()).hexdigest()
    model.brain.first_bin = lambda: frame.fill(255)
    result = model.observe(frame, "none")
    assert all(not stored.any() for stored in model.brain.frames)
    assert result["input_sha256"] == expected_hash
    assert result["total_spikes"] == 3


@pytest.mark.parametrize("operation", ["observe", "save", "restore"])
def test_controller_operations_cannot_interleave_neural_bins(tmp_path, operation):
    model = controller()
    entered, release, attempted = Event(), Event(), Event()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    def pause():
        entered.set()
        assert release.wait(3)
    model.brain.first_bin = pause
    def second():
        attempted.set()
        if operation == "observe":
            return model.observe(np.ones_like(frame), "none")
        return getattr(model, operation)(tmp_path / "synthetic.npz")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(model.observe, frame, "none")
        try:
            assert entered.wait(3)
            other = pool.submit(second)
            assert attempted.wait(3)
            assert not other.done()
        finally:
            release.set()
        assert first.result(timeout=3)["brain_ms"] == 30.
        other.result(timeout=3)
    if operation == "save":
        assert model.brain.saved == [30.]
    elif operation == "observe":
        assert [int(frame[0, 0, 0]) for frame in model.brain.frames] == [0, 0, 0, 1, 1, 1]
    else:
        assert model.brain.sim_ms == 0.


def test_partial_observation_failure_requires_restore_before_reuse_or_save(tmp_path):
    model = controller()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    model.brain.fail = True
    with pytest.raises(ValueError, match="partial neural failure"):
        model.observe(frame, "none")
    for action in (lambda: model.observe(frame, "none"), lambda: model.save(tmp_path / "bad.npz")):
        with pytest.raises(RuntimeError, match="requires restore"):
            action()
    assert model.brain.saved == []
    model.restore(tmp_path / "synthetic.npz")
    assert model.observe(frame, "none")["brain_ms"] == 30.


def test_freeze_retains_neural_state_and_declares_evaluation_profile():
    model = controller()
    model.observe(np.zeros((2, 2, 3), dtype=np.uint8), "reward")
    before = model.brain.sim_ms
    model.freeze_learning()
    assert model.brain.sim_ms == before
    assert model.s.learning is False
    assert model.brain.weights_frozen is True
    assert model.s.decoder_threshold_hz == 2
