"""SQL-layer regressions: real accounting functions, not native/WAL qualification.

These isolated connections do not construct Writer or override its SQLite
minimum. Target-runtime tests still exercise the thread/IPC/WAL boundaries.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.execution.helpers import context, fill_for, intent

from coinfly.domain.context import DomainError
from coinfly.execution.fact_reducer import apply_fill_tx
from coinfly.execution.intent_store import prepare_tx
from coinfly.execution.models import Reservation
from coinfly.execution.weight_planner import InstrumentContract, plan_weights
from coinfly.risk.admission_actor import ActiveBookSnapshot, AvailableAsset, ExecutionMark
from coinfly.strategy.capital import planning_book_tx, require_intent_tx, snapshot_tx
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.ownership import StrategyOwnership
from coinfly.strategy.target_coordinator import BoundTarget

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
IDENTITY = "malecns-autonomous:1"


def book(*, equity="10000", mark="100"):
    return ActiveBookSnapshot(context=context(), scope_id="book", evidence_id="isolated-fixture",
        observed_at=NOW, expires_at=NOW+timedelta(seconds=30), fact_sequence=0, reporting_currency="USD",
        funded_equity=Decimal(equity), eligible_collateral=Decimal(equity),
        available_assets=(AvailableAsset(asset="USD", amount=Decimal(equity)),),
        marks=(ExecutionMark(instrument="BTC-USD", price=Decimal(mark)),),
        complete_execution_scope=True, authoritative_account_evidence=True, conflicting_external_activity=False)


@pytest.fixture
def ledger():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript((Path(__file__).parents[2]/"src/coinfly/storage/schema.sql").read_text())
    StrategyOwnership._schema(db)
    db.execute("INSERT INTO cf_strategy_budgets VALUES(?,?,?,?)", (context().identity,IDENTITY,"100",NOW.isoformat()))
    db.execute("INSERT INTO cf_strategy_slots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        "slot-btc", context().identity, IDENTITY, "BTC-USD", "100", "ACTIVE", "fixture", NOW.isoformat(),NOW.isoformat(),None,0,None))
    db.commit()
    yield db
    db.close()


def test_planner_uses_strategy_allocation_not_account_wealth(ledger):
    view, actual = planning_book_tx(ledger, book(), IDENTITY)
    assert view.funded_equity == Decimal(100)
    assert actual == {}
    assert book().funded_equity == Decimal(10000)
    assert view.available_assets == book().available_assets  # not invented account truth


def test_admission_independently_rejects_unallocated_spend(ledger):
    with pytest.raises(DomainError, match="STRATEGY_CAPITAL_LIMIT"):
        require_intent_tx(ledger, intent(quantity="2"), book(), fee_buffer_fraction=Decimal(".01"))
    require_intent_tx(ledger, intent(quantity=".5"), book(), fee_buffer_fraction=Decimal(".01"))


def test_pending_entries_and_fee_funding_share_one_allocation(ledger):
    earlier = intent(quantity=".4")
    with ledger:
        prepare_tx(ledger, earlier, Reservation(asset="USD", amount=Decimal("40.4")))
    assert snapshot_tx(ledger, book(), IDENTITY).pending_entries == Decimal(40)
    assert snapshot_tx(ledger, book(), IDENTITY).pending_cash == Decimal("40.4")
    with pytest.raises(DomainError, match="STRATEGY_CAPITAL_LIMIT"):
        require_intent_tx(ledger, intent(quantity=".7", revision=2), book(), fee_buffer_fraction=Decimal(".01"))
    with pytest.raises(DomainError, match="STRATEGY_CASH_LIMIT"):
        require_intent_tx(ledger, intent(quantity=".6", revision=2), book(), fee_buffer_fraction=Decimal(".01"))
    # Actual dispatch excludes only its own already reserved notional.
    require_intent_tx(ledger, earlier, book(), fee_buffer_fraction=Decimal(".01"))


def test_booked_fees_and_losses_reduce_allocation_without_reseeding(ledger):
    order = intent(quantity=".4")
    with ledger:
        prepare_tx(ledger, order, Reservation(asset="USD", amount=Decimal("40.4")))
        applied = apply_fill_tx(ledger, fill_for(order, quantity=".4", fee=".1"))
    assert applied.applied
    value = snapshot_tx(ledger, book(mark="50"), IDENTITY)
    assert value.net_asset_value == Decimal("79.9")
    assert value.cash == Decimal("59.9")
    view, actual = planning_book_tx(ledger, book(mark="50"), IDENTITY)
    assert view.funded_equity == Decimal("79.9")
    assert actual == {"BTC-USD": Decimal(".4")}
    with ledger:
        duplicate = apply_fill_tx(ledger, fill_for(order, quantity=".4", fee=".1"))
    assert not duplicate.applied
    assert snapshot_tx(ledger, book(mark="50"), IDENTITY) == value
    assert snapshot_tx(ledger, book(mark="200"), IDENTITY).sizing_equity == Decimal(100)


def test_slot_cap_and_ownership_are_not_ui_preferences(ledger):
    ledger.execute("UPDATE cf_strategy_slots SET allocated_budget='20'")
    with pytest.raises(DomainError, match="STRATEGY_SLOT_CAPITAL_LIMIT"):
        require_intent_tx(ledger,intent(quantity=".3"),book(),fee_buffer_fraction=Decimal(".01"))
    ledger.execute("UPDATE cf_strategy_slots SET state='RETIRING'")
    with pytest.raises(DomainError, match="STRATEGY_SLOT_NOT_ACTIVE"):
        require_intent_tx(ledger,intent(quantity=".1"),book(),fee_buffer_fraction=Decimal(".01"))


def test_current_owned_row_needs_no_legacy_column_fallback(ledger):
    record = StrategyOwnership._owned(ledger.execute("SELECT * FROM cf_strategy_slots").fetchone())
    assert record.absent_count == 0
    assert record.retired_at is None


def test_btc_buy_never_rebalances_unchanged_eth_target():
    observations = book(equity="1100").model_copy(update={"marks": (
        ExecutionMark(instrument="BTC-USD",price=Decimal(100)),
        ExecutionMark(instrument="ETH-USD",price=Decimal(150)),
    )})
    proposal = TargetWeights(decision_id="btc-buy",model_id="fixed-decoder",model_version="1",
        snapshot_id="snapshot",allocation_revision=1,expires_at=NOW+timedelta(seconds=10),
        weights=(InstrumentWeight(instrument_id="BTC-USD",weight=Decimal(".05")),
                 InstrumentWeight(instrument_id="ETH-USD",weight=Decimal(".2"))),
        affected_instruments=("BTC-USD",))
    contracts = tuple(InstrumentContract(instrument=i,quantity_increment=Decimal(".01"),
        price_increment=Decimal(".01"),minimum_quantity=Decimal(".01"),minimum_notional=Decimal(1))
        for i in ("BTC-USD","ETH-USD"))
    plans = plan_weights(BoundTarget(context=context(),proposal=proposal,accepted_at=NOW),
        observations,contracts,{"ETH-USD":Decimal(2)},{},now=NOW)
    assert [(p.instrument,p.quantity) for p in plans] == [("BTC-USD",Decimal(".55"))]
    with pytest.raises(ValidationError):
        TargetWeights.model_validate_json(proposal.model_dump_json().replace('"affected_instruments":["BTC-USD"]','"affected_instruments":["DOGE-USD"]'))
