"""Baseline-centered anti-Hebbian eligibility model.

Adaptation of Huang, Luo et al. 2024, Appendix equations 3.2--3.5.
The original reduced rate model is NOT substituted for the connectome. Actual
full-network spike counts supply the rates. u/w represent deviations from each
reconstructed edge's initial efficacy. Bounds and trace constants are declared
model choices; applying the rule to this male reconstruction is a hypothesis.
"""

import math
from typing import Any, TypedDict

import numpy as np
from numpy.typing import NDArray

Array = NDArray[Any]

class RuleParameters(TypedDict):
    trace_kc_seconds: float
    trace_dan_seconds: float
    memory_decay_seconds: float
    weight_filter_seconds: float
    minimum_fraction: float
    maximum_fraction: float
    maximum_rate_bin_ms: float
    source: str
    interpretation: str


PARAMETERS: RuleParameters = {
    "trace_kc_seconds": 1.0,
    "trace_dan_seconds": 1.0,
    "memory_decay_seconds": 1800.0,
    "weight_filter_seconds": 0.05,
    "minimum_fraction": 0.1,
    "maximum_fraction": 2.0,
    "maximum_rate_bin_ms": 10.0,
    "source": "https://doi.org/10.1038/s41586-024-07819-w",
    "interpretation": "Centered rate-rule adaptation on existing individual edges, not an exact reproduction of the published nine-unit model. Equal trace constants, efficacy bounds, gain and 50 ms weight filter require calibration.",
}


def advance(
    y_kc: Array, y_dan: Array, u: Array, w: Array, kc_hz: Array, dan_hz: Array,
    gain: Array, dt_seconds: float, eta: float, learning: bool = True, frozen: bool = False,
    *, diagnostics: dict[str, float | int] | None = None,
) -> None:
    """Mutate traces and efficacy deviations with constant rates for one bin.

    Traces and the passive two-state memory system use closed-form solutions.
    The nonlinear rate/trace products use midpoint traces. No game variables enter.
    """
    if not math.isfinite(dt_seconds) or dt_seconds <= 0 or dt_seconds > 0.0100001:
        raise ValueError("Rate bins must be 0--10 ms")
    if not math.isfinite(eta) or eta < 0:
        raise ValueError("Finite nonnegative learning rate required")
    vectors = (y_kc, y_dan, u, w, kc_hz, dan_hz)
    if (any(a.ndim != 1 or a.dtype.kind != "f" or not np.isfinite(a).all() for a in vectors)
            or any(a.dtype != np.float64 for a in (y_kc, y_dan, u, w))
            or any(a.shape != u.shape for a in (y_kc, w, kc_hz))
            or y_dan.shape != dan_hz.shape or gain.shape != (len(y_dan), len(u))
            or not np.isfinite(gain).all() or np.any(gain < 0)
            or np.any(kc_hz < 0) or np.any(y_kc < 0)
            or any(not a.flags.writeable for a in (y_kc, y_dan, u, w))):
        raise ValueError("Invalid plasticity state, rates or compartment gains")
    h = dt_seconds
    p = PARAMETERS
    lo = p["minimum_fraction"] - 1
    hi = p["maximum_fraction"] - 1
    if any(np.any(a < lo) or np.any(a > hi) for a in (u, w)):
        raise ValueError("Plasticity state outside declared efficacy bounds")
    # Compute candidates before writing any state. Clipping infinity would hide
    # an unstable step, and NaN survives clipping and poisons later propagation.
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            ak = math.exp(-h / p["trace_kc_seconds"])
            ad = math.exp(-h / p["trace_dan_seconds"])
            kmid = y_kc * math.sqrt(ak) + kc_hz * (-math.expm1(-h / (2 * p["trace_kc_seconds"])))
            dmid = y_dan * math.sqrt(ad) + dan_hz * (-math.expm1(-h / (2 * p["trace_dan_seconds"])))
            next_k = y_kc * ak + kc_hz * (-math.expm1(-h / p["trace_kc_seconds"]))
            next_d = y_dan * ad + dan_hz * (-math.expm1(-h / p["trace_dan_seconds"]))
            if not frozen:
                drive = (eta * (kc_hz * (gain.T @ dmid) - (gain.T @ dan_hz) * kmid)
                         if learning else np.zeros_like(u))
                tu, tw = p["memory_decay_seconds"], p["weight_filter_seconds"]
                eu, ew = math.exp(-h / tu), math.exp(-h / tw)
                # expm1 avoids subtracting two numbers near one for short bins.
                c = tu / (tu - tw) * (math.expm1(-h / tu) - math.expm1(-h / tw))
                next_u = u * eu + drive * tu * (-math.expm1(-h / tu))
                next_w = w * ew + u * c + drive * tu * (-math.expm1(-h / tw) - c)
                if not np.isfinite(next_u).all() or not np.isfinite(next_w).all():
                    raise ValueError("Nonfinite plasticity update")
    except FloatingPointError as error:
        if diagnostics is not None:
            diagnostics["nonfinite_updates"] = diagnostics.get("nonfinite_updates", 0) + 1
        raise ValueError("Nonfinite plasticity update") from error
    except ValueError:
        if diagnostics is not None:
            diagnostics["nonfinite_updates"] = diagnostics.get("nonfinite_updates", 0) + 1
        raise
    if diagnostics is not None:
        diagnostics["bins"] = diagnostics.get("bins", 0) + 1
        if not frozen and u.size:
            # Inspect candidates BEFORE clipping: bounded efficacies alone hide
            # a destabilizing drive. These are plasticity updates, not gradients.
            largest = max(float(np.max(np.abs(next_u - u))), float(np.max(np.abs(next_w - w))))
            diagnostics["maximum_candidate_delta"] = max(diagnostics.get("maximum_candidate_delta", 0), largest)
            clipped = int(np.count_nonzero((next_u < lo) | (next_u > hi))
                          + np.count_nonzero((next_w < lo) | (next_w > hi)))
            diagnostics["clipped_candidates"] = diagnostics.get("clipped_candidates", 0) + clipped
            diagnostics["maximum_clipped_fraction"] = max(
                diagnostics.get("maximum_clipped_fraction", 0), clipped / (2 * u.size))
    y_kc[:], y_dan[:] = next_k, next_d
    if not frozen:
        # Preserve reconstructed signs and declared bounds; report saturation.
        np.clip(next_u, lo, hi, out=u)
        np.clip(next_w, lo, hi, out=w)
