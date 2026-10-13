"""Causal sensory frames (exact-tick transform) and deterministic universe selection."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase, skipUnless

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.market.feed import fixture_catalog
from coinfly.market.observations import DataSource, DepthLevelObservation, MarketObservation
from coinfly.strategy.definition import (
    ControllerProfile,
    MappingVersion,
    SelectionOrder,
    StrategyDefinition,
    UniverseFilters,
    research_paper_preset,
)
from coinfly.strategy.sensory import (
    FEATURE_NAMES,
    SensoryFrameBuilder,
    SensoryScale,
    exact_ticks,
    fit_scales,
    observation_features,
    render_frame,
)
from coinfly.strategy.universe import select_universe

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
HAS_NUMBA = importlib.util.find_spec("numba") is not None


def book(instrument: str, sequence: int, bid: str, ask: str, *, at: datetime = NOW, bid_size: str = "10", ask_size: str = "10") -> MarketObservation:
    return MarketObservation(instrument=instrument, bids=(DepthLevelObservation(price=Decimal(bid), quantity=Decimal(bid_size)),),
                             asks=(DepthLevelObservation(price=Decimal(ask), quantity=Decimal(ask_size)),), observed_at=at, received_at=at,
                             sequence=sequence, source=DataSource.SYNTHETIC_FIXTURE)


class SensoryTests(TestCase):
    def test_features_are_exact_and_on_grid(self) -> None:
        first = book("BTC-USD", 1, "99.90", "100.10")
        second = book("BTC-USD", 2, "100.00", "100.20", bid_size="30", ask_size="10")
        row = observation_features(first, second, price_tick=Decimal("0.01"))
        self.assertEqual(row, (Decimal("100.10"), Decimal("19.98"), Decimal("0.500000"), Decimal("0.10")))
        self.assertEqual(len(row), len(FEATURE_NAMES))

    def test_exact_ticks_reference_algorithm_and_offgrid_rejection(self) -> None:
        scales = (SensoryScale("mid_offset", Decimal("100.00"), Decimal("0.01"), 200, NOW),
                  SensoryScale("x", Decimal(0), Decimal("0.5"), 4, NOW))
        ticks = exact_ticks(((Decimal("100.25"), Decimal("-1.5")), (Decimal("99.99"), Decimal("2"))), scales)
        self.assertEqual(ticks.tolist(), [[25, -3], [-1, 4]])
        self.assertEqual(ticks.dtype, np.int64)
        with self.assertRaisesRegex(DomainError, "SENSORY_VALUE_OFF_GRID"):
            exact_ticks(((Decimal("100.001"), Decimal(0)),), scales)
        rgb = render_frame(ticks, scales)
        self.assertEqual(rgb.shape, (2, 2, 3))
        self.assertFalse(rgb.flags.writeable)
        self.assertEqual(rgb[0, 0, 0], rgb[0, 0, 2])  # grayscale replicated across channels
        self.assertEqual(rgb[1, 1, 0], 255)  # saturates at +1

    @skipUnless(HAS_NUMBA, "research vectorization requires numba")
    def test_exact_ticks_equal_research_vectorize(self) -> None:
        from coinfly.research.vectorization import TickScale, vectorize

        scales = (SensoryScale("mid_offset", Decimal("100.00"), Decimal("0.01"), 200, NOW),
                  SensoryScale("spread_bps", Decimal(0), Decimal("0.01"), 5000, NOW))
        rows = ((Decimal("100.25"), Decimal("19.98")), (Decimal("99.99"), Decimal("0.01")))
        research = vectorize(rows, tuple(TickScale(s.name, s.origin, s.tick, s.scale_ticks, s.fitted_through) for s in scales))
        self.assertEqual(exact_ticks(rows, scales).tolist(), research.ticks.tolist())
        # And the rendered pixels equal the research projection of the same tensor.
        pixels = np.rint((np.clip(research.values, -1, 1) + 1) * 127.5).astype(np.uint8)
        self.assertEqual(render_frame(exact_ticks(rows, scales), scales)[:, :, 0].tolist(), pixels.tolist())

    def test_builder_is_causal_and_warms_from_its_own_window(self) -> None:
        builder = SensoryFrameBuilder(instrument="BTC-USD", price_tick=Decimal("0.01"), warmup_observations=3, feature_window=4)
        for index in range(1, 5):
            self.assertTrue(builder.ingest(book("BTC-USD", index, "99.90", "100.10", at=NOW + timedelta(seconds=index))))
        self.assertTrue(builder.warm)
        self.assertFalse(builder.ingest(book("BTC-USD", 2, "99.90", "100.10", at=NOW + timedelta(seconds=9))))  # sequence regression ignored
        with self.assertRaisesRegex(DomainError, "OBSERVATION_TIME_REGRESSION"):
            builder.ingest(book("BTC-USD", 9, "99.90", "100.10", at=NOW))
        scales = builder.scales
        assert scales is not None
        self.assertEqual(scales[0].fitted_through, NOW + timedelta(seconds=3))  # fitted on the warm-up rows only
        frame = builder.frame(decision_at=NOW + timedelta(seconds=10))
        self.assertEqual(frame.observed_through, NOW + timedelta(seconds=4))
        self.assertEqual(frame.rgb.shape, (4, 4, 3))
        self.assertEqual(len(frame.sha256), 64)
        again = builder.frame(decision_at=NOW + timedelta(seconds=10))
        self.assertEqual(frame.sha256, again.sha256)  # deterministic
        with self.assertRaisesRegex(DomainError, "SENSORY_FUTURE_OBSERVATION"):
            builder.frame(decision_at=NOW)
        cold = SensoryFrameBuilder(instrument="BTC-USD", price_tick=Decimal("0.01"), warmup_observations=3, feature_window=4)
        with self.assertRaisesRegex(DomainError, "SENSORY_WARMUP_INCOMPLETE"):
            cold.frame(decision_at=NOW)
        with self.assertRaisesRegex(DomainError, "SENSORY_WARMUP_REQUIRED"):
            fit_scales((), price_tick=Decimal("0.01"), fitted_through=NOW)


class UniverseTests(TestCase):
    def definition(self, **overrides: object) -> StrategyDefinition:
        base = research_paper_preset().model_dump()
        base.update(overrides)
        return StrategyDefinition.model_validate(base)

    def test_selection_explains_every_rejection_and_is_deterministic(self) -> None:
        catalog = fixture_catalog(("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "BTC-EUR"), now=NOW)
        catalog = tuple(entry.model_copy(update={"volume_24h": Decimal(volume)}) for entry, volume in
                        zip(catalog, ("500", "900", "900", "50", "100")))
        observations = {
            "BTC-USD": book("BTC-USD", 1, "99.90", "100.10"),
            "ETH-USD": book("ETH-USD", 1, "49.90", "50.10"),
            "SOL-USD": book("SOL-USD", 1, "9.99", "10.01", at=NOW - timedelta(seconds=30), bid_size="100", ask_size="100"),  # stale
            "DOGE-USD": book("DOGE-USD", 1, "0.10", "0.11", bid_size="1", ask_size="1"),  # wide + thin
            "BTC-EUR": book("BTC-EUR", 1, "99.90", "100.10"),
        }
        definition = self.definition(max_active_slots=2)
        selection = select_universe(definition, catalog, observations, now=NOW)
        self.assertEqual(selection.selected, ("ETH-USD", "BTC-USD"))  # volume desc, ETH 900 > BTC 500
        codes = {reason.instrument: reason.codes for reason in selection.reasons}
        self.assertEqual(codes["SOL-USD"], ("OBSERVATION_STALE",))
        self.assertEqual(codes["DOGE-USD"], ("SPREAD_ABOVE_MAXIMUM", "TOP_OF_BOOK_BELOW_MINIMUM"))
        self.assertEqual(codes["BTC-EUR"], ("QUOTE_NOT_PERMITTED",))
        again = select_universe(definition, catalog, observations, now=NOW)
        self.assertEqual(selection.inputs_digest, again.inputs_digest)
        self.assertEqual(selection.as_dict()["selected"], ["ETH-USD", "BTC-USD"])
        thresholds = {reason.instrument: dict(reason.thresholds) for reason in selection.reasons}
        self.assertEqual(thresholds["DOGE-USD"]["max_spread_bps"], "50")

    def test_ties_break_by_instrument_id_and_priority_order_is_explicit(self) -> None:
        catalog = fixture_catalog(("SOL-USD", "ETH-USD", "BTC-USD"), now=NOW)
        observations = {name: book(name, 1, "99.90", "100.10") for name in ("SOL-USD", "ETH-USD", "BTC-USD")}
        definition = self.definition(max_active_slots=2)
        self.assertEqual(select_universe(definition, catalog, observations, now=NOW).selected, ("BTC-USD", "ETH-USD"))
        prioritized = self.definition(max_active_slots=2, filters=UniverseFilters(order=SelectionOrder.PRIORITY_LIST, priority=("SOL-USD",)).model_dump())
        selection = select_universe(prioritized, catalog, observations, now=NOW)
        self.assertEqual(selection.selected, ("SOL-USD", "BTC-USD"))
        beyond = [reason for reason in selection.reasons if reason.instrument == "ETH-USD"][0]
        self.assertEqual(beyond.codes, ("BEYOND_MAX_ACTIVE_SLOTS",))

    def test_untradable_and_not_permitted(self) -> None:
        catalog = fixture_catalog(("BTC-USD", "ETH-USD"), now=NOW)
        catalog = (catalog[0].model_copy(update={"trading_disabled": True}), catalog[1])
        observations = {name: book(name, 1, "99.90", "100.10") for name in ("BTC-USD", "ETH-USD")}
        definition = self.definition(permitted_instruments=("BTC-USD",))
        selection = select_universe(definition, catalog, observations, now=NOW)
        self.assertEqual(selection.selected, ())
        codes = {reason.instrument: reason.codes for reason in selection.reasons}
        self.assertEqual(codes["BTC-USD"], ("NOT_TRADABLE",))
        self.assertEqual(codes["ETH-USD"], ("NOT_PERMITTED",))

    def test_preset_is_research_only_with_the_preserved_controller(self) -> None:
        preset = research_paper_preset()
        self.assertTrue(preset.research_only)
        self.assertIs(preset.controller_profile, ControllerProfile.PRESERVED_MALECNS_V1)
        self.assertIs(preset.mapping_version, MappingVersion.SPOT_STEP_V1)
        self.assertFalse(preset.learning_enabled)
        self.assertEqual(preset.execution.target_step, Decimal("0.05"))
        self.assertLessEqual(preset.execution.target_step, preset.execution.max_turnover_fraction)
        self.assertEqual(preset.identity, "research-paper-spot@1")
