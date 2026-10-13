"""Exact synthetic derivative cashflows, without a venue or live credentials."""

import copy
from decimal import Decimal as D

import pytest

from coinfly.domain.context import DomainError
from coinfly.simulation.paper_margin import PaperMarginBook


@pytest.mark.parametrize("direction", [D(1), D(-1)])
def test_fifo_partial_and_final_settlement_never_uses_rounded_average(direction):
    book = PaperMarginBook(D(1000))
    book.apply_fill("BTC-USD", direction, D(100))
    book.apply_fill("BTC-USD", 2 * direction, D(101))
    book.set_mark("BTC-USD", D(110))
    assert book.snapshot().unrealized_pnl == D(28) * direction
    restored = PaperMarginBook.restore(book.dump_state())
    assert restored.snapshot() == book.snapshot()
    assert restored.apply_fill("BTC-USD", -direction, D(110), reduce_only=True) == D(10) * direction
    assert restored.snapshot().unrealized_pnl == D(18) * direction
    restored = PaperMarginBook.restore(restored.dump_state())
    assert restored.apply_fill("BTC-USD", -2 * direction, D(110), reduce_only=True) == D(18) * direction
    assert restored.collateral == D(1000) + D(28) * direction
    assert restored.positions == ()
    assert restored.dump_state()["lots"] == []


def test_position_reversal_retains_only_the_new_direction_lot():
    book = PaperMarginBook(D(1000))
    book.apply_fill("BTC-USD", D(1), D(100))
    book.apply_fill("BTC-USD", D(2), D(101))
    assert book.apply_fill("BTC-USD", D(-4), D(110)) == D(28)
    assert book.quantity("BTC-USD") == D(-1)
    assert book.apply_fill("BTC-USD", D(1), D(109), reduce_only=True) == D(1)
    assert book.collateral == D(1029)


def test_rejected_margin_fill_rolls_back_marks_lots_cash_and_positions():
    book = PaperMarginBook(D(1))
    before = book.dump_state()
    with pytest.raises(DomainError, match="PAPER_INSUFFICIENT_MARGIN"):
        book.apply_fill("BTC-USD", D(1), D(100))
    assert book.dump_state() == before


def test_failed_snapshot_rolls_back_the_entire_candidate(monkeypatch):
    book = PaperMarginBook(D(1000))
    book.apply_fill("BTC-USD", D(1), D(100))
    before = book.dump_state()
    def fail():
        raise ValueError("synthetic snapshot failure")
    monkeypatch.setattr(book, "snapshot", fail)
    with pytest.raises(ValueError):
        book.apply_fill("BTC-USD", D(2), D(101))
    assert book.dump_state() == before


def test_margin_restore_rejects_missing_or_inconsistent_cost_basis():
    book = PaperMarginBook(D(1000))
    book.apply_fill("BTC-USD", D(1), D(100))
    state = book.dump_state()
    for field, bad in (("entry_price", "99"), ("quantity", "2"), ("quantity", "-1")):
        changed = copy.deepcopy(state)
        changed["lots"][0][field] = bad
        with pytest.raises(DomainError, match="PAPER_MARGIN_LOT_MISMATCH"):
            PaperMarginBook.restore(changed)


@pytest.mark.parametrize("field", ["collateral", "initial_margin_fraction", "maintenance_margin_fraction"])
def test_margin_restore_rejects_binary_float_amounts(field):
    state = PaperMarginBook(D(1000)).dump_state()
    state[field] = .1
    with pytest.raises(DomainError, match="INVALID_PAPER_MARGIN_STATE"):
        PaperMarginBook.restore(state)


@pytest.mark.parametrize("has_position", [False, True])
@pytest.mark.parametrize("damage", ["unversioned", "old_version", "missing_lots", "migration"])
def test_margin_restore_requires_current_explicit_cost_basis(has_position, damage):
    book = PaperMarginBook(D(1000))
    if has_position:
        book.apply_fill("BTC-USD", D(1), D(100))
    state = book.dump_state()
    if damage == "unversioned":
        state.pop("schema_version")
        state.pop("lots")
    elif damage == "old_version":
        state["schema_version"] = 1
    elif damage == "missing_lots":
        state.pop("lots")
    else:
        state["migration"] = {}
    with pytest.raises(DomainError, match="INVALID_PAPER_MARGIN_STATE"):
        PaperMarginBook.restore(state)
