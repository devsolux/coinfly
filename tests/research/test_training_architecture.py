"""Synthetic fixtures validate mechanics, never profitability or the full graph."""

import hashlib
import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, localcontext
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import numpy as np
import optuna
import polars as pl
import pytest

from coinfly.domain.context import DomainError
from coinfly.research.causal_data import AuditedFrame, CausalFrameLoader
from coinfly.research.fee_presets import preset_fees
from coinfly.research.neural.brain import MemoryBrain
from coinfly.research.optimization import (
    ExecutionDiagnostics,
    Fold,
    StudyResources,
    _phase,
    objective,
    performance,
    walk_forward,
)
from coinfly.research.profile import NeuralProfile
from coinfly.research.reward import DecisionCredit, ExecutionCost, ObservationEvidence, RewardWindow
from coinfly.research.risk_reward import RiskAdjustedReward, RiskRewardConfig, latency_residual
from coinfly.research.training import TrainingController, TrainingProfile
from coinfly.research.vectorization import TickScale, vectorize

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
SCALE = (TickScale("price", D("100"), D("0.01"), 100),)


def synthetic_resources(horizon=1.):
    return StudyResources(lambda *a: None, lambda *a: None, "synthetic-test-evidence",
                          "synthetic-data", reward_credit_horizon_seconds=horizon)


def outcome(index=0, pnl="-1", capital="1000", **kwargs):
    return RewardWindow(start_at=NOW + timedelta(seconds=index), end_at=NOW + timedelta(seconds=index + 1),
                        available_at=NOW + timedelta(seconds=index + 1), capital=D(capital), pnl=D(pnl),
                        currency="USD", basis="net_execution", **kwargs)


def feature_table():
    return pl.DataFrame({"feature": ["price"], "value": [D("100.01")], "source_kind": ["market"],
                         "event_at": [NOW], "window_end": [NOW], "available_at": [NOW], "fitted_through": [NOW]})


def frame(index=0, *, partition="train", evaluation_start=None):
    return CausalFrameLoader(SCALE).frame(feature_table(), decision_at=NOW + timedelta(seconds=index),
                                         partition=partition, evaluation_start=evaluation_start)


def test_exact_centering_before_float_conversion_survives_hostile_context():
    spec = (TickScale("large_price", D("99999999999999999999"), D("0.000000000000000001"), 1024),)
    value = D("99999999999999999999.000000000000000003")
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        batch = vectorize([(value,)], spec)
        assert batch.exact_value(0, 0) == value
    assert batch.ticks[0, 0] == 3
    assert batch.values[0, 0] == 3 / 1024
    assert not batch.values.flags.writeable and not batch.ticks.flags.writeable


@pytest.mark.parametrize("value", [D("100.001"), D("NaN"), D("9007199254741092")])
def test_vectorizer_rejects_offgrid_nonfinite_or_unsafe_tick_distance(value):
    with pytest.raises(ValueError):
        vectorize([(value,)], SCALE)


@pytest.mark.parametrize("change", [
    pl.lit(NOW + timedelta(seconds=1)).alias("window_end"),
    pl.lit(NOW + timedelta(seconds=1)).alias("available_at"),
    pl.lit(NOW + timedelta(seconds=1)).alias("fitted_through"),
    pl.lit("execution").alias("source_kind"),
    pl.lit("future_high").alias("feature"),
])
def test_polars_rejects_information_boundary_violations(change):
    with pytest.raises(ValueError):
        CausalFrameLoader(SCALE).frame(feature_table().with_columns(change), decision_at=NOW, partition="train")


def test_no_silent_column_drop_float_input_duplicates_or_holdout_fitting():
    loader = CausalFrameLoader(SCALE)
    for table in (feature_table().with_columns(pl.lit(1).alias("future_low")),
                  feature_table().with_columns(pl.col("value").cast(pl.Float64)),
                  pl.concat([feature_table(), feature_table()])):
        with pytest.raises(ValueError):
            loader.frame(table, decision_at=NOW, partition="train")
    with pytest.raises(ValueError, match="holdout"):
        loader.frame(feature_table(), decision_at=NOW, partition="evaluation", evaluation_start=NOW)


def test_owned_audited_frame_detects_tampering():
    audited = frame()
    assert audited.tensor.exact_value(0, 0) == D("100.01")
    snapshot = audited.owned_rgb()
    audited.rgb.flags.writeable = True
    audited.rgb.fill(0)
    assert snapshot.any()
    with pytest.raises(ValueError, match="mutated"):
        audited.owned_rgb()


def test_fee_presets_have_exact_thresholds_and_region_entry_limits():
    def fees(preset, volume):
        return preset_fees(preset, trailing_volume_usd=D(volume), observed_at=NOW, expires_at=NOW + timedelta(days=1))
    assert fees("exchange-standard-2026-09-20", "9999.999999999999999999").rate == D("0.006")
    assert fees("exchange-standard-2026-09-20", "10000").rate == D("0.004")
    assert fees("advanced-entry-eu-uk-2026-09-16", "0").rate == D("0.005")
    with pytest.raises(ValueError, match="does not cover"):
        fees("advanced-entry-eu-uk-2026-09-16", "10000")


def test_jit_latency_penalty_is_asymmetric_and_not_double_charged():
    notional, adverse, latency, side = (np.array([1.0]) for _ in range(4))
    adverse[:] = 0
    assert latency_residual(notional, adverse, latency, side, 1., 2., 20.) == pytest.approx(.0001)
    side[:] = -1
    assert latency_residual(notional, adverse, latency, side, 1., 2., 20.) == pytest.approx(.0002)
    adverse[:] = .0003
    assert latency_residual(notional, adverse, latency, side, 1., 2., 20.) == 0


def test_risk_reward_bounded_warmup_constant_space_and_unshaped_pnl():
    reward = RiskAdjustedReward(RiskRewardConfig(window=4))
    for index in range(100):
        result = reward(outcome(index))
        assert result.net_pnl == D(-1)
        assert -1 <= result.normalized_reward < 0
        assert result.stimulus == "aversive"
    assert reward.count == 4 and len(reward.history) == 4
    assert reward.metrics["net_return"] == -.001
    assert np.isfinite(list(reward.metrics.values())).all()
    history = reward.history.copy()
    with pytest.raises(ValueError, match="contiguous"):
        reward(outcome(0))
    np.testing.assert_array_equal(reward.history, history)


def test_insolvency_stops_instead_of_clipping_a_failed_account_to_survival():
    reward = RiskAdjustedReward()
    with pytest.raises(ValueError, match="insolvent"):
        reward(outcome(pnl="-1000"))
    assert reward.count == 0


def test_turnover_penalty_changes_reinforcement_without_double_subtracting_booked_fees():
    fees = preset_fees("exchange-standard-2026-09-20", trailing_volume_usd=D(0), observed_at=NOW,
                       expires_at=NOW + timedelta(days=1))
    fill = ExecutionCost(fill_id="synthetic", currency="USD", side="BUY", quantity=D(10),
        reference_price=D(100), fill_price=D(100), decision_at=NOW, reference_at=NOW, reference_available_at=NOW,
        submitted_at=NOW, filled_at=NOW + timedelta(milliseconds=100), available_at=NOW + timedelta(milliseconds=100),
        fees=fees, actual_fee=D(6))
    config = RiskRewardConfig(buy_latency_bps_per_second=0, sell_latency_bps_per_second=0)
    result = RiskAdjustedReward(config)(outcome(pnl="0", executions=(fill,)))
    assert result.net_pnl == 0 and result.fees == 6
    # 0.1 * (1.0 gross turnover + 0.95 excess), separate from booked fees.
    assert result.normalized_reward == D("-0.195") and result.stimulus == "aversive"
    assert RiskAdjustedReward(config)(outcome(pnl="0")).stimulus == "none"


def synthetic_controller(seed=1, *, side="HOLD"):
    controller = TrainingController.__new__(TrainingController)
    controller.s = NeuralProfile(neural_ms=20., neural_bin_ms=10., pulse_ms=10.)
    controller._lock, controller._failed = RLock(), False
    seen = []
    def step(rgb, interval, **kwargs):
        seen.append((rgb.copy(), kwargs))
        controller.brain.sim_ms += interval
        counts = np.array([1, 0, 1] if len(seen) % 2 else [0, 1, 1], dtype=np.int32)
        kwargs["experimental_plasticity"].advance(controller.brain, counts, interval / 1000,
            learning=kwargs["learning"], stimulation=kwargs["stimulation"], delta_limit=kwargs["plasticity_delta_limit"])
        return counts, 0.
    controller.brain = SimpleNamespace(n=3, dt=.1, counts=np.zeros(3, dtype=np.int32),
        circuit={"reward": np.array([2]), "aversive": np.array([2]), "kc": np.array([0]),
                 "pre": np.array([0]), "edges": np.array([0]), "dan": np.array([2]), "gain": np.ones((1, 1))},
        post=np.array([1]), memory_u=np.zeros(1), memory_w=np.zeros(1), dan_baseline_hz=np.zeros(1),
        weight=np.ones(1), baseline_plastic=np.ones(1),
        eta=.001, plasticity_diagnostics={}, sim_ms=0., rgb_step=step, memory=lambda: {}, weights_frozen=False)
    controller.brain.prepare_experimental_frame = lambda *a, **kw: MemoryBrain.prepare_experimental_frame(controller.brain, *a, **kw)
    controller.brain.finalize_experimental_frame = lambda *a, **kw: MemoryBrain.finalize_experimental_frame(controller.brain, *a, **kw)
    controller._initialize_training(TrainingProfile(seed=seed), RiskRewardConfig())
    controller.decoder = SimpleNamespace(decode=lambda *_: {"side": side})
    return controller, seen


def test_training_loop_seeded_sensory_exploration_fixed_action_and_frozen_holdout():
    first, seen = synthetic_controller(side="BUY")
    second, _ = synthetic_controller(side="BUY")
    assert first.session._lock is first._lock
    result = first.run_frame(frame())
    assert result["side"] == "BUY"
    assert result["input_sha256"] == second.run_frame(frame())["input_sha256"]
    assert result["audited_input_sha256"] == frame().sha256
    first.run_frame(frame(1), outcome(credits=(DecisionCredit(decision_at=NOW, fraction=D(1)),)))
    assert first.risk_reward.count == 1
    assert any(options["stimulation"] is not None for _, options in seen)
    first.begin_evaluation()
    start = NOW + timedelta(seconds=2)
    holdout = frame(2, partition="evaluation", evaluation_start=start)
    before = len(seen)
    result = first.run_frame(holdout)
    assert result["input_sha256"] == holdout.sha256
    assert all(options["stimulation"] is None and not options["learning"] for _, options in seen[before:])
    assert first.brain.weights_frozen and not first.s.learning
    with pytest.raises(RuntimeError, match="causal evidence"):
        first.observe(holdout.rgb, "reward")
    with pytest.raises(RuntimeError, match="cannot resume"):
        first.restore(None)
    with pytest.raises(RuntimeError, match="already started"):
        first.begin_evaluation()


def test_leakage_rejected_before_risk_state_or_neural_progress():
    controller, seen = synthetic_controller()
    with pytest.raises(DomainError, match="INFORMATION_LEAKAGE"):
        controller.run_frame(frame(0), outcome(0))
    assert not seen and not controller.risk_reward.count


def test_volatility_tightens_trust_region_without_selecting_an_action():
    controller, _ = synthetic_controller()
    initial = controller._step_options()["plasticity_delta_limit"]
    controller.risk_reward.metrics["volatility"] = controller.profile.volatility_target
    assert controller._step_options()["plasticity_delta_limit"] == initial / 2


def test_real_brain_step_bounds_both_drive_and_weight_deltas():
    # Exercise the real integration/rule boundary on a synthetic one-edge fixture.
    brain = MemoryBrain.__new__(MemoryBrain)
    brain.n, brain.dt = 2, .1
    brain.circuit = {"pre": np.array([0]), "dan": np.array([1]), "gain": np.ones((1, 1)), "edges": np.array([0])}
    brain.rate_kc, brain.rate_dan = np.zeros(1), np.full(1, 1000.)
    brain.memory_u, brain.memory_w = np.zeros(1), np.zeros(1)
    brain.dan_baseline_hz = np.zeros(1)
    brain.eta, brain.weights_frozen = .001, False
    brain.weight, brain.baseline_plastic = np.ones(1), np.ones(1)
    brain.counts, brain.plasticity_diagnostics = np.zeros(2, dtype=np.int32), {}
    brain._neural_step = lambda *a, **kw: (np.array([100, 0]), 0.)
    brain.step(np.zeros(1), 10, learning=True, plasticity_delta_limit=.0001)
    assert 0 < brain.memory_u[0] <= .0001 and 0 < brain.memory_w[0] <= .0001
    assert brain.plasticity_diagnostics["trust_region_clipped"] == 2


def test_walkforward_purges_before_each_validation_and_final_holdout():
    folds = walk_forward(NOW, NOW + timedelta(days=20), folds=3, minimum_train=timedelta(days=5),
                         validation=timedelta(days=2), purge=timedelta(days=1))
    assert all(fold.validation_start - fold.train_end == timedelta(days=1) for fold in folds)
    assert folds[-1].validation_end < NOW + timedelta(days=19)
    with pytest.raises(ValueError, match="overlap"):
        walk_forward(NOW, NOW + timedelta(days=10), folds=3, minimum_train=timedelta(days=5),
                     validation=timedelta(days=2), purge=timedelta(days=1))


def test_mdd_includes_initial_equity_and_metrics_use_unshaped_returns():
    metrics = performance([-.1, .1])
    assert metrics["maximum_drawdown"] == pytest.approx(.1)
    assert metrics["net_return"] == pytest.approx(-.01)
    assert metrics["sortino"] == 0
    assert performance([0., 0.])["sortino"] == 0


def test_phase_requires_complete_equity_ledger_and_terminal_costs():
    controller, _ = synthetic_controller()
    controller.begin_evaluation()
    start = NOW + timedelta(seconds=1)
    observation = frame(1, partition="evaluation", evaluation_start=start)
    class Environment:
        execution_realm = "paper"
        actions = []
        def stream(self):
            yield observation, None
        def execute(self, side, at):
            self.actions.append(side)
        def reject(self, side, at, reason):
            raise AssertionError("synthetic frozen evaluation should not reject for tag capacity")
        def finish(self):
            return outcome(1)
    env = Environment()
    returns = _phase(controller, env, start=start, end=start + timedelta(seconds=1),
                     partition="evaluation", max_frames=10)
    assert returns.returns == [-.001] and env.actions == ["HOLD"]
    other, _ = synthetic_controller()
    other.begin_evaluation()
    env.finish = lambda: None
    with pytest.raises(ValueError, match="missing accounting"):
        _phase(other, env, start=start, end=start + timedelta(seconds=1), partition="evaluation", max_frames=10)


def test_optuna_multiobjective_smoke_uses_seed_repeats_and_net_metrics(monkeypatch):
    calls = []
    def evaluate(resources, fold, profile, reward):
        calls.append((fold, profile.seed, reward.interval_seconds))
        return {"sortino": 0.25, "maximum_drawdown": 0.1, "net_return": -0.01,
                "turnover_violations": 0, "training_saturation_peak": 0,
                "training_saturation_mean": 0, "unassigned_credit_windows": 0,
                "absolute_saturation_time_fraction": 0, "homeostatic_erasure_rate": 0,
                "execution_diagnostics": ExecutionDiagnostics().report()}
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluate)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"],
                               sampler=optuna.samplers.TPESampler(seed=1))
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("synthetic-fold",), (1, 2), RiskRewardConfig()), n_trials=2)
    assert len(calls) == 4
    assert study.trials[0].values == [.25, .1, 0, 0]
    assert "fees" not in study.trials[0].params and "latency" not in study.trials[0].params


def test_frame_hash_is_of_the_actual_sensory_projection():
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)
    evidence = ObservationEvidence(decision_at=NOW, features_event_through=NOW, features_available_through=NOW,
                                   preprocessing_fitted_through=NOW, partition="train")
    audited = AuditedFrame(rgb, vectorize([(D("100"),)], SCALE), evidence, hashlib.sha256(rgb.tobytes()).hexdigest())
    assert not audited.owned_rgb().flags.writeable
    with pytest.raises(ValueError):
        replace(TrainingProfile(), plasticity_delta_limit=float("nan"))


def test_study_contract_and_one_time_holdout_with_real_optuna_storage(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/optimize_malecns.py"
    spec = importlib.util.spec_from_file_location("synthetic_study_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    def synthetic_evaluation(*args, **kwargs):
        return {"sortino": .1, "maximum_drawdown": .2, "net_return": -.01,
                "turnover_violations": 0, "training_saturation_peak": 0,
                "training_saturation_mean": 0, "unassigned_credit_windows": 0,
                "absolute_saturation_time_fraction": 0, "homeostatic_erasure_rate": 0,
                "execution_diagnostics": ExecutionDiagnostics().report()}
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", synthetic_evaluation)
    monkeypatch.setattr(runner, "evaluate_fold", synthetic_evaluation)
    args = SimpleNamespace(storage=tmp_path / "synthetic.sqlite", study="synthetic-contract-only",
        start=NOW, holdout_start=NOW + timedelta(days=10), holdout_end=NOW + timedelta(days=11),
        seeds=[1, 2], evaluate_trial=None, trials=1, timeout=10, purge_seconds=86400)
    resources = synthetic_resources()
    folds = (Fold(NOW, NOW + timedelta(days=1), NOW + timedelta(days=2), NOW + timedelta(days=3)),)
    with runner.storage_lock(args.storage):
        with pytest.raises(FileExistsError):
            with runner.storage_lock(args.storage):
                pass
        runner.run_study(args, resources, folds, RiskRewardConfig())
    saved = optuna.load_study(study_name=args.study, storage=f"sqlite:///{args.storage.resolve()}")
    contract = saved.user_attrs["contract"]
    assert contract["schema"] == 4
    assert contract["objectives"] == [
        {"name": "capacity_penalized_net_sortino", "direction": "maximize"},
        {"name": "validation_maximum_drawdown", "direction": "minimize"},
        {"name": "training_near_bound_edge_time_occupancy", "direction": "minimize"},
        {"name": "training_passive_erasure_rate", "direction": "minimize"},
    ]
    coordinates = {item["name"]: (item["low"], item["high"], item["log"])
                   for item in contract["reinforcement_coordinates"]}
    assert coordinates == {"positive_return_threshold": (.0001, .01, True),
                           "positive_gain": (1., 2., False), "fee_shock_gain": (.25, 1., False),
                           "credit_tau_seconds": (.5, 150., True), "maximum_credit_age_seconds": (5., 150., True)}
    assert [direction.name for direction in saved.directions] == ["MAXIMIZE", "MINIMIZE", "MINIMIZE", "MINIMIZE"]
    for name, (low, high, logarithmic) in coordinates.items():
        distribution = saved.trials[0].distributions[name]
        assert distribution.high == high and distribution.log == logarithmic
        assert distribution.low >= low if name == "maximum_credit_age_seconds" else distribution.low == low
    with pytest.raises(ValueError, match="contract changed"):
        runner.run_study(args, replace(resources, evidence_id="different-data"), folds, RiskRewardConfig())
    with pytest.raises(ValueError, match="contract changed"):
        runner.run_study(args, replace(resources, reward_credit_horizon_seconds=120), folds, RiskRewardConfig())
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold",
                        lambda *a, **k: synthetic_evaluation() | {"turnover_violations": 1})
    runner.run_study(args, resources, folds, RiskRewardConfig())
    args.evaluate_trial = 1
    with pytest.raises(ValueError, match="infeasible"):
        runner.run_study(args, resources, folds, RiskRewardConfig())
    args.evaluate_trial = 0
    runner.run_study(args, resources, folds, RiskRewardConfig())
    with pytest.raises(ValueError, match="already claimed"):
        runner.run_study(args, resources, folds, RiskRewardConfig())
    args.evaluate_trial = None
    with pytest.raises(ValueError, match="sealed"):
        runner.run_study(args, resources, folds, RiskRewardConfig())
