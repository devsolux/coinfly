"""Opt-in recovery policy: bounded automatic resumption after a restart, every decision recorded.

The default stays manual re-enable (covered by the lifecycle restart test).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from coinfly.control.transition_state import TransitionPhase
from coinfly.worker.automation import AutomationState
from tests.worker.test_automation_lifecycle import AutomationFixture, fixture_strategy


class RecoveryPolicyTests(AutomationFixture):
    def recoveries(self) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.worker.writer.call(lambda connection: connection.execute(
            "SELECT resumed, reason, policy_version FROM cf_automation_recoveries ORDER BY decided_at").fetchall())]

    def restart(self) -> None:
        self.worker.close()
        self.worker = self.new_worker()

    def test_resumes_inside_the_mandate_after_reconciliation_and_records_it(self) -> None:
        self.initialize()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        self.configure(fixture_strategy(["HOLD"]), recovery={"auto_resume": True, "mandate_expires_at": expires, "max_resumptions": 1})
        self.enable()
        self.tick(2)
        self.restart()
        status = self.worker.automation.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["restart_reason"], "RESTART_REQUIRES_REENABLE")
        self.assertTrue(status["recovery"]["policy"]["auto_resume"])
        report = self.tick()
        self.assertTrue(report["auto_resume"]["resumed"], report)
        restoration = report["auto_resume"]["checks"]["restoration"]
        self.assertTrue(restoration["financial"]["ready"])
        self.assertEqual(restoration["neural"]["code"], "PIPELINE_FIXTURE")
        self.assertFalse(restoration["neural"]["session_resume"])
        self.assertEqual(self.recoveries(), [(1, "RESUMED_UNDER_RECOVERY_POLICY", 1)])
        # The resumption is the explicit-enable sequence: re-apply, arm, run.
        status = self.worker.automation.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["restart_reason"], "RESUMED_UNDER_RECOVERY_POLICY")
        operation_id = status["reactivation_operation_id"]
        assert operation_id is not None
        final = self.worker.coordinator.wait(operation_id, timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED)
        self.tick(5)
        self.assertIn(self.worker.automation.status()["state"], (AutomationState.RUNNING.value, AutomationState.OBSERVING.value, AutomationState.WARMING_UP.value))
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0]), 1)  # never reseeded
        # A second restart exceeds max_resumptions: refused, recorded, manual re-enable required.
        self.restart()
        report = self.tick()
        self.assertFalse(report["auto_resume"]["resumed"])
        self.assertIn("RECOVERY_RESUMPTIONS_EXHAUSTED", report["auto_resume"]["reasons"])
        self.assertEqual(self.recoveries()[-1][0], 0)
        self.assertEqual(self.worker.automation.status()["restart_reason"], "RESTART_REQUIRES_REENABLE:RECOVERY_REFUSED")
        self.assertFalse(self.worker.automation.status()["enabled"])

    def test_expired_mandate_and_open_obligations_refuse_resumption(self) -> None:
        self.initialize()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.configure(fixture_strategy(["BUY"]), recovery={"auto_resume": True, "mandate_expires_at": expires, "max_resumptions": 3})
        self.enable()
        self.tick(4)  # a resting BUY: an open intent survives the restart
        self.assertEqual(self.decision()["outcome"], "DISPATCHED")
        import time

        time.sleep(1.1)
        self.restart()
        report = self.tick()
        self.assertFalse(report["auto_resume"]["resumed"])
        reasons = report["auto_resume"]["reasons"]
        self.assertIn("RECOVERY_MANDATE_EXPIRED", reasons)
        self.assertIn("OPEN_INTENTS", reasons)
        self.assertEqual(self.recoveries()[0][0], 0)
        self.assertFalse(self.worker.automation.status()["enabled"])

    def test_policy_schema_requires_an_expiry_for_auto_resume(self) -> None:
        self.initialize()
        from coinfly.domain.context import DomainError

        with self.assertRaises(DomainError) as refused:
            self.configure(fixture_strategy(["HOLD"]), recovery={"auto_resume": True}, request_id="no-expiry")
        self.assertEqual(refused.exception.code, "INVALID_AUTOMATION_CONFIG")
