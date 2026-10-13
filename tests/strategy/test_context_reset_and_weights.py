import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from pydantic import ValidationError

from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.features.event_time import EventTimeBars, MarketTrade
from coinfly.features.rolling import RollingFeatures
from coinfly.strategy.context_state import ContextDecisionState
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.target_coordinator import TargetCoordinator

D = Decimal
NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def context(epoch=1):
    return ContextKey(authority_id="owner", environment=Environment.SANDBOX, product_track=ProductTrack.SPOT,
                      broker_profile_id="paper-local", epoch=epoch)


def proposal(*weights, revision=0, **updates):
    fields = dict(decision_id=f"decision-{revision}", model_id="fixed", model_version="1", snapshot_id="snapshot",
                  allocation_revision=revision, expires_at=NOW + timedelta(seconds=2),
                  weights=tuple(InstrumentWeight(instrument_id=key, weight=D(value)) for key, value in weights))
    fields.update(updates)
    fields.setdefault("affected_instruments", tuple(item.instrument_id for item in fields["weights"]))
    return TargetWeights(**fields)


class WeightContractTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = TargetCoordinator(context(), ("BTC-USD", "ETH-USD"))
        self.coordinator.set_snapshot("snapshot", NOW, warm=True)

    def test_no_proposal_is_distinct_from_explicit_full_zero(self):
        self.assertIsNone(self.coordinator.bind(None, expected_epoch=1, now=NOW))
        zero = self.coordinator.bind(proposal(("BTC-USD", "0"), ("ETH-USD", "0")), expected_epoch=1, now=NOW)
        self.assertTrue(zero.is_explicit_zero)
        self.assertEqual(zero.context, context())

    def test_model_cannot_choose_context_equity_or_routing(self):
        for key, value in (("environment", "LIVE"), ("equity", D("1000000")), ("context", context()), ("signer", "live")):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                proposal(("BTC-USD", "0"), ("ETH-USD", "0"), **{key: value})

    def test_full_scope_finite_nonnegative_and_gross_limits(self):
        for weights in ((("BTC-USD", "0.5"),), (("BTC-USD", "-0.1"), ("ETH-USD", "0.2")),
                        (("BTC-USD", "0.7"), ("ETH-USD", "0.4"))):
            with self.subTest(weights=weights), self.assertRaises(DomainError):
                self.coordinator.bind(proposal(*weights), expected_epoch=1, now=NOW)
        for value in ("NaN", "Infinity", "0.0000000000000000001", "1E+40"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                proposal(("BTC-USD", value), ("ETH-USD", "0"))

    def test_ambient_precision_does_not_hide_small_gross_breach(self):
        vector = proposal(("BTC-USD", "0.999999999999999999"), ("ETH-USD", "0.000000000000000002"))
        with localcontext() as arithmetic:
            arithmetic.prec = 6
            with self.assertRaisesRegex(DomainError, "GROSS_WEIGHT_LIMIT"):
                self.coordinator.bind(vector, expected_epoch=1, now=NOW)

    def test_epochs_revision_expiry_and_feature_warmup_are_gated(self):
        vector = proposal(("BTC-USD", "0.2"), ("ETH-USD", "0.3"))
        with self.assertRaisesRegex(DomainError, "STALE_CONTEXT"):
            self.coordinator.bind(vector, expected_epoch=0, now=NOW)
        with self.assertRaisesRegex(DomainError, "INVALID_PROPOSAL_EXPIRY"):
            self.coordinator.bind(vector, expected_epoch=1, now=NOW + timedelta(seconds=3))
        bound = self.coordinator.bind(vector, expected_epoch=1, now=NOW)
        self.assertEqual(bound, self.coordinator.bind(vector, expected_epoch=1, now=NOW))
        with self.assertRaisesRegex(DomainError, "STALE_ALLOCATION_REVISION"):
            self.coordinator.bind(vector.model_copy(update={"decision_id": "new-id"}), expected_epoch=1, now=NOW)
        self.coordinator.quiesce()
        with self.assertRaisesRegex(DomainError, "STRATEGY_FENCED"):
            self.coordinator.bind(vector, expected_epoch=1, now=NOW)
        self.coordinator.retire(stopped_context=context())
        next_coordinator = TargetCoordinator(context(2), ("BTC-USD", "ETH-USD"))
        with self.assertRaisesRegex(DomainError, "STRATEGY_FENCED"):
            next_coordinator.bind(vector, expected_epoch=2, now=NOW)

    def test_transient_memory_is_not_reused_between_realms_or_epochs(self):
        memory = ContextDecisionState(context())
        memory.put("recurrent", (1.0, 2.0), context=context())
        memory.retire(stopped_context=context())
        with self.assertRaisesRegex(DomainError, "STALE_CONTEXT"):
            memory.get("recurrent", context=context(2))


class EventTimeTests(unittest.TestCase):
    def trade(self, seconds, sequence, price="100", event_id=None):
        return MarketTrade(event_id=event_id or f"trade-{sequence}", instrument_id="BTC-USD", sequence=sequence,
                           event_time=NOW + timedelta(seconds=seconds), price=D(price), quantity=D("1"))

    def test_nonoverlap_out_of_order_and_no_lookahead(self):
        bars = EventTimeBars("BTC-USD", interval=timedelta(seconds=10), allowed_lateness=timedelta(seconds=3))
        self.assertEqual(bars.ingest(self.trade(3, 2, "110"), received_at=NOW + timedelta(seconds=3)), ())
        self.assertEqual(bars.ingest(self.trade(1, 1, "100"), received_at=NOW + timedelta(seconds=4)), ())
        closed = bars.ingest(self.trade(13, 3, "120"), received_at=NOW + timedelta(seconds=13))
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0].open, closed[0].close), (D("100"), D("110")))
        self.assertFalse(closed[0].sequence_gap)
        self.assertEqual(closed[0].end, NOW + timedelta(seconds=10))
        bars.ingest(self.trade(2, 4, "1000"), received_at=NOW + timedelta(seconds=14))
        self.assertEqual(bars.late_events, 1)
        self.assertEqual(closed[0].high, D("110"))

    def test_gap_is_unknown_and_resets_rolling_warmup(self):
        bars = EventTimeBars("BTC-USD", interval=timedelta(seconds=10), allowed_lateness=timedelta(0))
        rolling = RollingFeatures(period=2)
        bars.ingest(self.trade(0, 1), received_at=NOW)
        closed = bars.ingest(self.trade(20, 2), received_at=NOW + timedelta(seconds=20))
        self.assertEqual(len(closed), 2)
        self.assertTrue(closed[1].gap)
        self.assertIsNone(closed[1].close)
        rolling.update(closed[0])
        snapshot = rolling.update(closed[1])
        self.assertFalse(snapshot.warm)
        self.assertIsNone(snapshot.mean_close)

    def test_sequence_loss_is_not_silently_used(self):
        bars = EventTimeBars("BTC-USD", interval=timedelta(seconds=10), allowed_lateness=timedelta(0))
        bars.ingest(self.trade(0, 1), received_at=NOW)
        bars.ingest(self.trade(1, 3), received_at=NOW + timedelta(seconds=1))
        closed = bars.advance_watermark(NOW + timedelta(seconds=10))
        self.assertTrue(closed[0].sequence_gap)

    def test_late_arrival_in_an_earlier_unclosed_bucket_is_retained(self):
        bars = EventTimeBars("BTC-USD", interval=timedelta(seconds=10), allowed_lateness=timedelta(seconds=20))
        bars.ingest(self.trade(13, 2), received_at=NOW + timedelta(seconds=13))
        bars.ingest(self.trade(1, 1), received_at=NOW + timedelta(seconds=14))
        closed = bars.advance_watermark(NOW + timedelta(seconds=20))
        self.assertEqual(len(closed), 2)
        self.assertEqual(closed[0].start, NOW)
        self.assertEqual(sum(item.trade_count for item in closed), 2)


def test_neural_weight_adapter_is_independent_of_ambient_decimal_precision():
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal, Inexact, localcontext

    from coinfly.domain.context import ProductTrack
    from coinfly.research.neural_adapter import FixedDecoderWeightAdapter
    from coinfly.strategy.contracts import InstrumentWeight

    adapter = FixedDecoderWeightAdapter(("BTC-USD",))
    arguments = dict(side="BUY", instrument_id="BTC-USD",
                     previous_targets=(InstrumentWeight(instrument_id="BTC-USD", weight=Decimal("0.123456789012345678")),),
                     product_track=ProductTrack.SPOT, decision_id="synthetic", snapshot_id="snapshot",
                     allocation_revision=1, expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    reference = adapter.propose(**arguments)
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = adapter.propose(**arguments)
        assert result == reference
        assert result.weights[0].weight == Decimal("0.223456789012345678")
        assert ambient.prec == 2 and ambient.traps[Inexact]
