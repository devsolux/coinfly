# KC→MBON three-factor kernel

The `malecns-risk-adjusted-experiment-v2` plasticity path implements the
[reward and admission contract](malecns-capacity-fitness.md). Its discrete
eligibility rule filters P and Q before computing the timing term. Study
fingerprints bind the implementation and configuration; changed contracts require
a fresh study. Experimental neural-only checkpoints cannot resume training sessions.

The implementation is an engineered learning hypothesis. Synthetic verification
establishes numerical behavior, not biological fidelity or profitable learning.
The full retained graph, original C++ propagator, decoder, reward routes and paper
execution boundaries remain intact. The existing 15% absolute-saturation rule
prunes a completed **Optuna trial**, never neurons or synapses.

## State and execution

For E existing plastic KC→MBON edges and D identified DAN cells:

- `P`, `Q`, `tag`, captured credit, latent drive `u` and deviation `w` have E entries.
- The anatomical compartment matrix has shape D × E and nonnegative columns
  summing to one. It projects observed DAN responses onto their existing edges.
- The fixed tag ring has shape capacity × E, float64 values, and int64 microsecond
  timestamps. Slot lookup also uses integer microseconds, including across DST.

[experimental_plasticity.py](../src/coinfly/research/neural/experimental_plasticity.py)
provides the complete implementation. Each numerical kernel uses
`@njit(nogil=True, fastmath=False, boundscheck=True)`. Compilation is lazy; warm
representative dtypes before timing. Python validates array metadata and exact
reward evidence under the existing serialized controller lock. `plasticity_kernel`
then performs rate readout, tag updates, DAN projection, bounded capture, weight
writes and packed telemetry in one native call. No native loop allocates arrays.
Frozen or learning-disabled bins leave the plasticity state and weights unchanged.

For bounded normalized rates `p=min(KC_Hz/rate_scale_hz,1)` and
`q=min(MBON_Hz/rate_scale_hz,1)`:

```text
d = exp(-h / neural_trace_seconds)
f = -expm1(-h / neural_trace_seconds)
P = d*P + f*p
Q = d*Q + f*q
tag = clip(d*tag + f*(p*Q - q*P), -1, 1)
```

Each frame resets its tag but retains P/Q. An admitted decoded BUY/SELL stores
one in-place snapshot; HOLD uses no slot and its finalization releases the
frame's capacity reservation, so a later store at the same instant cannot
inject a HOLD tag. A full live ring rejects new order admission while
propagation and old credit capture continue.

At reward delivery, `capture_into` validates positive Decimal allocations,
duplicate instants, causality and an exact sum at most one before conversion to
float64. Fractions remain unnormalized. For each retained decision:

```text
delay = (delivery_microseconds - decision_microseconds) / 1_000_000
captured_tag += stored_tag * float(allocation) * exp(-delay / credit_tau_seconds)
```

Integer subtraction precedes float conversion. Compensated summation limits
roundoff; numerical tags are clipped only for floating-point boundary drift.
Fractions and decay are float64 approximations inside the kernel; accounting
remains exact Decimal. Missing or expired allocation mass is reported and is
never reassigned. Expiration is strictly `age > maximum_credit_age_seconds`.

Only annotated DAN cells stimulated by the delivered pulse contribute:

```text
dan = max(observed_Hz - baseline_Hz - dan_threshold_hz, 0)
drive = eta * eta_scale * reinforcement_gain * captured_tag * (gain.T @ dan)
s = fraction of edges with u or w near a bound (default 95%)
lambda = min(base_decay + saturation_decay*s, -log1p(-loss_budget))
r = exp(-lambda*h)
u_candidate = u*r + h*drive*outward_headroom
u_next = bounded_step(u_candidate, previous=u)
w_relaxed = w*r
w_candidate = w_relaxed + (-expm1(-h/0.05))*(u_next - w_relaxed)
w_next = bounded_step(w_candidate, previous=w)
edge_weight = original_edge_weight * (1 + w_next)
```

`bounded_step` preserves the existing duration-scaled trust limit and clips to
`[-0.9, 1]`. The loss budget defaults to 0.02 and cannot exceed 0.02. It bounds
passive latent attenuation, not total learning or the u-to-w filter's effect.
Outward headroom vanishes at a boundary; inward updates remain effective.
Representable original weights retain their signs. Nonfinite state, invalid
rates/indices/gains and structural weights that could overflow or round to zero
under the permitted multiplier fail before any neural state or weight write.

## Memory and diagnostics

The 64 MiB default is the **tag-array budget**, preserving the existing
`maximum_tag_bytes` contract. It is not a whole-process HFT hardware guarantee:
the retained graph alone exceeds it. The engine owns five additional E-length
float64 working arrays, D DAN values, ten packed diagnostic values, capacity
int64 timestamps and two 1,024-entry allocation scratch arrays. `buffer_nbytes`
reports all those NumPy bytes; frame telemetry separates `tag_buffer_bytes` and
`working_buffer_bytes`. Python metadata and the brain's arrays are additional.
`bank.tags` and `bank.capture` intentionally allocate owned diagnostic snapshots;
the training path uses in-place storage and `capture_into`.

The packed ten-value bin buffer contains near-bound fraction, effective lambda,
clipped-step count, maximum candidate delta, absolute-bound fraction, erasure
rate, passive loss fraction, requested lambda, capture-active and DAN-active flags.
The existing public cumulative diagnostic keys and end-of-epoch analyzer remain
compatible. Pre/post occupancy is counted conservatively; a finite bounded result
does not conceal clipping or a saturated input state.

`risk_signal` raises on a nonfinite shaped signal instead of clipping it;
clipping a NaN would otherwise have yielded the maximum reward. Both the v2 and
v3 profiles run this kernel unchanged.

## Verification

```sh
.venv/bin/python -m pytest -q tests/research
.venv/bin/python scripts/benchmark_plasticity.py --output build/rstdp-plasticity-stress.json
```

[test_rstdp_kernel.py](../tests/research/test_rstdp_kernel.py) includes a high-precision
tag oracle, an independent multibin scalar pipeline, float64 extrema/subnormals,
late invalid inputs with atomic failure, exact microsecond and expiry boundaries,
ring wrap, DST, signed float32/float64 weights, exact allocation rejection,
buffer-address reuse and absence of native NRT array-allocation calls. Existing
integration tests cover asymmetric gain, admission backpressure, frozen evaluation
and completed-epoch trial pruning. Full-graph latency, RSS and market-data learning
still require a qualified graph and replay adapter.
