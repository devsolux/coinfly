"""Multi-slot isolation through the worker: BTC alone equals BTC interleaved with ETH, bit for bit.

The preserved controller wrapper runs over a synthetic full-graph-shaped brain
with a deterministic toy propagator (no kernel here); the worker drives two
owned slots through real ticks. Evidence level: scripted pipeline.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import numpy as np

from coinfly.strategy.controllers import FULL_GRAPH_EDGES, PreservedMaleCNSController
from tests.strategy.test_neural_slot_isolation import synthetic_brain
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


class ToyFlyController:
    """Deterministic decoded sides from the brain's recurrent state; frozen weights."""

    def __init__(self) -> None:
        self.brain = synthetic_brain(n=166_700, edges=64, frozen=True)
        self.brain.post = np.zeros(FULL_GRAPH_EDGES, dtype=np.int8)
        self.s = SimpleNamespace(learning=False)

    def observe(self, rgb: Any, reinforcement: str) -> dict[str, Any]:
        stimulus = float(np.asarray(rgb)[:, :, 0].mean()) / 255.0
        self.brain.step(stimulus, learning=False)
        total = int(self.brain.counts.sum())
        side = "BUY" if total % 3 == 0 else "SELL" if total % 3 == 1 else "HOLD"
        return {"side": side, "left_hz": 0.0, "right_hz": 0.0, "difference_hz": 0.0, "gate_spikes": total, "total_spikes": total,
                "spike_sha256": "0" * 64}


def preserved_strategy(instruments: tuple[str, ...]) -> dict[str, Any]:
    definition = fixture_strategy(["HOLD"], instruments=instruments, max_active_slots=len(instruments))
    definition.update({"strategy_id": "multi-slot-neural-test", "label": "MULTI-SLOT — synthetic brain mechanics test",
                       "controller_profile": "coinfly-dual-compartment-v1", "fixture_sides": []})
    return definition


class MultiSlotNeuralTests(AutomationFixture):
    def drive(self, instruments: tuple[str, ...], rounds: int) -> tuple[PreservedMaleCNSController, list[str]]:
        controller = PreservedMaleCNSController(ToyFlyController(), learning=False)
        self.worker.automation._controller_factory = lambda strategy, environment, research: controller  # type: ignore[method-assign]
        self.initialize()
        self.configure(preserved_strategy(instruments), pipeline_test_mode=False)
        self.enable()
        self.books["ETH-USD"] = ("49.90", "50.10")  # ETH sees a different picture than BTC
        digests = []
        self.tick(3)
        for _ in range(rounds):
            self.tick()
            digests.append(controller._states.state_digest("BTC-USD"))
            time.sleep(1.05)
        return controller, digests

    def test_btc_state_is_identical_alone_and_interleaved_with_eth(self) -> None:
        # The alone run lives in its own worker (own ledger, own request ids).
        alone_fixture = MultiSlotNeuralTests("test_btc_state_is_identical_alone_and_interleaved_with_eth")
        alone_fixture.setUp()
        try:
            _, alone = alone_fixture.drive(("BTC-USD",), rounds=3)
        finally:
            alone_fixture.doCleanups()
        controller, interleaved = self.drive(("BTC-USD", "ETH-USD"), rounds=3)
        self.assertEqual(alone, interleaved)
        self.assertEqual(len(set(alone)), 3)  # BTC's state evolved every round
        status = self.worker.automation.status()
        self.assertEqual(status["resources"]["admitted_slots"], 2)
        self.assertTrue(status["resources"]["shared_graph"])
        generations = {slot["instrument"]: slot["last_decision"]["provenance"]["state_generation"] for slot in status["slots"]}
        self.assertEqual(generations, {"BTC-USD": 2, "ETH-USD": 2})  # provenance carries the generation observed under
        self.assertEqual(controller._states.generation("BTC-USD"), 3)
        self.assertEqual(controller._states.generation("ETH-USD"), 3)
        self.assertNotEqual(controller._states.state_digest("BTC-USD"), controller._states.state_digest("ETH-USD"))
