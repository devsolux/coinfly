"""RESEARCH_LEARNING through the worker: session, reward windows from the ledger, fenced disposal.

The controller is the real ``ResearchLearningController`` over the synthetic
full-graph-shaped ``TrainingController`` (``tests/strategy/synthetic_research``):
worker mechanics are exercised end to end; neural behaviour is not claimed.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from coinfly.strategy.definition import TrainingSettings
from coinfly.strategy.research_controller import ResearchLearningController
from tests.strategy.synthetic_research import synthetic_training_controller
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


def research_strategy(**overrides: Any) -> dict[str, Any]:
    definition = fixture_strategy(["HOLD"], max_active_slots=1)
    definition.update({
        "strategy_id": "research-session-test", "label": "RESEARCH — synthetic controller mechanics test",
        "controller_profile": "coinfly-dual-compartment-v1", "fixture_sides": [], "learning_enabled": True,
        "execution": {"proposal_ttl_seconds": 5, "decision_interval_seconds": 1, "target_step": "0.04", "max_turnover_fraction": "0.05",
                      "accounting_interval_seconds": 1, "order_ttl_seconds": 30},
        "neural": {"mode": "RESEARCH_LEARNING",
                   "training": {"profile_id": "malecns-risk-adjusted-experiment-v2", "implementation_revision": 4,
                                "reward_interval_seconds": 1, "reward_credit_horizon_seconds": 5}},
    })
    definition.update(overrides)
    return definition


class StallingResearchController(ResearchLearningController):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stall_seconds = 0.0
        self.started = threading.Event()
        self.release = threading.Event()

    def observe(self, frame: Any, *, reinforcement: str, decision_id: str, slot_key: str, outcome: Any = None) -> Any:
        self.started.set()
        if self.stall_seconds:
            self.release.wait(self.stall_seconds)
        return super().observe(frame, reinforcement=reinforcement, decision_id=decision_id, slot_key=slot_key, outcome=outcome)


class ResearchSessionTests(AutomationFixture):
    def install(self, sides: list[str]) -> StallingResearchController:
        training, _ = synthetic_training_controller(sides=sides)
        settings = TrainingSettings(profile_id="malecns-risk-adjusted-experiment-v2", implementation_revision=4,
                                    reward_interval_seconds=1.0, reward_credit_horizon_seconds=5.0)
        controller = StallingResearchController(training, settings=settings)
        self.worker.automation._controller_factory = lambda strategy, environment, research: controller  # type: ignore[method-assign]
        return controller

    def windows(self) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT window_start, state, fed_decision_id FROM cf_reward_windows ORDER BY window_start").fetchall())]

    def test_reward_windows_flow_from_the_ledger_into_the_session_in_order(self) -> None:
        controller = self.install(["BUY", "HOLD"])
        self.initialize()
        self.configure(research_strategy(), research_mode=True, pipeline_test_mode=False)
        self.enable()
        self.tick(4)
        status = self.worker.automation.status()
        research = status["research"]
        self.assertIsNotNone(research)
        self.assertEqual(research["reward_interval_seconds"], 1.0)
        self.assertEqual(status["provenance"]["mode"], "RESEARCH_LEARNING")
        self.assertEqual(status["provenance"]["checkpoint"]["kind"], "FRESH_QUALIFIED_GRAPH")
        first = self.decision()
        self.assertEqual((first["side"], first["outcome"]), ("BUY", "DISPATCHED"))
        self.assertIsNone(first["provenance"]["reward_window"])  # nothing had closed yet
        # Let a window close (post-tick equity samples), fill the order, decide again.
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.1)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        second = self.decision()
        self.assertEqual(second["side"], "HOLD")
        window = second["provenance"]["reward_window"]
        self.assertIsNotNone(window, second)
        self.assertEqual(window["credits"], 1)  # the BUY decision is credited for its inventory
        eligibility = second["provenance"]["training"]["eligibility"]
        self.assertEqual(eligibility["credit_allocations"], 1)
        self.assertEqual(eligibility["expired_credit_decisions"], 0)
        rows = self.windows()
        self.assertEqual(rows[0][1], "FED")
        self.assertIsNotNone(rows[0][2])
        research = self.worker.automation.status()["research"]
        self.assertEqual(research["windows_fed"], 1)
        self.assertEqual(research["horizon_exceeded"], 0)
        self.assertIn("eligibility", research["last_training"])
        self.assertEqual(self.worker.automation.status()["execution_health"]["intent_causes"]["DECODED_DECISION_ENTRY"], 1)
        self.assertEqual(controller.discarded, 0)

    def test_discarded_observation_keeps_its_window_and_learning_state(self) -> None:
        controller = self.install(["HOLD"])
        self.initialize()
        self.configure(research_strategy(), research_mode=True, pipeline_test_mode=False)
        self.enable()
        self.tick(4)
        time.sleep(1.1)
        self.tick()  # closes the first window (post-tick sample); the HOLD decision consumed nothing yet? it fed it if closed before
        fed_before = self.worker.automation.status()["research"]["windows_fed"]
        frames_before = controller._controller._frames
        time.sleep(1.1)
        controller.stall_seconds = 2.0
        automation = self.worker.automation
        worker = threading.Thread(target=lambda: automation.tick(datetime.now(timezone.utc)), daemon=True)
        worker.start()
        self.assertTrue(controller.started.wait(5))
        automation.pause_entries()  # during the compute: the result must be discarded
        controller.release.set()
        worker.join(timeout=10)
        decision = self.decision()
        self.assertEqual(decision["outcome"], "DISCARDED", decision)
        self.assertEqual(controller.discarded, 1)
        # The session did not advance (frames restored) and the window it was fed stays CLOSED for the next observation.
        self.assertEqual(controller._controller._frames, frames_before)
        self.assertEqual(self.worker.automation.status()["research"]["windows_fed"], fed_before)
        pending = [row for row in self.windows() if row[1] == "CLOSED"]
        self.assertTrue(pending)
