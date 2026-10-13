# MaleCNS experimental training and evaluation

The frozen experimental profile is `malecns-risk-adjusted-experiment-v2`
(implementation revision 4). It preserves the full retained graph, native spike
propagator, identified dopamine circuits and fixed BUY/SELL/HOLD decoder. It adds
bounded delayed credit and homeostasis to plasticity. Baseline `FlyController`
retains its separate centered KC/DAN rule. There is no differentiable actor,
critic, policy gradient or hidden profit-based action selection. Profitable
learning remains unverified.

`malecns-risk-adjusted-experiment-v3` (revision 5) runs the identical reward,
plasticity and decoder path under an evidence-gated study protocol: a trial must
produce enough executed decisions, completed round trips, nonzero reward pulses
and delivered credit before any profitability score counts, and every fold/seed
is paired with a frozen control on the same data. It changes what is *claimed*,
not what is *learned*; see [reinforcement and capacity fitness](malecns-capacity-fitness.md#v3-evidence-adequacy-and-paired-controls).
v2 studies keep their meaning and cannot be resumed under v3 or vice versa.

Use [dataset bootstrap](malecns-bootstrap.md) for graph provenance,
[reinforcement and capacity fitness](malecns-capacity-fitness.md) for reward/admission
and study contracts, and the [plasticity kernel](malecns-rstdp-kernel.md) for exact
trace ordering, decay limits and memory telemetry.

## Setup and execution

```sh
uv sync --locked --extra desktop --extra research --extra training
uv run python scripts/optimize_malecns.py --help
uv run python scripts/benchmark_training.py --output build/training-pipeline-stress.json
```

The training extra pins Numba, Polars and Optuna. Desktop startup does not activate
the experimental controller. The study CLI requires an explicit qualified
`module:factory` providing `StudyResources`, a verified full graph, attested native
kernel, immutable evidence ID, chronological market data and a declared maximum
reward-credit horizon. No synthetic evaluator is silently substituted.

A trusted `PaperEnvironment` provides a causal `stream()`, `execute(side,
decision_at)`, `reject(side, decision_at, reason)` and `finish()` with complete
terminal accounting. Rejection records no order and chooses no replacement.
Every fold and seed starts a fresh controller and paper book. The sequential study
uses four objectives and repeated seeds; see the capacity specification for bounds,
feasibility checks and completed-epoch saturation pruning.

The default Optuna database is `~/.devsolux/coinfly/runs/malecns-optuna.sqlite`.
An explicit `--storage` selects another study destination. Persistent contracts
bind data, costs, code, folds, horizons and objectives. Mismatches reject resume.
A preselected holdout trial is recorded before holdout access and seals the study,
including failed attempts; the holdout registry prevents cross-study reuse.

## Exact accounting and observations

`evaluate_reward` is the Decimal boundary. Gross-reference accounting subtracts
booked fees (or explicit fee-tier estimates) and signed implementation shortfall
once. Net-execution accounting reports those costs without charging them again.
Reward evidence must include every fill and be available before reinforcement.
Funding and inventory marks belong to the environment's net-equity ledger.

Only normalized ratios enter JIT risk/plasticity kernels. Booked net returns remain
separate from latency stress and risk shaping. Residual latency stress subtracts
only modeled adverse cost beyond already observed shortfall. Cost presets in
`fee_presets.py` are dated research scenarios, not live account entitlements.
Actual reported fees take precedence; unknown fee schedules require explicit evidence.

`CausalFrameLoader` accepts registered features with Decimal values and typed UTC
`event_at`, `window_end`, `available_at` and `fitted_through` timestamps:

```text
event_at <= window_end <= available_at <= decision_at
fitted_through <= decision_at
evaluation: fitted_through < evaluation_start <= decision_at
```

`TickScale` origin/tick/scale are fitted statistics too: they decide which
values saturate in the RGB projection. A scale may declare `fitted_through`;
the loader then applies the same two rules to it and reports the later of the
table and scale lineage as `preprocessing_fitted_through`. A scale without a
declaration leaves that lineage to the host.

Future labels, fills and P&L are not market feature inputs. The host must provide
honest upstream lineage and include feature lookback, fitting dependencies and
reward delivery age in the chronology purge. Metadata cannot repair mislabeled data.

`TickScale` subtracts a Decimal origin and divides by the exact tick before float
conversion. Off-grid values and centered offsets outside ±(2^53−1) are rejected.
Float normalization must reconstruct the same integer tick; exact sidecars remain
authoritative. RGB projection clips and quantizes to 8-bit sensory input, so it is
an engineered observation proxy rather than an exact monetary representation.
Float features never become order quantities without exact execution validation.

## Learning and evaluation boundaries

`TrainingController` serializes audited frames, completed reward windows, full-graph
propagation and decoding. Seeded, decaying sensory noise changes input pixels only.
The fixed decoder and graph topology are not optimized. Eligibility tags store
admitted BUY/SELL decisions; HOLD consumes no slot. The ring is bounded by 256
entries and a 64 MiB tag budget. Capacity rejection preserves the decoded side.

Rewards reference originating admitted decisions. Missing/expired credit is
reported; tags are never attributed to future actions. Engineered reinforcement
stimulates identified dopamine routes. It does not model pain, pleasure or
consciousness. Frozen evaluation disables plasticity, exploration and reward pulses
while retaining recurrent state driven by market observations.

Experimental neural checkpoints declare `session_resume=False`: they cannot restore
risk state, exploration cursors, ledger and neural state atomically. Replay a trial
from its verified initial state. Baseline checkpoints require exact provenance and
validated native-array layouts before mutation; no earlier fingerprints are accepted.

Compare preselected learned models with frozen controls on sealed chronological
holdout data, over multiple seeds and market regimes. Report untouched net returns,
drawdown, turnover, action coverage, credit loss, saturation, latency and RSS under
worsened costs/delays. The holdout evaluation and the v3 development protocol
both record `paired_deltas` (learned minus frozen) per seed and a `seed_summary`
with mean, sample standard deviation, minimum, maximum and the number of seeds
that improved; a single winning seed is never the result. Pareto membership or
weight changes alone do not establish useful learning. Synthetic tests and
benchmarks validate mechanics; full-graph market performance and profitable
convergence require independent evaluation, and no development-fold measurement
has been produced yet.
