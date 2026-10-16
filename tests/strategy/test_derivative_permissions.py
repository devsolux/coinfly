"""Declared derivative permissions take effect on the paper perpetual book, or are refused.

``margin_mode`` and ``max_maintenance_utilization`` are strategy fields. The paper
book is one pooled linear margin, so only CROSS has implemented semantics; the
maintenance ceiling is enforced by admission against every margin scenario.
"""

from __future__ import annotations

from coinfly.domain.context import DomainError
from tests.worker.test_automation_lifecycle import DerivativesFixture, derivatives_strategy


def permissions(**overrides: str) -> dict[str, object]:
    return {"max_leverage": "2", "directions": "BOTH", "allow_reversal": True, "margin_mode": "CROSS",
            "max_maintenance_utilization": "0.5", **overrides}


class DerivativePermissionsTests(DerivativesFixture):
    def intent_count(self) -> int:
        return int(self.worker.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]))

    def test_isolated_margin_mode_is_refused_at_configuration(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(DomainError, "MARGIN_MODE_UNSUPPORTED"):
            self.configure(derivatives_strategy(["BUY"], derivatives=permissions(margin_mode="ISOLATED")))
        self.assertIsNone(self.worker.automation.config())

    def test_entry_above_the_declared_maintenance_utilization_is_rejected_not_resized(self) -> None:
        # A 0.04 BTC entry near 100 on 100 of paper collateral projects about 0.002 utilization.
        self.initialize()
        self.configure(derivatives_strategy(["BUY"], derivatives=permissions(max_maintenance_utilization="0.001")))
        self.enable()
        self.tick(4)
        buy = self.decision()
        self.assertEqual((buy["side"], buy["outcome"], buy["outcome_code"]),
                         ("BUY", "RISK_REJECTED", "MAINTENANCE_UTILIZATION_LIMIT"), buy)
        self.assertEqual(self.intent_count(), 0)
        self.assertEqual(self.position(), 0)
