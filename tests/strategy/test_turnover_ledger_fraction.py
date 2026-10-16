from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from coinfly.domain.context import DomainError
from coinfly.storage.writer import Writer
from coinfly.strategy.turnover_ledger import TurnoverLedger
from tests.execution.helpers import context

NOW = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)


class TurnoverLedgerFractionTests(unittest.TestCase):
    """The configured ``max_turnover_fraction`` (not only the 5 % research ceiling) bounds admission."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.writer = Writer(Path(self.temp.name) / "turnover.sqlite")

    def tearDown(self) -> None:
        self.writer.close()
        self.temp.cleanup()

    def evaluate(self, fraction: str, quantity: str, policy: str = "REJECT"):
        ledger = TurnoverLedger(self.writer, fraction=Decimal(fraction), interval_seconds=60)
        return ledger.evaluate(
            context(), "strategy@1", now=NOW, funded_equity=Decimal(100), marks={}, quantity=Decimal(quantity),
            price=Decimal(1), multiplier=Decimal(1), quantity_increment=Decimal("0.1"), minimum_quantity=Decimal("0.1"),
            minimum_notional=Decimal(0), policy=policy)  # type: ignore[arg-type]

    def test_reject_policy_enforces_fraction_below_research_limit(self) -> None:
        # 4.5 is 4.5 % of 100: inside the 5 % research ceiling, outside the configured 2 %.
        verdict = self.evaluate("0.02", "4.5")
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.code, "TURNOVER_LIMIT_EXCEEDED")
        self.assertEqual(verdict.allowance, Decimal("2.00"))

    def test_constrain_policy_enforces_fraction_below_research_limit(self) -> None:
        verdict = self.evaluate("0.02", "4.5", policy="CONSTRAIN")
        self.assertTrue(verdict.admitted)
        self.assertEqual(verdict.code, "TURNOVER_CONSTRAINED")
        # Largest 0.1 multiple strictly below the 2.00 allowance.
        self.assertEqual(verdict.constrained_quantity, Decimal("1.9"))

    def test_below_configured_fraction_is_admitted(self) -> None:
        verdict = self.evaluate("0.02", "1.9")
        self.assertTrue(verdict.admitted)
        self.assertEqual(verdict.code, "TURNOVER_ADMITTED")

    def test_equality_with_configured_fraction_is_refused(self) -> None:
        verdict = self.evaluate("0.02", "2")
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.code, "TURNOVER_LIMIT_EXCEEDED")

    def test_default_fraction_keeps_strict_five_percent_boundary(self) -> None:
        self.assertEqual(self.evaluate("0.05", "4.9").code, "TURNOVER_ADMITTED")
        self.assertEqual(self.evaluate("0.05", "5").code, "TURNOVER_LIMIT_EXCEEDED")

    def test_fraction_above_research_limit_is_refused(self) -> None:
        with self.assertRaises(DomainError):
            TurnoverLedger(self.writer, fraction=Decimal("0.06"))


if __name__ == "__main__":
    unittest.main()
