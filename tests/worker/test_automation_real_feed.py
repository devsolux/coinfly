"""Automation through the real supervisor and the real public REST feed class over the captured catalog.

Regression for "Strategy automation does nothing": with the live catalog the
loop entered FAILED at SETUP on its first tick (a pydantic ValidationError from
single-letter asset codes escaped the catalog refresh), the feed factory ignored
the context's contract family, and a catalog/network problem was a fatal
control-plane failure. Here the feed is ``CoinbasePublicRestFeed`` itself; only
its reader is replaced by records captured from the public endpoint
(tests/market/captures), with books re-stamped to the current time. The polling
thread is replaced by explicit ``sweep_once`` calls so ticks are deterministic.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Sequence, cast
from unittest import TestCase

from coinfly.config.models import EngineConfig, default_config
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import DomainError, ProductTrack, RegionPolicy
from coinfly.domain.products import ProductSubtype
from coinfly.execution.intent_store import IntentStore
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCRequest
from coinfly.market.coinbase_public import CoinbasePublicRestFeed
from coinfly.market.observations import DataSource
from coinfly.worker.automation import AutomationConfig, AutomationState
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor

from .test_automation_lifecycle import TestOnlyBootstrap

CAPTURE = json.loads((Path(__file__).parents[1] / "market" / "captures" / "coinbase_public_catalog_2026-09-22.json").read_text("utf-8"))
# Top of book per instrument around the captured prices (bid, ask, displayed size).
BOOKS: dict[str, tuple[str, str, str]] = {
    "BTC-USD": ("86503.40", "86503.41", "0.1"), "ETH-USD": ("2760.02", "2760.03", "1.5"),
    "SOL-USD": ("118.39", "118.40", "25"), "S-USD": ("0.04382", "0.04383", "60000"),
    "BIP-20DEC30-CDE": ("86535", "86550", "96"), "ETP-20DEC30-CDE": ("2759.5", "2760", "120"),
    "SLP-20DEC30-CDE": ("118.38", "118.40", "40"), "TEK-19DEC30-CDE": ("2450.0", "2450.2", "30"),
}


class LiveShapedReader:
    """Captured catalog; books at the captured prices, stamped now; optional catalog outages."""

    def __init__(self) -> None:
        self.catalog_failures: list[str] = []
        self.book_failures: dict[str, list[str]] = {}
        self.catalog_calls = 0
        self.books = dict(BOOKS)

    def products(self, *, product_type: str = "SPOT") -> list[Mapping[str, Any]]:
        self.catalog_calls += 1
        if self.catalog_failures:
            raise DomainError(self.catalog_failures.pop(0))
        return list(CAPTURE["spot" if product_type == "SPOT" else "future"]["products"])

    def product_book(self, product_id: str, *, limit: int = 10) -> Mapping[str, Any]:
        pending = self.book_failures.get(product_id)
        if pending:
            raise DomainError(pending.pop(0))
        if product_id not in self.books:
            raise DomainError("HTTP_READ_FAILED")
        bid, ask, size = self.books[product_id]
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {"product_id": product_id, "time": stamp, "bids": [{"price": bid, "size": size}], "asks": [{"price": ask, "size": size}]}

    def retire(self) -> None:
        pass


class ManualRestFeed(CoinbasePublicRestFeed):
    """The production REST feed without its polling thread (tests sweep explicitly)."""

    def _run(self) -> None:
        return None


class RealFeedFixture(TestCase):
    subtype = ProductSubtype.SPOT

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-real-feed-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reader = LiveShapedReader()
        self.feeds: list[ManualRestFeed] = []
        self.worker = WorkerSupervisor(paths=WorkerPaths(self.root / "worker"), socket_path=self.root / "worker.sock",
                                       bootstrap=TestOnlyBootstrap(self.root))
        self.addCleanup(self.worker.close)
        self.worker.automation.loop_enabled = False
        self.worker.automation._feed_factory = self.new_feed  # type: ignore[method-assign]

    def new_feed(self, config: EngineConfig, automation: AutomationConfig) -> ManualRestFeed:
        # Same arguments as the supervisor's factory (subtype from the context), a scripted reader.
        feed = ManualRestFeed(quote_currencies=(config.reporting_currency,), subtype=config.effective_subtype,
                              reader=self.reader, sleeper=lambda _: None)  # type: ignore[arg-type]
        self.feeds.append(feed)
        return feed

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        epoch = self.worker.coordinator.status(self.worker.authority_id).transition_epoch
        request = RPCRequest.parse({"jsonrpc": "2.0", "id": request_id or method, "method": method, "params": params or {},
                                    "epoch": epoch, "deadline_ms": int(time.time() * 1000) + 10000})
        return cast(dict[str, Any], self.worker.handle(request, PeerIdentity(uid=os.getuid(), gid=os.getgid(), pid=os.getpid())))

    def initialize(self, config: EngineConfig | None = None) -> None:
        config = config or default_config()
        status = self.worker.coordinator.status(self.worker.authority_id)
        revisions = {"expected_revision": status.effective_revision, "expected_epoch": status.transition_epoch}
        preview = self.rpc("context.preview", {"config": config.model_dump(mode="json"), **revisions}, request_id="init-p")
        accepted = self.rpc("context.apply", {"preview_id": preview["preview_id"], "config_digest": preview["config_digest"],
                                              "confirmation": True, **revisions}, request_id="init-a")
        final = self.worker.coordinator.wait(accepted["operation_id"], timeout=10)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())

    def enable(self, request_id: str = "enable-1") -> dict[str, Any]:
        snapshot = self.rpc("state.snapshot")
        return self.rpc("automation.enable", {"expected_revision": snapshot["config_revision"],
                                              "expected_epoch": snapshot["context_epoch"]}, request_id=request_id)

    def tick(self, count: int = 1) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for _ in range(count):
            for feed in self.feeds:
                if feed.health().subscribed:
                    feed.sweep_once()
            report = self.worker.automation.tick(datetime.now(timezone.utc))
        return report

    def components(self) -> dict[str, dict[str, Any]]:
        return {item["component"]: item for item in self.worker.automation.status()["components"]}


def strategy(*, subtype: str = "SPOT", capital: str = "100", sides: Sequence[str] = (), **overrides: Any) -> dict[str, Any]:
    """The Overview's ``malecns-autonomous`` preset as the user configured it (4 slots, 25 % each)."""
    derivative = subtype != "SPOT"
    document: dict[str, Any] = {
        "strategy_id": "malecns-autonomous", "version": 1, "label": "Full MaleCNS automatic strategy — no profitability qualification implied",
        "research_only": True, "controller_profile": "fixture-sequence-v1" if sides else "coinfly-dual-compartment-v1",
        "mapping_version": "fixed-decoder-derivatives-step-v1" if derivative else "fixed-decoder-spot-step-v1",
        "product_track": "PERPETUAL" if derivative else "SPOT", "product_subtype": subtype,
        "quote_currencies": ["USD"], "market_data": {"max_observation_age_seconds": 5, "warmup_observations": 3, "feature_window": 4},
        "max_active_slots": 4, "capital_budget": capital, "per_slot_max_exposure_fraction": "0.2500",
        "execution": {"proposal_ttl_seconds": 5, "decision_interval_seconds": 2, "accounting_interval_seconds": 60,
                      "turnover_policy": "CONSTRAIN", "target_step": "0.05", "max_turnover_fraction": "0.05"},
        "fixture_sides": list(sides),
    }
    if derivative:
        document["derivatives"] = {"max_leverage": "1", "directions": "BOTH", "margin_mode": "CROSS"}
    document.update(overrides)
    return document


class SpotRealFeedTests(RealFeedFixture):
    def test_preserved_controller_without_kernel_waits_in_setup_and_says_why(self) -> None:
        self.initialize()
        self.rpc("automation.configure", {"strategy": strategy(), "approved_capital": "100",
                                          "feed": {"kind": "COINBASE_PUBLIC_REST"}}, request_id="configure-1")
        self.assertTrue(self.enable()["enabled"])
        self.tick(3)
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"], status["failure"])  # used to be FAILED at SETUP: ValidationError
        self.assertEqual(status["state"], AutomationState.SETUP.value)
        components = self.components()
        self.assertEqual(components["kernel"]["code"], "NATIVE_KERNEL_UNAVAILABLE")
        self.assertFalse(components["strategy"]["ready"])
        self.assertEqual((components["feed"]["ready"], components["feed"]["code"]), (True, "FEED_FRESH"))
        feed = status["feed"]
        self.assertEqual(feed["label"], "COINBASE PUBLIC REST (polled)")
        # Ranked by dollar volume and capped for REST freshness (4 slots + 2); the delisted product is not polled.
        self.assertEqual(feed["subscribed"], ["BTC-USD", "ETH-USD", "SOL-USD", "S-USD"])
        self.assertEqual(status["universe"]["selected"], ["BTC-USD", "ETH-USD", "SOL-USD", "S-USD"])

    def test_pipeline_fixture_buys_on_the_live_shaped_book(self) -> None:
        self.initialize()
        self.rpc("automation.configure", {"strategy": strategy(sides=["BUY"], max_active_slots=1, per_slot_max_exposure_fraction="0.34"),
                                          "approved_capital": "100", "feed": {"kind": "COINBASE_PUBLIC_REST"},
                                          "pipeline_test_mode": True}, request_id="configure-1")
        self.enable()
        self.tick(5)  # subscribe, then three observations of warm-up, then the decision
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"])
        self.assertEqual(status["feed"]["subscribed"], ["BTC-USD", "ETH-USD", "SOL-USD"])  # REST: 1 slot + 2 spares
        self.assertEqual(status["universe"]["selected"], ["BTC-USD"])
        decision = status["last_decision"]
        self.assertEqual((decision["side"], decision["outcome"]), ("BUY", "DISPATCHED"), decision)
        intent = IntentStore(self.worker.writer).get(decision["intent_ids"][0]).intent
        self.assertEqual(intent.instrument, "BTC-USD")
        self.assertEqual(intent.limit_price, Decimal("86503.40"))  # mark rounded down to the 0.01 tick
        self.assertLess(intent.quantity * intent.limit_price, Decimal("5"))  # CONSTRAIN: strictly below 5 % of 100
        self.assertGreater(intent.quantity * intent.limit_price, Decimal("4.99"))

    def test_catalog_outage_degrades_the_feed_and_recovers_without_failing(self) -> None:
        self.initialize()
        self.reader.catalog_failures = ["HTTP_READ_FAILED"]
        self.rpc("automation.configure", {"strategy": strategy(), "approved_capital": "100",
                                          "feed": {"kind": "COINBASE_PUBLIC_REST"}}, request_id="configure-1")
        self.enable()
        self.tick()
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"])
        self.assertEqual(status["state"], AutomationState.SETUP.value)
        feed = self.components()["feed"]
        self.assertEqual(feed["code"], "FEED_CATALOG_UNAVAILABLE")
        self.assertIn("HTTP_READ_FAILED", feed["detail"])
        # Backed off: further ticks neither fail nor hit the network again.
        self.tick(3)
        self.assertEqual(self.reader.catalog_calls, 1)
        self.assertIsNone(self.worker.automation.status()["failure"])
        # Once both back-offs have passed the start is retried and succeeds.
        self.worker.automation._feed_retry_at = datetime.now(timezone.utc)
        self.feeds[0]._catalog._failed_at = None
        self.tick(2)
        self.assertEqual(self.worker.automation.status()["feed"]["subscribed"], ["BTC-USD", "ETH-USD", "SOL-USD", "S-USD"])
        self.assertEqual(self.components()["feed"]["code"], "FEED_FRESH")

    def test_book_updates_reach_the_paper_venue_in_receive_order(self) -> None:
        """A poll that fails and succeeds within one tick must not replay an older book after a newer one."""
        self.initialize()
        self.rpc("automation.configure", {"strategy": strategy(max_active_slots=1), "approved_capital": "100",
                                          "feed": {"kind": "COINBASE_PUBLIC_REST"}}, request_id="configure-1")
        self.enable()
        self.tick(2)
        feed = self.feeds[0]
        self.reader.book_failures["BTC-USD"] = ["HTTP_READ_FAILED"]
        feed.sweep_once()  # ETH, SOL observed; BTC failed
        feed.sweep_once()  # BTC, ETH, SOL observed later
        self.worker.automation.tick(datetime.now(timezone.utc))
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"], status["failure"])  # used to be PAPER_TIME_REGRESSION → FAILED
        self.assertFalse(self.rpc("state.snapshot")["admissions_fenced"])

    def test_replay_without_a_recording_is_refused_at_configure(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(DomainError, "REPLAY_NAME_REQUIRED"):
            self.rpc("automation.configure", {"strategy": strategy(), "approved_capital": "100", "feed": {"kind": "REPLAY_FILE"}},
                     request_id="configure-replay")


def futures_context(*, funding: str = "100", max_order_notional: str = "10") -> EngineConfig:
    """The context the user ran: SANDBOX, GLOBAL, perpetual-style futures, its own paper book."""
    return default_config().model_copy(update={
        "region_policy": RegionPolicy.GLOBAL, "product_track": ProductTrack.PERPETUAL,
        "product_subtype": ProductSubtype.PERPETUAL_STYLE_FUTURE, "execution_scope": ("paper-perpetual-style-book",),
        "paper_funding": default_config().paper_funding.model_copy(update={"amount": Decimal(funding)}),
        "limits": default_config().limits.model_copy(update={"max_order_notional": Decimal(max_order_notional)})})


class FuturesRealFeedTests(RealFeedFixture):
    def test_supervisor_feed_factory_uses_the_context_contract_family(self) -> None:
        config = futures_context()
        automation = AutomationConfig.model_validate({"strategy": strategy(subtype="PERPETUAL_STYLE_FUTURE"), "approved_capital": "100",
                                                      "feed": {"kind": "COINBASE_PUBLIC_REST"}, "configured_at": datetime.now(timezone.utc),
                                                      "request_id": "r"}, strict=False)
        for kind in ("COINBASE_PUBLIC_REST", "COINBASE_PUBLIC_WEBSOCKET"):
            with self.subTest(kind):
                selected = automation.model_copy(update={"feed": automation.feed.model_copy(update={"kind": DataSource(kind)})})
                feed = WorkerSupervisor._feed_factory(self.worker, config, selected)
                self.assertIs(feed._catalog._subtype, ProductSubtype.PERPETUAL_STYLE_FUTURE)
                feed.stop()

    def test_small_capital_reports_contract_sizes_instead_of_failing(self) -> None:
        self.initialize(futures_context())
        self.rpc("automation.configure", {"strategy": strategy(subtype="PERPETUAL_STYLE_FUTURE"), "approved_capital": "100",
                                          "feed": {"kind": "COINBASE_PUBLIC_REST"}}, request_id="configure-1")
        self.enable()
        self.tick(2)
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"])
        self.assertEqual(status["state"], AutomationState.SETUP.value)
        self.assertEqual(self.components()["feed"]["code"], "NO_ELIGIBLE_INSTRUMENTS")
        self.assertEqual(status["feed"]["catalog"]["entries"], 4)
        self.assertEqual(status["feed"]["catalog"]["other_subtypes"], {"DATED_FUTURE": 4})
        reasons = {item["instrument"]: item for item in status["universe"]["reasons"]}
        self.assertIn("MIN_ORDER_ABOVE_SLOT_CAP", reasons["BIP-20DEC30-CDE"]["codes"])
        self.assertIn("MIN_ORDER_ABOVE_ORDER_LIMIT", reasons["BIP-20DEC30-CDE"]["codes"])

    def test_pipeline_fixture_opens_whole_contracts_with_the_contract_size(self) -> None:
        self.initialize(futures_context(funding="100000", max_order_notional="5000"))
        definition = strategy(subtype="PERPETUAL_STYLE_FUTURE", capital="100000", sides=["BUY"], max_active_slots=1,
                              per_slot_max_exposure_fraction="0.2500", permitted_instruments=["BIP-20DEC30-CDE"])
        self.rpc("automation.configure", {"strategy": definition, "approved_capital": "100000", "feed": {"kind": "COINBASE_PUBLIC_REST"},
                                          "pipeline_test_mode": True}, request_id="configure-1")
        self.enable()
        self.tick(5)
        status = self.worker.automation.status()
        self.assertIsNone(status["failure"])
        decision = status["last_decision"]
        self.assertEqual((decision["side"], decision["outcome"]), ("BUY", "DISPATCHED"), decision)
        intent = IntentStore(self.worker.writer).get(decision["intent_ids"][0]).intent
        # 5 % of 100,000 = 5,000 USD; one 0.01-BTC contract at ~86,540 is 865.40 USD → 5 whole contracts,
        # at a limit on the 5 USD tick, bound to the venue's contract size.
        self.assertEqual((intent.instrument, intent.quantity, intent.contract_multiplier, intent.limit_price),
                         ("BIP-20DEC30-CDE", Decimal("5"), Decimal("0.01"), Decimal("86540")))
        self.assertEqual(intent.base_asset, "BTC")
