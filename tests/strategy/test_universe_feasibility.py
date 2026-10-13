"""Universe ranking, contract-aware notional, order-size feasibility and feed subscription planning.

Uses catalog entries parsed from the captured public catalog (2026-09-22), so
contract sizes, ticks and volumes are the venue's, not invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import TestCase

from coinfly.market.coinbase_public import catalog_entry_from_product
from coinfly.market.observations import (
    DataSource,
    DepthLevelObservation,
    InstrumentCatalogEntry,
    MarketObservation,
)
from coinfly.strategy.definition import StrategyDefinition
from coinfly.strategy.universe import (
    minimum_order_notional,
    plan_subscription,
    select_universe,
    slot_cap_notional,
)

CAPTURE = json.loads((Path(__file__).parents[1] / "market" / "captures" / "coinbase_public_catalog_2026-09-22.json").read_text("utf-8"))
NOW = datetime(2026, 9, 22, 19, 25, tzinfo=timezone.utc)


def entries(section: str) -> tuple[InstrumentCatalogEntry, ...]:
    return tuple(catalog_entry_from_product(product, now=NOW) for product in CAPTURE[section]["products"])


def book(instrument: str, bid: str, ask: str, size: str = "10") -> MarketObservation:
    return MarketObservation(instrument=instrument, bids=(DepthLevelObservation(price=Decimal(bid), quantity=Decimal(size)),),
                             asks=(DepthLevelObservation(price=Decimal(ask), quantity=Decimal(size)),), observed_at=NOW,
                             received_at=NOW, sequence=1, source=DataSource.COINBASE_PUBLIC_REST)


def spot_strategy(**overrides: Any) -> StrategyDefinition:
    document: dict[str, Any] = {
        "strategy_id": "malecns-autonomous", "version": 1, "label": "test", "controller_profile": "coinfly-dual-compartment-v1",
        "mapping_version": "fixed-decoder-spot-step-v1", "product_track": "SPOT", "product_subtype": "SPOT",
        "max_active_slots": 4, "capital_budget": "100", "per_slot_max_exposure_fraction": "0.25",
        "execution": {"turnover_policy": "CONSTRAIN"}}
    document.update(overrides)
    return StrategyDefinition.model_validate_json(json.dumps(document))


def perp_strategy(capital: str = "100", **overrides: Any) -> StrategyDefinition:
    document: dict[str, Any] = {"mapping_version": "fixed-decoder-derivatives-step-v1", "product_track": "PERPETUAL",
                                "product_subtype": "PERPETUAL_STYLE_FUTURE", "capital_budget": capital,
                                "derivatives": {"directions": "BOTH"}}
    document.update(overrides)
    return spot_strategy(**document)


class FeasibilityTests(TestCase):
    def test_contract_notional_and_slot_cap(self) -> None:
        bip = next(entry for entry in entries("future") if entry.instrument_id == "BIP-20DEC30-CDE")
        # One nano-BTC contract (0.01 BTC) at 86,542.5 is 865.43 USD.
        self.assertEqual(minimum_order_notional(bip, Decimal("86542.5")), Decimal("865.425"))
        self.assertEqual(slot_cap_notional(perp_strategy()), Decimal("25"))
        selection = select_universe(perp_strategy(), entries("future"), {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")},
                                    now=NOW)
        reason = next(item for item in selection.reasons if item.instrument == "BIP-20DEC30-CDE")
        self.assertIn("MIN_ORDER_ABOVE_SLOT_CAP", reason.codes)
        self.assertIn("min_order_notional=865.42", reason.detail)  # 865.425, half-even
        self.assertIn("max_slot_notional<25.00", reason.detail)
        self.assertEqual(selection.selected, ())
        # 10,000: one contract fits a slot (2,500) but not the turnover guard
        # (strictly below 5 % = 500 per accounting window).
        tight = select_universe(perp_strategy("10000"), entries("future"), {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")},
                                now=NOW)
        reason = next(item for item in tight.reasons if item.instrument == "BIP-20DEC30-CDE")
        self.assertEqual(reason.codes[0], "MIN_ORDER_ABOVE_TURNOVER_LIMIT")
        self.assertIn("max_window_turnover<500.00", reason.detail)
        # 100,000: feasible for the strategy; the context's order limit is checked separately.
        roomy = select_universe(perp_strategy("100000"), entries("future"), {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")},
                                now=NOW, max_order_notional=Decimal("10"))
        reason = next(item for item in roomy.reasons if item.instrument == "BIP-20DEC30-CDE")
        self.assertEqual(reason.codes[0], "MIN_ORDER_ABOVE_ORDER_LIMIT")
        admitted = select_universe(perp_strategy("100000"), entries("future"),
                                   {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")}, now=NOW,
                                   max_order_notional=Decimal("1000"))
        self.assertEqual(admitted.selected, ("BIP-20DEC30-CDE",))

    def test_sizing_capital_not_the_budget_bounds_feasibility(self) -> None:
        # Budget 100 but only 10 approved: the turnover window admits < 0.50, below the 1 USD minimum order.
        btc = {"BTC-USD": book("BTC-USD", "86503.40", "86503.41", "0.1")}
        selection = select_universe(spot_strategy(), entries("spot"), btc, now=NOW, capital=Decimal("10"))
        reason = next(item for item in selection.reasons if item.instrument == "BTC-USD")
        self.assertEqual(reason.codes[0], "MIN_ORDER_ABOVE_TURNOVER_LIMIT")
        self.assertIn("max_window_turnover<0.50", reason.detail)
        self.assertEqual(select_universe(spot_strategy(), entries("spot"), btc, now=NOW, capital=Decimal("100")).selected, ("BTC-USD",))
        # Leverage never raises a slot's cash authority (as in admission).
        levered = perp_strategy("20000", max_active_slots=25, per_slot_max_exposure_fraction="0.04",
                                derivatives={"directions": "BOTH", "max_leverage": "2"})
        self.assertEqual(slot_cap_notional(levered), Decimal("800"))
        selection = select_universe(levered, entries("future"), {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")},
                                    now=NOW)
        self.assertIn("MIN_ORDER_ABOVE_SLOT_CAP", next(item for item in selection.reasons if item.instrument == "BIP-20DEC30-CDE").codes)

    def test_top_of_book_notional_counts_contract_size(self) -> None:
        # 10 contracts x 0.01 BTC x 86,535 = 8,653.50 USD — not 865,350.
        strategy = perp_strategy("100000", filters={"min_top_of_book_notional": "10000"})
        selection = select_universe(strategy, entries("future"), {"BIP-20DEC30-CDE": book("BIP-20DEC30-CDE", "86535", "86550")}, now=NOW)
        reason = next(item for item in selection.reasons if item.instrument == "BIP-20DEC30-CDE")
        self.assertIn("TOP_OF_BOOK_BELOW_MINIMUM", reason.codes)
        self.assertIn("top_of_book_notional=8653.50", reason.detail)


class RankingTests(TestCase):
    def test_volume_ranking_uses_quote_currency_volume(self) -> None:
        observations = {name: book(name, bid, ask, size) for name, bid, ask, size in (
            ("BTC-USD", "86503.40", "86503.41", "0.1"), ("S-USD", "0.04382", "0.04383", "50000"),
            ("ETH-USD", "2760.02", "2760.03", "1"), ("SOL-USD", "118.39", "118.40", "10"))}
        strategy = spot_strategy(max_active_slots=2, per_slot_max_exposure_fraction="0.5")
        selection = select_universe(strategy, entries("spot"), observations, now=NOW)
        # S-USD trades the most coins, BTC-USD and ETH-USD the most dollars.
        self.assertEqual(selection.selected, ("BTC-USD", "ETH-USD"))
        ranks = {reason.instrument: reason.rank for reason in selection.reasons if reason.rank is not None}
        self.assertEqual(ranks["S-USD"], 3)


class SubscriptionPlanTests(TestCase):
    def test_kept_instruments_first_then_ranked_candidates_within_capacity(self) -> None:
        plan = plan_subscription(spot_strategy(), entries("spot"), capacity=3, keep=("SOL-USD",))
        self.assertEqual(plan, ("SOL-USD", "BTC-USD", "ETH-USD"))
        # Delisted DAI and other quotes are never subscribed; capacity bounds the list.
        self.assertNotIn("DAI-USD", plan_subscription(spot_strategy(), entries("spot"), capacity=10))
        self.assertEqual(len(plan_subscription(spot_strategy(), entries("spot"), capacity=2)), 2)

    def test_infeasible_contracts_are_not_subscribed(self) -> None:
        self.assertEqual(plan_subscription(perp_strategy(), entries("future"), capacity=10), ())
        plan = plan_subscription(perp_strategy("100000"), entries("future"), capacity=10, max_order_notional=Decimal("5000"))
        self.assertEqual(plan[0], "BIP-20DEC30-CDE")  # largest dollar volume
        self.assertTrue(set(plan) <= {"BIP-20DEC30-CDE", "ETP-20DEC30-CDE", "SLP-20DEC30-CDE", "TEK-19DEC30-CDE"})
