"""RESEARCH_LEARNING on the worker core: session fencing, reward delivery, credit expiry.

Evidence level: scripted pipeline on a synthetic full-graph-shaped controller
(``tests/strategy/synthetic_research.py``). It proves the worker's mechanics
around the unchanged research controller, not neural behaviour.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import TestCase

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.research.reward import DecisionCredit
from coinfly.research.training import TrainingProfile
from coinfly.strategy.definition import TrainingSettings
from coinfly.strategy.research_controller import ResearchLearningController, restore_session, snapshot_session
from tests.strategy.synthetic_research import NOW, reward_window, sensory_frame, synthetic_training_controller

SETTINGS = TrainingSettings(profile_id="malecns-risk-adjusted-experiment-v2", implementation_revision=4, reward_interval_seconds=1.0)


def research_controller(sides: list[str] | None = None, **profile: object) -> tuple[ResearchLearningController, list[object]]:
    training, seen = synthetic_training_controller(sides=sides, profile=TrainingProfile(seed=1, **profile))  # type: ignore[arg-type]
    return ResearchLearningController(training, settings=SETTINGS), seen


class SessionFencingTests(TestCase):
    def test_discard_restores_session_and_brain_so_learning_never_advances(self) -> None:
        controller, seen = research_controller(["BUY"])
        controller.admit_slot("BTC-USD")
        first = controller.observe(sensory_frame(NOW), reinforcement="none", decision_id="d1", slot_key="BTC-USD")
        self.assertEqual(first.side, "BUY")
        controller.commit("BTC-USD")
        training = controller._controller
        before = snapshot_session(training)
        committed_v = training.brain.v.copy()
        generation = controller.provenance("BTC-USD")["state_generation"]
        # A second observation advances everything; discarding it must undo all of it.
        controller.observe(sensory_frame(NOW + timedelta(seconds=1)), reinforcement="none", decision_id="d2", slot_key="BTC-USD")
        self.assertNotEqual(training._frames, before.controller_scalars["_frames"])
        controller.discard("BTC-USD")
        after = snapshot_session(training)
        self.assertEqual(after.controller_scalars["_frames"], before.controller_scalars["_frames"])
        self.assertEqual(after.bank_scalars, before.bank_scalars)
        self.assertEqual(after.session_scalars, before.session_scalars)
        self.assertEqual(after.risk_scalars, before.risk_scalars)
        np.testing.assert_array_equal(after.bank_values, before.bank_values)
        np.testing.assert_array_equal(training.brain.v, committed_v)
        self.assertEqual(controller.provenance("BTC-USD")["state_generation"], generation)
        self.assertEqual(controller.discarded, 1)
        # The same decision instant can be observed again (the causal cursor was restored).
        again = controller.observe(sensory_frame(NOW + timedelta(seconds=1)), reinforcement="none", decision_id="d2b", slot_key="BTC-USD")
        self.assertEqual(again.decision_id, "d2b")
        controller.commit("BTC-USD")

    def test_single_slot_session(self) -> None:
        controller, _ = research_controller()
        controller.admit_slot("BTC-USD")
        with self.assertRaises(DomainError) as refused:
            controller.admit_slot("ETH-USD")
        self.assertEqual(refused.exception.code, "RESEARCH_SINGLE_SLOT")

    def test_restore_roundtrip_is_exact(self) -> None:
        controller, _ = research_controller(["BUY", "SELL"])
        controller.admit_slot("BTC-USD")
        training = controller._controller
        controller.observe(sensory_frame(NOW), reinforcement="none", decision_id="d1", slot_key="BTC-USD")
        controller.commit("BTC-USD")
        snapshot = snapshot_session(training)
        controller.observe(sensory_frame(NOW + timedelta(seconds=1)), reinforcement="none", decision_id="d2", slot_key="BTC-USD")
        restore_session(training, snapshot)
        replay = snapshot_session(training)
        for name in ("bank_scalars", "plasticity_scalars", "risk_scalars", "session_scalars", "controller_scalars"):
            self.assertEqual(getattr(replay, name), getattr(snapshot, name), name)
        for name in snapshot.plasticity_arrays:
            np.testing.assert_array_equal(replay.plasticity_arrays[name], snapshot.plasticity_arrays[name])


class RewardDeliveryTests(TestCase):
    def test_reward_windows_feed_the_declared_plasticity_and_credit_the_originating_decision(self) -> None:
        controller, seen = research_controller(["BUY", "HOLD"])
        controller.admit_slot("BTC-USD")
        first = controller.observe(sensory_frame(NOW), reinforcement="none", decision_id="d1", slot_key="BTC-USD")
        controller.commit("BTC-USD")
        self.assertEqual(first.side, "BUY")
        self.assertTrue(first.admission_allowed)
        self.assertEqual(first.provenance["training"]["eligibility"]["tag_retained"], 1)
        # The next observation delivers the completed window crediting decision d1 (0.9 s old: inside the credit age).
        window = reward_window(NOW, pnl="1", credits=(DecisionCredit(decision_at=NOW, fraction=Decimal(1)),))
        second = controller.observe(sensory_frame(NOW + timedelta(seconds=1)), reinforcement="none", decision_id="d2",
                                    slot_key="BTC-USD", outcome=window)
        controller.commit("BTC-USD")
        self.assertEqual(second.reinforcement, "reward")
        eligibility = second.provenance["training"]["eligibility"]
        self.assertEqual((eligibility["credit_allocations"], eligibility["missing_credit_decisions"], eligibility["expired_credit_decisions"]),
                         (1, 0, 0))
        self.assertEqual(second.provenance["reward"]["stimulus"], "reward")
        self.assertEqual(second.provenance["reward_window"]["credits"], 1)
        self.assertTrue(any(options["stimulation"] is not None for _, options in seen))

    def test_credit_older_than_the_credit_age_is_reported_expired_not_dropped(self) -> None:
        # Inventory held longer than maximum_credit_age_seconds (150 s): the
        # reward still names the originating decision; the plasticity engine
        # reports the expired credit instead of anyone re-timestamping it.
        controller, _ = research_controller(["BUY", "HOLD"])
        controller.admit_slot("BTC-USD")
        controller.observe(sensory_frame(NOW), reinforcement="none", decision_id="d1", slot_key="BTC-USD")
        controller.commit("BTC-USD")
        late = NOW + timedelta(seconds=200)
        window = reward_window(late - timedelta(seconds=1), pnl="-1", credits=(DecisionCredit(decision_at=NOW, fraction=Decimal(1)),))
        decision = controller.observe(sensory_frame(late), reinforcement="none", decision_id="d2", slot_key="BTC-USD", outcome=window)
        controller.commit("BTC-USD")
        eligibility = decision.provenance["training"]["eligibility"]
        self.assertEqual((eligibility["expired_credit_decisions"], eligibility["missing_credit_decisions"], eligibility["retained_credit_fraction"]),
                         (1, 1, 0.0))
        self.assertEqual(eligibility["maximum_credit_delay_seconds"], 200.0)
        self.assertEqual(decision.provenance["reward"]["stimulus"], "aversive")
        self.assertEqual(decision.provenance["session"]["expired_tags"], 1)

    def test_reward_windows_must_stay_contiguous(self) -> None:
        controller, _ = research_controller(["HOLD"])
        controller.admit_slot("BTC-USD")
        controller.observe(sensory_frame(NOW), reinforcement="none", decision_id="d1", slot_key="BTC-USD")
        controller.commit("BTC-USD")
        controller.observe(sensory_frame(NOW + timedelta(seconds=1)), reinforcement="none", decision_id="d2", slot_key="BTC-USD",
                           outcome=reward_window(NOW))
        controller.commit("BTC-USD")
        with self.assertRaises(ValueError):  # a skipped window is refused by the research risk state, never patched over
            controller.observe(sensory_frame(NOW + timedelta(seconds=3)), reinforcement="none", decision_id="d3", slot_key="BTC-USD",
                               outcome=reward_window(NOW + timedelta(seconds=2)))
        controller.discard("BTC-USD")
        # The session restored: the correct next window is still accepted.
        controller.observe(sensory_frame(NOW + timedelta(seconds=3)), reinforcement="none", decision_id="d3b", slot_key="BTC-USD",
                           outcome=reward_window(NOW + timedelta(seconds=1)))
        controller.commit("BTC-USD")

    def test_inference_modes_refuse_rewards(self) -> None:
        from types import SimpleNamespace

        from coinfly.domain.context import Environment
        from coinfly.strategy.controllers import FixtureSequenceController

        fixture = FixtureSequenceController(["HOLD"], environment=Environment.SANDBOX)
        with self.assertRaises(DomainError) as refused:
            fixture.observe(sensory_frame(NOW), reinforcement="none", decision_id="d", slot_key="BTC-USD", outcome=SimpleNamespace())
        self.assertEqual(refused.exception.code, "REWARD_NOT_APPLICABLE")
