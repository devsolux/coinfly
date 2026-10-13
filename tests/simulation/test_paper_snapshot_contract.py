import copy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from coinfly.domain.context import DomainError, ProductTrack
from coinfly.simulation.paper_margin import PaperMarginBook
from coinfly.simulation.paper_matching import PaperMatcher, Side
from tests.simulation.test_stateful_paper_matching import NOW, context, depth, instrument, request


def snapshot(*, direction=1, closed=False):
    ctx = context(ProductTrack.PERPETUAL)
    contract = instrument(ProductTrack.PERPETUAL)
    book = PaperMatcher(ctx, (contract,), initial_balances={}, margin=PaperMarginBook(D(1000)))
    side = Side.BUY if direction == 1 else Side.SELL
    book.submit(request(quantity="3", price="101" if direction == 1 else "100", side=side), now=NOW)
    bids = depth(("99", "3")) if direction == 1 else depth(("101", "1"), ("100", "2"))
    asks = depth(("100", "1"), ("101", "2")) if direction == 1 else depth(("102", "3"))
    book.update_book("BTC-USD", bids=bids, asks=asks, now=NOW)
    if closed:
        later = NOW + timedelta(seconds=1)
        book.submit(request("close", side=Side.SELL, quantity="1", price="110", reduce_only=True), now=later)
        book.update_book("BTC-USD", bids=depth(("110", "1")), asks=depth(("111", "1")), now=later)
    state = book.dump_state()
    return ctx, (contract,), book, state


@pytest.mark.parametrize("direction", [1, -1])
def test_matcher_preserves_original_lots_and_roundtrips(direction):
    ctx, contracts, original, state = snapshot(direction=direction)
    before = copy.deepcopy(state)
    restored = PaperMatcher.restore(ctx, contracts, state)
    assert state == before
    assert restored.margin.snapshot() == original.margin.snapshot()
    assert restored.margin.dump_state()["lots"] == original.margin.dump_state()["lots"]
    dumped = restored.dump_state()
    assert PaperMatcher.restore(ctx, contracts, dumped).dump_state() == dumped
    # Closing the original lots remains exact after restore.
    assert restored.margin.apply_fill("BTC-USD", D(-3 * direction), D(110)) == D(28 if direction == 1 else -29)


def test_partial_close_preserves_fifo_and_exact_cash():
    ctx, contracts, original, state = snapshot(closed=True)
    restored = PaperMatcher.restore(ctx, contracts, state)
    assert restored.margin.dump_state() == original.margin.dump_state()
    assert restored.fills == original.fills
    assert restored.margin.apply_fill("BTC-USD", D(-2), D(110)) == D(18)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "quantity", "counter", "float", "future", "order", "margin_config", "naive_time"])
def test_restore_rejects_incomplete_or_inconsistent_evidence_with_domain_error(damage):
    ctx, contracts, _, state = snapshot()
    if damage == "missing":
        state["fills"].pop()
    elif damage == "duplicate":
        state["fills"].append(state["fills"][0])
    elif damage == "quantity":
        state["margin"]["positions"][0]["quantity"] = "5"
    elif damage == "counter":
        state["trade_counter"] = 1
    elif damage == "float":
        state["fills"][0]["price"] = 100.1
    elif damage == "future":
        state["fills"][0]["as_of"] = (NOW + timedelta(days=1)).isoformat()
    elif damage == "order":
        state["fills"][0]["client_order_id"] = "forged"
    elif damage == "margin_config":
        state["margin"]["initial_margin_fraction"] = "0"
    else:
        state["last_time"] = NOW.replace(tzinfo=None).isoformat()
    with pytest.raises(DomainError):
        PaperMatcher.restore(ctx, contracts, state)
