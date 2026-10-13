"""Safety commands and status must stay responsive while full-graph inference runs.

A controller that stalls for a long time inside ``observe`` stands in for a
full-graph propagation (hundreds of milliseconds to seconds). While it runs,
``pause_entries``, ``status`` and ``stop`` must return within tens of
milliseconds, a stale result must be discarded without dispatch and without
advancing the slot's neural-state generation, and a controller failure must
not stop reconciliation services.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from coinfly.domain.context import Environment
from coinfly.strategy.controllers import FixtureSequenceController
from coinfly.worker.automation import AutomationState
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


class StallingController(FixtureSequenceController):
    """Scripted decisions with a deliberate compute stall and commit accounting."""

    def __init__(self, sides: Any, *, stall_seconds: float) -> None:
        super().__init__(sides, environment=Environment.SANDBOX)
        self.stall_seconds = stall_seconds
        self.started = threading.Event()
        self.release = threading.Event()
        self.commits: list[str] = []
        self.discards: list[str] = []
        self.fail_next = False

    def observe(self, frame: Any, *, reinforcement: str, decision_id: str, slot_key: str, outcome: Any = None) -> Any:
        self.started.set()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic propagation failure")
        if self.stall_seconds:
            self.release.wait(self.stall_seconds)
        return super().observe(frame, reinforcement=reinforcement, decision_id=decision_id, slot_key=slot_key, outcome=outcome)

    def commit(self, slot_key: str) -> None:
        self.commits.append(slot_key)

    def discard(self, slot_key: str) -> None:
        self.discards.append(slot_key)


class LivenessTests(AutomationFixture):
    def install(self, controller: StallingController) -> None:
        self.worker.automation._controller_factory = lambda strategy, environment, research: controller  # type: ignore[method-assign]

    def warm(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY", "BUY", "BUY", "BUY"]))
        self.enable()
        self.tick(3)  # warm-up: three observations, four-row window needs one more

    def test_pause_and_status_return_while_inference_stalls(self) -> None:
        controller = StallingController(["BUY", "BUY", "BUY"], stall_seconds=2.0)
        self.install(controller)
        self.warm()
        automation = self.worker.automation
        worker = threading.Thread(target=lambda: automation.tick(datetime.now(timezone.utc)), daemon=True)
        worker.start()
        self.assertTrue(controller.started.wait(5))
        started = time.perf_counter()
        status = automation.status()
        status_latency = time.perf_counter() - started
        started = time.perf_counter()
        paused = automation.pause_entries()
        pause_latency = time.perf_counter() - started
        controller.release.set()
        worker.join(timeout=10)
        self.assertLess(status_latency, 0.25, f"status blocked for {status_latency:.3f}s during inference")
        self.assertLess(pause_latency, 0.25, f"pause_entries blocked for {pause_latency:.3f}s during inference")
        self.assertTrue(paused["entries_paused"])
        self.assertIsNotNone(status["state"])
        # The stalled result completed after the pause: it must not have dispatched.
        decision = self.decision()
        self.assertNotEqual(decision.get("outcome"), "DISPATCHED", decision)
        self.assertIn(decision.get("outcome_code"), ("ENTRIES_PAUSED", "DISCARDED_ENTRIES_PAUSED", "AUTHORITY_NOT_READY"))
        self.assertEqual(controller.commits, [])
        self.assertEqual(controller.discards, ["BTC-USD"])
        self.assertEqual(self.worker.automation.status()["execution_health"]["open_intents"], 0)

    def test_inference_failure_keeps_reconciliation_alive(self) -> None:
        controller = StallingController(["BUY", "BUY"], stall_seconds=0.0)
        self.install(controller)
        self.warm()
        controller.fail_next = True
        report = self.tick()
        self.assertNotEqual(report["state"], AutomationState.FAILED.value)
        status = self.worker.automation.status()
        components = {item["component"]: item for item in status["components"]}
        self.assertFalse(components["strategy"]["ready"])
        self.assertIn("CONTROLLER_OBSERVE_FAILED", components["strategy"]["code"])
        # Ticks keep running the safety/reconciliation path and the loop is not stopped.
        report = self.tick()
        self.assertIn(report["state"], (AutomationState.SETUP.value, AutomationState.PAUSED_ENTRIES.value, AutomationState.WARMING_UP.value))
        self.assertFalse(self.worker.automation._stop_event.is_set())
        stopped = self.rpc("automation.stop", request_id="stop-after-failure")
        self.assertIn(stopped["state"], (AutomationState.DRAINING.value, AutomationState.STOPPED.value))

    def test_stale_result_after_reconfiguration_is_fenced(self) -> None:
        controller = StallingController(["BUY", "BUY", "BUY"], stall_seconds=2.0)
        self.install(controller)
        self.warm()
        automation = self.worker.automation
        worker = threading.Thread(target=lambda: automation.tick(datetime.now(timezone.utc)), daemon=True)
        worker.start()
        self.assertTrue(controller.started.wait(5))
        # A stop while the compute runs: the result must be discarded, not dispatched.
        started = time.perf_counter()
        self.rpc("automation.stop", request_id="stop-during-compute")
        stop_latency = time.perf_counter() - started
        controller.release.set()
        worker.join(timeout=10)
        self.assertLess(stop_latency, 1.5, f"stop blocked for {stop_latency:.3f}s during inference")
        decision = self.decision()
        self.assertNotEqual(decision.get("outcome"), "DISPATCHED", decision)
        self.assertEqual(controller.commits, [])
        self.assertEqual(self.worker.automation.status()["execution_health"]["open_intents"], 0)
