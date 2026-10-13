"""Only RGB and engineered reinforcement enter the network. No market policy."""

import hashlib
import math
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np

from coinfly.research.assets import NeuralPaths, VerifiedNativeKernel
from coinfly.research.profile import NeuralProfile

from .common import annotations
from .visual import VisualMemoryBrain


class Decoder:
    def __init__(self, ids: Any, annotation: Any, threshold: float) -> None:
        if len(ids) != len(annotation):
            raise ValueError("Decoder annotations must match the graph")
        types = annotation.type.fillna("")
        sides = annotation.somaSide.fillna("")
        self.left = np.flatnonzero(types.eq("DNp20") & sides.eq("L"))
        self.right = np.flatnonzero(types.eq("DNp20") & sides.eq("R"))
        self.gate = np.flatnonzero(types.eq("DNpe017"))
        if not len(self.left) or not len(self.right) or not len(self.gate):
            raise RuntimeError("Missing annotated BCI outputs")
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("Positive finite fixed decoder threshold required")
        self.neuron_count = len(ids)
        self.threshold = threshold
        self.identities = {
            k: [str(ids[i]) for i in getattr(self, k)]
            for k in ["left", "right", "gate"]
        }

    def decode(self, counts: Any, seconds: float) -> dict[str, Any]:
        counts = np.asarray(counts)
        if (counts.shape != (self.neuron_count,) or counts.dtype.kind not in "iu"
                or np.any(counts < 0) or not math.isfinite(seconds) or seconds <= 0):
            raise ValueError("Nonnegative integer counts and positive finite interval required")
        # Mean rates prevent side population size from creating a built-in bias.
        left = float(np.mean(counts[self.left]) / seconds)
        right = float(np.mean(counts[self.right]) / seconds)
        difference = right - left
        gate = int(counts[self.gate].sum())
        side = (
            "HOLD"
            if not gate or abs(difference) < self.threshold
            else "BUY"
            if difference > 0
            else "SELL"
        )
        return {
            "side": side,
            "left_hz": left,
            "right_hz": right,
            "difference_hz": difference,
            "gate_spikes": gate,
            "cell_ids": self.identities,
        }


class FlyController:
    def __init__(self, settings: NeuralProfile, paths: NeuralPaths, kernel: VerifiedNativeKernel) -> None:
        self.s = settings
        self._lock = RLock()
        self._failed = False
        self.brain = VisualMemoryBrain(paths, kernel)
        self.brain.weights_frozen = not settings.learning
        self.decoder = Decoder(
            self.brain.ids, annotations(self.brain.ids, paths.annotations), settings.decoder_threshold_hz
        )

    def observe(self, rgb: Any, reinforcement: str) -> dict[str, Any]:
        if reinforcement not in ("none", "reward", "aversive"):
            raise ValueError("Unknown reinforcement")
        # Own one immutable observation for every bin and for its provenance
        # hash; caller buffers can otherwise change while NumPy/ctypes drop GIL.
        frame = np.array(rgb, copy=True)
        if (frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8
                or min(frame.shape[:2]) < 1):
            raise ValueError("Nonempty RGB uint8 required")
        frame.flags.writeable = False
        with self._lock:
            if self._failed:
                raise RuntimeError("Neural state requires restore after failed observation")
            try:
                return self._observe(frame, reinforcement)
            except Exception:
                # A failed later bin may follow earlier committed neural ticks.
                # Do not silently reuse or save this partial observation state.
                self._failed = True
                raise

    def _observe(self, rgb: Any, reinforcement: str) -> dict[str, Any]:
        b = self.brain
        counts = np.zeros(b.n, dtype=np.int32)
        wall = 0.0
        remaining = round(self.s.neural_ms / b.dt)
        pulse = self._pulse_ticks(reinforcement)
        delivered = 0
        while remaining:
            n = min(remaining, round(self.s.neural_bin_ms / b.dt))
            if pulse:
                n = min(n, pulse)
            stimulus = (
                (b.circuit[reinforcement], self.s.pulse_current) if pulse else None
            )
            c, elapsed = b.rgb_step(
                rgb, n * b.dt, learning=self.s.learning, stimulation=stimulus, **self._step_options()
            )
            counts += c
            wall += elapsed
            remaining -= n
            if pulse:
                delivered += n
                pulse -= n
        b.counts[:] = counts
        return {
            **self.decoder.decode(counts, self.s.neural_ms / 1000),
            "brain_ms": b.sim_ms,
            "compute_seconds": wall,
            "stimulus": reinforcement,
            "stimulus_ms": delivered * b.dt,
            "reward_spikes": int(counts[b.circuit["reward"]].sum()),
            "aversive_spikes": int(counts[b.circuit["aversive"]].sum()),
            "KC_spikes": int(counts[b.circuit["kc"]].sum()),
            "total_spikes": int(counts.sum()),
            "spike_sha256": hashlib.sha256(counts.tobytes()).hexdigest(),
            "input_sha256": hashlib.sha256(np.asarray(rgb).tobytes()).hexdigest(),
            "memory": b.memory(),
        }

    def _pulse_ticks(self, reinforcement: str) -> int:
        return round(self.s.pulse_ms / self.brain.dt) if reinforcement != "none" else 0

    def _step_options(self) -> dict[str, Any]:
        return {}

    def _checkpoint_profile(self) -> dict[str, Any]:
        return asdict(self.s)

    def save(self, path: Path) -> None:
        with self._lock:
            if self._failed:
                raise RuntimeError("Neural state requires restore after failed observation")
            self.brain.checkpoint(path, controller_profile=self._checkpoint_profile())

    def freeze_learning(self) -> None:
        """One-way transition to chronological evaluation, retaining learned state.

        Restore the training checkpoint first, then freeze and save a separately
        named evaluation checkpoint. Decoder, neural state and parameters stay fixed.
        """
        with self._lock:
            if self._failed:
                raise RuntimeError("Neural state requires restore after failed observation")
            self.brain.weights_frozen = True
            self.s = replace(self.s, learning=False)

    def restore(self, path: Path) -> None:
        with self._lock:
            self.brain.restore(path, controller_profile=self._checkpoint_profile())
            self._failed = False
