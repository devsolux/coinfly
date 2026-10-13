"""A synthetic experimental controller with the full-graph *shape* and a toy propagator.

This validates worker mechanics (session snapshot/restore, reward delivery,
credit expiry reporting) — never neural behaviour. The brain has the preserved
graph's neuron/edge counts so the identity check passes, tiny per-neuron state
arrays, one plastic edge and a scripted decoder. The real research kernels run
in pure Python here when numba is absent (container shim) and compiled on the
qualified host.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from typing import Any

import numpy as np

from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.profile import NeuralProfile
from coinfly.research.reward import DecisionCredit, RewardWindow
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.training import TrainingController, TrainingProfile
from coinfly.strategy.controllers import FULL_GRAPH_EDGES, FULL_GRAPH_NEURONS
from coinfly.strategy.sensory import SensoryFrame

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def synthetic_training_controller(*, sides: list[str] | None = None, profile: TrainingProfile | None = None,
                                  reward: RiskRewardConfig | None = None) -> tuple[Any, list[Any]]:
    n = FULL_GRAPH_NEURONS
    controller = TrainingController.__new__(TrainingController)
    controller.s = NeuralProfile(neural_ms=20.0, neural_bin_ms=10.0, pulse_ms=10.0, learning=True)
    controller._lock, controller._failed = RLock(), False
    seen: list[Any] = []
    script = list(sides or ["HOLD"])

    fields = ["v", "g", "refractory", "drive", "queue", "queue_count", "counts", "luminance", "active", "active_flag", "nactive",
              "memory_u", "memory_w"]
    # Full-graph-sized index/weight vectors (zero pages are never touched
    # except edge 0): the research kernels validate their lengths.
    post = np.zeros(FULL_GRAPH_EDGES, dtype=np.int32)
    post[0] = 1  # the one plastic edge: neuron 0 -> neuron 1 (as in the research fixture)
    weight = np.zeros(FULL_GRAPH_EDGES, dtype=np.float32)
    weight[0] = 1.0
    brain = SimpleNamespace(
        n=n, dt=0.1, fields=fields, post=post, eta=0.001, plasticity_diagnostics={}, sim_ms=0.0, cursor=0, total_spikes=0,
        weights_frozen=False, weight=weight, baseline_plastic=np.ones(1, dtype=np.float32),
        circuit={"reward": np.array([2]), "aversive": np.array([2]), "kc": np.array([0]), "pre": np.array([0]),
                 "edges": np.array([0]), "dan": np.array([2]), "gain": np.ones((1, 1))},
        dan_baseline_hz=np.zeros(1), initial_weight_sha256="synthetic", build={"model": "synthetic"},
    )
    for name in fields:
        if name in ("memory_u", "memory_w"):
            setattr(brain, name, np.zeros(1))
        elif name == "queue":
            setattr(brain, name, np.zeros((2, 4), dtype=np.int32))
        elif name == "queue_count":
            setattr(brain, name, np.zeros(2, dtype=np.int32))
        elif name == "nactive":
            setattr(brain, name, np.zeros(1, dtype=np.int32))
        elif name == "counts":
            setattr(brain, name, np.zeros(n, dtype=np.int32))
        else:
            setattr(brain, name, np.zeros(4, dtype=np.float64))
    brain.initial = {name: getattr(brain, name).copy() for name in fields}

    def step(rgb: Any, interval: float, **kwargs: Any) -> tuple[Any, float]:
        seen.append((rgb.copy(), kwargs))
        brain.sim_ms += interval
        brain.cursor += int(round(interval / brain.dt))
        counts = np.zeros(n, dtype=np.int32)
        counts[:3] = [1, 0, 1] if len(seen) % 2 else [0, 1, 1]
        brain.v[0] += 1.0  # visible per-slot dynamic state
        kwargs["experimental_plasticity"].advance(brain, counts, interval / 1000, learning=kwargs["learning"],
                                                  stimulation=kwargs["stimulation"], delta_limit=kwargs["plasticity_delta_limit"])
        brain.total_spikes += int(counts.sum())
        return counts, 0.0

    brain.rgb_step = step
    brain.memory = lambda: {}
    brain.prepare_experimental_frame = lambda *a, **kw: MemoryBrain.prepare_experimental_frame(brain, *a, **kw)
    brain.finalize_experimental_frame = lambda *a, **kw: MemoryBrain.finalize_experimental_frame(brain, *a, **kw)
    controller.brain = brain
    controller._initialize_training(profile or TrainingProfile(seed=1), reward or RiskRewardConfig())

    def decode(*_: Any) -> dict[str, Any]:
        side = script.pop(0) if len(script) > 1 else script[0]
        return {"side": side, "left_hz": 0.0, "right_hz": 0.0, "difference_hz": 0.0, "gate_spikes": 1, "cell_ids": {}}
    controller.decoder = SimpleNamespace(decode=decode)
    return controller, seen


def sensory_frame(at: datetime, *, instrument: str = "BTC-USD", mid: str = "100.00") -> SensoryFrame:
    ticks = np.zeros((4, 4), dtype=np.int64)
    rgb = np.full((4, 4, 3), 127, dtype=np.uint8)
    rgb.flags.writeable = False
    rows = tuple((Decimal(mid), Decimal("20.00"), Decimal(0), Decimal(0)) for _ in range(4))
    return SensoryFrame(rgb=rgb, ticks=ticks, features=rows, decision_at=at, observed_through=at - timedelta(milliseconds=10),
                        fitted_through=at - timedelta(seconds=60), instrument=instrument, available_through=at - timedelta(milliseconds=5))


def reward_window(start: datetime, *, seconds: float = 1.0, pnl: str = "0", capital: str = "100",
                  credits: tuple[DecisionCredit, ...] = ()) -> RewardWindow:
    end = start + timedelta(seconds=seconds)
    return RewardWindow(start_at=start, end_at=end, available_at=end, currency="USD", pnl=Decimal(pnl), capital=Decimal(capital),
                        basis="net_execution", credits=credits)
