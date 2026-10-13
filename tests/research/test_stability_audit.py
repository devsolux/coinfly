"""Synthetic numerical tests, never substitutes for verified MaleCNS assets."""

import numpy as np
import pytest

from coinfly.research.neural.rule import advance


@pytest.mark.parametrize("bad", ["nan_eta", "negative_eta", "nan_rate", "overflow", "negative_kc"])
def test_invalid_plasticity_step_is_atomic(bad):
    k, d, u, w = np.zeros(2), np.zeros(1), np.zeros(2), np.zeros(2)
    kc, dan, eta = np.ones(2), np.ones(1), .001
    if bad == "nan_eta":
        eta = float("nan")
    elif bad == "negative_eta":
        eta = -1.
    elif bad == "nan_rate":
        dan[0] = np.nan
    elif bad == "negative_kc":
        kc[0] = -1.
    else:
        kc[:] = dan[:] = 1e308
    before = [a.copy() for a in (k, d, u, w)]
    with pytest.raises(ValueError):
        advance(k, d, u, w, kc, dan, np.ones((1, 2)), .01, eta)
    for current, original in zip((k, d, u, w), before):
        np.testing.assert_array_equal(current, original)


def test_sustained_plasticity_remains_finite_bounded_and_deterministic():
    def trial():
        k, d, u, w = np.zeros(2), np.zeros(1), np.zeros(2), np.zeros(2)
        for i in range(2000):
            advance(k, d, u, w, np.array([400., 0.]) if i % 40 < 20 else np.zeros(2),
                    np.array([400.]) if i % 40 >= 20 else np.zeros(1),
                    np.ones((1, 2)), .01, .001)
            assert all(np.isfinite(a).all() for a in (k, d, u, w))
            assert np.all(u >= -.9) and np.all(u <= 1)
            assert np.all(w >= -.9) and np.all(w <= 1)
        return w
    first, second = trial(), trial()
    np.testing.assert_array_equal(first, second)
    assert first[1] == 0


def test_simultaneous_proportional_rates_are_not_a_profit_learning_signal():
    k, d, u, w = np.zeros(1), np.zeros(1), np.zeros(1), np.zeros(1)
    for _ in range(1000):
        advance(k, d, u, w, np.array([20.]), np.array([30.]), np.ones((1, 1)), .01, .001)
    np.testing.assert_allclose(w, 0, atol=1e-12)


@pytest.mark.parametrize("duration", [-1., 0., 1e-10, .15, float("inf"), float("nan"), 60_001.])
def test_invalid_rgb_duration_is_rejected_before_touching_sensory_state(duration):
    from coinfly.research.neural.visual import VisualMemoryBrain

    brain = VisualMemoryBrain.__new__(VisualMemoryBrain)
    brain.dt = .1
    brain.r8_light = np.array([.5], dtype=np.float32)
    with pytest.raises(ValueError):
        brain.rgb_step(np.zeros((2, 2, 3), dtype=np.uint8), duration)
    np.testing.assert_array_equal(brain.r8_light, np.array([.5], dtype=np.float32))
