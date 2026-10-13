from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, localcontext
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from coinfly.domain.context import DomainError
from coinfly.research.neural.rule import advance
from coinfly.research.reward import (
    CostAwareController,
    ExecutionCost,
    ObservationEvidence,
    RewardWindow,
    TakerFees,
    evaluate_reward,
)

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def execution(**updates):
    values = dict(fill_id="one", currency="USD", side="BUY", quantity=D(1), reference_price=D(100),
                  fill_price=D(101), decision_at=NOW, reference_at=NOW, reference_available_at=NOW,
                  submitted_at=NOW, filled_at=NOW + timedelta(milliseconds=100),
                  available_at=NOW + timedelta(milliseconds=110),
                  fees=TakerFees(venue="coinbase-advanced", rate=D("0.006"), evidence_id="synthetic-tier",
                                 effective_at=NOW, observed_at=NOW, expires_at=NOW + timedelta(days=1)))
    return ExecutionCost(**(values | updates))


def window(**updates):
    values = dict(start_at=NOW, end_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1),
                  currency="USD", pnl=D("1.5"), capital=D(1000), basis="gross_reference",
                  executions=(execution(),))
    return RewardWindow(**(values | updates))


def evidence(**updates):
    return ObservationEvidence(**(dict(decision_at=NOW + timedelta(seconds=2), features_event_through=NOW,
                                        features_available_through=NOW, preprocessing_fitted_through=NOW,
                                        partition="train") | updates))


def test_fees_and_delayed_fill_price_turn_gross_gain_into_net_loss_exactly():
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = evaluate_reward(window())
    assert result.net_pnl == D("-0.106")
    assert result.fees == D("0.606")
    assert result.execution_shortfall == 1
    assert result.maximum_latency_ms == 100
    assert result.normalized_reward == D("-0.000106")
    assert result.stimulus == "aversive"


def test_booked_net_pnl_does_not_double_charge_and_actual_fee_overrides_tier():
    result = evaluate_reward(window(basis="net_execution", pnl=D("-0.106"),
                                    executions=(execution(actual_fee=D("0.5")),)))
    assert result.net_pnl == D("-0.106")
    assert result.fees == D("0.5")
    assert result.estimated_fee_count == 0


def test_sell_price_improvement_and_roundtrip_fees_are_accounted_once():
    buy, sell = execution(), execution(fill_id="two", side="SELL", actual_fee=D("0.2"))
    result = evaluate_reward(window(executions=(buy, sell)))
    assert result.execution_shortfall == 0
    assert result.fees == D("0.806")
    assert result.net_pnl == D("0.694")


@pytest.mark.parametrize("change", [dict(quantity=0.1), dict(reference_available_at=NOW + timedelta(seconds=1)),
                                   dict(filled_at=NOW.replace(tzinfo=None))])
def test_invalid_execution_evidence_rejected(change):
    with pytest.raises(ValidationError):
        execution(**change)


def test_duplicate_costs_currency_mismatch_and_stale_fee_tiers_rejected():
    for changes in (dict(executions=(execution(), execution())), dict(currency="EUR"),
                    dict(basis="net_execution")):
        with pytest.raises(ValidationError):
            window(**changes)
    with pytest.raises(ValidationError):
        execution(submitted_at=NOW - timedelta(days=1))


def wrapped(learning=True):
    observed = []
    def observe(rgb, stimulus):
        observed.append(stimulus)
        return {"side": "HOLD"}
    return CostAwareController(SimpleNamespace(s=SimpleNamespace(learning=learning), observe=observe)), observed


@pytest.mark.parametrize("change", [dict(features_available_through=NOW + timedelta(days=1)),
                                   dict(preprocessing_fitted_through=NOW + timedelta(days=1)),
                                   dict(partition="evaluation")])
def test_information_leakage_rejected_before_neural_state_is_mutated(change):
    wrapper, observed = wrapped()
    with pytest.raises(DomainError, match="INFORMATION_LEAKAGE"):
        wrapper.observe(None, evidence(**change), window())
    assert observed == []
    assert wrapper.leakage_rejections == 1


def test_no_future_reward_or_duplicate_interval_and_fixed_decoder_remains_unchanged():
    wrapper, observed = wrapped()
    with pytest.raises(DomainError):
        wrapper.observe(None, evidence(decision_at=NOW), window())
    result = wrapper.observe(None, evidence(), window())
    assert result["side"] == "HOLD"
    assert observed == ["aversive"]
    assert result["exploration"]["action_counts"]["HOLD"] == 1
    assert result["causality"]["reward_delay_microseconds"] == 1000000
    with pytest.raises(DomainError):
        wrapper.observe(None, evidence(decision_at=NOW + timedelta(seconds=3)), window())
    assert observed == ["aversive"]


def test_evaluation_requires_frozen_weights_and_pre_split_preprocessing():
    wrapper, observed = wrapped(False)
    wrapper.observe(None, evidence(partition="evaluation", evaluation_start=NOW + timedelta(seconds=1)))
    assert observed == ["none"]


def test_plasticity_diagnostics_detect_preclip_explosion_and_nonfinite_drive():
    def zeros():
        return np.zeros(1, dtype=np.float64)
    k, d, u, w = zeros(), zeros(), zeros(), zeros()
    d[:] = 1000
    metrics = {}
    advance(k, d, u, w, np.array([1000.]), zeros(), np.ones((1, 1)), .01, 100., diagnostics=metrics)
    assert metrics["maximum_candidate_delta"] > 1
    assert metrics["clipped_candidates"] > 0
    assert np.all(u <= 1) and np.all(w <= 1)
    with pytest.raises(ValueError, match="Nonfinite"):
        advance(k, d, u, w, np.array([1e308]), zeros(), np.ones((1, 1)), .01, 1e308, diagnostics=metrics)
    assert metrics["nonfinite_updates"] == 1


def test_diagnostics_do_not_change_plasticity_numerics():
    rng = np.random.default_rng(81)
    plain = [np.zeros(8), np.zeros(2), np.zeros(8), np.zeros(8)]
    tracked = [state.copy() for state in plain]
    gain = rng.uniform(0, 2, (2, 8))
    diagnostics = {}
    for _ in range(200):
        kc, dan = rng.uniform(0, 500, 8), rng.uniform(0, 500, 2)
        advance(*plain, kc, dan, gain, .01, .001)
        advance(*tracked, kc, dan, gain, .01, .001, diagnostics=diagnostics)
        for original, instrumented in zip(plain, tracked):
            np.testing.assert_array_equal(original, instrumented)
    assert diagnostics["bins"] == 200


def test_microfill_costs_and_tiny_normalized_rewards_keep_exact_digits():
    item = execution(quantity=D("1e-18"), multiplier=D("1e-18"),
                     reference_price=D("1e-18"), fill_price=D("1e-18"))
    result = evaluate_reward(window(pnl=D(0), executions=(item,)))
    assert result.fees == D("6e-57")
    assert result.normalized_reward == D("-6e-60")
