"""Worker-owned autonomous paper lifecycle, driven deterministically through the real supervisor.

The decision controller here is the scripted FIXTURE_SEQUENCE_V1 pipeline
fixture (SANDBOX only, labeled ``pipeline_fixture`` in every record); the feed is
a scripted book. Neither is market data or a neural decision, and the
assertions check that the records say so. The tests cover: configure → enable
(arm) → universe selection → owned slot creation/start → sensory warm-up →
BUY → paper fill → HOLD → SELL → paper fill → pause → stop-and-reconcile
(drain) → retirement; restart requiring explicit re-enable with the strategy
persisted; idempotent requests; ownership/budget invariants.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Sequence, cast
from unittest import TestCase

from coinfly.accounting.journal import Journal
from coinfly.bootstrap.installer import VerifiedDataset
from coinfly.bootstrap.manager import BootstrapManager, BootstrapProgress, BootstrapState
from coinfly.config.models import default_config
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import DomainError, EngineState, ProductTrack, RegionPolicy
from coinfly.domain.products import ProductSubtype
from coinfly.execution.intent_store import IntentStore
from coinfly.ipc.protocol import RPCRequest
from coinfly.market.feed import fixture_catalog
from coinfly.market.observations import (
    DataSource,
    DepthLevelObservation,
    FeedHealth,
    FeedState,
    InstrumentCatalogEntry,
    MarketObservation,
)
from coinfly.strategy.ownership import strategy_slot_id
from coinfly.worker.automation import AutomationState
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor
from tests.support import local_peer

_SENTINEL = object()


class TestOnlyBootstrap(BootstrapManager):
    __test__ = False

    def __init__(self, home: Path) -> None:
        super().__init__(home=home)
        self.progress = BootstrapProgress(BootstrapState.READY, reason="TEST_SENTINEL_NOT_CERTIFIED")

    def verified_dataset(self) -> VerifiedDataset:
        return cast(VerifiedDataset, _SENTINEL)

    async def check(self) -> BootstrapProgress:
        return self.progress


class ScriptedFeed:
    """A MarketFeed whose books the test sets explicitly; labeled SYNTHETIC_FIXTURE."""

    source = DataSource.SYNTHETIC_FIXTURE

    def __init__(self, instruments: Sequence[str], books: dict[str, tuple[str, str]] | None = None,
                 subtype: ProductSubtype = ProductSubtype.SPOT) -> None:
        self.instruments = tuple(instruments)
        self.subtype = subtype
        self.books: dict[str, tuple[str, str]] = books if books is not None else {name: ("99.90", "100.10") for name in self.instruments}
        # Displayed depth per instrument (the paper matcher consumes it) and
        # catalog volume (the universe ranking key); tests override both.
        self.depths: dict[str, Decimal] = {}
        self.volumes: dict[str, Decimal] = {}
        self.subscribed: tuple[str, ...] = ()
        self.stopped = False
        self._sequence = 0
        self._latest: dict[str, MarketObservation] = {}

    def start(self, instruments: Sequence[str]) -> None:
        self.subscribed = tuple(instruments)

    def stop(self) -> None:
        self.stopped = True
        self._latest.clear()

    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]:
        entries = fixture_catalog(self.instruments, now=datetime.now(timezone.utc), subtype=self.subtype)
        return tuple(entry.model_copy(update={"volume_24h": self.volumes[entry.instrument_id]}) if entry.instrument_id in self.volumes
                     else entry for entry in entries)

    def latest(self, instrument: str) -> MarketObservation | None:
        return None if self.stopped else self._latest.get(instrument)

    def health(self) -> FeedHealth:
        last = max((item.received_at for item in self._latest.values()), default=None)
        return FeedHealth(source=self.source, state=FeedState.STOPPED if self.stopped else FeedState.CONNECTED,
                          last_message_at=last, subscribed=self.subscribed, detail="scripted test book")

    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        if self.stopped:
            return ()
        self._sequence += 1
        produced = []
        for instrument in self.subscribed:
            bid, ask = self.books[instrument]
            depth = self.depths.get(instrument, Decimal("1000"))
            observation = MarketObservation(
                instrument=instrument,
                bids=(DepthLevelObservation(price=Decimal(bid), quantity=depth),),
                asks=(DepthLevelObservation(price=Decimal(ask), quantity=depth),),
                observed_at=now, received_at=now, sequence=self._sequence, source=self.source, lineage="scripted")
            self._latest[instrument] = observation
            produced.append(observation)
        return tuple(produced)


def fixture_strategy(sides: Sequence[str], *, instruments: Sequence[str] = ("BTC-USD",), max_active_slots: int = 1,
                     capital: str = "100", warmup: int = 3, **overrides: Any) -> dict[str, Any]:
    # Execution: a 4 % step is strictly below the 5 % turnover limit (an equal
    # 5 % step is refused — see tests/worker/test_turnover_planning.py) and the
    # accounting window is one second so that a BUY → SELL cycle driven with
    # one-second decision intervals lands in distinct windows.
    definition: dict[str, Any] = {
        "strategy_id": "pipeline-fixture", "version": 1, "label": "PIPELINE FIXTURE — scripted decisions, not a model",
        "research_only": True, "controller_profile": "fixture-sequence-v1", "mapping_version": "fixed-decoder-spot-step-v1",
        "product_track": "SPOT", "product_subtype": "SPOT", "permitted_instruments": list(instruments),
        "quote_currencies": ["USD"], "market_data": {"max_observation_age_seconds": 5, "warmup_observations": warmup, "feature_window": 4},
        "max_active_slots": max_active_slots, "capital_budget": capital, "per_slot_max_exposure_fraction": "0.34",
        "execution": {"proposal_ttl_seconds": 5, "decision_interval_seconds": 1, "target_step": "0.04", "max_turnover_fraction": "0.05",
                      "accounting_interval_seconds": 1, "order_ttl_seconds": 30},
        "fixture_sides": list(sides),
    }
    definition.update(overrides)
    return definition


class AutomationFixture(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-auto-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = WorkerPaths(self.root / "worker")
        self.books: dict[str, tuple[str, str]] = {name: ("99.90", "100.10") for name in ("BTC-USD", "ETH-USD", "SOL-USD")}
        self.subtype = ProductSubtype.SPOT
        self.feed = ScriptedFeed(("BTC-USD", "ETH-USD", "SOL-USD"), self.books)
        self.worker = self.new_worker()

    def new_feed(self, *args: Any) -> ScriptedFeed:
        # Like production, every (re)activation gets a fresh feed object.
        self.feed = ScriptedFeed(("BTC-USD", "ETH-USD", "SOL-USD"), self.books, subtype=self.subtype)
        return self.feed

    def new_worker(self) -> WorkerSupervisor:
        worker = WorkerSupervisor(paths=self.paths, socket_path=self.root / "worker.sock", bootstrap=TestOnlyBootstrap(self.root))
        self.addCleanup(worker.close)
        worker.automation.loop_enabled = False  # ticks are driven explicitly below
        worker.automation._feed_factory = self.new_feed  # type: ignore[method-assign]
        return worker

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        epoch = self.worker.coordinator.status(self.worker.authority_id).transition_epoch
        request = RPCRequest.parse({"jsonrpc": "2.0", "id": request_id or method, "method": method, "params": params or {},
                                    "epoch": epoch, "deadline_ms": int(time.time() * 1000) + 10000})
        result = self.worker.handle(request, local_peer())
        return cast(dict[str, Any], result)

    def initialize(self) -> None:
        config = default_config()
        status = self.worker.coordinator.status(self.worker.authority_id)
        revisions = {"expected_revision": status.effective_revision, "expected_epoch": status.transition_epoch}
        preview = self.rpc("context.preview", {"config": config.model_dump(mode="json"), **revisions}, request_id="init-p")
        accepted = self.rpc("context.apply", {"preview_id": preview["preview_id"], "config_digest": preview["config_digest"],
                                              "confirmation": True, **revisions}, request_id="init-a")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())

    def configure(self, strategy: dict[str, Any], *, request_id: str = "configure-1", **params: Any) -> dict[str, Any]:
        # Scripted fixture strategies run only in explicit pipeline test mode.
        return self.rpc("automation.configure", {"strategy": strategy, "feed": {"kind": "SYNTHETIC_FIXTURE", "fixture_profile": "random-walk-3"},
                                                 "pipeline_test_mode": True, **params}, request_id=request_id)

    def enable(self, *, request_id: str = "enable-1") -> dict[str, Any]:
        snapshot = self.rpc("state.snapshot")
        return self.rpc("automation.enable", {"expected_revision": snapshot["config_revision"], "expected_epoch": snapshot["context_epoch"]},
                        request_id=request_id)

    def tick(self, count: int = 1, *, pause: float = 0.0) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for _ in range(count):
            report = self.worker.automation.tick(datetime.now(timezone.utc))
            if pause:
                time.sleep(pause)
        return report

    def context(self) -> Any:
        context = self.worker.coordinator.status(self.worker.authority_id).effective_context
        assert context is not None
        return context

    def decision(self, instrument: str = "BTC-USD") -> dict[str, Any]:
        for slot in self.worker.automation.status()["slots"]:
            if slot["instrument"] == instrument:
                return cast(dict[str, Any], slot["last_decision"] or {})
        return {}


class PaperCycleTests(AutomationFixture):
    def test_buy_fill_hold_sell_cycle_with_owned_slot(self) -> None:
        self.initialize()
        configured = self.configure(fixture_strategy(["BUY", "HOLD", "SELL"]))
        self.assertEqual(configured["strategy"], "pipeline-fixture@1")
        self.assertEqual(configured["state"], "SETUP")
        status = self.worker.automation.status()
        self.assertFalse(status["enabled"])
        enabled = self.enable()
        self.assertTrue(enabled["enabled"])
        snapshot = self.rpc("state.snapshot")
        self.assertTrue(snapshot["armed"])  # enable armed the paper context explicitly
        # First tick: universe selected from the scripted catalog, the slot is
        # created AND started by the strategy owner (never by the UI).
        self.tick()
        status = self.worker.automation.status()
        universe = status["universe"]
        self.assertEqual(universe["selected"], ["BTC-USD"])
        rejected = {reason["instrument"]: reason["codes"] for reason in universe["reasons"] if not reason["eligible"]}
        self.assertEqual(rejected["ETH-USD"][0], "NOT_PERMITTED")
        context = self.context()
        slots = self.worker.slots.list(context)
        self.assertEqual(len(slots), 1)
        scope = self.worker.coordinator.status(self.worker.authority_id).effective_config.execution_scope[0]  # type: ignore[union-attr]
        expected_slot = strategy_slot_id(self.worker.automation.config(context).strategy, context, scope, "BTC-USD")  # type: ignore[union-attr]
        self.assertEqual(slots[0].slot_id, expected_slot)
        self.assertIs(slots[0].engine_state, EngineState.ACTIVE)
        self.assertEqual(status["slots"][0]["ownership_state"], "ACTIVE")
        self.assertEqual(Decimal(status["slots"][0]["allocated_budget"]), Decimal("34"))
        self.assertEqual(status["feed"]["label"], "SYNTHETIC FIXTURE — NOT MARKET DATA")
        self.assertFalse(status["feed"]["real_market_data"])
        # Warm-up: scales from 3 observations, a 4-row feature window; no decision before that.
        self.assertIn(self.worker.automation.status()["state"], ("WARMING_UP", "READY"))
        self.tick(2)
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.WARMING_UP.value)
        self.assertIsNone(self.worker.automation.status()["last_decision"])
        self.tick()
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.RUNNING.value)
        buy = self.decision()
        self.assertEqual(buy["side"], "BUY")
        self.assertTrue(buy["pipeline_fixture"])
        self.assertEqual(buy["controller_profile"], "fixture-sequence-v1")
        self.assertEqual(buy["outcome"], "DISPATCHED", buy)
        self.assertEqual(len(buy["intent_ids"]), 1)
        intent = IntentStore(self.worker.writer).get(buy["intent_ids"][0])
        self.assertEqual(intent.intent.limit_price, Decimal("100.00"))
        self.assertEqual(intent.intent.quantity, Decimal("0.04"))  # 4 % of the 100 USD paper book at 100
        self.assertFalse(intent.terminal)
        # The scripted book crosses the resting limit: a paper fill, journaled.
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        intent = IntentStore(self.worker.writer).get(buy["intent_ids"][0])
        self.assertTrue(intent.terminal)
        self.assertEqual(intent.filled_quantity, Decimal("0.04"))
        Journal(self.worker.writer).verify()
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["actual_quantity"]), Decimal("0.04"))
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["target_weight"]), Decimal("0.04"))
        # HOLD is recorded as a genuine hold, not as a failed trade.
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        hold = self.decision()
        self.assertEqual((hold["side"], hold["outcome"], hold["outcome_code"]), ("HOLD", "HOLD", "GENUINE_HOLD"))
        # SELL: the step mapping reduces the weight back to zero → an EXIT order.
        time.sleep(1.05)
        self.tick()
        sell = self.decision()
        self.assertEqual((sell["side"], sell["outcome"]), ("SELL", "DISPATCHED"))
        exit_intent = IntentStore(self.worker.writer).get(sell["intent_ids"][0])
        self.assertEqual(exit_intent.intent.side.value, "SELL")
        self.assertEqual(exit_intent.intent.limit_price, Decimal("100.00"))
        self.feed.books["BTC-USD"] = ("100.10", "100.20")
        self.tick()
        exit_intent = IntentStore(self.worker.writer).get(sell["intent_ids"][0])
        self.assertTrue(exit_intent.terminal)
        Journal(self.worker.writer).verify()
        self.assertEqual(Decimal(self.worker.automation.status()["slots"][0]["actual_quantity"]), Decimal(0))
        self.assertEqual(self.worker.automation.status()["execution_health"]["fills"], 2)
        # Persisted decisions carry the fixture label so no report can call them neural.
        rows = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT side, pipeline_fixture, outcome FROM cf_automation_decisions ORDER BY decided_at").fetchall())
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [("BUY", 1, "DISPATCHED"), ("HOLD", 1, "HOLD"), ("SELL", 1, "DISPATCHED")])
        # Pause entries: the authority fences admissions; observations keep flowing.
        paused = self.rpc("automation.pause_entries", request_id="pause-1")
        self.assertEqual(paused["state"], AutomationState.PAUSED_ENTRIES.value)
        self.tick()
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.PAUSED_ENTRIES.value)
        self.assertTrue(self.rpc("state.snapshot")["admissions_fenced"])
        # Stop and reconcile: the owned slot drains through the protected slot
        # stop, ownership goes RETIRING → RETIRED and the budget is released.
        stopped = self.rpc("automation.stop", request_id="stop-1")
        self.assertIn(stopped["state"], (AutomationState.DRAINING.value, AutomationState.STOPPED.value))
        for _ in range(20):
            if self.worker.automation.status()["state"] == AutomationState.STOPPED.value:
                break
            time.sleep(0.05)
            self.tick()
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.STOPPED.value)
        owned = self.worker.automation.ownership.owned(context)
        self.assertEqual([slot.state for slot in owned], ["RETIRED"])
        self.assertEqual(self.worker.automation.ownership.budget(context, self.worker.automation.config(context).strategy).allocated, Decimal(0))  # type: ignore[union-attr]
        self.assertIs(self.worker.slots.list(context)[0].engine_state, EngineState.STOPPED)
        self.assertFalse(self.worker.automation.status()["enabled"])

    def test_restart_never_resumes_and_keeps_the_persisted_strategy(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"]))
        self.enable()
        self.tick(2)
        context = self.context()
        self.assertEqual(len(self.worker.slots.list(context)), 1)
        self.worker.close()
        self.worker = self.new_worker()
        status = self.worker.automation.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["restart_reason"], "RESTART_REQUIRES_REENABLE")
        self.assertEqual(status["strategy"]["identity"], "pipeline-fixture@1")
        self.assertEqual(status["strategy"]["controller_profile"], "fixture-sequence-v1")
        report = self.tick()
        self.assertEqual(report["state"], AutomationState.RECOVERING.value)
        self.assertFalse(self.rpc("state.snapshot")["armed"])
        # Explicit re-enable = the operator's mandate: the recovered (quiesced)
        # context is re-applied with the SAME configuration, then armed once READY.
        enabled = self.enable(request_id="enable-after-restart")
        self.assertEqual(enabled["state"], AutomationState.RECOVERING.value)
        operation_id = enabled["reactivation_operation_id"]
        assert operation_id is not None
        final = self.worker.coordinator.wait(operation_id, timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())
        self.tick()  # arms under the recorded enable mandate
        snapshot = self.rpc("state.snapshot")
        self.assertTrue(snapshot["armed"])
        self.assertEqual(snapshot["context_epoch"], 2)
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0]), 1)  # never reseeded
        new_context = self.context()
        self.assertNotEqual(new_context.identity, context.identity)
        self.tick(4)
        status = self.worker.automation.status()
        self.assertTrue(status["enabled"])
        self.assertIsNone(status["restart_reason"])
        self.assertIsNone(status["reactivation_operation_id"])
        self.assertIn(status["state"], (AutomationState.RUNNING.value, AutomationState.OBSERVING.value, AutomationState.WARMING_UP.value))
        # Ownership moved with the re-activation: the old context's slot is history.
        self.assertEqual([slot.state for slot in self.worker.automation.ownership.owned(context)], ["RETIRED"])
        self.assertEqual([slot.state for slot in self.worker.automation.ownership.owned(new_context)], ["ACTIVE"])
        self.assertEqual(len(self.worker.slots.list(new_context)), 1)
        # Enable is idempotent after re-activation too.
        self.assertTrue(self.enable(request_id="enable-after-restart-2")["enabled"])

    def test_requests_are_idempotent_and_conflicts_are_explicit(self) -> None:
        self.initialize()
        strategy = fixture_strategy(["HOLD"])
        first = self.configure(strategy, request_id="cfg")
        replay = self.configure(strategy, request_id="cfg")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(DomainError) as conflict:
            self.configure(fixture_strategy(["BUY"]), request_id="cfg")
        self.assertEqual(conflict.exception.code, "REQUEST_ID_CONFLICT")
        self.enable(request_id="en")
        self.assertTrue(self.enable(request_id="en")["enabled"])
        # A different strategy cannot replace an enabled one without a stop.
        with self.assertRaises(DomainError) as blocked:
            self.configure(fixture_strategy(["HOLD"], strategy_id="other-fixture"), request_id="cfg-2")
        self.assertEqual(blocked.exception.code, "AUTOMATION_STOP_REQUIRED")

    def test_strategy_context_mismatches_are_refused_before_persisting(self) -> None:
        self.initialize()
        cases = (
            ("STRATEGY_PRODUCT_MISMATCH", dict(product_track="PERPETUAL", product_subtype="PERPETUAL_NO_EXPIRY",
                                              mapping_version="fixed-decoder-derivatives-step-v1",
                                              derivatives={"max_leverage": "2", "directions": "LONG_ONLY"})),
            ("STRATEGY_QUOTE_MISMATCH", dict(quote_currencies=["EUR"])),
            # Learning is exactly the declared RESEARCH_LEARNING mode; it needs research mode at the worker.
            ("INVALID_STRATEGY_DEFINITION", dict(learning_enabled=True)),
            ("LEARNING_REQUIRES_RESEARCH_MODE", dict(learning_enabled=True, controller_profile="coinfly-dual-compartment-v1", fixture_sides=[],
                                                    neural={"mode": "RESEARCH_LEARNING",
                                                            "training": {"profile_id": "malecns-risk-adjusted-experiment-v2",
                                                                         "implementation_revision": 4, "reward_interval_seconds": 1}})),
            # Scripted sides belong to the fixture controller only: definition invalid.
            ("INVALID_STRATEGY_DEFINITION", dict(controller_profile="coinfly-dual-compartment-v1")),
            # The step can never exceed the per-decision turnover fraction.
            ("INVALID_STRATEGY_DEFINITION", dict(execution={"target_step": "0.10", "max_turnover_fraction": "0.05"})),
        )
        for code, overrides in cases:
            with self.subTest(code=code), self.assertRaises(DomainError) as refused:
                self.configure(fixture_strategy(["HOLD"], **overrides), request_id=f"cfg-{code}")
            self.assertEqual(refused.exception.code, code)
        self.assertFalse(self.worker.automation.status()["configured"])

    def test_preserved_controller_reports_missing_kernel_instead_of_substituting(self) -> None:
        self.initialize()
        self.rpc("automation.configure", {"preset": "research-paper-spot", "feed": {"kind": "SYNTHETIC_FIXTURE"}}, request_id="preset")
        self.enable()
        self.tick(2)
        status = self.worker.automation.status()
        components = {item["component"]: item for item in status["components"]}
        self.assertFalse(components["kernel"]["ready"])
        self.assertEqual(components["strategy"]["code"], "NATIVE_KERNEL_UNAVAILABLE")
        self.assertEqual(status["strategy"]["controller_profile"], "coinfly-dual-compartment-v1")
        self.assertIsNone(status["last_decision"])
        self.assertIn(status["state"], (AutomationState.SETUP.value, AutomationState.PAUSED_ENTRIES.value))

    def test_manual_slot_blocks_strategy_ownership_and_budget_is_bounded(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["HOLD"], instruments=("BTC-USD", "ETH-USD", "SOL-USD"), max_active_slots=3))
        self.enable()
        snapshot = self.rpc("state.snapshot")
        # A manually created BTC slot is never taken over by the strategy.
        self.rpc("slot.create", {"instrument": "BTC-USD", "expected_revision": snapshot["config_revision"]}, request_id="manual-btc")
        self.tick()
        reconciliation = self.worker.automation.status()["reconciliation"]
        self.assertIn(["BTC-USD", "MANUAL_SLOT_OCCUPIES_INSTRUMENT"], reconciliation["blocked"])
        context = self.context()
        owned = {slot.instrument: slot for slot in self.worker.automation.ownership.owned(context)}
        self.assertEqual(sorted(owned), ["ETH-USD", "SOL-USD"])
        self.assertEqual({slot.allocated_budget for slot in owned.values()}, {Decimal("34")})
        budget = self.worker.automation.ownership.budget(context, self.worker.automation.config(context).strategy)  # type: ignore[union-attr]
        self.assertEqual((budget.approved_capital, budget.allocated, budget.slots), (Decimal(100), Decimal(68), 2))
        with self.assertRaises(DomainError) as below:
            self.worker.automation.ownership.approve_budget(context, self.worker.automation.config(context).strategy, Decimal(50), now=datetime.now(timezone.utc))  # type: ignore[union-attr]
        self.assertEqual(below.exception.code, "BUDGET_BELOW_EXISTING_ALLOCATIONS")

    def test_stale_observations_skip_decisions_without_failing(self) -> None:
        self.initialize()
        self.configure(fixture_strategy(["BUY"]))
        self.enable()
        self.tick(4)
        self.assertEqual(self.decision()["side"], "BUY")
        # Freeze the feed: no new observations → the next decision is skipped as stale.
        self.feed.stopped = True
        time.sleep(1.05)
        self.tick()
        status = self.worker.automation.status()
        components = {item["component"]: item for item in status["components"]}
        self.assertFalse(components["feed"]["ready"])
        self.assertNotEqual(status["state"], AutomationState.FAILED.value)


class DerivedTimestampsTests(TestCase):
    def test_observation_age_helpers(self) -> None:
        now = datetime.now(timezone.utc)
        observation = MarketObservation(instrument="BTC-USD", bids=(DepthLevelObservation(price=Decimal("1"), quantity=Decimal("1")),),
                                        asks=(DepthLevelObservation(price=Decimal("2"), quantity=Decimal("1")),), observed_at=now - timedelta(seconds=3),
                                        received_at=now, sequence=1, source=DataSource.SYNTHETIC_FIXTURE)
        self.assertEqual(observation.age(now), timedelta(seconds=3))
        with self.assertRaises(DomainError):
            observation.require_fresh(now, max_age=timedelta(seconds=2))


def derivatives_strategy(sides: Sequence[str], **overrides: Any) -> dict[str, Any]:
    definition = fixture_strategy(sides, product_track="PERPETUAL", product_subtype="PERPETUAL_NO_EXPIRY",
                                  mapping_version="fixed-decoder-derivatives-step-v1",
                                  derivatives={"max_leverage": "2", "directions": "BOTH", "allow_reversal": True,
                                               "margin_mode": "CROSS", "max_maintenance_utilization": "0.5"})
    definition.update(overrides)
    return definition


class DerivativesFixture(AutomationFixture):
    """A GLOBAL perpetual paper context (the international no-expiry perpetual book)."""

    def initialize(self) -> None:  # type: ignore[override]
        self.subtype = ProductSubtype.PERPETUAL_NO_EXPIRY
        config = default_config().model_copy(update={"region_policy": RegionPolicy.GLOBAL, "product_track": ProductTrack.PERPETUAL,
                                                     "execution_scope": ("paper-perpetual-book",)})
        status = self.worker.coordinator.status(self.worker.authority_id)
        revisions = {"expected_revision": status.effective_revision, "expected_epoch": status.transition_epoch}
        preview = self.rpc("context.preview", {"config": config.model_dump(mode="json"), **revisions}, request_id="init-p")
        accepted = self.rpc("context.apply", {"preview_id": preview["preview_id"], "config_digest": preview["config_digest"],
                                              "confirmation": True, **revisions}, request_id="init-a")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())

    def position(self, instrument: str = "BTC-USD") -> Decimal:
        row = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT quantity FROM ex_positions WHERE instrument=?", (instrument,)).fetchone())
        return Decimal(row[0]) if row is not None else Decimal(0)


class PaperDerivativesCycleTests(DerivativesFixture):
    """The paper perpetual book: long → flat → short → flat, then a short residual drains."""

    def test_long_flat_short_flat_with_margin_admission(self) -> None:
        self.initialize()
        self.configure(derivatives_strategy(["BUY", "SELL", "SELL", "BUY"]))
        self.enable()
        snapshot = self.rpc("state.snapshot")
        self.assertTrue(snapshot["armed"])
        self.assertEqual(snapshot["effective_context"]["product_track"], "PERPETUAL")
        self.tick(4)
        buy = self.decision()
        self.assertEqual((buy["side"], buy["outcome"]), ("BUY", "DISPATCHED"), buy)
        intent = IntentStore(self.worker.writer).get(buy["intent_ids"][0])
        self.assertEqual(intent.intent.quantity, Decimal("0.04"))
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.assertEqual(self.position(), Decimal("0.04"))
        balance = self.rpc("state.snapshot")["balance"]
        self.assertEqual(balance["coverage"], "COMPLETE_VERIFIED")
        self.assertIsNotNone(balance["unsettled_perpetual_upl"])
        # SELL from long: reduce to flat (EXIT, reduce-only through the paper matcher).
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        sell = self.decision()
        self.assertEqual((sell["side"], sell["outcome"]), ("SELL", "DISPATCHED"), sell)
        self.feed.books["BTC-USD"] = ("100.10", "100.20")
        self.tick()
        self.assertEqual(self.position(), Decimal(0))
        # SELL from flat: open a short (ENTRY against margin).
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        short = self.decision()
        self.assertEqual((short["side"], short["outcome"]), ("SELL", "DISPATCHED"), short)
        self.feed.books["BTC-USD"] = ("100.10", "100.20")
        self.tick()
        self.assertEqual(self.position(), Decimal("-0.04"))
        engine = self.worker._engines[self.context().identity]
        margin = engine.matcher.margin
        assert margin is not None
        self.assertEqual([item.quantity for item in margin.positions], [Decimal("-0.04")])
        # BUY from short: back to flat.
        time.sleep(1.05)
        self.feed.books["BTC-USD"] = ("99.90", "100.10")
        self.tick()
        cover = self.decision()
        self.assertEqual((cover["side"], cover["outcome"]), ("BUY", "DISPATCHED"), cover)
        self.feed.books["BTC-USD"] = ("99.80", "99.90")
        self.tick()
        self.assertEqual(self.position(), Decimal(0))
        Journal(self.worker.writer).verify()
        self.assertEqual(self.worker.automation.status()["execution_health"]["fills"], 4)
        rows = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT side, outcome FROM cf_automation_decisions ORDER BY decided_at").fetchall())
        self.assertEqual([tuple(row) for row in rows], [("BUY", "DISPATCHED"), ("SELL", "DISPATCHED"), ("SELL", "DISPATCHED"), ("BUY", "DISPATCHED")])

    def test_short_residual_drains_through_stop_and_reconcile(self) -> None:
        self.initialize()
        self.configure(derivatives_strategy(["SELL"]))
        self.enable()
        self.tick(4)
        self.assertEqual((self.decision()["side"], self.decision()["outcome"]), ("SELL", "DISPATCHED"), self.decision())
        self.feed.books["BTC-USD"] = ("100.10", "100.20")
        self.tick()
        self.assertEqual(self.position(), Decimal("-0.04"))
        stopped = self.rpc("automation.stop", request_id="stop-short")
        self.assertIn(stopped["state"], (AutomationState.DRAINING.value, AutomationState.STOPPED.value))
        for _ in range(30):
            if self.worker.automation.status()["state"] == AutomationState.STOPPED.value:
                break
            self.feed.books["BTC-USD"] = ("100.00", "100.10")
            time.sleep(0.05)
            self.tick()
        self.assertEqual(self.worker.automation.status()["state"], AutomationState.STOPPED.value)
        self.assertEqual(self.position(), Decimal(0))
        context = self.context()
        self.assertIs(self.worker.slots.list(context)[0].engine_state, EngineState.STOPPED)
        Journal(self.worker.writer).verify()

    def test_leverage_and_margin_limits_reject_instead_of_shrinking(self) -> None:
        self.initialize()
        # One decision may never exceed the per-decision turnover fraction: an
        # oversized step is rejected at definition time, never trimmed later.
        with self.assertRaises(DomainError) as refused:
            self.configure(derivatives_strategy(["BUY"], execution={"target_step": "0.5", "max_turnover_fraction": "0.05"}), request_id="big")
        self.assertEqual(refused.exception.code, "INVALID_STRATEGY_DEFINITION")
        self.configure(derivatives_strategy(["BUY"]))
        self.enable()
        self.tick(4)
        self.assertEqual(self.decision()["outcome"], "DISPATCHED", self.decision())
