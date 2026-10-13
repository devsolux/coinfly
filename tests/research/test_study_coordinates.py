"""Every sampled Optuna coordinate reaches its runtime value, inside the accepted config domain."""

import math
from dataclasses import asdict, fields

import pytest

from coinfly.research.neural.experimental_plasticity import PlasticityConfig
from coinfly.research.optimization import REINFORCEMENT_COORDINATES, ExecutionDiagnostics, objective
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.training import TrainingProfile
from tests.research.test_training_architecture import synthetic_resources


class RecordingTrial:
    """Deterministic stand-in: returns the geometric/arithmetic midpoint of each range."""

    def __init__(self):
        self.params, self.bounds, self.user_attrs, self.constraints = {}, {}, {}, {}

    def suggest_float(self, name, low, high, *, log=False, step=None):
        assert name not in self.params, f"{name} sampled twice"
        assert low <= high, f"{name}: empty range"
        value = math.sqrt(low * high) if log and low > 0 else (low + high) / 2
        self.params[name], self.bounds[name] = value, (low, high, log)
        return value

    def suggest_int(self, name, low, high, *, log=False, step=1):
        assert name not in self.params
        value = int(round(math.sqrt(low * high))) if log else (low + high) // 2
        self.params[name], self.bounds[name] = value, (low, high, log)
        return value

    def suggest_categorical(self, name, choices):
        assert name not in self.params
        self.params[name], self.bounds[name] = choices[len(choices) // 2], (tuple(choices), None, None)
        return self.params[name]

    def set_user_attr(self, key, value):
        self.user_attrs[key] = value

    def set_constraint(self, key, value):
        self.constraints[key] = value


def evaluation():
    return {"sortino": .1, "maximum_drawdown": .1, "net_return": 0., "turnover_violations": 0,
            "training_saturation_peak": 0, "training_saturation_mean": 0, "unassigned_credit_windows": 0,
            "absolute_saturation_time_fraction": 0, "homeostatic_erasure_rate": 0,
            "execution_diagnostics": ExecutionDiagnostics().report()}


def test_every_coordinate_reaches_the_profile_or_reward_actually_evaluated(monkeypatch):
    seen = []
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold",
                        lambda resources, fold, profile, reward, **kw: seen.append((profile, reward)) or evaluation())
    trial = RecordingTrial()
    objective(trial, synthetic_resources(60.), ("fold",), (1,), RiskRewardConfig(interval_seconds=60.))
    profile, reward = seen[0]
    plasticity = asdict(profile.plasticity)
    routed = {
        "positive_return_threshold": plasticity["positive_return_threshold"], "positive_gain": plasticity["positive_gain"],
        "fee_shock_gain": plasticity["fee_shock_gain"], "credit_tau_seconds": plasticity["credit_tau_seconds"],
        "maximum_credit_age_seconds": plasticity["maximum_credit_age_seconds"],
        "neural_trace_seconds": plasticity["neural_trace_seconds"], "dan_threshold_hz": plasticity["dan_threshold_hz"],
        "eta_scale": plasticity["eta_scale"], "homeostasis_per_second": plasticity["homeostasis_per_second"],
        "saturation_decay_per_second": plasticity["saturation_decay_per_second"],
        "sensory_noise": profile.sensory_noise, "exploration_decay_frames": profile.exploration_decay_frames,
        "plasticity_delta_limit": profile.plasticity_delta_limit, "volatility_target": profile.volatility_target,
        "risk_window": reward.window, "alpha": reward.sortino_weight, "signal_scale": reward.signal_scale,
        "turnover_penalty": reward.turnover_penalty,
    }
    assert set(routed) == set(trial.params), "a sampled coordinate is unused or a runtime value is unsampled"
    for name, value in routed.items():
        assert value == trial.params[name], name
    # The reinforcement coordinates advertised in the contract are the ones sampled, with the same bounds.
    for name, low, high, log in REINFORCEMENT_COORDINATES:
        sampled_low, sampled_high, sampled_log = trial.bounds[name]
        assert sampled_high == high and sampled_log == log
        assert sampled_low == (max(low, trial.params["credit_tau_seconds"], 60.) if name == "maximum_credit_age_seconds" else low)
    # Fees, latency assumptions, decoder and topology stay outside the search.
    assert not {"fees", "latency", "decoder_threshold_hz", "neural_bin_ms", "rate_scale_hz"} & set(trial.params)
    assert trial.user_attrs["fitness"]["raw_net_sortino"] == .1 and trial.user_attrs["profile"]["profile_id"].endswith("v2")


@pytest.mark.parametrize("name,low,high,log", REINFORCEMENT_COORDINATES)
def test_sampler_bounds_lie_inside_the_accepted_plasticity_domain(name, low, high, log):
    base = PlasticityConfig()
    for value in (low, high):
        overrides = {name: value}
        if name == "credit_tau_seconds":
            overrides["maximum_credit_age_seconds"] = max(value, base.maximum_credit_age_seconds)
        if name == "maximum_credit_age_seconds":
            overrides["credit_tau_seconds"] = min(value, base.credit_tau_seconds)
        PlasticityConfig(**{**asdict(base), **overrides})  # must not raise at either bound
    assert {field.name for field in fields(PlasticityConfig)} >= {name}


def test_profile_bounds_match_the_sampled_exploration_and_trust_region_ranges():
    for noise in (0., 8.):
        TrainingProfile(sensory_noise=noise)
    for frames in (1000, 100000):
        TrainingProfile(exploration_decay_frames=frames)
    for limit in (1e-5, 1e-2):
        TrainingProfile(plasticity_delta_limit=limit)
    for target in (1e-4, 1e-2):
        TrainingProfile(volatility_target=target)
    for window in (64, 1024):
        RiskRewardConfig(window=window)
    RiskRewardConfig(sortino_weight=.25, signal_scale=1., turnover_penalty=.05)
    RiskRewardConfig(sortino_weight=.95, signal_scale=16., turnover_penalty=5.)
