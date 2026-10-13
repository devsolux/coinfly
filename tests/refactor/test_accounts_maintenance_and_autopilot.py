"""Isolated contract regressions; no exchange calls, vault I/O or native claims."""
import time
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, RunProfile
from coinfly.strategy.autopilot import automatic_strategy
from coinfly.strategy.definition import (
    ControllerProfile,
    DirectionPermission,
    MappingVersion,
    NeuralSettings,
    StrategyDefinition,
)
from coinfly.venues.coinbase.execution_accounts import collect_accounts
from coinfly.worker.automation import AutomationController, AutomationState, ComputeRequest
from coinfly.worker.maintenance import MaintenanceLoop

NOW = datetime(2026,9,22,tzinfo=timezone.utc)


def account(identity="a", scope="portfolio", currency="USD", available="10", held="1", **updates):
    return {"uuid":identity,"retail_portfolio_id":scope,"currency":currency,"active":True,"ready":True,
            "available_balance":{"value":available,"currency":currency},
            "hold":{"value":held,"currency":currency},**updates}


class Pages:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.calls = []
    def get(self, path, *, query=None):
        self.calls.append((path,query))
        return next(self.pages)


def test_distinct_account_ids_same_currency_aggregate_only_inside_delegated_scope():
    reader=Pages({"accounts":[account(),account(identity="outside",scope="different",available="999999")],"has_next":True,"cursor":"p2"},
                 {"accounts":[account(identity="b",available="2.123456789012345678")],"has_next":False})
    result=collect_accounts(reader,"portfolio")
    assert result.balances == (("USD",Decimal("12.123456789012345678"),Decimal(2)),)
    assert len(result.accounts)==2
    assert result.pages==2
    assert reader.calls[1][1]["cursor"]=="p2"


@pytest.mark.parametrize("page,code",[
    ({"accounts":[],"has_next":True},"ACCOUNT_PAGINATION_INCOMPLETE"),
    ({"accounts":[],"has_next":"false"},"ACCOUNT_PAGE_INVALID"),
    ({"accounts":[account(),account()],"has_next":False},"ACCOUNT_ID_INVALID_OR_DUPLICATE"),
    ({"accounts":[account(retail_portfolio_id=None)],"has_next":False},"ACCOUNT_SCOPE_UNVERIFIED"),
    ({"accounts":[account(active=None)],"has_next":False},"ACCOUNT_STATUS_UNVERIFIED"),
])
def test_unproven_completeness_is_never_qualified(page,code):
    with pytest.raises(DomainError,match=code):
        collect_accounts(Pages(page),"portfolio")


def test_cursor_cycle_and_page_budget_fail():
    page={"accounts":[],"has_next":True,"cursor":"repeat"}
    with pytest.raises(DomainError,match="ACCOUNT_PAGINATION_INCOMPLETE"):
        collect_accounts(Pages(page,page),"portfolio")
    with pytest.raises(DomainError,match="ACCOUNT_PAGES_EXHAUSTED"):
        collect_accounts(Pages(page),"portfolio",maximum_pages=1)


def test_inactive_or_unready_balance_is_not_spendable():
    result=collect_accounts(Pages({"accounts":[account(active=False)],"has_next":False}),"portfolio")
    assert result.balances==(("USD",Decimal(0),Decimal(11)),)


def test_maintenance_single_flight_and_recovery_without_ui_or_decisions():
    visited,finish=Event(),Event()
    lock=Lock()
    state={"calls":0,"active":0,"peak":0}
    failures=[]
    def run(now):
        with lock:
            state["calls"]+=1
            state["active"]+=1
            state["peak"]=max(state["peak"],state["active"])
        try:
            if state["calls"]==1:
                raise DomainError("ISOLATED_NETWORK_FAILURE")
            visited.set()
            finish.wait(2)
        finally:
            with lock:
                state["active"]-=1
    loop=MaintenanceLoop(run,failures.append,interval_seconds=.05,clock=lambda:NOW)
    try:
        loop.start()
        loop.start()
        assert visited.wait(2)
        assert not loop.stop(timeout=.01)  # never claims a blocked operation stopped
        finish.set()
        assert loop.stop(timeout=2)
        assert state["peak"]==1
        assert failures==["ISOLATED_NETWORK_FAILURE"]
        before=state["calls"]
        time.sleep(.06)
        assert state["calls"]==before
        with pytest.raises(RuntimeError,match="retired"):
            loop.start()
    finally:
        finish.set()
        loop.stop(2)


@pytest.mark.parametrize("slots",[1,2,3,6,7,8,9,11,99,100])
def test_automatic_plan_selects_full_malecns_not_a_fixture(slots):
    config=EngineConfig(authority_id="owner",execution_scope=("paper",))
    strategy=automatic_strategy(config,capital=Decimal(100),neural=NeuralSettings(),max_active_slots=slots)
    assert strategy.controller_profile is ControllerProfile.PRESERVED_MALECNS_V1
    assert strategy.mapping_version is MappingVersion.SPOT_STEP_V1
    assert strategy.max_active_slots==slots
    assert strategy.capital_budget==Decimal(100)
    assert strategy.execution.turnover_policy=="CONSTRAIN"
    assert not strategy.learning_enabled
    assert strategy.permitted_environments==(Environment.SANDBOX,)
    assert strategy.per_slot_max_exposure_fraction*slots<=Decimal(1)


def test_spot_short_is_rejected_and_live_plan_never_arms_anything():
    paper=EngineConfig(authority_id="owner",execution_scope=("paper",))
    with pytest.raises(DomainError,match="SPOT_SHORT_PERMISSION_FORBIDDEN"):
        automatic_strategy(paper,capital=Decimal(100),neural=NeuralSettings(),directions=DirectionPermission.BOTH)
    live=EngineConfig(authority_id="owner",execution_scope=("portfolio",),environment=Environment.LIVE,
        run_profile=RunProfile.STANDARD,credential_binding_id="a"*32,endpoint_profile_id="coinbase-advanced-spot")
    result=automatic_strategy(live,capital=Decimal(100),neural=NeuralSettings())
    assert result.permitted_environments==(Environment.LIVE,)
    assert result.research_only is False  # configuration, NOT authority/qualification


def test_strategy_schemas_are_explicit_and_not_upgraded():
    config=EngineConfig(authority_id="owner",execution_scope=("paper",))
    strategy=automatic_strategy(config,capital=Decimal(100),neural=NeuralSettings())
    payload=strategy.model_dump_json().replace('"schema_version":3','"schema_version":1')
    with pytest.raises(ValidationError):
        StrategyDefinition.model_validate_json(payload)


def test_thousands_of_deferred_frames_do_not_accumulate():
    # Supply only control-phase read collaborators. This calls the actual result
    # handler and proves its reference cleanup, not a full-graph throughput rate.
    controller=AutomationController.__new__(AutomationController)
    controller._effective=lambda:(None,None)
    controller.config=lambda:None
    controller._row=lambda:None
    controller.authority_id="fixture"
    controller.coordinator=SimpleNamespace(status=lambda _:SimpleNamespace())
    controller._controller=None
    controller._controller_failed=None
    controller._state=AutomationState.OBSERVING
    controller._frames={}
    controller._pending_rewards={}
    controller._compute={"deferred":0}
    controller._due_since={"BTC-USD":NOW}
    requests=[]
    for number in range(1000):
        request=ComputeRequest(slot_id="slot",instrument="BTC-USD",decision_id=f"decision-{number}",
            decided_at=NOW,frame_sha256="a"*64,observed_through=NOW,observation_sequence=number,
            epoch=1,revision=1,control_generation=1,state_generation=1)
        controller._frames[request.decision_id]=object()
        controller._pending_rewards[request.decision_id]=object()
        requests.append((request,None,"DEFERRED_COMPUTE_BUDGET"))
    controller._admit_results(NOW,requests,{})
    assert not controller._frames
    assert not controller._pending_rewards
    assert controller._compute["deferred"]==1000
    assert controller._due_since["BTC-USD"]==NOW  # fairness debt retained
