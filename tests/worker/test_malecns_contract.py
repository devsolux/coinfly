"""Full-MaleCNS runtime contract at the worker boundary.

* A scripted fixture controller can only be configured in explicit pipeline
  test mode (SANDBOX), and the mode is persisted and shown in status.
* The preserved controller refuses any graph that is not the full MaleCNS
  (166,700 neurons / 25,582,938 edges) — no reduced or substituted graph.
* Every intent traces to its cause: a decoded decision (entry/exit, with the
  decision id and the controller that produced it) or a protective drain.
* Status carries controller provenance and the kernel requirement wording.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.execution.intent_causes import causes_tx
from coinfly.strategy.controllers import (
    FULL_GRAPH_EDGES,
    FULL_GRAPH_NEURONS,
    ControllerUnavailable,
    PreservedMaleCNSController,
)
from coinfly.worker.automation import FEATURE_TRANSFORM_VERSION, AutomationState
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


class GraphIdentityTests(TestCase):
    def test_reduced_graph_is_refused_not_substituted(self) -> None:
        brain = SimpleNamespace(n=1000, post=[0] * 5000, fields=("v",), v=None)
        with self.assertRaises(ControllerUnavailable) as refused:
            PreservedMaleCNSController(SimpleNamespace(brain=brain), learning=False)
        self.assertEqual(refused.exception.code, "GRAPH_IDENTITY_MISMATCH")
        self.assertEqual(refused.exception.missing, (f"neurons=1000/{FULL_GRAPH_NEURONS}", f"edges=5000/{FULL_GRAPH_EDGES}"))


class PipelineTestModeTests(AutomationFixture):
    def test_fixture_controller_requires_explicit_test_mode(self) -> None:
        self.initialize()
        with self.assertRaises(DomainError) as refused:
            self.rpc("automation.configure", {"strategy": fixture_strategy(["HOLD"]), "feed": {"kind": "SYNTHETIC_FIXTURE"}}, request_id="no-mode")
        self.assertEqual(refused.exception.code, "FIXTURE_REQUIRES_TEST_MODE")
        self.assertFalse(self.worker.automation.status()["configured"])
        configured = self.configure(fixture_strategy(["HOLD"]))
        self.assertEqual(configured["state"], "SETUP")
        status = self.worker.automation.status()
        self.assertTrue(status["pipeline_test_mode"])
        self.assertTrue(status["strategy"]["pipeline_test_mode"])
        # The persisted configuration carries the flag: a restart shows it too.
        self.worker.close()
        self.worker = self.new_worker()
        self.assertTrue(self.worker.automation.status()["pipeline_test_mode"])

    def test_preset_does_not_need_test_mode_and_status_names_the_kernel_requirement(self) -> None:
        self.initialize()
        self.rpc("automation.configure", {"preset": "research-paper-spot", "feed": {"kind": "SYNTHETIC_FIXTURE"}}, request_id="preset")
        self.enable()
        self.tick(2)
        status = self.worker.automation.status()
        self.assertFalse(status["pipeline_test_mode"])
        components = {item["component"]: item for item in status["components"]}
        self.assertFalse(components["kernel"]["ready"])
        self.assertIn("Native execution of the full graph is required", components["kernel"]["detail"])
        self.assertEqual(components["strategy"]["code"], "NATIVE_KERNEL_UNAVAILABLE")
        self.assertIsNone(status["provenance"])  # no controller was built: nothing is claimed
        self.assertIsNone(status["last_decision"])


class IntentCauseTests(AutomationFixture):
    def causes(self) -> dict[str, dict[str, Any]]:
        context = self.context()
        return self.worker.writer.call(lambda connection: causes_tx(connection, context.identity))

    def test_entries_exits_and_drains_are_labeled_separately(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY", "HOLD", "BUY"]))
        self.enable()
        self.tick(4)
        buy = self.decision()
        self.assertEqual(buy["outcome"], "DISPATCHED")
        status = self.worker.automation.status()
        provenance = status["provenance"]
        self.assertEqual((provenance["controller"], provenance["mode"], provenance["pipeline_fixture"]),
                         ("fixture-sequence-v1", "PIPELINE_FIXTURE", True))
        self.assertEqual(provenance["feature_transform"], FEATURE_TRANSFORM_VERSION)
        causes = self.causes()
        [entry] = [item for item in causes.values()]
        self.assertEqual((entry["cause"], entry["origin"], entry["controller_profile"], entry["pipeline_fixture"]),
                         ("DECODED_DECISION_ENTRY", "AUTOMATION", "fixture-sequence-v1", True))
        self.assertEqual(entry["decision_id"], self.decision_id(buy))
        # Fill, then stop with the position open: the drain's exit is a protective
        # drain, never a decoded decision.
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.rpc("automation.stop", request_id="stop")
        for _ in range(30):
            if self.worker.automation.status()["state"] == AutomationState.STOPPED.value:
                break
            self.feed.books["BTC-USD"] = ("100.00", "100.10")
            time.sleep(0.05)
            self.tick()
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.STOPPED.value)
        summary = self.worker.automation.status()["execution_health"]["intent_causes"]
        self.assertEqual(summary["DECODED_DECISION_ENTRY"], 1)
        self.assertGreaterEqual(summary["PROTECTIVE_DRAIN"], 1)
        self.assertEqual(summary["UNLABELED"], 0)
        drains = [item for item in self.causes().values() if item["cause"] == "PROTECTIVE_DRAIN"]
        self.assertTrue(drains)
        self.assertEqual({item["origin"] for item in drains}, {"SLOT_DRAIN"})
        self.assertTrue(all(item["decision_id"] is None for item in drains))

    def decision_id(self, decision: dict[str, Any]) -> str:
        row = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT decision_id FROM cf_automation_decisions WHERE instrument=? AND side='BUY' ORDER BY decided_at LIMIT 1",
            (decision["instrument"],)).fetchone())
        return str(row[0])

    def test_strategy_exit_is_a_decoded_exit(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY", "SELL"]))
        self.enable()
        self.tick(4)
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        sell = self.decision()
        self.assertEqual((sell["side"], sell["outcome"]), ("SELL", "DISPATCHED"))
        causes = {item["cause"] for item in self.causes().values()}
        self.assertEqual(causes, {"DECODED_DECISION_ENTRY", "DECODED_DECISION_EXIT"})
