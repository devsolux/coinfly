"""Scalar oracles and numerical boundaries for the experimental RSTDP hypothesis."""

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, localcontext
from types import SimpleNamespace

import numpy as np
import pytest

from coinfly.research.neural.experimental_plasticity import (
    EligibilityBank,
    ExperimentalPlasticity,
    PlasticityConfig,
    capture_kernel,
    credit_kernel,
    plasticity_kernel,
    store_kernel,
    tag_kernel,
)
from coinfly.research.reward import DecisionCredit

NOW = datetime(9998, 1, 1, tzinfo=timezone.utc)  # microseconds exceed exact float64 integers
TINY = np.nextafter(0., 1.)


def test_tag_uses_new_traces_and_matches_high_precision_oracle():
    pre, post, tag = np.array([.8]), np.array([.2]), np.array([-.3])
    with localcontext() as context:
        context.prec = 80
        d = (-D('.01') / D('.3')).exp()
        p = d * D('.8') + (1 - d) * D('.1')
        q = d * D('.2') + (1 - d) * D('.7')
        expected = d * D('-.3') + (1 - d) * (D('.1') * q - D('.7') * p)
    tag_kernel(pre, post, tag, np.array([10.]), np.array([70.]), .01, .3, 100.)
    np.testing.assert_allclose([pre[0], post[0], tag[0]], [float(p), float(q), float(expected)],
                               rtol=3e-16, atol=1e-16)


@pytest.mark.parametrize('seconds', [TINY, np.finfo(float).tiny, 1e-16, .01, 10., np.finfo(float).max])
@pytest.mark.parametrize('initial', [-1., -TINY, 0., TINY, 1.])
def test_silent_tag_decay_at_float64_boundaries(seconds, initial):
    pre, post, tag = np.array([1.]), np.array([TINY]), np.array([initial])
    rates = np.zeros(1)
    tag_kernel(pre, post, tag, rates, rates, seconds, 1., 100.)
    decay = math.exp(-seconds)
    assert tag[0] == initial * decay
    assert pre[0] == decay and post[0] == TINY * decay
    assert np.isfinite(tag).all() and abs(tag[0]) <= abs(initial)


@pytest.mark.parametrize('scale', [TINY, 100., np.finfo(float).max])
def test_extreme_finite_rates_normalize_without_overflow(scale):
    pre, post, tag = np.zeros(2), np.zeros(2), np.zeros(2)
    maximum = np.finfo(float).max
    rates = np.array([maximum, TINY])
    tag_kernel(pre, post, tag, rates, rates, .01, 1., scale)
    assert np.isfinite(pre).all() and np.all((pre >= 0) & (pre <= 1))
    np.testing.assert_array_equal(pre, post)
    np.testing.assert_array_equal(tag, [0., 0.])


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf, -TINY])
def test_invalid_last_rate_does_not_partially_update_tags(bad):
    pre, post, tag = np.array([.3, .4]), np.array([.4, .2]), np.array([-.2, .1])
    before = [a.copy() for a in (pre, post, tag)]
    with pytest.raises(ValueError):
        tag_kernel(pre, post, tag, np.array([1., bad]), np.ones(2), .01, 1., 100.)
    for expected, actual in zip(before, (pre, post, tag)):
        np.testing.assert_array_equal(actual, expected)


def brain_fixture(dtype=np.float64):
    weights = np.array([7., -2., 11., 3., 13., .4, 17.], dtype=dtype)
    return SimpleNamespace(
        circuit={'edges': np.array([1, 3, 5]), 'pre': np.array([0, 1, 0]),
                 'dan': np.array([5, 6]), 'gain': np.array([[1., .25, 0.], [0., .75, 1.]], dtype=np.float32)},
        post=np.array([2, 2, 3, 3, 4, 4, 2]), weight=weights,
        baseline_plastic=weights[[1, 3, 5]].copy(), dan_baseline_hz=np.array([10., 20.]),
        memory_u=np.array([.99, -.88, .2]), memory_w=np.array([1., -.9, -.2]),
        eta=.1, weights_frozen=False, plasticity_diagnostics={})


def reference_bin(brain, engine, counts, h, stimulation, limit):
    """Independent scalar form of the equations, including the existing step cap."""
    cfg = engine.config
    p, q, tags, u, w = [a.copy() for a in
                       (engine.pre_trace, engine.post_trace, engine.tag, brain.memory_u, brain.memory_w)]
    near = sum(a <= -.9 * cfg.near_bound_fraction or a >= cfg.near_bound_fraction
               or b <= -.9 * cfg.near_bound_fraction or b >= cfg.near_bound_fraction for a, b in zip(u, w)) / len(u)
    rate = min(cfg.homeostasis_per_second + cfg.saturation_decay_per_second * near,
               -math.log1p(-cfg.maximum_homeostatic_loss_per_second))
    decay = math.exp(-rate * h)
    d, fraction = math.exp(-h / cfg.neural_trace_seconds), -math.expm1(-h / cfg.neural_trace_seconds)
    smooth = -math.expm1(-h / .05)
    limit *= h / .01
    def clip(value, low, high):
        return max(low, min(value, high))
    for i, edge in enumerate(brain.circuit['edges']):
        pre = min(counts[brain.circuit['pre'][i]] / h / cfg.rate_scale_hz, 1.)
        post = min(counts[brain.post[edge]] / h / cfg.rate_scale_hz, 1.)
        p[i], q[i] = d * p[i] + fraction * pre, d * q[i] + fraction * post
        tags[i] = clip(d * tags[i] + fraction * (pre * q[i] - post * p[i]), -1., 1.)
        dan = sum(float(brain.circuit['gain'][j, i]) * max(counts[neuron] / h
                  - brain.dan_baseline_hz[j] - cfg.dan_threshold_hz, 0.)
                  for j, neuron in enumerate(brain.circuit['dan']) if neuron in stimulation)
        drive = brain.eta * cfg.eta_scale * engine.reinforcement_gain * engine.credit[i] * dan
        headroom = clip(1. - u[i] if drive > 0 else (u[i] + .9) / .9, 0., 1.)
        candidate_u = u[i] * decay + h * drive * headroom
        u[i] = clip(u[i] + clip(candidate_u - u[i], -limit, limit), -.9, 1.)
        relaxed_w = w[i] * decay
        candidate_w = relaxed_w + smooth * (u[i] - relaxed_w)
        w[i] = clip(w[i] + clip(candidate_w - w[i], -limit, limit), -.9, 1.)
    return p, q, tags, u, w


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
@pytest.mark.parametrize('h', [.002, .01])
@pytest.mark.parametrize('stimulation', [(), (5,), (5, 6)])
def test_pipeline_matches_scalar_oracle_and_preserves_structural_signs(dtype, h, stimulation):
    brain = brain_fixture(dtype)
    cfg = replace(PlasticityConfig(), eta_scale=2., positive_gain=3., dan_threshold_hz=5.,
                  homeostasis_per_second=10., saturation_decay_per_second=10.)
    engine = ExperimentalPlasticity(brain, cfg)
    engine.set_reinforcement(1., .01, .001, net_return=.01)
    engine.pre_trace[:] = [.2, .1, .7]
    engine.post_trace[:] = [.5, .8, .3]
    engine.tag[:] = [-.2, .8, .3]
    engine.credit[:] = [.8, -.4, .7]
    unchanged = brain.weight[[0, 2, 4, 6]].copy()
    stimulus = (np.array(stimulation, dtype=np.int64), 20.) if stimulation else None
    rng = np.random.default_rng(738)
    for _ in range(30):
        counts = rng.integers(0, 3, 7, dtype=np.int32)
        expected = reference_bin(brain, engine, counts, h, stimulation, .03)
        engine.advance(brain, counts, h, learning=True, stimulation=stimulus, delta_limit=.03)
        for actual, reference in zip((engine.pre_trace, engine.post_trace, engine.tag,
                                      brain.memory_u, brain.memory_w), expected):
            np.testing.assert_allclose(actual, reference, rtol=1e-14, atol=1e-16)
        np.testing.assert_array_equal(brain.weight[[0, 2, 4, 6]], unchanged)
        np.testing.assert_array_equal(brain.weight[brain.circuit['edges']],
                                      (brain.baseline_plastic * (1 + brain.memory_w)).astype(dtype))
        np.testing.assert_array_equal(np.sign(brain.weight[brain.circuit['edges']]), np.sign(brain.baseline_plastic))
    assert brain.plasticity_diagnostics['trust_region_clipped'] > 0
    assert brain.plasticity_diagnostics['absolute_saturation_seconds'] >= h
    assert brain.plasticity_diagnostics['homeostasis_rate'] <= -math.log1p(-.02)


def test_microsecond_credit_mapping_survives_large_epoch_and_ring_wrap():
    bank = EligibilityBank(2, replace(PlasticityConfig(), maximum_tags=2, credit_tau_seconds=.1,
                                      maximum_credit_age_seconds=.1))
    buffer = bank._values
    for offset, values in ((0, [1., -1.]), (1, [.5, .25])):
        now = NOW + timedelta(microseconds=offset)
        assert bank.reserve(now)
        bank.store(now, np.array(values))
    credits = (DecisionCredit(decision_at=NOW, fraction=D('.25')),
               DecisionCredit(decision_at=NOW + timedelta(microseconds=1), fraction=D('.5')))
    out = np.empty(2)
    metrics = bank.capture_into(NOW + timedelta(microseconds=2), credits, out)
    expected = np.array([1., -1.]) * .25 * math.exp(-.000002 / .1)
    expected += np.array([.5, .25]) * .5 * math.exp(-.000001 / .1)
    np.testing.assert_allclose(out, expected, rtol=0, atol=1e-16)
    assert metrics['retained_credit_fraction'] == .75  # never normalize the missing quarter
    boundary = NOW + timedelta(microseconds=100001)
    assert bank.reserve(boundary) and bank.expired_tags == 1
    bank.store(boundary, np.array([-.5, .5]))
    assert bank._values is buffer
    metrics = bank.capture_into(boundary, credits, out)
    np.testing.assert_allclose(out, np.array([.5, .25]) * .5 * math.exp(-1), rtol=1e-15)
    assert metrics['expired_credit_decisions'] == 1 and metrics['retained_credit_fraction'] == .5


def test_exact_overallocation_rejected_before_frame_or_credit_mutation():
    brain = brain_fixture()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    engine.begin_frame(NOW, (), has_reward=False)
    engine.tag.fill(.2)
    engine.finalize_frame(NOW, 'BUY')
    engine.credit.fill(.7)
    previous_clock = engine.bank._clock_us
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        credits = (DecisionCredit(decision_at=NOW, fraction=D('.500000000000000001')),
                   DecisionCredit(decision_at=NOW - timedelta(microseconds=1), fraction=D('.5')))
        with pytest.raises(ValueError, match='exceed one'):
            engine.begin_frame(NOW + timedelta(seconds=1), credits, has_reward=True)
    assert engine.bank._clock_us == previous_clock and engine._frame_at == NOW
    np.testing.assert_array_equal(engine.credit, [.7] * 3)
    np.testing.assert_array_equal(engine.tag, [.2] * 3)


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf, np.finfo(float).max, -TINY])
def test_invalid_last_observation_is_atomic(bad):
    brain = brain_fixture()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    counts = np.ones(7)
    counts[4] = bad  # last edge's MBON, after two valid edges
    arrays = (engine.pre_trace, engine.post_trace, engine.tag, brain.memory_u, brain.memory_w, brain.weight)
    before = [a.copy() for a in arrays]
    with pytest.raises(ValueError):
        engine.advance(brain, counts, .01, learning=True, stimulation=None, delta_limit=.01)
    for actual, expected in zip(arrays, before):
        np.testing.assert_array_equal(actual, expected)
    assert brain.plasticity_diagnostics == {'nonfinite_or_invalid_updates': 1}


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf, np.finfo(float).max])
def test_capture_rejects_nonfinite_or_unbounded_drive_before_mutation(bad):
    u, w = np.array([.4, -.4]), np.array([.2, -.2])
    before = [u.copy(), w.copy()]
    with pytest.raises(ValueError):
        capture_kernel(u, w, np.ones(2), np.array([1., bad]), .01, .1, .001, .5, .95, .01)
    np.testing.assert_array_equal(u, before[0])
    np.testing.assert_array_equal(w, before[1])


def test_warmed_pipeline_reuses_all_array_storage_and_has_no_native_array_allocator(monkeypatch):
    brain = brain_fixture()
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    counts = np.ones(7, dtype=np.int32)
    stimulus = (np.array([5, 6]), 20.)
    engine.begin_frame(NOW, (), has_reward=False)
    engine.advance(brain, counts, .01, learning=True, stimulation=stimulus, delta_limit=.01)
    engine.finalize_frame(NOW, 'BUY')
    credits = (DecisionCredit(decision_at=NOW, fraction=D(1)),)
    arrays = (engine.pre_trace, engine.post_trace, engine.tag, engine.credit, engine._dopamine,
              engine._dan, engine._bin_metrics, engine.bank._values, engine.bank._timestamps,
              engine.bank._credit_slots, engine.bank._credit_fractions)
    pointers = [a.ctypes.data for a in arrays]
    size = engine.buffer_nbytes
    def allocate(*args, **kwargs):
        raise AssertionError('array allocation in warmed training path')
    with monkeypatch.context() as patch:
        for name in ('array', 'asarray', 'empty', 'zeros', 'ones', 'full', 'empty_like', 'zeros_like'):
            patch.setattr(np, name, allocate)
        for i in range(1, 11):
            now = NOW + timedelta(microseconds=i)
            engine.begin_frame(now, credits, has_reward=True)
            engine.advance(brain, counts, .01, learning=True, stimulation=stimulus, delta_limit=.01)
            engine.finalize_frame(now, 'BUY')
    actual = (engine.pre_trace, engine.post_trace, engine.tag, engine.credit, engine._dopamine,
              engine._dan, engine._bin_metrics, engine.bank._values, engine.bank._timestamps,
              engine.bank._credit_slots, engine.bank._credit_fractions)
    assert [a.ctypes.data for a in actual] == pointers and engine.buffer_nbytes == size
    for kernel in (plasticity_kernel, credit_kernel, store_kernel):
        assert kernel.nopython_signatures
        assert kernel.targetoptions['nogil'] and kernel.targetoptions['boundscheck']
        assert kernel.targetoptions['fastmath'] is False
        for llvm in kernel.inspect_llvm().values():
            assert 'NRT_MemInfo_alloc' not in llvm


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
@pytest.mark.parametrize('value', ['overflow', 'underflow'])
def test_unrepresentable_structural_weights_fail_before_state_update(dtype, value):
    brain = brain_fixture(dtype)
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    brain.baseline_plastic[-1] = np.finfo(dtype).max if value == 'overflow' else np.finfo(dtype).smallest_subnormal
    before = [a.copy() for a in (engine.tag, brain.memory_u, brain.memory_w, brain.weight)]
    with pytest.raises(ValueError):
        engine.advance(brain, np.ones(7, dtype=np.int32), .01, learning=True, stimulation=None, delta_limit=.01)
    for actual, expected in zip((engine.tag, brain.memory_u, brain.memory_w, brain.weight), before):
        np.testing.assert_array_equal(actual, expected)


def test_dst_fold_is_ordered_by_integer_instant_not_local_wall_clock():
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('Europe/Berlin')
    first = datetime(2026, 10, 25, 2, 59, 59, 999999, tzinfo=zone, fold=0)
    later = datetime(2026, 10, 25, 2, 0, tzinfo=zone, fold=1)
    bank = EligibilityBank(1, PlasticityConfig())
    assert bank.reserve(first)
    bank.store(first, np.ones(1))
    assert bank.reserve(later)
    out = np.zeros(1)
    metrics = bank.capture_into(later, (DecisionCredit(decision_at=first.astimezone(timezone.utc), fraction=D(1)),), out)
    assert metrics['maximum_credit_delay_seconds'] == .000001
    assert out[0] == math.exp(-.000001 / 30.)


def test_passive_study_budget_cannot_be_raised_above_two_percent():
    with pytest.raises(ValueError):
        replace(PlasticityConfig(), maximum_homeostatic_loss_per_second=np.nextafter(.02, 1.))
    u, w, zero = np.ones(1), np.ones(1), np.zeros(1)
    with pytest.raises(ValueError):
        capture_kernel(u, w, zero, zero, .01, .001, 10., 10., .95, .01, .03)
    assert u[0] == w[0] == 1.
