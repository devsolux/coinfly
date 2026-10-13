"""Synthetic native dispatch and execution contracts; no connectome qualification."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from decimal import Inexact, localcontext

import numpy as np
import optuna
import pytest
from pydantic import ValidationError

from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.neural.experimental_plasticity import (
    ExperimentalPlasticity,
    PlasticityConfig,
    reinforcement_gain_kernel,
)
from coinfly.research.optimization import (
    PLASTICITY_CUMULATIVE_KEYS,
    AbsoluteSaturationError,
    ExecutionDiagnostics,
    PlasticityEpochAudit,
    _phase,
    objective,
)
from coinfly.research.reward import ExecutionCost
from coinfly.research.risk_reward import RiskAdjustedReward, RiskRewardConfig
from tests.research.test_credit_homeostasis import NOW, credited_engine, pulse, synthetic_brain
from tests.research.test_temporal_calibration import quote
from tests.research.test_training_architecture import (
    frame,
    outcome,
    synthetic_controller,
    synthetic_resources,
)


def test_native_gain_preserves_open_fee_boundaries_at_adjacent_float64_values():
    fee = .002
    inside_fee = float(np.nextafter(-fee, 0.))
    outside_fee = float(np.nextafter(-fee, -1.))
    for loss, expected in ((-fee, 1.), (inside_fee, .25), (outside_fee, 1.), (0., 1.)):
        actual = reinforcement_gain_kernel(-1., loss, loss, fee, .001, 2., .25)
        assert actual == expected
    assert reinforcement_gain_kernel.nopython_signatures
    assert reinforcement_gain_kernel.targetoptions["fastmath"] is False
    assert reinforcement_gain_kernel.targetoptions["nogil"] is True


@pytest.mark.parametrize("field", ["signal", "net_return", "stressed_return", "booked_fee_return"])
def test_nonfinite_gain_evidence_is_rejected_without_changing_previous_gain(field):
    engine = ExperimentalPlasticity(synthetic_brain(), replace(PlasticityConfig(), positive_gain=2))
    values = dict(signal=1., net_return=.01, stressed_return=.01, booked_fee_return=.001)
    engine.set_reinforcement(**values)
    values[field] = float("nan")
    with pytest.raises(ValueError):
        engine.set_reinforcement(**values)
    assert engine.reinforcement_gain == 2


def test_compiled_fee_gain_reaches_native_eta_without_changing_reward_sign():
    first_brain, first = credited_engine()
    second_brain, second = credited_engine()
    second.config = replace(second.config, fee_shock_gain=.25)
    second.set_reinforcement(-1., -.001, .002, net_return=-.001)
    pulse(first, first_brain, [0, 0, 1], stimulated=True)
    pulse(second, second_brain, [0, 0, 1], stimulated=True)
    assert second_brain.memory_u[0] == pytest.approx(first_brain.memory_u[0] * .25)
    assert first_brain.memory_u[0] < 0 and second_brain.memory_u[0] < 0
    assert second_brain.plasticity_diagnostics["effective_eta"] == .00025


def test_booked_fee_normalization_ignores_hostile_decimal_context_and_rejects_estimates():
    fees = quote().fees
    executions = tuple(ExecutionCost(fill_id=f"synthetic-{i}", currency="USD", side="BUY", quantity=D(".01"),
        reference_price=D(100), fill_price=D(100), decision_at=NOW, reference_at=NOW,
        reference_available_at=NOW, submitted_at=NOW, filled_at=NOW + timedelta(milliseconds=100),
        available_at=NOW + timedelta(milliseconds=100), fees=fees, actual_fee=D(".006123456789"))
        for i in range(2))
    window = outcome(pnl="-.01", executions=executions)
    evaluator = RiskAdjustedReward()
    with localcontext() as hostile:
        hostile.prec = 2
        hostile.traps[Inexact] = True
        evaluator(window)
    assert evaluator.metrics["booked_fee_return"] == float(D(".000012246913578"))
    estimated = ExecutionCost(**{**executions[0].model_dump(), "actual_fee": None})
    with pytest.raises(ValidationError, match="requires booked fees"):
        outcome(executions=(estimated,))


def test_brain_finalizes_hold_without_storing_and_rejects_order_on_full_bank(monkeypatch):
    brain = synthetic_brain()
    engine = ExperimentalPlasticity(brain, replace(PlasticityConfig(), maximum_tags=1))
    store = engine.bank.store
    stored = []
    def recording_store(now, tag):
        stored.append(now)
        store(now, tag)
    monkeypatch.setattr(engine.bank, "store", recording_store)
    for index, side, allowed in ((0, "HOLD", True), (1, "BUY", True), (2, "HOLD", True), (3, "SELL", False)):
        now = NOW + timedelta(seconds=index)
        MemoryBrain.prepare_experimental_frame(brain, engine, now, (), has_reward=False)
        assert stored == ([] if index < 2 else [NOW + timedelta(seconds=1)])
        admission = MemoryBrain.finalize_experimental_frame(brain, engine, now, side, learning=True)
        assert admission == {"allowed": allowed, "reason": None if allowed else "eligibility_capacity"}
        with pytest.raises(ValueError, match="unfinalized"):
            MemoryBrain.finalize_experimental_frame(brain, engine, now, side, learning=True)
    assert stored == [NOW + timedelta(seconds=1)] and len(engine.bank) == 1


def test_wrong_clock_or_side_cannot_finalize_or_store_a_tag():
    brain = synthetic_brain()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    MemoryBrain.prepare_experimental_frame(brain, engine, NOW, (), has_reward=False)
    for now, side in ((NOW - timedelta(seconds=1), "BUY"), (NOW, "REPLACE")):
        with pytest.raises(ValueError):
            MemoryBrain.finalize_experimental_frame(brain, engine, now, side, learning=True)
        assert len(engine.bank) == 0
    assert MemoryBrain.finalize_experimental_frame(brain, engine, NOW, "BUY", learning=True)["allowed"]


def diagnostics(seconds=1.):
    return {"bins": round(seconds * 100), "observed_neural_seconds": seconds,
            "near_saturation_peak": 1., "near_saturation_seconds": seconds * .5,
            "absolute_saturation_seconds": seconds * .1, "absolute_saturation_edge_seconds": seconds * .05,
            "severe_saturation_seconds": seconds * .1, "homeostatic_erasure_integral": seconds * .01,
            "maximum_bin_homeostatic_loss": .0001}


@pytest.mark.parametrize("key", PLASTICITY_CUMULATIVE_KEYS)
def test_frame_audit_rejects_cumulative_counter_reset_even_with_forward_clock(key):
    audit = PlasticityEpochAudit()
    audit.observe(diagnostics())
    previous = dict(audit.previous)
    changed = diagnostics(2.)
    changed[key] = 0
    with pytest.raises(ValueError):
        audit.observe(changed)
    assert audit.previous == previous


def test_frame_audit_rejects_occupancy_and_erasure_jumps_hidden_by_epoch_totals():
    for key, value in (("absolute_saturation_seconds", .3), ("homeostatic_erasure_integral", .02)):
        audit = PlasticityEpochAudit()
        audit.observe(diagnostics())
        changed = diagnostics(1.01)
        changed[key] = value
        with pytest.raises(ValueError, match="frame"):
            audit.observe(changed)


def test_saturation_is_checked_at_epoch_end_and_a_recovering_prefix_can_pass():
    audit = PlasticityEpochAudit()
    first = diagnostics(.01) | {"absolute_saturation_seconds": .01}
    audit.observe(first)
    with pytest.raises(AbsoluteSaturationError):
        audit.finish()
    later = diagnostics(.1) | {"absolute_saturation_seconds": .01}
    audit.observe(later)
    assert audit.finish()["absolute_saturation_time_fraction"] == pytest.approx(.1)


def test_completed_saturated_phase_prunes_before_an_evaluator_opens_validation(monkeypatch):
    opened = []
    def evaluate(*args, **kwargs):
        controller, _ = synthetic_controller()
        controller.brain.memory_u.fill(1.)
        controller.brain.memory_w.fill(1.)
        class Environment:
            execution_realm = "paper"
            def stream(self):
                yield frame(), None
            def execute(self, side, at):
                assert side == "HOLD"
            def reject(self, *args):
                raise AssertionError("HOLD cannot hit capacity backpressure")
            def finish(self):
                return outcome(pnl="0")
        _phase(controller, Environment(), start=NOW, end=NOW + timedelta(seconds=1), partition="train", max_frames=1)
        opened.append("validation")
        raise AssertionError("saturated training must not open validation")
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluate)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"])
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("synthetic",), (1,), RiskRewardConfig()), n_trials=1)
    trial = study.trials[0]
    assert trial.state == optuna.trial.TrialState.PRUNED and trial.values is None and opened == []
    assert trial.user_attrs["absolute_saturation_audit"]["absolute_saturation_time_fraction"] > .15


@pytest.mark.parametrize("requested,missing,expected", [(0, 0, False), (100, 0, False), (100, 1, False),
                                                       (100, 2, True), (99, 1, True), (10000, 100, False),
                                                       (10000, 101, True)])
def test_missing_credit_alert_is_strictly_above_one_percent(requested, missing, expected):
    audit = ExecutionDiagnostics()
    for index in range(requested):
        audit.record_reward(requested=True, missing=index < missing, maximum_delay=1., horizon=1.)
    # No pulse requested: it must neither create an alert nor dilute the ratio.
    audit.record_reward(requested=False, missing=True, maximum_delay=1., horizon=1.)
    report = audit.report()
    assert report["missing_credit_alert"] is expected
    assert report["reward_pulses_requested"] == requested and report["missing_credit_pulses"] == missing
