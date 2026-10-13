"""Capacity and reinforcement mechanics on synthetic fixtures; no market/graph performance claim."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import numpy as np
import optuna
import pytest

from coinfly.domain.context import DomainError
from coinfly.research.fee_presets import preset_fees
from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.neural.experimental_plasticity import (
    ExperimentalPlasticity,
    PlasticityConfig,
    capture_kernel,
)
from coinfly.research.optimization import (
    AbsoluteSaturationError,
    ExecutionDiagnostics,
    _phase,
    capacity_penalty,
    objective,
    required_credit_horizon,
    trial_constraints,
    validate_epoch_constraints,
)
from coinfly.research.reward import DecisionCredit, ExecutionCost, RewardWindow
from coinfly.research.risk_reward import RiskAdjustedReward, RiskRewardConfig
from tests.research.test_credit_homeostasis import NOW, credited_engine, synthetic_brain
from tests.research.test_training_architecture import (
    frame,
    outcome,
    synthetic_controller,
    synthetic_resources,
)


@pytest.mark.parametrize("signal,net,stressed,fee,expected", [
    (-1., -.001, -.0015, .002, .25),  # both losses strictly inside booked fee
    (-1., -.002, -.002, .002, 1.),   # exact fee-sized loss is not inside
    (-1., 0., 0., .002, 1.),
    (-1., -.001, -.003, .002, 1.),   # latency stress is larger than fee
    (-1., -.003, -.003, .002, 1.),
    (-1., .001, -.001, .002, 1.),    # cost stress on an otherwise profitable interval
    (-1., -.001, -.001, 0., 1.),     # estimated fees cannot justify damping
    (1., 0., .002, .002, 1.),
    (1., .002, .001, .002, 2.),      # exact positive threshold qualifies
    (1., .002, .000999, .002, 1.),
])
def test_preparation_selects_gain_from_causal_booked_evidence(signal, net, stressed, fee, expected):
    brain = synthetic_brain()
    engine = ExperimentalPlasticity(brain, replace(PlasticityConfig(), positive_gain=2, fee_shock_gain=.25))
    MemoryBrain.prepare_experimental_frame(brain, engine, NOW, (), has_reward=True,
        signal=signal, net_return=net, stressed_return=stressed, booked_fee_return=fee)
    assert engine.reinforcement_gain == expected


@pytest.mark.parametrize("actual_fee,booked", [(None, 0.), (D(".6"), .0006)])
def test_reward_keeps_estimated_and_booked_fee_evidence_separate(actual_fee, booked):
    fees = preset_fees("exchange-standard-2026-09-20", trailing_volume_usd=D(0),
                       observed_at=NOW, expires_at=NOW + timedelta(days=1))
    fill = ExecutionCost(fill_id="synthetic", currency="USD", side="BUY", quantity=D(1),
        reference_price=D(100), fill_price=D(100), decision_at=NOW, reference_at=NOW,
        reference_available_at=NOW, submitted_at=NOW, filled_at=NOW + timedelta(milliseconds=100),
        available_at=NOW + timedelta(milliseconds=100), fees=fees, actual_fee=actual_fee)
    reward = RiskAdjustedReward()
    # Gross basis permits estimated fees; net_execution requires actual booked fees.
    window = RewardWindow(**{**outcome(pnl="1").model_dump(), "basis": "gross_reference", "executions": (fill,)})
    reward(window)
    assert reward.metrics["fee_return"] == .0006
    assert reward.metrics["booked_fee_return"] == booked


def test_backward_brain_clock_fails_before_mutating_gain_credit_or_weights():
    brain, engine = credited_engine()
    before = [array.copy() for array in (brain.memory_u, brain.memory_w, engine.credit, engine.tag)]
    clock, tags = engine.bank._clock_us, len(engine.bank)
    with pytest.raises(ValueError, match="strictly increase"):
        MemoryBrain.prepare_experimental_frame(brain, engine, NOW + timedelta(seconds=9), (), has_reward=True,
            signal=-1., net_return=-.001, stressed_return=-.001, booked_fee_return=.002)
    assert engine.reinforcement_gain == 1. and engine.bank._clock_us == clock and len(engine.bank) == tags
    for old, current in zip(before, (brain.memory_u, brain.memory_w, engine.credit, engine.tag)):
        np.testing.assert_array_equal(old, current)


def test_backward_controller_clock_fails_before_risk_or_neural_progress():
    controller, seen = synthetic_controller(side="BUY")
    controller.run_frame(frame(2))
    steps, count, clock = len(seen), controller.risk_reward.count, controller.plasticity.bank._clock_us
    with pytest.raises(DomainError):
        controller.run_frame(frame(1), outcome())
    assert len(seen) == steps and controller.risk_reward.count == count
    assert controller.plasticity.bank._clock_us == clock


def test_hold_cadence_does_not_consume_order_credit_capacity():
    controller, _ = synthetic_controller()
    controller.plasticity = ExperimentalPlasticity(controller.brain, replace(PlasticityConfig(), maximum_tags=1))
    for index in range(300):
        result = controller.run_frame(frame(index / 1000))
        assert result["side"] == "HOLD" and result["execution_admission"]["allowed"]
    assert len(controller.plasticity.bank) == 0
    controller.decoder.decode = lambda *_: {"side": "BUY"}
    assert controller.run_frame(frame(1))["training"]["eligibility"]["tag_retained"] == 1
    controller.decoder.decode = lambda *_: {"side": "HOLD"}
    result = controller.run_frame(frame(2))
    assert result["execution_admission"] == {"allowed": True, "reason": None}
    assert len(controller.plasticity.bank) == 1


def test_reject_failure_is_propagated_without_execute_retry_or_finish():
    controller, _ = synthetic_controller(side="SELL")
    controller.plasticity = ExperimentalPlasticity(controller.brain, replace(PlasticityConfig(), maximum_tags=1))
    events = []
    class Environment:
        execution_realm = "paper"
        def stream(self):
            yield frame(), None
            yield frame(1), outcome(pnl="0")
        def execute(self, side, at):
            events.append(("execute", side))
        def reject(self, side, at, reason):
            events.append((reason, side))
            raise RuntimeError("synthetic rejection journal failed")
        def finish(self):
            raise AssertionError("must stop on failed rejection bookkeeping")
    with pytest.raises(RuntimeError, match="rejection journal failed"):
        _phase(controller, Environment(), start=NOW, end=NOW + timedelta(seconds=2), partition="train", max_frames=2)
    assert events == [("execute", "SELL"), ("eligibility_capacity", "SELL")]


def test_diagnostics_use_order_attempts_and_count_suppressed_reward_pulses():
    audit = ExecutionDiagnostics()
    for _ in range(1000):
        audit.record_order("HOLD", True, None)
    for _ in range(8):
        audit.record_order("BUY", True, None)
    audit.record_order("SELL", False, "eligibility_capacity")
    audit.record_order("BUY", False, "turnover")
    assert audit.report()["capacity_order_rejection_rate"] == .1
    for index in range(100):
        audit.record_reward(missing=index == 0, maximum_delay=1., horizon=1., requested=True)
    audit.record_reward(missing=True, maximum_delay=1., horizon=1., requested=False)
    assert audit.report()["missing_credit_fraction"] == .01
    assert not audit.report()["missing_credit_alert"]
    audit.record_reward(missing=True, maximum_delay=1.001, horizon=1., requested=True)
    assert audit.report()["missing_credit_alert"] and audit.credit_horizon_violations == 1


def test_phase_reports_expired_pulse_and_actual_credit_horizon_violation():
    controller, seen = synthetic_controller(side="BUY")
    controller.plasticity = ExperimentalPlasticity(controller.brain, replace(PlasticityConfig(),
        credit_tau_seconds=.1, maximum_credit_age_seconds=.5))
    class Environment:
        execution_realm = "paper"
        def stream(self):
            yield frame(), None
            yield frame(1.1), outcome(pnl="-1", credits=(DecisionCredit(decision_at=NOW, fraction=D(1)),))
        def execute(self, side, at):
            pass
        def reject(self, side, at, reason):
            raise AssertionError("expired tag must have been purged before admission")
        def finish(self):
            return outcome(1, pnl="0", capital="999")
    phase = _phase(controller, Environment(), start=NOW, end=NOW + timedelta(seconds=2), partition="train", max_frames=2)
    report = phase.execution.report()
    assert report["reward_pulses_requested"] == report["missing_credit_pulses"] == 1
    assert report["missing_credit_alert"] and report["credit_horizon_violations"] == 1
    assert report["maximum_observed_credit_delay_seconds"] == 1.1
    assert phase.unassigned_credit_windows == 1
    assert all(kwargs["stimulation"] is None for _, kwargs in seen)  # suppressed pulse still counted


def test_capacity_penalty_is_smooth_bounded_and_never_improves_negative_sortino():
    assert capacity_penalty(.1) == capacity_penalty(0) == 0
    assert capacity_penalty(1) == 1
    assert 0 < capacity_penalty(.100001) < 2e-12
    assert -.5 - capacity_penalty(.5) < -.5
    for rate in (-.1, 1.1, float("nan")):
        with pytest.raises(ValueError):
            capacity_penalty(rate)


@pytest.mark.parametrize("horizon,interval,required", [(120., 60., 120.), (1., 120., 120.), (150., 150., 150.)])
def test_objective_aligns_horizon_and_applies_capacity_fitness_without_falsifying_pnl(monkeypatch, horizon, interval, required):
    profiles = []
    def evaluation(resources, fold, profile, reward):
        profiles.append(profile)
        diagnostics = ExecutionDiagnostics(order_attempts=10, capacity_order_rejections=5).report()
        return {"sortino": -.5, "maximum_drawdown": .2, "net_return": -.01,
                "training_saturation_mean": .001, "training_saturation_peak": .005,
                "homeostatic_erasure_rate": .002, "absolute_saturation_time_fraction": .15,
                "turnover_violations": 0, "unassigned_credit_windows": 0, "execution_diagnostics": diagnostics}
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluation)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"],
                               sampler=optuna.samplers.TPESampler(seed=4))
    study.enqueue_trial({"credit_tau_seconds": 150., "maximum_credit_age_seconds": 150.})
    study.optimize(lambda trial: objective(trial, synthetic_resources(horizon), ("synthetic",), (1,),
                                          RiskRewardConfig(interval_seconds=interval)), n_trials=2)
    for trial, profile in zip(study.trials, profiles):
        assert profile.reward_credit_horizon_seconds == required
        assert profile.plasticity.maximum_credit_age_seconds >= max(required, profile.plasticity.credit_tau_seconds)
        assert trial.values == [pytest.approx(-.5 - capacity_penalty(.5)), .2, .001, .002]
        assert trial.user_attrs["fitness"]["raw_net_sortino"] == -.5
        assert all(value <= 0 for value in trial_constraints(trial))


@pytest.mark.parametrize("horizon", [None, 0., -1., True, float("nan"), 150.001])
def test_host_must_declare_a_credit_horizon_that_fits_hardware(horizon):
    with pytest.raises(ValueError):
        required_credit_horizon(synthetic_resources(horizon), RiskRewardConfig())


def test_absolute_saturation_tolerance_and_epoch_time_are_separate_from_erasure():
    for value, absolute in ((1 - 5e-13, 1.), (1 - 2e-12, 0.), (-.9 + 5e-13, 1.), (-.9 + 2e-12, 0.)):
        result = capture_kernel(np.array([value]), np.array([value]), np.zeros(1), np.zeros(1),
                                .01, .001, .001, 0., .95, .01, .02)
        assert result[4] == absolute and result[5] > 0
    validate_epoch_constraints({"absolute_saturation_time_fraction": .15})
    with pytest.raises(AbsoluteSaturationError):
        validate_epoch_constraints({"absolute_saturation_time_fraction": .150000000001})
    for fraction in (-1, float("nan"), 1.1):
        with pytest.raises(ValueError):
            validate_epoch_constraints({"absolute_saturation_time_fraction": fraction})
