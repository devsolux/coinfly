#!/usr/bin/env python3
"""Offline full-graph Optuna study. See docs/malecns-capacity-fitness.md."""

import argparse
import hashlib
import importlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import optuna

from coinfly.paths import ApplicationPaths
from coinfly.research.evidence import EvidenceRequirements
from coinfly.research.neural.experimental_plasticity import PlasticityConfig
from coinfly.research.optimization import (
    ABSOLUTE_SATURATION_TIME_LIMIT,
    CAPACITY_PENALTY_WEIGHT,
    CAPACITY_REJECTION_THRESHOLD,
    MISSING_CREDIT_ALERT_FRACTION,
    OBJECTIVE_MAP,
    REINFORCEMENT_COORDINATES,
    SATURATION_LIMIT,
    V2_PROTOCOL,
    AbsoluteSaturationError,
    Fold,
    StudyProtocol,
    StudyResources,
    assert_development_unsealed,
    claim_holdout,
    comparable_metrics,
    evaluate_fold,
    implementation_files,
    implementation_fingerprint,
    objective,
    paired_deltas,
    required_credit_horizon,
    summarize_seeds,
    trial_constraints,
    v3_protocol,
    walk_forward,
)
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.training import TrainingProfile
from coinfly.research.turnover import TURNOVER_LIMIT


@contextmanager
def storage_lock(storage):
    """One process per database; atomic exclusive creation also fences holdout claims."""
    lock = storage.resolve().with_suffix(storage.suffix + ".lock")
    with lock.open("x") as handle:
        handle.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory", required=True, help="trusted host module:callable returning StudyResources")
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--holdout-start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--holdout-end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--validation-days", type=int, default=14)
    parser.add_argument("--purge-seconds", type=int, required=True, help="at least feature lookback + label/report horizon")
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=86400)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1729, 2718, 3141])
    parser.add_argument("--storage", type=Path, help="study database; defaults to ~/.devsolux/coinfly/runs/malecns-optuna.sqlite")
    parser.add_argument("--study", help="study name; defaults to the profile's own name, never shared across profiles")
    parser.add_argument("--profile", choices=("v2", "v3"), default="v2",
                        help="v2: frozen contract (schema 4). v3: evidence gate + paired frozen controls (schema 5)")
    parser.add_argument("--win-rate-precision", type=float,
                        help="v3: derive evidence minimums from this 95%% win-rate half-width (e.g. 0.1)")
    parser.add_argument("--evidence-minimums", type=int, nargs=3, metavar=("DECISIONS", "ROUND_TRIPS", "PULSES"),
                        help="v3: explicit executed decisions, completed round trips and nonzero reward pulses")
    parser.add_argument("--evidence-credit", type=float, nargs=2, metavar=("COVERAGE", "RETAINED_MASS"),
                        default=(0.95, 0.05), help="v3: minimum credit coverage and median retained credit mass")
    parser.add_argument("--no-paired-control", action="store_true", help="v3: skip the frozen control per fold/seed")
    parser.add_argument("--evaluate-trial", type=int, help="evaluate ONE preselected completed trial on final holdout")
    args = parser.parse_args()
    args.protocol = build_protocol(args, parser)
    if args.study is None:
        args.study = args.protocol.default_study_name
    if args.storage is None:
        paths = ApplicationPaths.for_current_user()
        paths.prepare()
        args.storage = paths.runs / "malecns-optuna.sqlite"
    if (args.trials < 1 or args.timeout < 1 or not args.seeds or len(set(args.seeds)) != len(args.seeds)
            or any(at.tzinfo is None for at in (args.start, args.holdout_start, args.holdout_end))
            or args.holdout_end <= args.holdout_start):
        parser.error("positive budgets, distinct seeds and an aware final holdout interval required")
    folds = walk_forward(args.start, args.holdout_start, folds=args.folds,
                         minimum_train=timedelta(days=args.train_days),
                         validation=timedelta(days=args.validation_days), purge=timedelta(seconds=args.purge_seconds))
    module, name = args.factory.split(":", 1)
    resources = getattr(importlib.import_module(module), name)()
    if not isinstance(resources, StudyResources) or not resources.evidence_id or not resources.dataset_id:
        raise TypeError("qualified factory must supply StudyResources with immutable evidence identity")
    reward = RiskRewardConfig(interval_seconds=args.interval_seconds)
    args.storage.parent.mkdir(parents=True, exist_ok=True)
    with storage_lock(args.storage):
        run_study(args, resources, folds, reward)


def build_protocol(args, parser):
    """v2 stays byte-identical; v3 requires deliberately selected evidence minimums."""
    if args.profile == "v2":
        if args.win_rate_precision is not None or args.evidence_minimums is not None or args.no_paired_control:
            parser.error("evidence and paired-control options belong to --profile v3")
        return V2_PROTOCOL
    coverage, retained = args.evidence_credit
    if args.evidence_minimums is not None:
        if args.win_rate_precision is not None:
            parser.error("choose either --evidence-minimums or --win-rate-precision, not both")
        decisions, round_trips, pulses = args.evidence_minimums
        evidence = EvidenceRequirements(decisions, round_trips, pulses, coverage, retained,
                                        derivation="explicit CLI selection")
    elif args.win_rate_precision is not None:
        evidence = EvidenceRequirements.from_protocol(win_rate_precision=args.win_rate_precision,
                                                      credit_coverage=coverage, retained_credit_mass=retained)
    else:
        parser.error("--profile v3 requires --win-rate-precision or --evidence-minimums; no default is assumed")
    return v3_protocol(evidence, paired_control=not args.no_paired_control)


def run_study(args, resources, folds, reward):
    protocol = getattr(args, "protocol", V2_PROTOCOL)
    if not isinstance(protocol, StudyProtocol):
        raise TypeError("a StudyProtocol is required")
    if not resources.dataset_id:
        raise ValueError("a stable market dataset identity is required for the holdout registry")
    horizon = required_credit_horizon(resources, reward)
    if args.purge_seconds < horizon:
        raise ValueError("purge must cover the declared decision-to-reward horizon")
    study = optuna.create_study(study_name=args.study, storage=f"sqlite:///{args.storage.resolve()}",
                               directions=[direction for _, direction in OBJECTIVE_MAP], load_if_exists=True,
                               sampler=optuna.samplers.TPESampler(seed=1729, n_startup_trials=12))
    if study.directions != [optuna.study.StudyDirection[direction.upper()] for _, direction in OBJECTIVE_MAP]:
        raise ValueError("existing study has incompatible objectives")
    if study.trials and "contract" not in study.user_attrs:
        raise ValueError("existing trials lack the required evidence contract")
    contract = {"folds": [{k: v.isoformat() for k, v in asdict(fold).items()} for fold in folds],
                "holdout": [args.holdout_start.isoformat(), args.holdout_end.isoformat()],
                "reward": asdict(reward), "seeds": args.seeds, "evidence_id": resources.evidence_id,
                "dataset_id": resources.dataset_id, "turnover_limit_exclusive": str(TURNOVER_LIMIT),
                "maximum_saturation_fraction": SATURATION_LIMIT,
                "maximum_absolute_saturation_time_fraction": ABSOLUTE_SATURATION_TIME_LIMIT,
                "reward_credit_horizon_seconds": horizon,
                "capacity_rejection_threshold": CAPACITY_REJECTION_THRESHOLD,
                "capacity_penalty_weight": CAPACITY_PENALTY_WEIGHT,
                "missing_credit_alert_fraction": MISSING_CREDIT_ALERT_FRACTION,
                "objectives": [{"name": name, "direction": direction} for name, direction in OBJECTIVE_MAP],
                "reinforcement_coordinates": [{"name": name, "low": low, "high": high, "log": log}
                                               for name, low, high, log in REINFORCEMENT_COORDINATES],
                "credit_age_lower_bound": "max(5, credit_tau_seconds, reward_credit_horizon_seconds)",
                "implementation_sha256": implementation_fingerprint(),
                "implementation_files": list(implementation_files()),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "schema": protocol.schema}
    if protocol.profile_id != V2_PROTOCOL.profile_id:
        contract["protocol"] = protocol.public_dict()
    existing = study.user_attrs.get("contract")
    if existing is not None and (existing.get("schema") != contract["schema"]
                                 or existing.get("protocol", {}).get("profile_id") != contract.get("protocol", {}).get("profile_id")):
        raise ValueError("study belongs to another profile/schema; v2 and v3 histories are never merged")
    if study.user_attrs.get("contract", contract) != contract:
        raise ValueError("study evidence/split/cost contract changed; use a new study")
    study.set_user_attr("contract", contract)
    if args.evaluate_trial is not None:
        if "holdout_claim" in study.user_attrs:
            raise ValueError("final holdout already claimed; do not repeatedly select on holdout performance")
        trial = next((t for t in study.trials if t.number == args.evaluate_trial), None)
        if trial is None or trial.state != optuna.trial.TrialState.COMPLETE:
            raise ValueError("choose a completed development trial before accessing final holdout")
        if any(value > 0 for value in trial_constraints(trial)):
            raise ValueError("infeasible trial cannot consume final holdout")
        assert_development_unsealed(args.storage, resources.dataset_id,
            [(args.start, args.holdout_start - timedelta(seconds=args.purge_seconds))])
        # Claim BEFORE reading holdout, including failed runs. Operational resumes
        # need an explicit new, untouched holdout and study contract.
        claim_holdout(args.storage, resources.dataset_id, args.holdout_start, args.holdout_end,
                      args.study, trial.number)
        study.set_user_attr("holdout_claim", trial.number)
        fold = Fold(args.start, args.holdout_start - timedelta(seconds=args.purge_seconds),
                    args.holdout_start, args.holdout_end)
        profile_data = dict(trial.user_attrs["profile"])
        profile_data["plasticity"] = PlasticityConfig(**profile_data["plasticity"])
        profile = TrainingProfile(**profile_data)
        selected_reward = RiskRewardConfig(**trial.user_attrs["reward"])
        try:
            results = {"learned": [evaluate_fold(resources, fold, replace(profile, seed=seed), selected_reward)
                                   for seed in args.seeds],
                       "frozen_control": [evaluate_fold(resources, fold, replace(profile, seed=seed), selected_reward,
                                                        learning=False) for seed in args.seeds]}
        except AbsoluteSaturationError as error:
            study.set_user_attr("holdout_failure", {"reason": str(error), "audit": error.audit})
            raise
        deltas = [paired_deltas(comparable_metrics(learned), comparable_metrics(frozen))
                  for learned, frozen in zip(results["learned"], results["frozen_control"])]
        results["paired_deltas"] = deltas
        results["seed_summary"] = summarize_seeds(deltas)
        study.set_user_attr("holdout_results", results)
        feasible = all(item["turnover_violations"] == 0 and item["training_saturation_peak"] <= SATURATION_LIMIT
                       and item["absolute_saturation_time_fraction"] <= ABSOLUTE_SATURATION_TIME_LIMIT
                       and item["execution_diagnostics"]["credit_horizon_violations"] == 0
                       and item["unassigned_credit_windows"] == 0 for item in results["learned"])
        study.set_user_attr("holdout_constraints_passed", feasible)
        print(json.dumps({"selected_trial": trial.number, "holdout": results, "constraints_passed": feasible}, indent=2))
        if not feasible:
            raise SystemExit(2)
    else:
        if "holdout_claim" in study.user_attrs:
            raise ValueError("study sealed after holdout access")
        assert_development_unsealed(args.storage, resources.dataset_id,
            [(start, end) for fold in folds for start, end in
             ((fold.train_start, fold.train_end), (fold.validation_start, fold.validation_end))])
        study.optimize(partial(objective, resources=resources, folds=folds, seeds=tuple(args.seeds),
                               fixed_reward=reward, protocol=protocol), n_trials=args.trials, timeout=args.timeout,
                       n_jobs=1, gc_after_trial=True)
        print(json.dumps({"objective_names": [name for name, _ in OBJECTIVE_MAP],
                          "pareto": [{"trial": t.number, "objectives": t.values, "params": t.params,
                                      "fitness": t.user_attrs["fitness"]}
                                     for t in study.best_trials if all(v <= 0 for v in trial_constraints(t))],
                          "holdout": "untouched"}, indent=2))


if __name__ == "__main__":
    main()
