"""Synthetic causal clocks and ledgers; no profitability or graph qualification."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from decimal import Inexact, localcontext

import numpy as np
import optuna
import pytest

from coinfly.research.fee_presets import preset_fees
from coinfly.research.neural.experimental_plasticity import (
    EligibilityBank,
    ExperimentalPlasticity,
    PlasticityConfig,
    capture_kernel,
    expired_prefix,
)
from coinfly.research.optimization import (
    ExecutionDiagnostics,
    _phase,
    analyze_plasticity,
    objective,
    trial_constraints,
)
from coinfly.research.reward import DecisionCredit
from coinfly.research.risk_reward import RiskRewardConfig
from coinfly.research.turnover import PaperTurnoverReservations, ReservationReport, quote_reservation
from tests.research.test_credit_homeostasis import NOW, credited_engine, pulse, synthetic_brain
from tests.research.test_training_architecture import (
    frame,
    outcome,
    synthetic_controller,
    synthetic_resources,
)


def test_ring_preserves_exact_age_boundary_and_reuses_expired_storage():
    bank = EligibilityBank(2, replace(PlasticityConfig(), maximum_tags=1))
    backing = bank._values
    assert bank.reserve(NOW)
    bank.store(NOW, np.array([.2, -.2]))
    original = bank.tags[NOW]
    assert not bank.reserve(NOW + timedelta(seconds=150))
    now = NOW + timedelta(seconds=150, microseconds=1)
    assert bank.reserve(now) and bank.expired_tags == 1
    bank.store(now, np.array([.3, -.3]))
    assert bank._values is backing and bank.capacity_rejections == 1
    np.testing.assert_array_equal(original, [.2, -.2])
    assert expired_prefix.nopython_signatures


def test_hft_ring_remains_bounded_without_live_eviction_or_session_abort():
    bank = EligibilityBank(4, replace(PlasticityConfig(), maximum_tags=32,
        maximum_credit_age_seconds=1, credit_tau_seconds=.5))
    storage = bank._values
    for index in range(10000):
        now = NOW + timedelta(milliseconds=index)
        if bank.reserve(now):
            bank.store(now, np.full(4, .1))
        assert len(bank) <= 32 and bank._values is storage
    assert bank.expired_tags > 0 and bank.capacity_rejections > 9000
    assert storage.nbytes == 32 * 4 * 8


def test_clock_reversal_and_unreserved_store_do_not_mutate_live_tags():
    bank = EligibilityBank(1, PlasticityConfig())
    bank.reserve(NOW)
    bank.store(NOW, np.array([.3]))
    for wrong in (NOW, NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            bank.reserve(wrong)
    with pytest.raises(ValueError):
        bank.store(NOW + timedelta(seconds=1), np.array([.4]))
    assert len(bank) == 1 and bank.tags[NOW][0] == .3


def test_capacity_backpressure_keeps_propagation_credit_and_fixed_decoder():
    controller, seen = synthetic_controller()
    controller.profile = replace(controller.profile, plasticity=replace(PlasticityConfig(), maximum_tags=1))
    controller.plasticity = ExperimentalPlasticity(controller.brain, controller.profile.plasticity)
    controller.decoder.decode = lambda *_: {"side": "BUY"}  # explicitly synthetic decoder
    first = controller.run_frame(frame())
    before = len(seen)
    later = controller.run_frame(frame(1), outcome(credits=(DecisionCredit(decision_at=NOW, fraction=D(1)),)))
    assert first["execution_admission"]["allowed"] and later["side"] == "BUY"
    assert later["execution_admission"] == {"allowed": False, "reason": "eligibility_capacity"}
    assert len(seen) > before and not controller._failed
    assert later["training"]["eligibility"]["retained_credit_fraction"] == 1
    assert any(kwargs["stimulation"] is not None for _, kwargs in seen[before:])


def test_phase_passes_denial_to_execution_guard_without_replacement_action():
    controller, _ = synthetic_controller()
    controller.plasticity = ExperimentalPlasticity(controller.brain, replace(PlasticityConfig(), maximum_tags=1))
    controller.decoder.decode = lambda *_: {"side": "BUY"}
    class Environment:
        execution_realm = "paper"
        def __init__(self):
            self.events = []
        def stream(self):
            yield frame(), None
            yield frame(1), outcome(0, pnl="0")
        def execute(self, side, at):
            self.events.append(("execute", side))
        def reject(self, side, at, reason):
            self.events.append((reason, side))
        def finish(self):
            return outcome(1, pnl="0")
    environment = Environment()
    result = _phase(controller, environment, start=NOW, end=NOW + timedelta(seconds=2),
                    partition="train", max_frames=2)
    assert result.capacity_rejections == 1
    assert environment.events == [("execute", "BUY"), ("eligibility_capacity", "BUY")]


@pytest.mark.parametrize("signal,net,fee,expected", [
    (1., .002, .001, 2.), (1., .0001, .001, 1.),
    (-1., -.001, .002, .25), (-1., -.003, .002, 1.),
    (-1., -.001, 0., 1.), (-1., .001, .002, 1.), (0., .01, .001, 1.),
])
def test_gain_only_uses_completed_return_and_fee_evidence(signal, net, fee, expected):
    engine = ExperimentalPlasticity(synthetic_brain(), replace(PlasticityConfig(), positive_gain=2, fee_shock_gain=.25))
    engine.set_reinforcement(signal, net, fee, net_return=net)
    assert engine.reinforcement_gain == expected


def test_dan_threshold_suppresses_capture_without_erasing_the_past_tag():
    brain, engine = credited_engine()
    engine.config = replace(engine.config, dan_threshold_hz=100)
    assert engine.credit[0] != 0
    pulse(engine, brain, [0, 0, 1], stimulated=True)
    assert brain.memory_u[0] == 0 and brain.plasticity_diagnostics["capture_active_bins"] == 0


def test_eta_gain_changes_capture_magnitude_without_changing_route_or_bound():
    first_brain, first = credited_engine()
    second_brain, second = credited_engine()
    second.config = replace(second.config, positive_gain=2)
    second.set_reinforcement(1., .01, .001, net_return=.01)
    pulse(first, first_brain, [0, 0, 1], stimulated=True)
    pulse(second, second_brain, [0, 0, 1], stimulated=True)
    assert second_brain.memory_u[0] == pytest.approx(2 * first_brain.memory_u[0])
    assert second_brain.plasticity_diagnostics["effective_eta"] == .002


def test_homeostasis_cap_prevents_single_bin_erasure_under_extreme_lambda():
    u, w, zeros = np.ones(2), np.ones(2), np.zeros(2)
    for _ in range(100):
        result = capture_kernel(u, w, zeros, zeros, .01, .001, 10., 10., .95, .1, .02)
        assert result[6] <= 1 - .98**.01 + 1e-15
        assert result[7] > result[1]
    assert u[0] == pytest.approx(.98)  # exactly 2% passive latent loss per neural second


def test_epoch_analyzer_measures_saturation_separately_from_erasure():
    brain = synthetic_brain()
    brain.memory_u.fill(1)
    brain.memory_w.fill(1)
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    pulse(engine, brain, [0, 0, 0])
    audit = analyze_plasticity(brain.plasticity_diagnostics, learning=True)
    assert audit["absolute_saturation_time_fraction"] == 1
    assert audit["severe_saturation_time_fraction"] == 1
    assert audit["homeostatic_erasure_rate"] > 0
    for changed in ({}, {**brain.plasticity_diagnostics, "observed_neural_seconds": float("nan")},
                    {**brain.plasticity_diagnostics, "maximum_bin_homeostatic_loss": .9}):
        with pytest.raises(ValueError):
            analyze_plasticity(changed, learning=True)


@pytest.mark.parametrize("duration,feasible", [(.15, True), (.150001, False)])
def test_optuna_epoch_saturation_boundary_and_temporal_search(monkeypatch, duration, feasible):
    profiles = []
    def evaluation(resources, fold, profile, reward):
        profiles.append(profile)
        return {"sortino": .1, "maximum_drawdown": .2, "training_saturation_mean": .001,
                "training_saturation_peak": .005, "homeostatic_erasure_rate": .001,
                "absolute_saturation_time_fraction": duration, "turnover_violations": 0,
                "unassigned_credit_windows": 0, "execution_diagnostics": ExecutionDiagnostics().report()}
    monkeypatch.setattr("coinfly.research.optimization.evaluate_fold", evaluation)
    study = optuna.create_study(directions=["maximize", "minimize", "minimize", "minimize"],
                               sampler=optuna.samplers.TPESampler(seed=2))
    study.optimize(lambda trial: objective(trial, synthetic_resources(), ("synthetic",), (1,), RiskRewardConfig()), n_trials=2)
    for trial, profile in zip(study.trials, profiles):
        if feasible:
            assert trial.state == optuna.trial.TrialState.COMPLETE
            assert all(value <= 0 for value in trial_constraints(trial))
        else:
            assert trial.state == optuna.trial.TrialState.PRUNED and trial.values is None
            assert trial.user_attrs["absolute_saturation_audit"]["absolute_saturation_time_fraction"] == duration
        p = profile.plasticity
        assert .02 <= p.neural_trace_seconds <= 5
        assert .5 <= p.credit_tau_seconds <= 150
        assert p.credit_tau_seconds <= p.maximum_credit_age_seconds <= 150
        assert 0 <= p.dan_threshold_hz <= 25 and .25 <= p.fee_shock_gain <= 1
        assert p.maximum_homeostatic_loss_per_second == .02


def quote(quantity="0.49", price="100", *, now=NOW, volume="0"):
    return quote_reservation(quantity=D(quantity), price_ceiling=D(price),
        preset_id="exchange-standard-2026-09-20", trailing_volume_usd=D(volume),
        observed_at=now, expires_at=now + timedelta(days=1), now=now)


def report(status, quantity="0", notional="0", fees="0", *, sequence=1, seconds=1):
    return ReservationReport(order_id="one", status=status, sequence=sequence,
        event_at=NOW + timedelta(seconds=seconds), available_at=NOW + timedelta(seconds=seconds),
        filled_quantity=D(quantity), filled_notional=D(notional), actual_fees=D(fees))


def book():
    return PaperTurnoverReservations(D(1000), NOW, NOW + timedelta(seconds=60))


def test_partial_fills_recalculate_remaining_notional_and_terminal_evidence_releases_it():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        ledger = book()
        assert ledger.reserve("one", quote(), now=NOW)
        assert not ledger.reserve("equality", quote(".01"), now=NOW)  # 49 + 1 == 50
        partial = report("partial", ".2", "19", ".114")
        ledger.reconcile(partial, now=partial.available_at)
        assert ledger.totals()["spent_notional"] == 19
        assert ledger.totals()["reserved_notional"] == 29
        assert ledger.reserve("two", quote(".019999999999999999"), now=partial.available_at)
        canceled = report("canceled", ".2", "19", ".114", sequence=2, seconds=2)
        ledger.reconcile(canceled, now=canceled.available_at)
        assert ledger.totals()["reserved_notional"] == D("1.999999999999999900")
        assert ledger.totals()["actual_fees"] == D(".114")


def test_turnover_reservation_excludes_fees_but_keeps_exact_fee_funding_separate():
    ledger = book()
    value = quote(".4999")
    assert value.notional == D("49.9900") and value.notional + value.fee_reserve > 50
    assert ledger.reserve("one", value, now=NOW)
    assert ledger.totals()["reserved_fees"] == D(".29994")


def test_unknown_outcomes_do_not_expire_or_release_without_terminal_evidence():
    ledger = book()
    ledger.reserve("one", quote(), now=NOW)
    unknown = report("unknown")
    ledger.reconcile(unknown, now=unknown.available_at)
    assert not ledger.reserve("two", quote(".001"), now=NOW + timedelta(seconds=59))
    assert ledger.totals()["reserved_notional"] == 49
    with pytest.raises(ValueError, match="unresolved"):
        ledger.close(now=NOW + timedelta(seconds=60))
    done = report("canceled", sequence=2, seconds=60)
    ledger.reconcile(done, now=done.available_at)
    assert ledger.close(now=done.available_at)["reserved_notional"] == 0


def test_fee_refresh_uses_current_evidence_without_rewriting_booked_fees_or_order_age():
    ledger = book()
    ledger.reserve("one", quote(), now=NOW)
    partial = report("partial", ".2", "19", ".114")
    ledger.reconcile(partial, now=partial.available_at)
    new = preset_fees("exchange-standard-2026-09-20", trailing_volume_usd=D(10000),
                      observed_at=NOW + timedelta(seconds=2), expires_at=NOW + timedelta(days=1))
    ledger.refresh_fees("one", new, now=NOW + timedelta(seconds=2))
    assert ledger.totals()["reserved_fees"] == D(".116")
    assert ledger.totals()["actual_fees"] == D(".114")
    done = report("canceled", ".2", "19", ".114", sequence=2, seconds=1.5)
    ledger.reconcile(done, now=NOW + timedelta(seconds=3))
    assert ledger.totals()["reserved_notional"] == 0


def test_future_fee_tiers_and_reports_fail_before_changing_reservations():
    ledger = book()
    ledger.reserve("one", quote(), now=NOW)
    before = ledger.totals()
    future = quote(now=NOW + timedelta(seconds=2))
    with pytest.raises(ValueError):
        ledger.reserve("future", future, now=NOW)
    with pytest.raises(ValueError):
        ledger.refresh_fees("one", future.fees, now=NOW)
    with pytest.raises(ValueError):
        ledger.reconcile(report("filled", ".49", "49", ".294"), now=NOW)
    assert ledger.totals() == before


def test_reconciliation_is_idempotent_and_rejects_unbacked_release_or_price_bounds():
    ledger = book()
    ledger.reserve("one", quote(), now=NOW)
    partial = report("partial", ".2", "19", ".114")
    ledger.reconcile(partial, now=partial.available_at)
    ledger.reconcile(partial, now=partial.available_at)
    before = ledger.totals()
    for bad in (report("filled", ".2", "19", ".114", sequence=2, seconds=2),
                report("partial", ".3", "40", ".2", sequence=2, seconds=2),
                report("open", sequence=2, seconds=2),
                report("canceled", ".2", "19", ".114")):
        with pytest.raises(ValueError):
            ledger.reconcile(bad, now=NOW + timedelta(seconds=2))
        assert ledger.totals() == before


def test_concurrent_reservations_cannot_over_admit_exact_budget():
    ledger = PaperTurnoverReservations(D(10000), NOW, NOW + timedelta(seconds=60))
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda i: ledger.reserve(str(i), quote(".1"), now=NOW), range(100)))
    assert sum(admitted) == 49 and ledger.totals()["reserved_notional"] == 490


def test_late_terminal_ack_releases_remainder_without_misbooking_cross_interval_fills():
    ledger = book()
    ledger.reserve("one", quote(), now=NOW)
    partial = report("partial", ".2", "19", ".114")
    ledger.reconcile(partial, now=partial.available_at)
    later_fill = report("filled", ".49", "48", ".288", sequence=2, seconds=61)
    with pytest.raises(ValueError, match="later accounting interval"):
        ledger.reconcile(later_fill, now=later_fill.available_at)
    terminal = report("canceled", ".2", "19", ".114", sequence=2, seconds=61)
    ledger.reconcile(terminal, now=terminal.available_at)
    assert ledger.close(now=terminal.available_at)["reserved_notional"] == 0


def test_fractional_microsecond_age_uses_same_boundary_for_purge_and_capture():
    cfg = replace(PlasticityConfig(), maximum_credit_age_seconds=1.0000006, credit_tau_seconds=.5)
    bank = EligibilityBank(1, cfg)
    bank.reserve(NOW)
    bank.store(NOW, np.array([.2]))
    now = NOW + timedelta(microseconds=1000001)
    assert bank.purge_expired(now) == 1
    tag, metrics = bank.capture(now, (DecisionCredit(decision_at=NOW, fraction=D(1)),))
    assert tag[0] == 0 and metrics["expired_credit_decisions"] == 1
