# MaleCNS reinforcement and capacity fitness

The [KC→MBON kernel specification](malecns-rstdp-kernel.md) specifies the
current trace ordering, allocation-free numerical pipeline and memory telemetry.
The reward/admission and completed-epoch fitness contracts below remain in force.

Symmetric reinforcement can repeatedly emphasize fee losses while delayed gains
are attenuated by market-clock credit decay. This can bias plasticity toward
inactivity, but permanent HOLD and profitable convergence have not been established
on this graph. Eligibility time scales should match reward delays; see the
[three-factor learning review](https://www.frontiersin.org/journals/neural-circuits/articles/10.3389/fncir.2018.00053/full).
The financial gain rules here remain engineered hypotheses, not measured fly biology.

The experimental profile preserves the full graph, fixed decoder, baseline controller and
attested C++ propagator. Its experimental v2 Python orchestration and Numba plasticity path are described
in [training and evaluation](malecns-training.md).

## Causal asymmetric gain

`MemoryBrain.prepare_experimental_frame` validates completed reward evidence and
the market clock, then delegates gain selection to `ExperimentalPlasticity`:

```python
reinforcement_gain = 1.0
if signal > 0 and net_return > 0 and stressed_return >= positive_return_threshold:
    reinforcement_gain = positive_gain
elif (signal < 0 and -booked_fee_return < net_return < 0
      and -booked_fee_return < stressed_return < 0):
    reinforcement_gain = fee_shock_gain
effective_eta = brain.eta * eta_scale * reinforcement_gain
```

`reinforcement_gain_kernel` performs that scalar dispatch using Numba
`@njit(cache=True, nogil=True, fastmath=False)`. The wrapper validates finite
evidence before calling it; the native branch receives float64 normalized ratios,
never Decimal money or fee estimates. Exact accounting remains outside JIT.
Compilation is lazy and cached; no throughput improvement is claimed for this
small scalar dispatch. See [Numba compilation semantics](https://numba.readthedocs.io/en/stable/user/jit.html).

Both fee comparisons are strict. Zero, exactly fee-sized losses, losses beyond
booked fees and losses dominated by added latency stress receive neutral gain.
`booked_fee_return` includes only reported `actual_fee`, summed and normalized
with isolated Decimal accounting; `fee_return` can also contain estimates for
gross-reference research windows. Estimated costs cannot justify damping.
Net-execution study ledgers already require actual fees.

The gain never changes reward sign, route identity or monetary results. Only
stimulated annotated DAN cells with observed activity capture tags. Existing
delta bounds, anti-windup and the 2% direct passive-loss budget still apply.
Positive-event selection uses completed returns, never future action profitability.
The loss interval identifies a cost-bounded scenario; it does not prove causal
attribution of that loss to fees. Default gains remain one until calibrated.

## Order tags and credit timing

The ring reserves capacity against the current observed clock before propagation,
and stores tags only for decoded BUY/SELL decisions with capacity admission.
HOLD does not represent an order and consumes no tag. A full ring still propagates
the graph and captures old credit, then rejects a new BUY/SELL with
`execution_admission={"allowed": False, "reason": "eligibility_capacity"}`.
The original decoded side is preserved.

Preparation cannot know the current side before propagation and decoding, so it
does not store a tag. The paired `MemoryBrain.finalize_experimental_frame` owns admission after decoding. The engine verifies the frame timestamp, permits
one finalization only, skips HOLD storage, and checks live capacity before storing
BUY/SELL tags. Both stages run under the existing serialized controller lock.

The trusted paper adapter must implement `reject(side, decision_at, reason)` to
record the rejection, advance replay and submit no order. A rejection-hook failure
propagates immediately: no execute fallback, retry, replacement trade or finish
call occurs. Other execution guards, including exact `<5%` turnover, remain in
the adapter. `turnover.py` tracks exact spent/reserved notional and separate fee funding.

Reward allocation must reference the originating admitted BUY/SELL decisions,
including returns from inventory held afterward. Do not allocate exposure reward
to intervening HOLD frames or rejected entries. Orders rejected later by an
adapter's cash/turnover guard may retain a conservative unused tag until expiry;
the current execute protocol has no terminal receipt to justify early removal.
Capacity is still bounded by the smaller of 256 decisions and 64 MiB divided by
the float64 plastic-edge tag size. Metadata and working arrays are additional.
No live tags are evicted, and expiry never uses a future timestamp.

`StudyResources.reward_credit_horizon_seconds` is mandatory for studies.
Declare the maximum decision-to-reward delivery age from development execution
semantics: include exposure attribution age, reward batching and report latency.
The study requires:

```python
H = max(resources.reward_credit_horizon_seconds, reward.interval_seconds)
maximum_age >= max(credit_tau_seconds, H)
maximum_age <= 150
```

An unknown horizon or one exceeding 150 seconds rejects setup. The chronology
purge must also cover H, and the host must additionally cover feature lookback
and preprocessing dependencies. The rolling risk-statistics window is not the
delivery delay: a 256-interval variance estimator does not defer each interval's
reward by 256 intervals. If the host actually batches/delays allocation that long,
it must declare that age and cannot silently fit it into this hardware envelope.

Every allocation contributes to maximum observed credit delay, including expired
or unknown tags. Delivery beyond the declared H makes the trial infeasible even
when the tag happens to remain available; under the v3 protocol it prunes the
trial outright (`REWARD_DELIVERY_BEYOND_DECLARED_HORIZON`, no objective values)
so a study can never learn from a silently truncated subset of its own credit. `missing_credit_fraction` counts
nonzero shaped reward windows with any missing allocation or no allocation,
divided by all requested nonzero reward pulses. Suppressed pulses stay in the
denominator; zero signals do not. `missing_credit_alert` is true strictly above
1%. The existing feasibility requirement remains zero missing-credit reward
windows; the alert is not a relaxed acceptance threshold.

## Four objectives, capacity penalty and saturation trial pruning

`ExecutionDiagnostics` reports BUY/SELL attempts, capacity denials, other neural
admission denials, HOLD count, reward pulses and credit-delay violations per phase.
Capacity rate uses decoded BUY/SELL attempts, not all market ticks. Execution-side
turnover rejections remain separately recorded by the trusted adapter.

For each completed development fold/seed, let r be its capacity rejection rate.
Use the worst fold/seed r so busy regimes are not hidden by quieter ones:

```python
penalty = (max(r - 0.10, 0.0) / 0.90) ** 2
fitness = median_validation_net_sortino - penalty
```

This additive penalty is continuous with continuous first derivative at 10%,
ranges from zero to one, and worsens negative Sortino too. It changes selection
fitness only; raw net returns and raw Sortino remain separate saved metrics.
The fixed coefficient is one unannualized Sortino unit at total rejection;
changing it requires a new study contract, not tuning against final holdout.

| Objective | Direction | Aggregation across folds/seeds |
|---|---|---|
| Capacity-penalized net Sortino | Maximize | Median raw Sortino minus worst capacity penalty |
| Validation maximum drawdown | Minimize | Maximum |
| Training near-bound edge-time occupancy | Minimize | Mean |
| Training passive erasure rate | Minimize | Maximum |

The strict analyzer checks cumulative diagnostics every frame. At the end of
each training epoch, absolute-saturation time above 15% raises
`AbsoluteSaturationError`; the objective records its audit and raises Optuna
`TrialPruned`, producing no fabricated objective values. Validation data is not
opened for an invalid training epoch. A transient prefix over 15% is not itself
the completed epoch ratio. Exactly 15% is allowed.

`PlasticityEpochAudit` retains a fixed-size snapshot of the nine cumulative
counters. Each frame must increase bin count and neural time, never decrease any
cumulative counter, and keep added occupancy and erasure within that frame's
time budget. Resetting saturation while continuing the clock fails explicitly.
The end-of-phase check also protects direct `_phase` callers before returning to
their evaluator. Kernel pre/post-bin measurements remain unchanged.

Absolute saturation means at least one plastic edge's latent u or expressed w
touches either bound within 1e-12; the pre/post-bin states are counted conservatively.
The separate peak near-bound edge constraint remains <=1%. Passive erasure is
integrated effective decay while nonzero memory exists, divided by simulated
neural seconds. It is a rate coefficient (1/second), not observed total memory
loss or financial return. Requested/applied decay and capped-bin diagnostics stay
available to distinguish saturation avoidance from aggressive passive erasure.

## Search coordinates and execution

| Coordinate | Bounds | Scale |
|---|---|---|
| `positive_return_threshold` | 0.0001–0.01 of interval equity | Log |
| `positive_gain` | 1–2 | Linear |
| `fee_shock_gain` | 0.25–1 | Linear |
| `neural_trace_seconds` | 0.02–5 simulated seconds | Log |
| `credit_tau_seconds` | 0.5–150 market seconds | Log |
| `maximum_credit_age_seconds` | max(5, credit tau, H)–150 market seconds | Conditional log |
| `dan_threshold_hz` | 0–25 above baseline | Linear |
| `eta_scale` | 0.1–2 | Log |
| `homeostasis_per_second` | 0.00001–0.01 | Log |
| `saturation_decay_per_second` | 0.001–0.2 | Log |
| `alpha` (Sortino weight) | 0.25–0.95 | Linear |
| `risk_window` | 64, 128, 256, 512, 1024 intervals | Categorical |
| `turnover_penalty` | 0.05–5 | Log |
| `signal_scale` | 1–16 | Log |

Sensory-noise, exploration-decay and update-limit coordinates are retained.
The credit-tau upper bound covers the entire 150-second envelope: at that
delay tau=60 retains only exp(-2.5), about 8.2%, of a tag; tau=150 retains exp(-1),
about 36.8%. This makes longer credit decay testable without extending tag age.
Fees, interval length, decoder, graph topology, memory budget, 5% turnover and
2% direct passive-loss limits remain outside the search. Search bounds are
hypotheses; no calibrated values are claimed.

The executable runner remains `scripts/optimize_malecns.py`, using sequential
Optuna TPE, repeated seeds, persistent SQLite and the sealed holdout registry.
Its default v2 study name is `malecns-risk-v2-capacity`; schema 4 fingerprints
the code, reward horizon and penalty settings. `implementation_fingerprint()`
covers every module under `coinfly/research` plus `neural/kernel.cpp`, so a
decoder-threshold or compartment change also changes the contract. Studies
persisted under a different fingerprint refuse resume; their trials keep their
recorded provenance. The
contract records the ordered objective names/directions and all five
reinforcement coordinate definitions from the same constants used by the
sampler. Objective order follows
[Optuna's multi-objective contract](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html).
Trial constraints use Optuna 5.0's `Trial.set_constraint`/`constraints`; the
deprecated `constraints_func` is not used. Studies require an exact contract
match. Use a new study after changing the implementation or configuration.
Experimental neural checkpoints cannot resume partial training; replay from a
fresh controller and never combine trial histories.

## v3 evidence adequacy and paired controls

`--profile v3` selects `StudyProtocol(profile_id="malecns-risk-adjusted-experiment-v3",
implementation_revision=5, schema=5)` with default study name
`malecns-risk-v3-evidence`. The controller, reward, plasticity kernel, decoder,
objectives, coordinates, penalty and turnover limit are byte-identical to v2.
What changes is the claim discipline:

* **Evidence requirements** (`coinfly.research.evidence.EvidenceRequirements`)
  are part of the contract and must be selected deliberately: either explicit
  `--evidence-minimums DECISIONS ROUND_TRIPS PULSES` or derived from a declared
  estimation precision with `--win-rate-precision p`, which requires
  `ceil((1.96 / 2p)^2)` executed validation decisions (97 at p = 0.1), half as
  many completed round trips and as many nonzero training reward pulses.
  `--evidence-credit COVERAGE RETAINED_MASS` sets the minimum fraction of nonzero reward
  pulses whose allocations reached a retained tag and the minimum median
  retained credit mass after `exp(-delay/credit_tau_seconds)` decay. There is
  no default; v3 refuses to run without them.
* **Classification, not punishment.** A fold/seed whose training or validation
  evidence misses a requirement makes the trial `INSUFFICIENT_EVIDENCE`: Optuna
  records it as pruned with the reasons and every raw measurement, and it gets
  no objective values. HOLD is still allowed whenever HOLD is what the decoder
  produces; a policy that trades enough to be judged is judged on the unchanged
  four objectives. Under v2 the same inactive trial scores `(0, 0, 0, 0)` and
  stays Pareto-optimal forever.
* **Paired frozen control.** Unless `--no-paired-control`, every fold/seed is
  also evaluated with plasticity, exploration noise and reward pulses disabled
  on the same chronological data and seed. `paired_control.deltas` reports
  learned minus frozen net return, Sortino, drawdown, turnover, validation order
  attempts, HOLD fraction, completed round trips, credit-loss fraction, capacity
  rejection rate, saturation peak, compute seconds and peak RSS, and
  `seed_summary` gives mean, sample standard deviation, minimum, maximum and the
  number of improving seeds. The deltas are saved metrics, not objectives, so
  they exert no selection pressure toward a convenient control. Compute cost
  doubles.
* **Measured, not corrected.** `training_evidence` and `validation_evidence`
  report, per phase: decoded BUY/SELL, admitted and executed decisions,
  BUY/SELL fills and completed round trips (lower bound `min(buys, sells)`),
  HOLD count and fraction, action entropy, capacity and execution-side denials,
  exposed and accounted windows, turnover, nonzero pulses, allocations,
  delivered pulses, credit coverage, missing/expired/unknown credit, horizon
  violations, the distribution of decision-to-reward delay and retained credit
  mass (count/min/median/max), ring occupancy peak, expired tags, the sensory
  noise sigma schedule (first/last/mean and frames below the 0.15 effective
  floor) and `sign_disagreement_fraction`: the share of nonzero pulses whose
  turnover-penalized shaped signal has the opposite sign to the booked net
  return. These are the diagnostics needed to tell loss-driven collapse into
  HOLD, fee-noise domination, credit arriving after eligibility decayed,
  horizon incompatibility, saturation, aggressive passive forgetting,
  inadequate exploration, capacity masking and reward-scale instability apart.
  v3 does not change the reward, the decoder or the kernel in response to them:
  no development-fold measurement on the qualified graph exists yet, and a
  causal-baseline or booked-sign reward variant remains a future, separately
  fingerprinted experiment with v2/v3 as its control.

Measured properties of the current kernel contract that such an experiment
must account for (pinned by tests, unchanged here): with 10 ms bins observed
DAN rates are multiples of 100 Hz, so `dan_threshold_hz` in 0–25 never changes
which cells count as active and only rescales amplitude that `eta_scale`
already covers; `rate_scale_hz = 100` saturates the eligibility rates p and q
at a single spike per bin; the per-frame tag reset makes long
`neural_trace_seconds` values shrink tag magnitude roughly in proportion to
their inverse; the turnover regularizer lengthens loss pulses relative to
gain pulses because pulse duration follows `|signal|`; and the first windows
after each `RiskAdjustedReward` reset amplify the signal through the
`downside_floor` until the rolling statistics warm up. Valence is anatomical:
reward and aversive pulses drive different MBON compartments in the direction of
the stored tag; neither inverts the sign.

```sh
.venv/bin/python scripts/optimize_malecns.py --profile v3 --win-rate-precision 0.1 \
    --factory host.module:factory --start … --holdout-start … --holdout-end … --purge-seconds …
```

```sh
.venv/bin/python scripts/optimize_malecns.py --help
.venv/bin/python -m pytest -q tests/research
./dev.sh test --native-ui
```

Supply a qualified host `module:factory` and explicit chronological dates to run
the real study. No qualified full-graph market adapter or assets are bundled in
this checkout. Regression fixtures validate mechanics, not full-graph throughput,
memory fragmentation, convergence or profitability. Compare a preselected learned
model to frozen controls on the sealed holdout after development calibration.
Inactivity can still be Pareto-optimal; Pareto membership alone is not profitable learning.
