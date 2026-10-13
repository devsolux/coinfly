"""Actual live-domain methods on isolated SQL and HTTP fixtures.

The inline transaction harness does not replace or qualify production Writer,
its thread, WAL, native credentials, or a real broker. No real account is read.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from tests.venues.test_live_spot_engine import BTC, PORTFOLIO, FakeTransport, live_config, live_context

from coinfly.domain.context import DomainError
from coinfly.execution.fact_reducer import FactReducer, acknowledge_tx
from coinfly.execution.intent_store import get_tx, prepare_tx
from coinfly.execution.models import IntentKind, OrderIntent, Reservation, Side
from coinfly.operations.live_arming import (
    MANDATE_STATEMENT,
    QUALIFICATION_STATEMENT,
    LiveMandate,
    LiveMandateStore,
    require_order_mandate_tx,
)
from coinfly.risk.admission_actor import ActiveBookSnapshot, AvailableAsset, ExecutionMark
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drawdown import DrawdownStore
from coinfly.venues.coinbase.advanced_trade_orders import AdvancedTradeOrderGateway
from coinfly.venues.coinbase.live_spot_engine import LiveSpotEngine

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


class InlineSql:
    """Single-thread SQL unit harness, explicitly not production Writer."""
    def __init__(self, db):
        self.db = db

    def call(self, operation):
        if self.db.in_transaction:
            raise AssertionError("nested SQL transaction in unit harness")
        with self.db:
            self.db.execute("BEGIN")
            return operation(self.db)


@pytest.fixture
def sql():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript((Path(__file__).parents[2] / "src/coinfly/storage/schema.sql").read_text())
    yield InlineSql(db)
    db.close()


def order(**updates):
    values = dict(context=live_context(), scope_id=PORTFOLIO, profile_version=1, signer_version=1,
                  slot_id="slot-btc", allocation_revision=1, instrument="BTC-USD", base_asset="BTC",
                  quote_asset="USD", side=Side.BUY, kind=IntentKind.ENTRY, quantity=Decimal(".1"), limit_price=Decimal(100))
    return OrderIntent(**{**values, **updates})


def observed_book():
    return ActiveBookSnapshot(context=live_context(), scope_id=PORTFOLIO, evidence_id="fixture", observed_at=NOW,
        expires_at=NOW+timedelta(seconds=30), fact_sequence=0, reporting_currency="USD",
        funded_equity=Decimal(1000), eligible_collateral=Decimal(1000),
        available_assets=(AvailableAsset(asset="USD", amount=Decimal(1000)),),
        marks=(ExecutionMark(instrument="BTC-USD", price=Decimal(100)),),
        complete_execution_scope=True, authoritative_account_evidence=True, conflicting_external_activity=False)


def mandate(sql, *, qualification=False):
    cfg = live_config()
    if qualification:
        cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={
            "max_order_notional": Decimal(25), "max_daily_loss": Decimal(25)})})
    value = LiveMandate(authority_id=cfg.authority_id, approver="isolated fixture",
        statement=QUALIFICATION_STATEMENT if qualification else MANDATE_STATEMENT,
        kind="QUALIFICATION" if qualification else "TRADING", product_track=cfg.product_track,
        product_subtype=cfg.effective_subtype, portfolio_id=PORTFOLIO, allowed_instruments=("BTC-USD",),
        max_order_notional=cfg.limits.max_order_notional, max_gross_exposure=cfg.limits.max_gross_exposure,
        max_daily_loss=cfg.limits.max_daily_loss, approved_at=NOW-timedelta(minutes=1), expires_at=NOW+timedelta(hours=1))
    store = LiveMandateStore(sql)
    store.record(value, now=NOW)
    store.bind_context(cfg, live_context().identity, now=NOW)
    risk = DrawdownStore(sql)
    for scope in (PORTFOLIO, "authority"):
        risk.record(live_context(), scope, equity=Decimal(1000), evidence_id=scope, observed_at=NOW)
    return cfg, store, value


def test_bound_mandate_is_enforced_at_each_order_boundary(sql):
    cfg, store, value = mandate(sql)
    sql.call(lambda db: require_order_mandate_tx(db, order(), cfg, observed_book(), now=NOW))
    with pytest.raises(DomainError, match="INSTRUMENT_NOT_PERMITTED"):
        sql.call(lambda db: require_order_mandate_tx(db, order(instrument="ETH-USD"), cfg, observed_book(), now=NOW))
    with pytest.raises(DomainError, match="LIVE_MANDATE_EXPIRED"):
        sql.call(lambda db: require_order_mandate_tx(db, order(), cfg, observed_book(), now=value.expires_at))
    store.revoke(value.mandate_id, now=NOW)
    with pytest.raises(DomainError, match="LIVE_MANDATE_REVOKED"):
        sql.call(lambda db: require_order_mandate_tx(db, order(), cfg, observed_book(), now=NOW))


def test_reduction_guard_keeps_original_mandate_bounds_after_expiry(sql):
    cfg, store, value = mandate(sql)
    store.revoke(value.mandate_id, now=NOW)
    # This function supplies authority only; reduce-only proof is independently
    # mandatory in AdmissionActor and ContextInterlock.
    reduction = order(side=Side.SELL, kind=IntentKind.EXIT)
    sql.call(lambda db: require_order_mandate_tx(db, reduction, cfg, observed_book(), now=value.expires_at))
    with pytest.raises(DomainError, match="INSTRUMENT_NOT_PERMITTED"):
        sql.call(lambda db: require_order_mandate_tx(db, reduction.model_copy(update={"instrument": "ETH-USD"}),
                                                   cfg, observed_book(), now=value.expires_at))


def test_changed_config_cannot_reuse_old_context_authority(sql):
    cfg, _, _ = mandate(sql)
    changed = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_daily_loss": Decimal(99)})})
    with pytest.raises(DomainError, match="LIVE_MANDATE_CONFIG_CHANGED"):
        sql.call(lambda db: require_order_mandate_tx(db, order(), changed, observed_book(), now=NOW))


def test_qualification_has_absolute_aggregate_cap_not_only_equity_ratio(sql):
    cfg, _, _ = mandate(sql, qualification=True)
    first = order(quantity=Decimal(".2"))
    sql.call(lambda db: prepare_tx(db, first, Reservation(asset="USD", amount=Decimal(20))))
    with pytest.raises(DomainError, match="LIVE_MANDATE_GROSS_LIMIT"):
        sql.call(lambda db: require_order_mandate_tx(db, order(allocation_revision=2), cfg, observed_book(), now=NOW))
    sql.call(lambda db: require_order_mandate_tx(db, first, cfg, observed_book(), now=NOW))


def test_daily_loss_is_not_reset_by_another_order(sql):
    cfg, _, _ = mandate(sql)
    sql.call(lambda db: db.execute("UPDATE cf_drawdown SET daily_net_result='-100'"))
    with pytest.raises(DomainError, match="LIVE_MANDATE_LOSS_LIMIT"):
        sql.call(lambda db: require_order_mandate_tx(db, order(), cfg, observed_book(), now=NOW))


def engine(sql):
    transport = FakeTransport()
    value = LiveSpotEngine(live_config(), live_context(), sql, FactReducer(sql), ContextInterlock(sql),
        transport_factory=lambda: transport, mandate_check=lambda *_: False, instruments=(BTC,), clock=lambda: NOW)
    value.initialize(live_config(), live_context())
    value._gateway = AdvancedTradeOrderGateway(transport, context=live_context(), scope_id=PORTFOLIO, writer=sql, clock=lambda: NOW)
    return value, transport


def test_sync_finds_unowned_order_even_on_an_unselected_instrument(sql):
    value, transport = engine(sql)
    transport.orders = [{"order_id":"external", "client_order_id":"other", "status":"OPEN", "product_id":"ETH-USD"}]
    report = value.sync(NOW)
    assert report["external_open_orders"] == 1
    assert value._external_activity
    with pytest.raises(DomainError, match="STOP_PROOF_ACCOUNT_EVIDENCE_UNAVAILABLE"):
        value.reconcile()
    for path, query in transport.gets:
        if query.get("order_status"):
            assert "start_date" not in query and "product_ids" not in query


def test_incomplete_or_wrong_scope_order_history_invalidates_previous_sync(sql):
    value, transport = engine(sql)
    value.sync(NOW)
    assert value._account_complete
    transport.orders = [{"order_id":"external", "client_order_id":"other", "status":"OPEN", "retail_portfolio_id":"wrong"}]
    with pytest.raises(DomainError, match="ORDER_PORTFOLIO_SCOPE_UNVERIFIED"):
        value.sync(NOW)
    assert not value._account_complete
    assert value.balance_snapshot()["total"] is None


def test_cancel_ack_keeps_reservation_until_terminal_history_confirms_fills(sql):
    value, transport = engine(sql)
    intended = order()
    sql.call(lambda db: prepare_tx(db, intended, Reservation(asset="USD", amount=Decimal(10))))
    sql.call(lambda db: acknowledge_tx(db, intended.intent_id, "venue-1", "ack"))
    transport.cancel_results = [{"success":True,"order_id":"venue-1"}]
    pending = value.cancel_intent(intended.intent_id)
    assert pending["cancel_requested"] and not pending["cancelled"]
    assert sql.call(lambda db: get_tx(db, intended.intent_id)).reservation_remaining == Decimal(10)
    transport.orders = [{"order_id":"venue-1", "client_order_id":intended.intent_id, "status":"CANCELLED", "filled_size":"0"}]
    value.sync(NOW)
    finished = sql.call(lambda db: get_tx(db, intended.intent_id))
    assert finished.terminal and finished.reservation_remaining == 0


def test_sync_does_not_label_cash_as_total_valued_wealth(sql):
    value, _ = engine(sql)
    value.sync(NOW)
    assert value.balance_snapshot()["coverage"] == "VERIFIED_CASH_SUBTOTAL"
