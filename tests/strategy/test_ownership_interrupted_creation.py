from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coinfly.domain.context import EngineState
from coinfly.storage.writer import Writer
from coinfly.strategy.definition import research_paper_preset
from coinfly.strategy.ownership import StrategyOwnership
from coinfly.strategy.universe import UniverseSelection
from tests.execution.helpers import context


class InterruptibleSlots:
    """Slot service double whose creation can die after ownership was recorded."""

    def __init__(self) -> None:
        self.fail = True
        self.created: list[str] = []

    def list(self, _context: Any) -> tuple[Any, ...]:
        return ()

    def create(self, **kwargs: Any) -> Any:
        if self.fail:
            raise RuntimeError("process died while creating the slot")
        self.created.append(kwargs["slot_id"])
        return SimpleNamespace(engine_state=EngineState.STOPPED, tombstoned=False, operation_id=None, revision=1)


class InterruptedSlotCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.writer = Writer(Path(self.temp.name) / "ownership.sqlite")
        self.slots = InterruptibleSlots()
        self.ownership = StrategyOwnership(self.writer, self.slots)  # type: ignore[arg-type]
        self.context = context()
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.writer.close()
        self.temp.cleanup()

    def reconcile(self, definition: Any, selection: UniverseSelection):
        return self.ownership.reconcile(
            context=self.context, scope_id="book", definition=definition, selection=selection, config_revision=1,
            now=self.now, may_start=False, start_epoch=self.context.epoch)

    def test_retry_after_interrupted_creation_reuses_its_recorded_allocation_at_the_slot_cap(self) -> None:
        definition = research_paper_preset(capital_budget=Decimal(100)).model_copy(
            update={"max_active_slots": 1, "per_slot_max_exposure_fraction": Decimal("0.34")})
        selection = UniverseSelection(strategy_identity=definition.identity, selected=("BTC-USD",), reasons=(),
                                      selected_at=self.now, inputs_digest="0" * 64)
        self.ownership.approve_budget(self.context, definition, Decimal(100), now=self.now)

        with self.assertRaisesRegex(RuntimeError, "process died"):
            self.reconcile(definition, selection)
        crashed = self.ownership.owned(self.context, definition)
        self.assertEqual([(slot.instrument, slot.state, slot.allocated_budget) for slot in crashed],
                         [("BTC-USD", "PENDING_START", Decimal("34"))])

        self.slots.fail = False
        result = self.reconcile(definition, selection)

        self.assertEqual(result.blocked, ())
        self.assertEqual(result.created, ("BTC-USD",))
        self.assertEqual(len(self.slots.created), 1)
        # No second row and no second allocation: the interrupted reservation was resumed.
        resumed = self.ownership.owned(self.context, definition)
        self.assertEqual([(slot.slot_id, slot.allocated_budget) for slot in resumed],
                         [(crashed[0].slot_id, Decimal("34"))])
        self.assertEqual(result.budget.allocated if result.budget else None, Decimal("34"))

    def test_new_instrument_beyond_the_slot_cap_is_still_blocked(self) -> None:
        self.slots.fail = False
        definition = research_paper_preset(capital_budget=Decimal(100)).model_copy(
            update={"max_active_slots": 1, "per_slot_max_exposure_fraction": Decimal("0.34")})
        self.ownership.approve_budget(self.context, definition, Decimal(100), now=self.now)
        selection = UniverseSelection(strategy_identity=definition.identity, selected=("BTC-USD", "ETH-USD"), reasons=(),
                                      selected_at=self.now, inputs_digest="0" * 64)
        result = self.reconcile(definition, selection)
        self.assertEqual(result.created, ("BTC-USD",))
        self.assertEqual(result.blocked, (("ETH-USD", "BUDGET_EXHAUSTED"),))


if __name__ == "__main__":
    unittest.main()
