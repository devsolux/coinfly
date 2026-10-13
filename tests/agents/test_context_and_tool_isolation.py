import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from pydantic import ValidationError

from coinfly.agents.action_provider import ActionProvider
from coinfly.agents.model_gateway import create_model_provider_gateway
from coinfly.agents.non_signing_provider import NonSigningProvider, default_wallet_provider
from coinfly.agents.runner import create_model_host
from coinfly.agents.tool_contracts import BookLevel, CachedBook, CachedFeatures, FeatureValue
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


class Cache:
    as_of = NOW

    def features(self, query):
        return CachedFeatures(
            snapshot_id=query.snapshot_id,
            as_of=self.as_of,
            values=(
                FeatureValue(instrument_id=query.instruments[0], name="return_1m", value=Decimal("0.01")),
            ),
        )

    def order_book(self, query):
        return CachedBook(
            snapshot_id=query.snapshot_id,
            instrument_id=query.instrument_id,
            as_of=self.as_of,
            bids=(BookLevel(price=Decimal("99"), quantity=Decimal("1")),),
            asks=(BookLevel(price=Decimal("101"), quantity=Decimal("1")),),
        )


class Sink:
    def __init__(self):
        self.proposals = []

    def submit(self, proposal, *, expected_epoch):
        self.proposals.append((proposal, expected_epoch))
        return "proposal-receipt"


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.context = ContextKey(
            authority_id="owner",
            environment=Environment.SANDBOX,
            product_track=ProductTrack.SPOT,
            broker_profile_id="paper",
            epoch=7,
        )
        self.cache = Cache()
        self.sink = Sink()
        self.current = True
        self.now = NOW
        self.provider = ActionProvider(
            context=self.context,
            cache=self.cache,
            sink=self.sink,
            context_is_current=lambda context: self.current and context == self.context,
            deadline=NOW + timedelta(seconds=30),
            clock=lambda: self.now,
        )

    def proposal(self, **updates):
        values = dict(
            decision_id="decision",
            model_id="fixed-test",
            model_version="1.0.0",
            snapshot_id="snapshot",
            allocation_revision=1,
            expires_at=(NOW + timedelta(seconds=20)).isoformat(),
            weights=[{"instrument_id": "BTC-USD", "weight": "0.20"}],
            # The action scope is explicit (TargetWeights.affected_instruments):
            # a full weight vector alone is not authority to rebalance everything.
            affected_instruments=["BTC-USD"],
        )
        values.update(updates)
        return json.dumps(values)

    def test_exact_tool_allowlist_and_no_wallet_authority(self):
        self.assertEqual(
            self.provider.tool_names,
            ("get_cached_features", "get_cached_order_book", "submit_target_weights"),
        )
        for name in (
            "transfer",
            "sign_transaction",
            "context.apply",
            "get_balance",
            "sweep",
            "set_region",
            "stop",
            "delete",
            "acknowledge_alarm",
        ):
            with self.assertRaisesRegex(DomainError, "AGENT_TOOL_FORBIDDEN"):
                self.provider.invoke(name, "{}")
        wallet = NonSigningProvider()
        for operation in (
            wallet.get_address,
            lambda: wallet.sign_message(b"test"),
            lambda: wallet.send_transaction(b"test"),
            default_wallet_provider,
            create_model_host,
            create_model_provider_gateway,
        ):
            with self.assertRaises(DomainError):
                operation()

    def test_weight_only_submission_bound_by_runtime_epoch(self):
        receipt = json.loads(self.provider.invoke("submit_target_weights", self.proposal()))
        self.assertEqual(receipt["status"], "PROPOSED")
        self.assertEqual(len(self.sink.proposals), 1)
        proposal, epoch = self.sink.proposals[0]
        self.assertEqual(epoch, 7)
        self.assertEqual(proposal.weights[0].weight, Decimal("0.20"))
        self.assertFalse(hasattr(proposal, "context"))

    def test_context_balance_and_region_overrides_rejected_before_sink(self):
        for field, value in (
            ("context", {"epoch": 8}),
            ("environment", "LIVE"),
            ("region_policy", "GLOBAL"),
            ("equity", "1000000"),
            ("signer", "key"),
            ("margin", "100"),
        ):
            with self.assertRaisesRegex(DomainError, "AGENT_TOOL_INPUT_INVALID"):
                self.provider.invoke("submit_target_weights", self.proposal(**{field: value}))
        self.assertEqual(self.sink.proposals, [])

    def test_cached_features_and_book_only_have_fresh_scoped_market_data(self):
        features = json.loads(
            self.provider.invoke(
                "get_cached_features", '{"snapshot_id":"snapshot","instruments":["BTC-USD"]}'
            )
        )
        book = json.loads(
            self.provider.invoke(
                "get_cached_order_book", '{"snapshot_id":"snapshot","instrument_id":"BTC-USD","depth":1}'
            )
        )
        self.assertEqual(features["values"][0]["name"], "return_1m")
        self.assertEqual(book["instrument_id"], "BTC-USD")
        with self.assertRaises(ValidationError):
            FeatureValue(instrument_id="BTC-USD", name="total_equity", value=Decimal(100))
        self.cache.as_of = NOW - timedelta(minutes=1)
        with self.assertRaisesRegex(DomainError, "AGENT_CACHE_STALE"):
            self.provider.invoke(
                "get_cached_features", '{"snapshot_id":"snapshot","instruments":["BTC-USD"]}'
            )

    def test_retirement_and_deadline_never_turn_failure_into_zero_weights(self):
        self.current = False
        with self.assertRaisesRegex(DomainError, "AGENT_CONTEXT_RETIRED"):
            self.provider.invoke("submit_target_weights", self.proposal())
        self.current = True
        self.now = NOW + timedelta(seconds=31)
        with self.assertRaisesRegex(DomainError, "AGENT_RUN_EXPIRED"):
            self.provider.invoke("submit_target_weights", self.proposal())
        self.assertEqual(self.sink.proposals, [])

    def test_expired_and_nonfinite_proposals_do_not_reach_sink(self):
        for payload in (
            self.proposal(expires_at=NOW.isoformat()),
            self.proposal(weights=[{"instrument_id": "BTC-USD", "weight": "NaN"}]),
        ):
            with self.assertRaises(DomainError):
                self.provider.invoke("submit_target_weights", payload)
        self.assertEqual(self.sink.proposals, [])

    def test_environment_is_not_provider_configuration(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "secret-canary", "COINBASE_API_KEY": "secret-canary", "MODE": "LIVE"},
        ):
            result = self.provider.invoke("submit_target_weights", self.proposal())
            self.assertNotIn("secret-canary", result)
            self.assertEqual(self.context.environment, Environment.SANDBOX)

    def test_bounded_calls_and_payload(self):
        with self.assertRaisesRegex(DomainError, "AGENT_PAYLOAD_TOO_LARGE"):
            self.provider.invoke("submit_target_weights", "x" * (64 * 1024 + 1))
        provider = ActionProvider(
            context=self.context,
            cache=self.cache,
            sink=self.sink,
            context_is_current=lambda context: True,
            deadline=NOW + timedelta(seconds=30),
            max_calls=1,
            clock=lambda: NOW,
        )
        provider.invoke("get_cached_features", '{"snapshot_id":"snapshot","instruments":["BTC-USD"]}')
        with self.assertRaisesRegex(DomainError, "AGENT_RUN_EXPIRED"):
            provider.invoke("get_cached_features", '{"snapshot_id":"snapshot","instruments":["BTC-USD"]}')


if __name__ == "__main__":
    unittest.main()
