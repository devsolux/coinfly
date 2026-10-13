"""Reward sign/route, credit boundaries and fitted-scale lineage on synthetic fixtures."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from coinfly.research.causal_data import CausalFrameLoader
from coinfly.research.neural import circuit
from coinfly.research.neural.experimental_plasticity import (
    EligibilityBank,
    ExperimentalPlasticity,
    PlasticityConfig,
)
from coinfly.research.risk_reward import risk_signal
from coinfly.research.vectorization import TickScale
from tests.research.test_credit_homeostasis import NOW
from tests.research.test_training_architecture import feature_table


def synthetic_circuit():
    types = ["PAM11"] * 15 + ["PPL101"] * 2 + ["MBON07"] * 4 + ["MBON11"] * 2 + ["KC"] * 2
    annotation = pd.DataFrame({"type": types, "instance": ["synthetic"] * len(types)})
    ptr, post, weight = [0], [], []
    for i in range(len(types)):
        targets = range(17, 21) if i < 15 else range(21, 23) if i < 17 else range(17, 23) if i >= 23 else [i]
        for target in targets:
            post.append(target)
            weight.append(1. if i < 17 else .001)
        ptr.append(len(post))
    brain = SimpleNamespace(n=len(types), ids=np.arange(len(types), dtype=np.int64), ptr=np.array(ptr, np.int64),
                            post=np.array(post, np.int32), weight=np.array(weight, np.float32),
                            paths=SimpleNamespace(annotations=Path("synthetic-not-read.feather")))
    return brain, annotation


def test_each_dopamine_route_projects_only_onto_its_own_compartment(monkeypatch):
    brain, annotation = synthetic_circuit()
    monkeypatch.setattr(circuit, "annotations", lambda *_: annotation)
    result = circuit.identify(brain)
    reward_rows = np.isin(result["dan"], result["reward"])
    aversive_rows = np.isin(result["dan"], result["aversive"])
    reward_edges = np.isin(brain.post[result["edges"]], np.arange(17, 21))
    aversive_edges = np.isin(brain.post[result["edges"]], np.arange(21, 23))
    gain = result["gain"]
    # PAM11 (reward) cells never gain onto MBON11 edges; PPL101 never onto MBON07 edges.
    assert np.all(gain[np.ix_(reward_rows, aversive_edges)] == 0)
    assert np.all(gain[np.ix_(aversive_rows, reward_edges)] == 0)
    assert np.all(gain[np.ix_(reward_rows, reward_edges)].sum(axis=0) == pytest.approx(1))
    assert np.all(gain[np.ix_(aversive_rows, aversive_edges)].sum(axis=0) == pytest.approx(1))


def two_compartment_brain():
    # Edge 0 belongs to the reward compartment (DAN 2), edge 1 to the aversive one (DAN 3).
    return SimpleNamespace(circuit={"edges": np.array([0, 1]), "pre": np.array([0, 0]), "dan": np.array([2, 3]),
                                    "gain": np.array([[1., 0.], [0., 1.]])},
                           post=np.array([1, 1]), eta=.001, memory_u=np.zeros(2), memory_w=np.zeros(2),
                           dan_baseline_hz=np.zeros(2), weight=np.ones(2), baseline_plastic=np.ones(2),
                           weights_frozen=False, plasticity_diagnostics={})


def credited(brain):
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    engine.begin_frame(NOW, (), has_reward=False)
    engine.advance(brain, np.array([1, 0, 0, 0]), .01, learning=True, stimulation=None, delta_limit=.01)
    engine.advance(brain, np.array([0, 1, 0, 0]), .01, learning=True, stimulation=None, delta_limit=.01)
    assert engine.tag[0] < 0 and engine.tag[1] < 0
    engine.bank.store(NOW, engine.tag)
    from coinfly.research.reward import DecisionCredit
    engine.begin_frame(NOW + timedelta(seconds=1), (DecisionCredit(decision_at=NOW, fraction=D(1)),), has_reward=True)
    return engine


@pytest.mark.parametrize("stimulated,touched,untouched", [(2, 0, 1), (3, 1, 0)])
def test_a_pulse_on_one_route_cannot_stimulate_the_other_compartment(stimulated, touched, untouched):
    brain = two_compartment_brain()
    engine = credited(brain)
    engine.advance(brain, np.array([0, 0, 1, 1]), .01, learning=True, stimulation=(np.array([stimulated]), 20),
                   delta_limit=.01)
    assert brain.memory_u[touched] != 0 and brain.memory_w[touched] != 0
    assert brain.memory_u[untouched] == 0 and brain.memory_w[untouched] == 0
    # Both routes observed spikes, but only the stimulated route is projected.
    assert engine._dopamine[touched] > 0 and engine._dopamine[untouched] == 0


def test_valence_is_anatomical_not_a_sign_flip():
    # Documented v1/v2 contract: reward and aversive pulses push their own
    # compartment in the direction of the stored tag; neither inverts the sign.
    outcomes = {}
    for stimulated, edge in ((2, 0), (3, 1)):
        brain = two_compartment_brain()
        engine = credited(brain)
        engine.advance(brain, np.array([0, 0, 1, 1]), .01, learning=True, stimulation=(np.array([stimulated]), 20),
                       delta_limit=.01)
        outcomes[stimulated] = float(brain.memory_u[edge])
    assert outcomes[2] < 0 and outcomes[3] < 0 and outcomes[2] == pytest.approx(outcomes[3])


def test_hold_finalization_releases_the_reservation():
    brain = two_compartment_brain()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    engine.begin_frame(NOW, (), has_reward=False)
    assert engine.finalize_frame(NOW, "HOLD")
    with pytest.raises(ValueError, match="unreserved"):
        engine.bank.store(NOW, engine.tag)
    assert len(engine.bank) == 0


def test_full_ring_rejects_new_orders_without_evicting_live_tags():
    bank = EligibilityBank(2, replace(PlasticityConfig(), maximum_tags=2))
    for index in range(2):
        assert bank.reserve(NOW + timedelta(seconds=index))
        bank.store(NOW + timedelta(seconds=index), np.array([.1, .2]))
    assert not bank.reserve(NOW + timedelta(seconds=2)) and len(bank) == 2 and bank.capacity_rejections == 1
    assert sorted(bank.tags) == [NOW, NOW + timedelta(seconds=1)]


def test_nonfinite_shaped_signal_fails_instead_of_clipping_to_maximum_reward():
    history = np.zeros(4)
    with pytest.raises(ValueError, match="nonfinite"):
        risk_signal(history, 4, float("nan"), 1e-4, .75, 4., 0., .05, .1)
    finite = risk_signal(history, 4, .001, 1e-4, .75, 4., 0., .05, .1)
    assert -1 <= finite[0] <= 1


def test_tick_scale_lineage_rejects_scales_fitted_after_the_decision_or_on_holdout():
    fitted_late = (TickScale("price", D("100"), D("0.01"), 100, fitted_through=NOW + timedelta(seconds=1)),)
    with pytest.raises(ValueError, match="fitted after"):
        CausalFrameLoader(fitted_late).frame(feature_table(), decision_at=NOW, partition="train")
    fitted_on_holdout = (TickScale("price", D("100"), D("0.01"), 100, fitted_through=NOW),)
    with pytest.raises(ValueError, match="fitted on holdout"):
        CausalFrameLoader(fitted_on_holdout).frame(feature_table(), decision_at=NOW + timedelta(seconds=2),
                                                   partition="evaluation", evaluation_start=NOW)
    honest = (TickScale("price", D("100"), D("0.01"), 100, fitted_through=NOW - timedelta(days=1)),)
    audited = CausalFrameLoader(honest).frame(feature_table(), decision_at=NOW, partition="train")
    assert audited.evidence.preprocessing_fitted_through == NOW  # the later of table and scale lineage
    with pytest.raises(ValueError, match="aware"):
        TickScale("price", D("100"), D("0.01"), 100, fitted_through=NOW.replace(tzinfo=None))


def test_reinforcement_gain_is_a_bounded_positive_scalar_selected_only_from_completed_evidence():
    from coinfly.research.neural.experimental_plasticity import reinforcement_gain_kernel

    rng = np.random.default_rng(20260921)
    for _ in range(2000):
        signal = float(rng.uniform(-1, 1))
        net = float(rng.normal(0, .002))
        stressed = net - abs(float(rng.normal(0, .001)))
        fee = abs(float(rng.normal(0, .002)))
        threshold, positive_gain, fee_gain = .001, 2., .25
        gain = reinforcement_gain_kernel(signal, net, stressed, fee, threshold, positive_gain, fee_gain)
        assert gain in (positive_gain, fee_gain, 1.) and gain > 0
        expected = (positive_gain if signal > 0 and net > 0 and stressed >= threshold
                    else fee_gain if signal < 0 and -fee < net < 0 and -fee < stressed < 0 else 1.)
        assert gain == expected
        # A damped or boosted eta never changes which route or sign the pulse carries.
        assert np.sign(signal * gain) == np.sign(signal)


def test_shaped_signal_sign_follows_booked_sign_without_turnover_and_is_measured_otherwise():
    rng = np.random.default_rng(7)
    history = rng.normal(0, 1e-3, 64)
    for _ in range(500):
        current = float(rng.normal(0, 1e-3))
        signal = risk_signal(history, 64, current, 1e-4, .75, 4., 0., .05, .1)[0]
        assert np.sign(signal) == np.sign(current)
    # With turnover the regularizer can flip a small profit to an aversive route;
    # v3 records this as sign_disagreement_fraction instead of hiding it.
    flipped = risk_signal(history, 64, .0005, 1e-4, .75, 16., .03, .05, 5.)[0]
    assert flipped < 0


def test_rate_quantum_of_ten_millisecond_bins_documents_inert_dan_threshold_and_binary_rates():
    """Measured property, not a target: with 10 ms bins, observed rates are multiples of 100 Hz.

    Any threshold below 100 Hz never changes which DAN counts as active, and
    rate_scale_hz=100 saturates p/q at a single spike. Calibrating these needs
    development data on the qualified graph; this test only pins the arithmetic
    so the study documentation stays honest if bin size or scale ever change.
    """
    from coinfly.research.profile import NeuralProfile

    bin_seconds = NeuralProfile().neural_bin_ms / 1000
    rates = {spikes / bin_seconds for spikes in range(4)}
    assert rates == {0., 100., 200., 300.}
    assert all((max(rate - threshold, 0.) > 0) == (rate > 0) for rate in rates for threshold in (0., 12.5, 25.))
    assert all(min(rate / PlasticityConfig().rate_scale_hz, 1.) in (0., 1.) for rate in rates)
