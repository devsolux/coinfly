"""v3 evidence adequacy, paired frozen controls and contract separation from the frozen v2 profile.

Synthetic fixtures only: nothing here measures the full graph or market performance.
"""

import importlib.util
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import optuna
import pytest

from coinfly.research.evidence import (
    EFFECTIVE_NOISE_SIGMA,
    EvidenceRequirements,
    EvidenceSummary,
    assess_evidence,
    paired_deltas,
    summarize_seeds,
)
from coinfly.research.fee_presets import preset_fees
from coinfly.research.neural.experimental_plasticity import ExperimentalPlasticity, PlasticityConfig
from coinfly.research.optimization import (
    V2_PROFILE_ID,
    V2_PROTOCOL,
    V3_PROFILE_ID,
    ExecutionDiagnostics,
    Fold,
    StudyProtocol,
    _phase,
    comparable_metrics,
    implementation_files,
    implementation_fingerprint,
    objective,
    v3_protocol,
)
from coinfly.research.reward import DecisionCredit, ExecutionCost
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.training import TrainingProfile
from tests.research.test_training_architecture import (
    NOW,
    frame,
    outcome,
    synthetic_controller,
    synthetic_resources,
)

REQUIREMENTS = EvidenceRequirements(minimum_executed_decisions=4, minimum_completed_round_trips=1,
                                    minimum_nonzero_reward_pulses=2, minimum_credit_coverage=.5,
                                    minimum_retained_credit_mass=.05)


def fill(side, index):
    fees = preset_fees("exchange-standard-2026-09-20", trailing_volume_usd=D(0), observed_at=NOW,
                       expires_at=NOW + timedelta(days=1))
    at = NOW + timedelta(seconds=index)
    return ExecutionCost(fill_id=f"{side}-{index}", currency="USD", side=side, quantity=D("0.1"),
                         reference_price=D(100), fill_price=D(100), decision_at=at, reference_at=at,
                         reference_available_at=at, submitted_at=at, filled_at=at + timedelta(milliseconds=100),
                         available_at=at + timedelta(milliseconds=100), fees=fees, actual_fee=D("0.006"))


def synthetic_evaluation(*, executed=10, round_trips=5, pulses=10, delivered=10, mass=.5, violations=0, learned=True):
    training = EvidenceSummary()
    for index in range(pulses):
        training.record_reward({"credit_allocations": 1, "credit_decay_mass": mass if index < delivered else 0.,
                                "maximum_credit_delay_seconds": 1., "unassigned_reward": 0,
                                "missing_credit_decisions": 0 if index < delivered else 1},
                               requested=True, signal=1., net_return=.001, horizon=1.)
    validation = EvidenceSummary()
    validation.record_window(buys=round_trips, sells=executed - round_trips, credits=1, turnover=.01)
    for _ in range(executed):
        validation.record_decision("BUY", True, None, retained_tags=1, expired_tags=0, sigma=None)
    diagnostics = ExecutionDiagnostics(order_attempts=executed, credit_horizon_violations=violations)
    return {"sortino": .25 if learned else .1, "maximum_drawdown": .1, "net_return": .01 if learned else .0,
            "turnover_violations": 0, "training_saturation_peak": 0, "training_saturation_mean": 0,
            "unassigned_credit_windows": 0, "absolute_saturation_time_fraction": 0, "homeostatic_erasure_rate": 0,
            "turnover_peak": "0.02", "compute_seconds": 1.5, "max_rss_bytes": 1000, "learned": learned,
            "execution_diagnostics": diagnostics.report(), "training_evidence": training.report(),
            "validation_evidence": validation.report()}


def test_requirements_are_derived_from_a_declared_precision_not_a_magic_number():
    derived = EvidenceRequirements.from_protocol(win_rate_precision=.1)
    assert derived.minimum_executed_decisions == 97  # ceil((1.96 / 0.2) ** 2)
    assert derived.minimum_completed_round_trips == 49 and derived.minimum_nonzero_reward_pulses == 97
    assert "0.1" in derived.derivation
    tighter = EvidenceRequirements.from_protocol(win_rate_precision=.05)
    assert tighter.minimum_executed_decisions == 385
    for precision in (0.001, 0.6, float("nan")):
        with pytest.raises(ValueError):
            EvidenceRequirements.from_protocol(win_rate_precision=precision)
    for bad in ({"minimum_executed_decisions": 0}, {"minimum_credit_coverage": 0.}, {"minimum_retained_credit_mass": 1.5},
                {"minimum_nonzero_reward_pulses": True}):
        with pytest.raises(ValueError):
            replace(REQUIREMENTS, **bad)


def test_inactive_policy_is_insufficient_evidence_not_a_favorable_score():
    idle = EvidenceSummary()
    for _ in range(500):
        idle.record_decision("HOLD", True, None, retained_tags=0, expired_tags=0, sigma=2.)
    for _ in range(500):
        idle.record_window(buys=0, sells=0, credits=0, turnover=0.)
    report = idle.report()
    assert report["hold_fraction"] == 1. and report["executed_decisions"] == 0 and report["credit_coverage"] == 0.
    verdict = assess_evidence(report, report, REQUIREMENTS)
    assert verdict["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert set(verdict["evidence_reasons"]) == {"validation_executed_decisions", "validation_completed_round_trips",
                                                 "training_nonzero_reward_pulses", "training_credit_coverage",
                                                 "training_retained_credit_mass"}
    active = synthetic_evaluation()
    assert assess_evidence(active["training_evidence"], active["validation_evidence"], REQUIREMENTS)["evidence_status"] == "SUFFICIENT"


def test_numerically_annihilated_credit_is_detected_even_when_every_count_is_perfect():
    starved = synthetic_evaluation(mass=1e-9)
    training = starved["training_evidence"]
    assert training["credit_coverage"] == 1. and training["credit_loss_fraction"] == 0.
    verdict = assess_evidence(training, starved["validation_evidence"], REQUIREMENTS)
    assert verdict["evidence_reasons"] == ["training_retained_credit_mass"]


def test_phase_measures_fills_round_trips_credit_mass_sign_disagreement_and_noise_schedule():
    controller, _ = synthetic_controller(side="BUY")
    controller.plasticity = ExperimentalPlasticity(controller.brain, replace(PlasticityConfig(), credit_tau_seconds=150.))

    class Environment:
        execution_realm = "paper"

        def stream(self):
            yield frame(), None
            # A small booked profit whose turnover-penalized shaped signal is
            # negative: the disagreement is measured, never silently corrected.
            yield frame(1), outcome(pnl="0.01", executions=(fill("BUY", 0),),
                                    credits=(DecisionCredit(decision_at=NOW, fraction=D(1)),))
            yield frame(2), outcome(1, pnl="-1", capital="1000.01", executions=(fill("SELL", 1),),
                                    credits=(DecisionCredit(decision_at=NOW, fraction=D(1)),))

        def execute(self, side, at):
            pass

        def reject(self, side, at, reason):
            raise AssertionError("no rejection expected")

        def finish(self):
            return outcome(2, pnl="0", capital="999.01")

    controller.risk_reward = controller.risk_reward.__class__(replace(RiskRewardConfig(), turnover_penalty=5.))
    controller.session = controller.session.__class__(controller, controller.risk_reward)
    phase = _phase(controller, Environment(), start=NOW, end=NOW + timedelta(seconds=3), partition="train", max_frames=3)
    report = phase.evidence.report()
    assert report["frames"] == 3 and report["decoded_orders"] == 3 and report["admitted_decisions"] == 3
    assert report["buy_fills"] == report["sell_fills"] == 1 and report["completed_round_trips"] == 1
    assert report["executed_decisions"] == 2 and report["exposed_windows"] == 2 and report["accounted_windows"] == 3
    assert report["nonzero_reward_pulses"] == 2 and report["reward_allocations"] == 2
    assert report["delivered_pulses"] == 2 and report["credit_coverage"] == 1.
    assert 0 < report["retained_credit_mass"]["median"] <= 1. and report["credit_delay_seconds"]["max"] == 2.
    assert report["sign_disagreements"] == 1 and report["sign_disagreement_fraction"] == .5
    sigma = report["sensory_noise_sigma"]
    assert sigma["first"] == 2. and sigma["last"] < sigma["first"] and sigma["ineffective_frames"] == 0
    assert report["ring_occupancy_peak"] >= 1 and phase.compute_seconds >= 0
    assert report["action_counts"] == {"BUY": 3, "SELL": 0, "HOLD": 0} and report["hold_fraction"] == 0.
    assert EFFECTIVE_NOISE_SIGMA < .5


def test_v3_objective_prunes_insufficient_evidence_and_records_paired_frozen_control(monkeypatch):
    calls = []

    def evaluate(resources, fold, profile, reward, **kwargs):
        learning = kwargs.get("learning", True)
        calls.append((fold, profile.seed, profile.profile_id, learning))
        return synthetic_evaluation(learned=learning)

    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluate)
    protocol = v3_protocol(REQUIREMENTS)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"],
                               sampler=optuna.samplers.TPESampler(seed=3))
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("fold",), (1, 2), RiskRewardConfig(),
                                          protocol=protocol), n_trials=1)
    trial = study.trials[0]
    assert trial.state == optuna.trial.TrialState.COMPLETE and trial.values[0] == pytest.approx(.25)
    assert [call[2:] for call in calls] == [(V3_PROFILE_ID, True), (V3_PROFILE_ID, False)] * 2
    assert trial.user_attrs["profile"]["profile_id"] == V3_PROFILE_ID
    assert trial.user_attrs["profile"]["implementation_revision"] == 5
    assert trial.user_attrs["protocol"]["schema"] == 5 and trial.user_attrs["protocol"]["evidence"] == REQUIREMENTS.public_dict()
    paired = trial.user_attrs["paired_control"]
    assert len(paired["frozen_controls"]) == 2 and all(not item["learned"] for item in paired["frozen_controls"])
    assert paired["deltas"][0]["sortino"] == pytest.approx(.15) and paired["deltas"][0]["net_return"] == pytest.approx(.01)
    summary = paired["seed_summary"]
    assert summary["sortino"]["samples"] == 2 and summary["sortino"]["improving"] == 2 and summary["sortino"]["std"] == 0.
    assert summary["max_rss_bytes"]["mean"] == 0. and all(item["evidence_status"] == "SUFFICIENT" for item in trial.user_attrs["evidence"])

    # The identical inactive evaluation scores (0, 0, 0, 0) under v2 but is pruned under v3.
    idle = synthetic_evaluation(executed=0, round_trips=0, pulses=0, delivered=0)
    idle.update({"sortino": 0., "maximum_drawdown": 0., "net_return": 0.})
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", lambda *a, **k: idle)
    for current, expected in ((V2_PROTOCOL, optuna.trial.TrialState.COMPLETE), (protocol, optuna.trial.TrialState.PRUNED)):
        run = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"])
        run.optimize(lambda trial, p=current: objective(trial, synthetic_resources(), ("fold",), (1,), RiskRewardConfig(),
                                                        protocol=p), n_trials=1)
        assert run.trials[0].state == expected
    pruned = run.trials[0]
    assert pruned.values is None and pruned.user_attrs["pruning_reason"] == "INSUFFICIENT_EVIDENCE"
    assert pruned.user_attrs["evidence"][0]["evidence_reasons"][0] == "validation_executed_decisions"
    assert run.best_trials == []


def test_v3_fails_explicitly_when_reward_arrives_beyond_the_declared_horizon(monkeypatch):
    late = synthetic_evaluation(violations=1)
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", lambda *a, **k: late)
    for current, expected in ((V2_PROTOCOL, optuna.trial.TrialState.COMPLETE),
                              (v3_protocol(REQUIREMENTS, paired_control=False), optuna.trial.TrialState.PRUNED)):
        study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"])
        study.optimize(lambda trial, p=current: objective(trial, synthetic_resources(), ("fold",), (1,), RiskRewardConfig(),
                                                          protocol=p), n_trials=1)
        assert study.trials[0].state == expected
    assert study.trials[0].user_attrs["pruning_reason"] == "REWARD_DELIVERY_BEYOND_DECLARED_HORIZON"
    assert "paired_control" not in study.trials[0].user_attrs


def test_paired_deltas_and_seed_uncertainty_keep_unmeasured_metrics_explicit():
    learned = comparable_metrics(synthetic_evaluation())
    frozen = comparable_metrics(synthetic_evaluation(learned=False))
    deltas = paired_deltas(learned, frozen)
    assert deltas["net_return"] == pytest.approx(.01) and deltas["hold_fraction"] == 0. and deltas["max_rss_bytes"] == 0.
    unmeasured = paired_deltas({**learned, "max_rss_bytes": None}, frozen)
    assert unmeasured["max_rss_bytes"] is None
    summary = summarize_seeds([deltas, {**deltas, "net_return": -.02}, unmeasured])
    assert summary["net_return"] == {"samples": 3, "mean": pytest.approx(0.), "std": pytest.approx(.017320508),
                                     "min": -.02, "max": pytest.approx(.01), "improving": 2}
    assert summary["max_rss_bytes"]["samples"] == 2


def test_profiles_and_protocols_cannot_be_mixed():
    assert TrainingProfile(profile_id=V3_PROFILE_ID, implementation_revision=5).profile_id == V3_PROFILE_ID
    for identity, revision in ((V3_PROFILE_ID, 4), (V2_PROFILE_ID, 5), ("malecns-risk-adjusted-experiment-v4", 6)):
        with pytest.raises(ValueError):
            TrainingProfile(profile_id=identity, implementation_revision=revision)
    with pytest.raises(ValueError, match="v3 evidence"):
        StudyProtocol(V2_PROFILE_ID, 4, 4, "x", evidence=REQUIREMENTS)
    with pytest.raises(ValueError, match="requires explicitly"):
        StudyProtocol(V3_PROFILE_ID, 5, 5, "x")
    with pytest.raises(ValueError, match="unknown"):
        StudyProtocol("malecns-risk-adjusted-experiment-v4", 6, 6, "x", evidence=REQUIREMENTS)
    assert V2_PROTOCOL.public_dict()["evidence"] is None and not V2_PROTOCOL.paired_control


def test_fingerprint_covers_every_behavior_module_and_the_native_kernel():
    files = implementation_files()
    for required in ("profile.py", "neural/circuit.py", "neural/visual.py", "neural/sensory.py", "assets.py",
                     "evidence.py", "optimization.py", "neural/experimental_plasticity.py", "neural/kernel.cpp"):
        assert required in files, required
    assert len(implementation_fingerprint()) == 64 and implementation_fingerprint() == implementation_fingerprint()


def test_runner_never_merges_v2_and_v3_histories(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/optimize_malecns.py"
    spec = importlib.util.spec_from_file_location("v3_study_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", lambda *a, **k: synthetic_evaluation())
    monkeypatch.setattr(runner, "evaluate_fold", lambda *a, **k: synthetic_evaluation())
    folds = (Fold(NOW, NOW + timedelta(days=1), NOW + timedelta(days=2), NOW + timedelta(days=3)),)
    base = dict(storage=tmp_path / "study.sqlite", start=NOW, holdout_start=NOW + timedelta(days=10),
                holdout_end=NOW + timedelta(days=11), seeds=[1], evaluate_trial=None, trials=1, timeout=10,
                purge_seconds=86400)
    v3 = v3_protocol(REQUIREMENTS)
    assert v3.default_study_name != V2_PROTOCOL.default_study_name
    runner.run_study(SimpleNamespace(study="shared-name", protocol=V2_PROTOCOL, **base), synthetic_resources(), folds,
                     RiskRewardConfig())
    with pytest.raises(ValueError, match="never merged"):
        runner.run_study(SimpleNamespace(study="shared-name", protocol=v3, **base), synthetic_resources(), folds,
                         RiskRewardConfig())
    runner.run_study(SimpleNamespace(study="v3-only", protocol=v3, **base), synthetic_resources(), folds, RiskRewardConfig())
    saved = optuna.load_study(study_name="v3-only", storage=f"sqlite:///{base['storage'].resolve()}")
    contract = saved.user_attrs["contract"]
    assert contract["schema"] == 5 and contract["protocol"]["profile_id"] == V3_PROFILE_ID
    assert contract["protocol"]["evidence"]["minimum_executed_decisions"] == 4
    assert "neural/kernel.cpp" in contract["implementation_files"]
    assert saved.trials[0].state == optuna.trial.TrialState.COMPLETE
    assert "paired_control" in saved.trials[0].user_attrs
    with pytest.raises(ValueError, match="never merged"):
        runner.run_study(SimpleNamespace(study="v3-only", protocol=V2_PROTOCOL, **base), synthetic_resources(), folds,
                         RiskRewardConfig())
    changed = v3_protocol(replace(REQUIREMENTS, minimum_executed_decisions=5))
    with pytest.raises(ValueError, match="contract changed"):
        runner.run_study(SimpleNamespace(study="v3-only", protocol=changed, **base), synthetic_resources(), folds,
                         RiskRewardConfig())
