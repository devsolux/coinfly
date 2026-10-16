# Worker-owned autonomous lifecycle

The worker (`coinfly.worker.automation.AutomationController`) owns the whole
loop. The desktop sends revision-bound commands and renders immutable status;
it never selects an instrument, never runs a decision and never submits an
order. One control lock serializes configuration, the control phases of a
tick and supervisor recovery. The neural computation of a tick runs **outside**
that lock: `tick()` = control phase A (feed, readiness, order TTL, selection,
reconcile, sensory ingest, slot admission, compute requests bound to
generation tokens) → per due slot: lock-free `observe`, then, under the lock,
re-validate every token and commit or discard and dispatch — before the shared
brain is swapped to the next slot (an uncommitted working state is never
overwritten by another instrument's observation; `tests/worker/test_multi_slot_neural.py`)
— bounded by the strategy's compute budget → publish an immutable status
snapshot. `status()` returns the last published
snapshot and `pause_entries`/`stop` only bump the control generation, so the compute phase does not itself hold the control lock
(`tests/worker/test_automation_liveness.py`). Catalog refresh, controller
construction and synchronous live dispatch still include blocking work under
control/interlock boundaries. No target-hardware response-time guarantee or
separate NeuralHost process is claimed. A result whose tokens no longer
match is discarded with a named reason (`DISCARDED_ENTRIES_PAUSED`,
`DISCARDED_GENERATION_CHANGED`, `DISCARDED_STATE_GENERATION_CHANGED`,
`DISCARDED_OBSERVATION_STALE`, …) and the slot's neural state is restored to
its last committed generation instead of advancing.

## Pipeline per tick

```
validated config → recover context → component readiness → feed poll
  → paper venue marks (every engine instrument) → universe selection (reasons)
  → ownership reconcile (create/start/retire owned slots, transactional budget)
  → per-slot causal sensory ingest (warm-up gate)
  → controller decode (preserved MaleCNS or scripted fixture)
  → neural execution admission → target proposal (versioned step mapping)
  → exact planner (target − actual − outstanding, increments)
  → exact turnover guard (strict < fraction of interval-start equity)
  → risk/admission actor → persisted intent → dispatch
  → paper fill / order TTL classification → reconcile
  → drawdown/daily-loss evidence → immutable status
```

## States

| State | Meaning |
| --- | --- |
| `NOT_CONFIGURED` | no strategy for this authority |
| `SETUP` | configured; components not all ready or not enabled |
| `WARMING_UP` | slots active, sensory windows not yet warm |
| `READY` | everything ready, no active owned slot yet |
| `OBSERVING` | warm slots observed in this tick, no decision due |
| `RUNNING` | decisions issued this tick |
| `PAUSED_ENTRIES` | admissions fenced (operator pause or authority fence); observations continue |
| `DRAINING` | stop requested; owned slots retire through their protected drain |
| `RECOVERING` | after a worker restart, or while a re-activation transition runs |
| `FAILED` | a control-plane defect raised in a tick; the failure record (stage, code, redacted chain, code origin `path:line function`, corrective action) is in status and the worker log; entries are fenced; *Enable* retries from `SETUP` |
| `STOPPED` | not enabled; nothing owned is active |

Component readiness is reported individually every tick: `storage`, `dataset`,
`kernel`, `feed` (state + freshness), `strategy` (controller built or the exact
missing prerequisite), `execution_adapter`, `account_authority`.

A feed that cannot start is a degraded component, not a failure: the catalog
being unreachable (`FEED_CATALOG_UNAVAILABLE`, with the reader's code), empty
for the context's contract type (`FEED_CATALOG_EMPTY`) or offering nothing the
strategy could ever trade (`NO_ELIGIBLE_INSTRUMENTS`) keeps the state in
`SETUP`, is shown on the `feed` component with the next retry time, and is
retried with a bounded back-off (2 s doubling to 60 s; a failed catalog read
is not repeated for 15 s, so no tick blocks on the network again meanwhile).
Order TTL expiry keeps running while the feed is down. The feed reads the
catalog of the effective context's contract family (`EngineConfig.effective_subtype`).
Only instruments that pass every book-independent check are subscribed —
owned slots and open positions first (their marks are required), then the
ranked candidates — up to the feed's capacity (REST: `max_active_slots + 2`,
at most 25, polled with a 1 s pause between sweeps so every book stays inside
the observation age; WebSocket level2: 30). Each tick applies at most one book update per instrument to the paper
venue (the latest), because a level2 stream can change a book hundreds of
times per second and every paper update is a journaled transaction.

Feed health fails closed:

* A lost connection (peer close, silent timeout, protocol error) drops every
  book, so nothing decides on the old connection's data while it reconnects.
* The WebSocket feed reports `CONNECTED` only once every subscribed product
  has a level2 snapshot (`CONNECTING` names the products still pending) and
  stays `DEGRADED` after a heartbeat gap until heartbeats are consecutive again.
* Reconnects back off from 1 s to at most 30 s, jitter included, and start
  again at 1 s after a connection that stayed up for 30 s.
* `sequence_gaps` counts venue sequence gaps reported by the sequence tracker.
* A replay file must be chronological (`REPLAY_NOT_CHRONOLOGICAL`) with
  strictly increasing sequences per instrument (`REPLAY_SEQUENCE_NOT_MONOTONIC`);
  a recording that restarts its sequences is refused at load.

## Commands (RPC)

| Method | Effect | Idempotency |
| --- | --- | --- |
| `automation.configure` | validate the strategy against the effective context (track, subtype, quote, environment, research/learning flags), persist it for the authority, approve the budget | receipt by request id; identical replay returns `replayed: true`, different content → `REQUEST_ID_CONFLICT`; changing strategy identity while enabled → `AUTOMATION_STOP_REQUIRED` |
| `automation.enable` | explicit mandate: arm the context (or re-apply a recovered/stopped context with the **same** configuration and arm it once READY), start the loop | receipt by request id; revision/epoch bound |
| `automation.pause_entries` | fence admissions durably; observations and drains continue | idempotent |
| `automation.resume_entries` | same as enable | idempotent |
| `automation.stop` | fence entries, retire every owned slot via the protected drain, release budget; `STOPPED` when nothing remains | idempotent |
| `automation.status` | immutable status block (see below) | read |

## Restart / re-arm policy

A worker restart never resumes automation by default. On start the controller
sets `enabled=0`, `state=RECOVERING`, `restart_reason=RESTART_REQUIRES_REENABLE`
for any enabled record. The recovered paper context is attached quiesced and
fenced. `automation.enable` is the way forward: it re-applies the same
effective configuration (no reseed — the `SIMULATION_SEED` count stays 1),
waits for `READY_UNARMED`, arms under the recorded enable request id, closes the
ownership rows of the retired context (their budget is released) and continues
with a fresh feed and fresh sensory windows. `test_restart_never_resumes_and_keeps_the_persisted_strategy`
demonstrates the sequence.

**Opt-in recovery policy (`RecoveryPolicy`, policy version 1).** A configuration
may declare `recovery: {auto_resume: true, mandate_expires_at, max_resumptions,
require_flat}`. Then, on the first tick after a restart, the worker evaluates a
resumption *once*, records the decision in `cf_automation_recoveries` with
every check, and either runs the explicit-enable sequence itself (request id
`recovery-resume:<id>`, `restart_reason=RESUMED_UNDER_RECOVERY_POLICY`) or
leaves the record on `RESTART_REQUIRES_REENABLE:RECOVERY_REFUSED` for manual
re-enable. Checks: the mandate has not expired; fewer than `max_resumptions`
resumptions were used; storage and the dataset are back; the ledger is
reconciled (no `UNKNOWN` outcomes, no non-terminal intents, no authority
fence; `require_flat` additionally demands no position); the controller is
restorable (baseline or frozen checkpoint with the kernel present, or a
pipeline fixture — a research session is never resumed:
`RESEARCH_SESSION_NOT_RESUMABLE`); a live context has an unexpired live
mandate; no transition is in flight. The status block reports the policy, the
last decision and the *restoration report*, which keeps infrastructure
(storage, dataset, kernel, feed), financial (open intents, unknown outcomes,
fences, positions, seeds) and neural (mode, restorable, session resume = never)
restoration separate. `tests/worker/test_recovery_policy.py`.

**Build identity and IPC handshake.** The worker captures its build identity
once at startup (`build_identity()`: application id, version, protocol
version, source fingerprint, Python, frozen flag, capture time; immutable) and
publishes it in the snapshot. Every desktop connection begins with
`worker.hello` carrying the client's identity: a different protocol version or
application id is refused (`PROTOCOL_VERSION_MISMATCH`,
`APPLICATION_MISMATCH`), a different source fingerprint is reported
(`build_match=false`) and that connection may only read and retire the worker
(`WORKER_BUILD_MISMATCH` on any other command); a connection that never
handshook may only read (`HANDSHAKE_REQUIRED`). Handshakes are bound to the
transport's per-connection id, never to a pid. `tests/ipc/test_handshake.py`.

**Graceful worker retirement.** `worker.shutdown` fences entries at once, but
a worker that manages positions, open intents or an enabled automation is not
terminated abruptly: it stops and reconciles the automation (protected
drains) and exits only once every intent is terminal and the book is flat,
within a bounded wait (`RETIREMENT_TIMEOUT_SECONDS`), otherwise it stays
alive and reports `WORKER_RETIREMENT_BLOCKED` with the remaining exposure.
`worker.shutdown {"force": true}` is the explicit operator choice to exit
immediately; nothing financial is deleted either way.

## Strategy definition (versioned, persisted)

`coinfly.strategy.definition.StrategyDefinition` (schema version 3): identity
`strategy_id@version`, label, `research_only`, controller profile
(`coinfly-dual-compartment-v1` = preserved MaleCNS; `fixture-sequence-v1` =
scripted pipeline fixture, SANDBOX only, always labeled `pipeline_fixture`),
mapping version (`fixed-decoder-spot-step-v1` unchanged; `fixed-decoder-derivatives-step-v1`
separately named), product track/subtype, permitted instruments, quote
currencies, market-data requirements (max age, depth, warm-up, window),
universe filters (tradable, spread, top-of-book notional, volume, order),
`max_active_slots`, `capital_budget`, per-slot exposure fraction, execution
settings (below), derivative permissions (leverage ≤ 10, directions, margin
mode, reversal; `margin_mode` and `max_maintenance_utilization` are stored and
digested but not yet enforced, since the paper book uses one pooled linear
margin), `learning_enabled` (research mode only), permitted
environments, `excluded_instruments` (operator exclusions, rejected as
`EXCLUDED_BY_OPERATOR`; overlap with the permitted list is invalid). Only
strategy schema 3 is accepted; any other `schema_version` is rejected, not upgraded. The
full JSON is stored in `cf_automation.config_json`; its digest is shown in status.

Execution settings (`ExecutionSettings`):

| Field | Default | Meaning |
| --- | --- | --- |
| `target_step` | 0.05 | the fixed-decoder step; must be ≤ `max_turnover_fraction` |
| `max_turnover_fraction` | 0.05 | per accounting window, ≤ 0.05 (the research limit) |
| `accounting_interval_seconds` | 60 | the turnover window (epoch-aligned, half-open) |
| `turnover_policy` | `REJECT` | `REJECT` refuses an order that does not fit; `CONSTRAIN` shrinks it to the largest feasible quantity |
| `order_ttl_seconds` | 30 | a resting limit older than this is cancelled and classified |
| `expiry_policy` | `ROLLBACK_UNFILLED_TARGET` | after expiry the intended target returns to what is held; `KEEP_TARGET` keeps it |
| `proposal_ttl_seconds`, `decision_interval_seconds` | 5, 2 | unchanged |
| `compute_budget_seconds` | 30 | the lock-free compute budget per tick; later slots are deferred, never skipped silently |

`research_paper_preset()` = `research-paper-spot@1`: preserved controller, spot
step mapping, USD quotes, 3 slots, 100 budget, 0.34 per slot, step 0.05 with
`turnover_policy=CONSTRAIN` (a 5 % step is exactly the allowance, so the first
order of a window is constrained to one increment below it).

## Exact turnover guard

`coinfly.strategy.turnover_ledger.TurnoverLedger` mirrors the research contract
(`turnover_admissible`: `spent + reserved + proposed < capital × fraction`) with
every term explicit and exact:

* window = `[k·interval, (k+1)·interval)` in UTC, shared by all slots of one
  strategy in one context (`cf_turnover_windows` records the window and its
  capital once);
* capital = the book's funded equity at the first admission inside the window;
* spent = gross filled notional (both sides, partial fills included) recorded
  inside the window (`ex_fills`);
* reserved = remaining notional of every non-terminal intent at a price
  ceiling (BUY: limit; SELL: max(limit, mark));
* proposed = quantity × price × multiplier of the planned order.

Equal is not admitted: 5.0000 proposed against a 5.0000 allowance is refused
(`TURNOVER_LIMIT_EXCEEDED`) under `REJECT`, or reduced to the largest multiple
of the quantity increment whose notional is strictly below the allowance under
`CONSTRAIN` (`TURNOVER_CONSTRAINED`, outcome code `ADMITTED_CONSTRAINED`; below
the instrument minimums → `TURNOVER_ALLOWANCE_INFEASIBLE`). The verdict with
all five terms is stored in the decision's provenance. Protective drains
(`LocalPaperEngine.drain` / `drain_slot`) do not pass through the guard. The guard is
applied per planned delta inside `LocalPaperEngine.submit_target(constraint=…)`,
which refuses any constraint that changes instrument, side or kind, or that
enlarges a quantity (`EXECUTION_CONSTRAINT_INVALID`).

The planner works from three distinct states — intended target (durable
`targets_json`), outstanding (non-terminal intents, signed) and actual
(`ex_positions`) — so a resting order is never re-issued and targets never
compound. Every proposal consumes one durable allocation revision whether or
not it executes (a refused proposal cannot leave the coordinator's revision
ahead of the ledger's).

## Order TTL

`cf_automation_orders` tracks every automation intent: `RESTING` → `SETTLED`
(`FILLED` / `TERMINAL_UNFILLED`) or `EXPIRED`. A resting order older than
`order_ttl_seconds` is cancelled (never converted into a market order) and
classified from the book at cancel time: `ORDER_EXPIRED_NONMARKETABLE` (the
limit was never crossed), `ORDER_EXPIRED_LIQUIDITY_MISSING` (crossed, nothing
executable at the displayed depth) or `ORDER_EXPIRED_PARTIAL` (with the filled
quantity). An intent whose resolution is `UNKNOWN` is never cancelled blindly.
The originating decision's durable outcome becomes `ORDER_EXPIRED` — neither a
HOLD nor an execution — and under `ROLLBACK_UNFILLED_TARGET` the intended
target returns to the held weight.

## Selection hysteresis

An owned instrument that drops out of the ranking is not drained at once: it is
blocked `RETIREMENT_GRACE:n/N` for `filters.retirement_grace_selections`
consecutive selections (the counter resets when it is selected again), then
retired through the protected drain. A retired instrument is not re-selected
before `filters.reentry_cooldown_seconds` (`SELECTION_COOLDOWN`); after that it
is re-admitted against the budget and its stopped slot restarts. An operator
stop or a context drain bypasses the grace. Both parameters are part of the
versioned definition. `tests/worker/test_turnover_planning.py` demonstrates all
of the above.

## Universe selection and ownership

`select_universe` ranks catalog entries deterministically (24 h volume desc —
in the quote currency when the venue reports it, because coin or contract
counts are not comparable across instruments — priority list, or id) with a
tie-break by instrument id and explains every rejection
(`EXCLUDED_BY_OPERATOR`, `NOT_PERMITTED`, `SUBTYPE_MISMATCH`, `QUOTE_NOT_PERMITTED`, `NOT_TRADABLE`,
`VOLUME_BELOW_MINIMUM`, `NO_TWO_SIDED_OBSERVATION`, `OBSERVATION_STALE`,
`DEPTH_BELOW_MINIMUM`, `SPREAD_ABOVE_MAXIMUM`, `TOP_OF_BOOK_BELOW_MINIMUM`,
`BEYOND_MAX_ACTIVE_SLOTS`) with thresholds and observed values; the selection
carries an inputs digest. Top-of-book notional includes the contract size.

Order-size feasibility is part of eligibility, not a preference: the smallest
order the venue accepts (one lot × price × contract size, or the quote
minimum) must fit the slot cap (sizing capital × `per_slot_max_exposure_fraction`,
unlevered like admission, else `MIN_ORDER_ABOVE_SLOT_CAP`), be strictly below
the turnover guard's window limit (sizing capital × `max_turnover_fraction`,
else `MIN_ORDER_ABOVE_TURNOVER_LIMIT`) and not exceed the context's
`max_order_notional` (`MIN_ORDER_ABOVE_ORDER_LIMIT`). The sizing capital is the
approved capital within the strategy budget and, on a paper book, at most its
funding. Without these checks a
slot would be owned for an instrument that can never receive an order, which
looks exactly like an automation that "does nothing".

`StrategyOwnership` owns slots with deterministic ids
(`uuid5("coinfly:strategy-slot:{strategy}:{context}:{scope}:{instrument}")`),
reserves budget transactionally (`min(per-slot cap, remaining)`, bounded by
`max_active_slots`), creates and starts slots through the existing `SlotService`
with deterministic request ids, never takes over a manual slot
(`MANUAL_SLOT_OCCUPIES_INSTRUMENT`), and retires departed instruments by the
protected drain (`RETIRING` → `RETIRED`, budget released).

## Full-MaleCNS runtime contract

The worker enforces, not merely documents, what "neural automation" means:

* **Graph identity.** `PreservedMaleCNSController` refuses any brain that is
  not the full preserved graph (166,700 neurons / 25,582,938 edges):
  `GRAPH_IDENTITY_MISMATCH`, with the observed sizes. There is no pruned,
  reduced or substituted graph and no scripted or hidden policy behind the
  preserved profile. Plasticity runs only in research mode with the declared
  `NeuralProfile(learning=True)`; otherwise the weights are frozen and their
  digest is verified around every observation
  (`NEURAL_WEIGHTS_MUTATED_WHILE_FROZEN`).
* **Native execution required.** The `kernel` component stays not-ready until
  a verified native kernel is present; the status says so in words ("Native
  execution of the full graph is required before neural automation is ready;
  no Python or reduced-graph substitute runs"). Nothing runs in its place.
* **Explicit test mode.** The scripted `fixture-sequence-v1` controller can be
  configured only with `pipeline_test_mode: true` (`FIXTURE_REQUIRES_TEST_MODE`
  otherwise), only in SANDBOX (`TEST_MODE_REQUIRES_SANDBOX`). The flag is part
  of the persisted configuration and of every status block
  (`pipeline_test_mode`, `strategy.pipeline_test_mode`), every decision record
  says `pipeline_fixture`, and the desktop shows "PIPELINE TEST MODE". The
  preset never needs it.
* **Provenance.** `status.provenance` reports the controller (profile, mode:
  `BASELINE_INFERENCE` / `PIPELINE_FIXTURE`, checkpoint or
  `UNTRAINED_BASELINE`), the graph (neurons, edges, dataset digest, initial
  weight digest), the kernel (binary/source digests, signed identity), the
  neural profile and the feature-transform version (`sensory-frame-v1`).
  Every decision adds its slot's neural-state generation, the native frame
  counters (cursor, sim_ms, total_spikes), the generation tokens it was
  computed under and the turnover verdict.
* **Traceable orders.** `cf_intent_causes` labels every intent:
  `DECODED_DECISION_ENTRY` / `DECODED_DECISION_EXIT` with the decision id and
  the controller that produced it (fixture decisions carry
  `pipeline_fixture=1`), `PROTECTIVE_DRAIN` (context or slot drain, operator
  stop) or `OPERATOR_ACTION`. An intent without a label is reported as
  `UNLABELED`, never inferred. The counts are in
  `execution_health.intent_causes`. HOLD is a valid, recorded outcome
  (`GENUINE_HOLD`), not a failed trade.

`tests/worker/test_malecns_contract.py` exercises the refusals, the flag, the
provenance and the labels at the RPC boundary.

## Controller modes and the research reward loop

`StrategyDefinition.neural` (schema 3) declares which preserved controller
runs; nothing is chosen for the operator and nothing learns silently:

| Mode | Weights | Requires | Provenance `checkpoint.kind` |
| --- | --- | --- | --- |
| `BASELINE_INFERENCE` (default) | the preserved initial efficacies, frozen | dataset + native kernel | `UNTRAINED_BASELINE` (+ initial weight digest) |
| `FROZEN_CHECKPOINT` | restored from `neural.checkpoint_id` (a file under the application's `models/checkpoints`), frozen | the checkpoint's provenance must match (`brain.restore`: graph digests, build, rule parameters, configuration signature, array layout), `weights_frozen`, a non-learning profile equal to the running one, and `neural.expected_training_profile_id` equal to the experiment the checkpoint declares (v2 and v3 are different experiments) | `FROZEN_CHECKPOINT` with file digest, experiment id/revision, restored cursor/spikes, plastic weight digest |
| `RESEARCH_LEARNING` | fresh qualified graph, plastic under `neural.training` (`TrainingProfile` + `RiskRewardConfig` built from the declared overrides) | `learning_enabled=true`, research mode, SANDBOX, the preserved controller, exactly one slot, `training.reward_interval_seconds == execution.accounting_interval_seconds` | `FRESH_QUALIFIED_GRAPH`, plus the full training/risk profiles |

`learning_enabled` is exactly the `RESEARCH_LEARNING` mode (both directions);
there is no "learning" mode without rewards. Refusals are explicit: `CHECKPOINT_NOT_FOUND`,
`CHECKPOINT_PROFILE_MISMATCH`, `CHECKPOINT_NEURAL_PROFILE_MISMATCH`,
`CHECKPOINT_NOT_FROZEN`, `CHECKPOINT_PROVENANCE_MISMATCH`,
`RESEARCH_MODE_REQUIRED`, `RESEARCH_LEARNING_SANDBOX_ONLY`,
`TRAINING_PROFILE_INVALID`.

**Research reward loop (`strategy.reward_ledger`, `strategy.research_controller`).**
The research controller is the unchanged `TrainingController` on the worker's
causal core: the worker's exact sensory frame becomes the `AuditedFrame`
(evidence: decision time, observed-through, available-through, fitted-through,
partition `train`), and `run_frame` runs `prepare_experimental_frame →
propagation/decoding → finalize_experimental_frame` with the paper ledger's
`RewardWindow` as the outcome. Reward windows are built from the ledger, not
estimated: equity samples at the start and end of every tick (the paper book
changes only inside ticks, so equity at a window boundary is exact); windows
of `accounting_interval_seconds` from the session start, contiguous in time
and equity (`capital(next) = equity(end)`), a tick gap closing the skipped
windows with zero P&L; executions from `ex_fills` with the decision-time
reference mid (`cf_automation_orders` v4), fill price, booked fee and the full
causal timestamp chain; exposure credits naming the originating admitted
decisions by their exact decision time (open lots by the decision that opened
them, window fills by theirs, fractions rounded down; drains earn none).
Windows are delivered strictly in order, one per observation, and marked
`FED`; a backlog is measured and reported (`status.research`), never dropped;
a window fed later than the declared `reward_credit_horizon_seconds` is still
fed and counted (`horizon_exceeded`). Credits older than the plasticity
profile's `maximum_credit_age_seconds` (≤ 150 s) are reported by the engine as
`expired_credit_decisions` / `missing_credit_decisions` in the decision's
provenance — long-held inventory is not re-timestamped and the horizon is not
worked around. An insolvent paper account (`REWARD_CAPITAL_INVALID`) fails the
session explicitly. A restart or re-enable starts a new session (new windows);
nothing resumes.

**Fenced disposal in research mode.** The experimental session (eligibility
ring and tags, traces, dopamine state, gain, reward history, causal cursors,
frame counter) is snapshotted before every observation; a discarded result
restores the session and the brain to the last committed generation, so
learning never advances on discarded work and the reward window it consumed
stays `CLOSED` for the next observation. Evidence:
`tests/strategy/test_research_controller.py`,
`tests/strategy/test_reward_ledger.py`, `tests/worker/test_research_session.py`
(synthetic full-graph-shaped controller: worker mechanics, not neural
behavior) and `tests/strategy/test_checkpoint_modes.py`.

## Neural resources and multi-slot isolation

`SharedGraphSlotStates` keeps one shared graph per controller and swaps the
complete per-slot dynamic state: every `brain.fields` array (including
`r8_light`), the native counters (`cursor`, `sim_ms`, `total_spikes`),
`plasticity_diagnostics`, and the plastic edge weights when learning. The
inventory is asserted at construction (`NEURAL_STATE_INVENTORY_INCOMPLETE`,
`NEURAL_STATE_ALIASED`); frozen weights are digest-verified around every
observation (`NEURAL_WEIGHTS_NOT_FROZEN`, `NEURAL_WEIGHTS_MUTATED_WHILE_FROZEN`);
a slot's state advances only through `commit` (generation +1), `discard`
restores the last committed generation, a failed swap leaves no active slot,
and a released slot's uncommitted work is disposed. The byte budget is measured
from the real arrays and admits a bounded number of slots
(`NEURAL_STATE_BUDGET_EXCEEDED`); usage is reported in status (`resources`).
`tests/strategy/test_neural_slot_isolation.py` proves, by exact digest
comparison, that slot A observed alone equals slot A interleaved with slot B in
frozen and in learning mode, and covers simultaneous timestamps,
retirement/recreation, an exception during a swap, aliasing and the budget;
the test includes a synthetic full-neuron-count state swap. It does not establish
full retained graph latency, per-slot RSS or target-hardware qualification.

## Persistence and schema

This is a fresh 1.0.0 format. `storage/schema.sql` defines the root ledger;
`cf_application_format` pins its identity. Each worker component installs its
final DDL inside the writer transaction and records an immutable digest in
`cf_schema_contracts`. Reopen verifies actual tables/indexes, not only markers.
Old `cf_schema_versions`, unmarked component tables, unknown formats and modified
structure are refused. There are no additive upgrade sequences or imports.
Existing incompatible data is never erased or reseeded to make initialization pass.

Automation, decisions, order lifecycle, recovery receipts, strategy-owned slots,
approved capital, turnover windows, intent causes, research rewards and live
mandates remain separate current-format tables. Configuration, budget approval
and the idempotency receipt commit together. The whole plan digest controls
component reuse; strategy name/version alone cannot conceal a changed plan.

`tests/storage/test_component_schema.py` and `tests/refactor/` cover final-format
installation/reopen, legacy rejection, structure tampering, capital/action scope,
mandates and deferred-frame ownership. Scientific v2/v3 profile IDs and
checkpoint provenance remain distinct from application persistence.

**Where the desktop shows it.** Overview's automation panel renders the
controller provenance (mode, graph identity, kernel digest, checkpoint or
untrained baseline, feature transform), `PIPELINE TEST MODE`, the research
session (reward windows closed/fed/pending, credit horizon, last training),
the recovery policy with the last decision and the restoration report, and the
catalog's exclusions (counts by code, unverified subtypes). Settings renders
the worker build identity next to the desktop's (protocol, source fingerprint,
Python; a mismatch is a warning), the retirement state, and the same
provenance and readiness lines — all from the one `state.snapshot` the worker
publishes; the desktop derives none of it locally.
