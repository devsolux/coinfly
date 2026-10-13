"""Synthetic synapses and ledgers test hypotheses' mechanics, never profitability."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, localcontext
from types import SimpleNamespace

import numpy as np
import optuna
import pytest
from pydantic import ValidationError

from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.neural.experimental_plasticity import (
    EligibilityBank,
    EligibilityCapacityError,
    ExperimentalPlasticity,
    PlasticityConfig,
    capture_kernel,
    tag_kernel,
)
from coinfly.research.optimization import (
    ExecutionDiagnostics,
    assert_development_unsealed,
    claim_holdout,
    objective,
    trial_constraints,
)
from coinfly.research.reward import DecisionCredit, RewardWindow
from coinfly.research.risk_reward import RiskRewardConfig, risk_signal
from coinfly.research.turnover import turnover_admissible
from coinfly.research.vectorization import TickScale, normalize_ticks, vectorize
from tests.research.test_training_architecture import synthetic_resources

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def synthetic_brain():
    return SimpleNamespace(circuit={"edges": np.array([0]), "pre": np.array([0]), "dan": np.array([2]),
                                    "gain": np.ones((1, 1))}, post=np.array([1]), eta=.001,
                           memory_u=np.zeros(1), memory_w=np.zeros(1), dan_baseline_hz=np.zeros(1),
                           weight=np.ones(1), baseline_plastic=np.ones(1),
                           weights_frozen=False, plasticity_diagnostics={})


def pulse(engine, brain, counts, *, stimulated=False, learning=True):
    engine.advance(brain, np.asarray(counts), .01, learning=learning,
                   stimulation=(np.array([2]), 20) if stimulated else None, delta_limit=.01)


def credited_engine(delay=10):
    brain = synthetic_brain()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    engine.begin_frame(NOW, (), has_reward=False)
    pulse(engine, brain, [1, 0, 0])
    pulse(engine, brain, [0, 1, 0])
    assert engine.tag[0] < 0
    engine.bank.store(NOW, engine.tag)
    engine.begin_frame(NOW + timedelta(seconds=delay),
                       (DecisionCredit(decision_at=NOW, fraction=D(1)),), has_reward=True)
    return brain, engine


def test_delayed_dopamine_captures_past_tag_with_current_spikes_absent():
    brain, engine = credited_engine()
    assert engine.tag[0] == 0 and engine.credit[0] < 0
    assert engine.credit_metrics["credit_decay_mass"] == pytest.approx(np.exp(-10 / 30))
    pulse(engine, brain, [0, 0, 1], stimulated=True)
    assert brain.memory_u[0] < 0 and brain.memory_w[0] < 0
    assert brain.plasticity_diagnostics["capture_active_bins"] == 1


def test_no_capture_without_observed_dopamine_or_without_delivered_pulse():
    for counts, stimulated in (([0, 0, 0], True), ([0, 0, 1], False)):
        brain, engine = credited_engine()
        pulse(engine, brain, counts, stimulated=stimulated)
        assert brain.memory_u[0] == brain.memory_w[0] == 0


def test_expired_credit_is_not_reassigned_to_new_neural_activity():
    brain, engine = credited_engine(delay=151)
    assert not engine.credit.any()
    assert engine.credit_metrics["missing_credit_decisions"] == 1
    pulse(engine, brain, [1, 1, 1], stimulated=True)
    assert brain.memory_u[0] == brain.memory_w[0] == 0


def test_anti_hebbian_tags_depend_on_order_and_cancel_simultaneous_activity():
    def tag(order):
        pre, post, tag = np.zeros(1), np.zeros(1), np.zeros(1)
        for p, q in order:
            tag_kernel(pre, post, tag, np.array([p]), np.array([q]), .01, 1., 100.)
        return tag[0]
    assert tag([(100., 0.), (0., 100.)]) < 0
    assert tag([(0., 100.), (100., 0.)]) > 0
    assert tag([(100., 100.)] * 10) == 0


def test_native_homeostasis_recovers_saturation_and_preserves_efficacy_bounds():
    u, w, zeros = np.ones(3), np.ones(3), np.zeros(3)
    disabled_u, disabled_w = u.copy(), w.copy()
    for _ in range(6000):
        capture_kernel(u, w, zeros, zeros, .01, .001, .01, .5, .95, .01)
        capture_kernel(disabled_u, disabled_w, zeros, zeros, .01, .001, 0., 0., .95, .01)
    assert np.all(u < .6) and np.all(w < .6)
    assert np.all(u >= -.9) and np.all(w >= -.9)
    np.testing.assert_array_equal(disabled_u, np.ones(3))
    np.testing.assert_array_equal(disabled_w, np.ones(3))
    assert capture_kernel.nopython_signatures


def test_persistent_capture_stays_inside_bounds_with_homeostatic_headroom():
    u, w = np.zeros(2), np.zeros(2)
    credits, dan = np.array([1., -1.]), np.full(2, 100.)
    for _ in range(10000):
        before = np.stack([u, w])
        capture_kernel(u, w, credits, dan, .01, .001, .05, .5, .95, .001)
        assert np.max(np.abs(np.stack([u, w]) - before)) <= .001 + 1e-15
    assert 0 < u[0] < .9 and -.8 < u[1] < 0
    assert np.all(1 + w > 0)


def test_frozen_evaluation_changes_neither_tags_nor_efficacies():
    brain, engine = credited_engine()
    brain.weights_frozen = True
    before = [a.copy() for a in (brain.memory_u, brain.memory_w, engine.tag, engine.pre_trace, engine.post_trace)]
    pulse(engine, brain, [1, 1, 1], stimulated=True)
    for old, value in zip(before, (brain.memory_u, brain.memory_w, engine.tag, engine.pre_trace, engine.post_trace)):
        np.testing.assert_array_equal(old, value)


def test_invalid_state_is_rejected_before_tag_or_weight_mutation():
    brain, engine = credited_engine()
    brain.memory_u[0] = np.nan
    before = engine.pre_trace.copy()
    with pytest.raises(ValueError, match="invalid experimental"):
        pulse(engine, brain, [1, 1, 1], stimulated=True)
    np.testing.assert_array_equal(before, engine.pre_trace)


def test_bounded_tag_bank_owns_snapshots_and_never_silently_evicts_live_tags():
    bank = EligibilityBank(2, replace(PlasticityConfig(), maximum_tags=1, maximum_tag_bytes=16))
    values = np.array([.2, -.2])
    bank.reserve(NOW)
    bank.store(NOW, values)
    values.fill(0)
    assert bank.tags[NOW][0] == .2 and not bank.tags[NOW].flags.writeable
    assert not bank.reserve(NOW + timedelta(seconds=1))
    bank.reserve(NOW + timedelta(seconds=151))
    assert not bank.tags
    with pytest.raises(ValueError, match="one graph tag"):
        EligibilityBank(3, replace(PlasticityConfig(), maximum_tag_bytes=16))


def test_credit_fractions_are_exact_bounded_and_causal():
    def window(credits):
        return RewardWindow(start_at=NOW, end_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=2),
                            pnl=D(0), capital=D(100), currency="USD", basis="net_execution", credits=credits)
    one = DecisionCredit(decision_at=NOW, fraction=D("0.500000000000000001"))
    two = DecisionCredit(decision_at=NOW - timedelta(seconds=1), fraction=D("0.5"))
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        with pytest.raises(ValidationError, match="exceed one"):
            window((one, two))
    with pytest.raises(ValidationError, match="duplicate"):
        window((one, one))
    with pytest.raises(ValidationError, match="future"):
        window((DecisionCredit(decision_at=NOW + timedelta(seconds=1), fraction=D(1)),))


@pytest.mark.parametrize("sign", [-1, 1])
def test_tick_limits_accept_exact_endpoint_and_reject_one_beyond_before_float(sign):
    scales = (TickScale("value", D(0), D(1), 1),)
    bound = sign * (2**53 - 1)
    result = vectorize([(D(bound),)], scales)
    assert result.exact_value(0, 0) == bound
    assert result.values[0, 0] == bound
    with pytest.raises(ValueError):
        vectorize([(D(sign * 2**53),)], scales)
    with pytest.raises(ValueError):
        normalize_ticks(np.array([[sign * 2**53]], dtype=np.int64), np.array([1], dtype=np.int64))


@pytest.mark.parametrize("ticks,scales", [
    (np.array([[np.iinfo(np.int64).min]]), np.array([1], dtype=np.int64)),
    (np.array([[1.]]), np.array([1], dtype=np.int64)),
    (np.array([[1]], dtype=np.int64), np.array([0], dtype=np.int64)),
    (np.array([[1]], dtype=np.int64), np.array([2**40 + 1], dtype=np.int64)),
])
def test_native_vector_entry_rejects_unsafe_integer_or_scale_inputs(ticks, scales):
    with pytest.raises(ValueError):
        normalize_ticks(ticks, scales)


def test_exact_strict_turnover_budget_including_prior_reservations():
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        assert turnover_admissible(D(1000), D(20), D("29.999999999999999999"))
        assert not turnover_admissible(D(1000), D(20), D(30))
        assert not turnover_admissible(D(1000), D(20), D("30.000000000000000001"))
    with pytest.raises(ValueError):
        turnover_admissible(D(1000), D(0), 1.0)


def test_objectives_record_and_reject_budget_saturation_and_credit_violations(monkeypatch):
    def evaluate(*args, **kwargs):
        return {"sortino": 100., "maximum_drawdown": 0., "net_return": 1., "turnover_violations": 1,
                "training_saturation_peak": .2, "training_saturation_mean": .1, "unassigned_credit_windows": 2,
                "absolute_saturation_time_fraction": .15, "homeostatic_erasure_rate": .001,
                "execution_diagnostics": ExecutionDiagnostics().report()}
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluate)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"],
                               sampler=optuna.samplers.TPESampler(seed=1))
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("synthetic",), (1,), RiskRewardConfig()), n_trials=1)
    assert all(value > 0 for value in trial_constraints(study.trials[0])[:3])
    assert trial_constraints(study.trials[0])[3] == 0
    assert study.best_trials == []
    assert trial_constraints(SimpleNamespace(user_attrs={})) == (1., 1., 1., 1.)


def test_holdout_registry_rejects_overlapping_reuse_across_studies(tmp_path):
    path = tmp_path / "studies.sqlite"
    claim_holdout(path, "dataset-one", NOW, NOW + timedelta(days=10), "first", 1)
    with pytest.raises(ValueError, match="already claimed"):
        claim_holdout(path, "dataset-one", NOW + timedelta(days=9), NOW + timedelta(days=11), "second", 2)
    claim_holdout(path, "dataset-one", NOW + timedelta(days=10), NOW + timedelta(days=11), "third", 3)
    claim_holdout(path, "dataset-two", NOW, NOW + timedelta(days=10), "different-data", 4)


def test_consumed_holdout_cannot_be_reused_for_development(tmp_path):
    path = tmp_path / "studies.sqlite"
    claim_holdout(path, "dataset-one", NOW, NOW + timedelta(days=10), "first", 1)
    for start, end in ((NOW - timedelta(days=1), NOW + timedelta(days=1)),
                       (NOW + timedelta(days=9), NOW + timedelta(days=11))):
        with pytest.raises(ValueError, match="development data overlaps"):
            assert_development_unsealed(path, "dataset-one", [(start, end)])
    assert_development_unsealed(path, "dataset-one", [(NOW - timedelta(days=1), NOW)])
    assert_development_unsealed(path, "dataset-two", [(NOW, NOW + timedelta(days=1))])


def test_turnover_penalty_is_identifiable_below_hard_budget():
    def signal(turnover, penalty):
        return risk_signal(np.zeros(2), 2, 0., .0001, .75, 4., turnover, .05, penalty)
    assert signal(0., .1)[0] == 0
    assert signal(.01, .1)[0] < 0
    assert signal(.01, 1.)[0] < signal(.01, .1)[0]
    assert signal(.01, .1)[-1] == pytest.approx(.001)


def test_real_brain_step_dispatches_experimental_kernel_without_baseline_double_update():
    fixture = synthetic_brain()
    brain = MemoryBrain.__new__(MemoryBrain)
    brain.__dict__.update(vars(fixture))
    brain.n, brain.dt = 3, .1
    brain.weight, brain.baseline_plastic = np.ones(1), np.ones(1)
    brain.counts = np.zeros(3, dtype=np.int32)
    # rate_kc/rate_dan intentionally absent: invoking the baseline rule would fail.
    spikes = iter([np.array([1, 0, 0]), np.array([0, 1, 0]), np.array([0, 0, 1])])
    brain._neural_step = lambda *a, **kw: (next(spikes), 0.)
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    engine.begin_frame(NOW, (), has_reward=False)
    brain.step(np.zeros(1), 20, learning=True, plasticity_delta_limit=.01, experimental_plasticity=engine)
    engine.bank.store(NOW, engine.tag)
    engine.begin_frame(NOW + timedelta(seconds=10),
                       (DecisionCredit(decision_at=NOW, fraction=D(1)),), has_reward=True)
    brain.step(np.zeros(1), 10, learning=True, plasticity_delta_limit=.01, experimental_plasticity=engine,
               stimulation=(np.array([2]), 20))
    assert brain.weight[0] < 1 and brain.weight[0] == 1 + brain.memory_w[0]
    assert brain.plasticity_diagnostics["bins"] == 3


def test_capacity_rejection_prunes_without_inventing_profit_metrics(monkeypatch):
    def reject(*args, **kwargs):
        raise EligibilityCapacityError("synthetic capacity limit")
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", reject)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"])
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("synthetic",), (1,), RiskRewardConfig()), n_trials=1)
    assert study.trials[0].state == optuna.trial.TrialState.PRUNED
    assert study.trials[0].values is None
