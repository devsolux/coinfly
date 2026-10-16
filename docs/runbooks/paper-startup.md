# Paper startup (credential-free)

For the fresh 1.0.0 format and all three desktop targets, use `python dev.py setup`,
`prepare`, `kernel`, then `run`. Native Python/Tk and linked SQLite >=3.51.3 are
required. Mac-specific `dev.sh` examples below are convenience equivalents.
See [validation evidence](../evidence-matrix.md): a scripted cycle is not a
full-graph or native desktop qualification. Legacy ledgers are not migrated.

Everything here runs without an exchange credential. `./dev.sh run` remains
paper-only: the development worker cannot construct a live adapter, and the
capability resolver closes every LIVE cell of a source checkout.

## 1. Prepare and start

```
./dev.sh prepare        # dataset verification (offline after the first run)
./dev.sh kernel         # optional: explicit local build of the native kernel
./dev.sh run            # local paper desktop + development worker
```

`./dev.sh kernel` runs `scripts/build_kernel.py`: CMake compiles the pinned
`kernel.cpp` (source SHA-256 must match `KERNEL_SOURCE_SHA256`), signs the
library (ad-hoc or `--sign-identity`), and writes
`~/.devsolux/coinfly/models/kernels/<source-hash>/libcoinfly_neural.dylib.attestation.json`.
The worker verifies that record on every load. The result is the
`LOCAL_DEVELOPMENT_KERNEL` capability: it lets the preserved MaleCNS controller
run for paper research and satisfies **no** release gate. Without it the
`kernel` component reports `NATIVE_KERNEL_UNAVAILABLE` and the preserved
controller is refused (nothing substitutes for it).

## 2. Create the paper context

Overview → Requested configuration: `SPOT` / `SPOT` / `SANDBOX` / `GERMANY`
(or any other combination the capability line reports as `SUPPORTED`) →
*Preview change* → confirm. The first activation records `NO_PREVIOUS_CONTEXT`
and applies the configured paper funding **once** for the new book
(`paper_funding`, default 100 USD, visible under *Balance* as
`SIMULATED — LOCAL PAPER BOOK`, provenance `LOCAL_PAPER_LEDGER`).

## 3. Configure automation

Overview → *Strategy automation*:

* Strategy: `malecns-autonomous` (preserved MaleCNS controller as baseline
  inference or a frozen checkpoint; step mapping with `target_step` 0.05 and
  the `CONSTRAIN` turnover policy; capital and slot limit set in the panel).
  This preset never learns; research learning needs an explicit research
  strategy definition.
* Data source — labeled truthfully everywhere:
  * `COINBASE_PUBLIC_WEBSOCKET` — public level2 + heartbeats, sequence-gap
    resync, bounded reconnects (real market data, no credential).
  * `COINBASE_PUBLIC_REST` — polled public order book (real market data).
  * `REPLAY_FILE` — a JSON-lines recording under
    `~/.devsolux/coinfly/datasets/replays/<name>` (`REPLAYED RECORDING — NOT LIVE`).
  * `SYNTHETIC_FIXTURE` (deterministic scripted books, `NOT MARKET DATA`) is
    for tests and explicit pipeline test mode only; the panel does not offer it.
* Approved capital (≤ the strategy budget), *Auto-manage slots*, *Research
  mode* (only then can a research strategy learn).

*Configure* persists the strategy for the authority; *Enable automation* arms
the context (confirmation dialog) and starts the worker-owned loop. The panel
then shows component readiness, the data source health (state, last message,
gaps, reconnects, resyncs), the selected universe with rejection reasons, the
last observation, the last decision with its reason (a scripted controller is
tagged `PIPELINE FIXTURE` in the strategy line, and explicit pipeline test mode
adds `PIPELINE TEST MODE`), execution health (open intents, unknown outcomes, fills,
fence) and the owned slots (strategy, selection reason, lifecycle, ownership,
target weight, actual quantity, budget, warm-up, last decision, blocking
reason).

## If automation does not trade

The Overview's automation panel names the blocking component and what to do
(`→` lines). In order of how often they apply:

1. `kernel: NATIVE_KERNEL_UNAVAILABLE` (state stays `SETUP`) — the preserved
   MaleCNS controller needs the native kernel: `python dev.py kernel`
   (`./dev.sh kernel`), then *Enable automation* again. Nothing substitutes
   for it; a scripted fixture runs only in explicit pipeline test mode.
2. `dataset: …` — `python dev.py prepare` (dataset verification).
3. `feed: FEED_CATALOG_UNAVAILABLE` — the public catalog could not be read;
   the worker retries on its own (network, proxy, rate limit).
4. `feed: NO_ELIGIBLE_INSTRUMENTS` with universe rejections
   `MIN_ORDER_ABOVE_SLOT_CAP` / `MIN_ORDER_ABOVE_TURNOVER_LIMIT` /
   `MIN_ORDER_ABOVE_ORDER_LIMIT` — a futures contract is larger than the
   strategy can ever hold or trade. Coinbase's nano contracts are hundreds of
   dollars each (BTC ≈ 865 USD, ETH ≈ 276 USD, SOL ≈ 590 USD at 2026-09-22
   prices; the smallest perpetual-style contract listed that day was
   1000SHIB at ≈ 61 USD). Under the 5 % turnover guard a contract needs an
   approved capital above 20 × its notional (≈ 17,300 USD for BTC, ≈ 1,220 USD
   for the smallest), a slot share that holds it, and a context
   `max_order_notional` at least that large. With a 100 USD paper book use a
   `SPOT` context (1 USD minimum orders) — or create a new perpetual-style
   context with larger paper funding and raise the risk limits in Settings.
5. `OBSERVATION_STALE` in the universe — the books are older than
   `max_observation_age_seconds`; prefer the WebSocket feed or fewer slots.

A genuine `HOLD` from the controller is shown as such and is not a fault.

## 4. Simulated execution

Paper fills are produced by the local matcher against the fed book: limit
orders rest at the validated mark, fill when the book crosses them **against
the displayed depth only** (a level's quantity is consumed once per book
snapshot, so a 0.04 order against 0.01 displayed fills 0.01 and rests for the
remainder), pay the configured fee, and honor the configured latency. **Queue
position is not modeled**: a resting order fills as soon as the opposite side
trades through its price, which is optimistic compared with real queue
priority. The balance snapshot states this (`fill_model`). Derivative paper
books use the synthetic linear margin (10 % initial / 5 % maintenance),
reduce-only enforcement and a synthetic settlement schedule per declared
subtype: funding at every interval boundary the clock crosses
(`PERPETUAL_NO_EXPIRY` every 8 h, `PERPETUAL_STYLE_FUTURE` hourly) at the
declared rate `PAPER_FUNDING_RATE_PER_INTERVAL` (1 bp, long pays), and cash
settlement of a `DATED_FUTURE` at its catalog expiry at the mark. Funding is
journaled as `PAPER_FUNDING`. Expiry closes the position through the ledger as
one fee-free synthetic fill (so lots, positions, cash, drawdown and stop proofs
all agree), recorded as a `PAPER_EXPIRY_SETTLEMENT` event and labeled with the
intent cause `VENUE_SETTLEMENT`, never as a strategy trade. Both appear in
`automation.status.settlements` as synthetic. None of this is presented as
venue behavior.

## 5. Pause, stop, restart

The development worker runs in the background and keeps running after the
desktop closes (it owns the paper book). When the checkout changes (a pull, an
edit, a patch), a worker started before the change runs the previous code: the
desktop shows `STALE WORKER` next to the context caption and the worker
refuses every command with `WORKER_BUILD_MISMATCH`. `./dev.sh run` and `./dev.sh worker` detect
this through the `worker.hello` handshake and retire that worker first
(gracefully: automation stopped, paper orders/positions drained), then start a
current one; the successor waits for the predecessor's authority lock
(`AUTHORITY_HANDOVER_PENDING`, up to 90 s) instead of blocking. To stop the
worker explicitly: `./dev.sh stop-worker` (graceful) or
`./dev.sh stop-worker --force` (immediately; the next start recovers the
ledger, fences and open obligations). The Overview's *Restart stale worker*
does the same from the desktop.

*Pause entries* fences admissions; *Stop and reconcile* retires every owned slot
through the protected drain and releases the budget. After a worker restart the
panel shows `RESTART_REQUIRES_REENABLE`; *Enable automation* re-applies the same
configuration (no new funding) and arms it once READY.

## Automated coverage

`tests/worker/test_automation_lifecycle.py` drives the real supervisor through
configure → enable → universe → owned slot → warm-up → BUY → fill → HOLD →
SELL → fill → pause → stop/drain → retire; restart → re-enable; derivatives
long → flat → short → flat and a short-residual drain. The scripted controller
and feed are labeled as fixtures in every record they produce.
