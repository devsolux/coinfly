from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.execution.models import IntentKind, Side
from coinfly.execution.weight_planner import InstrumentContract, plan_weights
from coinfly.risk.admission_actor import ActiveBookSnapshot, AvailableAsset, ExecutionMark
from coinfly.strategy.contracts import InstrumentWeight, TargetWeights
from coinfly.strategy.target_coordinator import BoundTarget
from tests.execution.helpers import context


class WeightPlannerTests(TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.book = ActiveBookSnapshot(
            context=context(),
            scope_id="book",
            evidence_id="account-evidence",
            observed_at=self.now,
            expires_at=self.now + timedelta(seconds=5),
            fact_sequence=0,
            reporting_currency="USD",
            funded_equity=Decimal(1000),
            eligible_collateral=Decimal(1000),
            available_assets=(AvailableAsset(asset="USD", amount=Decimal(1000)),),
            marks=(ExecutionMark(instrument="BTC-USD", price=Decimal("100.123")),),
            complete_execution_scope=True,
            authoritative_account_evidence=True,
            conflicting_external_activity=False,
        )
        self.contracts = (
            InstrumentContract(
                instrument="BTC-USD",
                quantity_increment=Decimal("0.01"),
                price_increment=Decimal("0.01"),
                minimum_quantity=Decimal("0.01"),
                minimum_notional=Decimal(1),
            ),
        )

    def target(self, weight: str) -> BoundTarget:
        proposal = TargetWeights(affected_instruments=("BTC-USD",),
            decision_id="decision",
            model_id="model",
            model_version="1",
            snapshot_id="market",
            allocation_revision=1,
            expires_at=self.now + timedelta(seconds=5),
            weights=(InstrumentWeight(instrument_id="BTC-USD", weight=Decimal(weight)),),
        )
        return BoundTarget(context=context(), proposal=proposal, accepted_at=self.now)

    def test_uses_active_funded_equity_and_subtracts_actual_and_pending(self) -> None:
        plans = plan_weights(
            self.target("0.5"),
            self.book,
            self.contracts,
            {"BTC-USD": Decimal(1)},
            {"BTC-USD": Decimal(2)},
            now=self.now,
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].quantity, Decimal("1.99"))
        self.assertEqual(plans[0].limit_price, Decimal("100.12"))
        self.assertEqual(plans[0].side, Side.BUY)

    def test_explicit_zero_closes_but_retains_untradeable_dust(self) -> None:
        plans = plan_weights(
            self.target("0"), self.book, self.contracts, {"BTC-USD": Decimal("0.015")}, {}, now=self.now
        )
        self.assertEqual(plans[0].kind, IntentKind.EXIT)
        self.assertEqual(plans[0].side, Side.SELL)
        self.assertEqual(plans[0].quantity, Decimal("0.01"))
        self.assertEqual(plans[0].limit_price, Decimal("100.13"))

    def perpetual(self) -> tuple[ContextKey, ActiveBookSnapshot]:
        derivatives = ContextKey(authority_id="owner", environment=Environment.SANDBOX, product_track=ProductTrack.PERPETUAL,
                                 broker_profile_id="paper-local", epoch=1)
        return derivatives, self.book.model_copy(update={"context": derivatives})

    def signed_target(self, weight: str, derivatives: ContextKey) -> BoundTarget:
        return self.target(weight).model_copy(update={"context": derivatives})

    def test_a_reversal_never_crosses_zero_in_one_proposal(self) -> None:
        derivatives, book = self.perpetual()
        # Long 1, target short: only the closing leg, labeled EXIT; the short waits.
        plans = plan_weights(self.signed_target("-0.5", derivatives), book, self.contracts, {"BTC-USD": Decimal(1)}, {}, now=self.now)
        self.assertEqual([(p.side, p.kind, p.quantity) for p in plans], [(Side.SELL, IntentKind.EXIT, Decimal(1))])
        # The closing leg is resting (pending -1): nothing more until it is reconciled.
        plans = plan_weights(self.signed_target("-0.5", derivatives), book, self.contracts, {"BTC-USD": Decimal(1)}, {"BTC-USD": Decimal(-1)}, now=self.now)
        self.assertEqual(plans, ())
        # Reconciled flat: the opening leg is an ENTRY against the fresh snapshot.
        plans = plan_weights(self.signed_target("-0.5", derivatives), book, self.contracts, {}, {}, now=self.now)
        self.assertEqual([(p.side, p.kind, p.quantity) for p in plans], [(Side.SELL, IntentKind.ENTRY, Decimal("4.99"))])
        # Short 2 with a resting cover (+2) and a long target: still nothing until the cover reconciles.
        plans = plan_weights(self.signed_target("0.5", derivatives), book, self.contracts, {"BTC-USD": Decimal(-2)}, {"BTC-USD": Decimal(2)}, now=self.now)
        self.assertEqual(plans, ())

    def test_an_unfilled_entry_is_not_reduced_by_an_order_against_it(self) -> None:
        # Nothing held; a BUY still resting; target flat → no order (the resting BUY runs out under its TTL).
        plans = plan_weights(self.target("0"), self.book, self.contracts, {}, {"BTC-USD": Decimal(2)}, now=self.now)
        self.assertEqual(plans, ())
        # Long 1 with a resting BUY 1 and target flat → close only what is held.
        plans = plan_weights(self.target("0"), self.book, self.contracts, {"BTC-USD": Decimal(1)}, {"BTC-USD": Decimal(1)}, now=self.now)
        self.assertEqual([(p.side, p.kind, p.quantity) for p in plans], [(Side.SELL, IntentKind.EXIT, Decimal(1))])

    def test_proposal_cannot_select_a_different_context(self) -> None:
        proposal = self.target("0.5").model_copy(update={"context": context(8)})
        with self.assertRaises(DomainError):
            plan_weights(proposal, self.book, self.contracts, {}, {}, now=self.now)

    def test_observation_subset_cannot_authorize_planning(self) -> None:
        book = self.book.model_copy(update={"complete_execution_scope": False})
        with self.assertRaises(DomainError):
            plan_weights(self.target("0.5"), book, self.contracts, {}, {}, now=self.now)
