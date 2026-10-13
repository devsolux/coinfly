# Coinfly

**1.0.0** · **com.devsolux.coinfly** · Publisher: **devsolux** · [MIT License](LICENSE)

> **Experimental.** Coinfly is shared as-is for research and education. There is
> no guarantee that it works, is stable or will be updated, and no claim that it
> trades profitably. It is not financial advice. Use it at your own risk.

Coinfly is desktop trading research software. It feeds crypto market data into
the full retained **MaleCNS v1.0** male fruit-fly connectome (166,700 neurons,
25,582,938 edges, no pruning) and lets identified output neurons propose paper
trades. An independent worker owns the autonomous loop, exact accounting, risk
limits and isolated paper execution; the desktop only sends commands and shows
status. Targets: **macOS ARM64, Windows AMD64 and Linux AMD64**.

## How it works

Each worker tick runs these separate stages for every strategy-owned slot:

1. **Observe** – read an order book from a labeled source: Coinbase public
   REST/WebSocket, a recorded replay file, or a synthetic test fixture. Stale
   data, sequence gaps and silent feeds block decisions.
2. **Sense** – four fixed features (mid offset, spread, depth imbalance, one-step
   return) become exact integer ticks, are scaled only with statistics fitted on
   earlier data, and are rendered as an 8-bit RGB frame. A photoreceptor proxy
   delivers that frame to the graph. This is an engineered input, not a market model.
3. **Propagate** – the native C++ spiking kernel runs the whole graph.
4. **Decode** – a fixed decoder reads the DNp20 (left/right) and DNpe017
   descending neurons as BUY, SELL or HOLD. A HOLD is never replaced by a
   scripted trade.
5. **Map** – a fixed mapping moves only the chosen instrument's target weight,
   by at most 0.10 of strategy capital for spot. Paper perpetuals use a separate
   signed mapping.
6. **Admit** – an exact Decimal planner, turnover guard and risk actor check
   capital, exposure, drawdown and loss limits. Risk may reject an order; it
   never chooses a different one.
7. **Execute** – the order intent is stored before dispatch, filled in the paper
   book and reconciled. An unknown outcome halts execution until reconciliation;
   orders are never blindly retried.
8. **Reinforce** *(research learning mode only)* – paper-ledger profit/loss
   becomes engineered stimulation of two identified dopamine cells (PAM11,
   PPL101). A bounded three-factor rule changes only existing KC→MBON synapses.
   This does not model pain, pleasure or consciousness.

Baseline and frozen-checkpoint controllers do not learn. Profitable learning is
unverified. Details: [autonomous lifecycle](docs/autonomous-lifecycle.md),
[dataset bootstrap](docs/malecns-bootstrap.md), [training](docs/malecns-training.md)
and [plasticity kernel](docs/malecns-rstdp-kernel.md).

## What is supported

| Area | Supported | Not supported or not yet qualified |
| --- | --- | --- |
| Platforms | macOS ARM64, Windows AMD64, Linux AMD64 from source; Python ≥3.11 with SQLite ≥3.51.3 | Only Linux kernel build/load and component tests ran; Windows/macOS native runs and full-graph desktop operation were not exercised |
| Execution | Paper (SANDBOX) trading with an exact local ledger: spot and synthetic perpetuals | Real-money trading; live mode is closed by default |
| Venue | Coinbase public market data (no credentials); live Advanced Trade spot code path | Live spot qualification (enrollment, signing, account evidence); live derivatives; other exchanges |
| Market data | Coinbase public REST and WebSocket level2, JSON-lines replay, labeled synthetic fixtures | — |
| Neural controllers | Full preserved graph in baseline inference, frozen checkpoint, or single-slot paper research learning (`malecns-risk-adjusted-experiment-v2`/`-v3`) | Separate NeuralHost process; model promotion workflow; any validated trading result |
| Research tooling | Optuna studies with paired frozen controls; training and plasticity benchmarks | No development-fold or holdout result has been produced |
| Regions | GLOBAL and GERMANY policies through the capability resolver | Verified regional entitlements |
| Credentials | macOS Keychain, Windows Credential Manager, Linux Secret Service; no plaintext fallback | Linux native execution-vault bridge |
| Desktop | Tk/CustomTkinter UI in English and German | Packaged smoke tests on every target |
| Storage | Fresh 1.0.0 SQLite ledger pinned by schema digests | Migration or import of older or modified ledgers |

The full matrix is in [implementation status](docs/implementation-status.md) and
[venue capabilities](docs/venues/capability-matrix.md).

## Native development

Use native Python >=3.11 linked to **SQLite >=3.51.3**, with Tk, uv and the
locked dependencies. A native C++ toolchain and CMake are needed for the explicit
local research kernel build, not for ordinary application startup.

```sh
python dev.py setup
python dev.py prepare
python dev.py kernel
python dev.py run
```

On Windows, `py dev.py …` or `./dev.ps1 …` uses the same launcher. On macOS,
`./dev.sh` remains a convenience entry point. The portable launcher selects the
native virtual-environment layout and rejects unsupported CPU/OS combinations.

Preparation verifies all retained neurons and edges against pinned source/array
identities (about 1.1 GB of source downloads on first use). Application data
lives under the native account's `~/.devsolux/coinfly` root; development workers
have checkout-specific ledgers and authenticated local IPC. Paper execution never
acquires exchange-mutation authority.

In Overview, configure `malecns-autonomous`, its capital allocation, slot limit,
feed and baseline/frozen-checkpoint controller, then enable automation. The worker
selects eligible instruments and manages owned slots. A native kernel and valid
observations are required. Fixture controllers are for isolated tests, not
ordinary strategy choices.

```sh
python dev.py test
python dev.py test-ui
python dev.py check
```

## Fresh 1.0.0 format

This is a fresh-install project, not an upgrade distribution. `storage/schema.sql`
and final component DDL are pinned by format/digest contracts. Old or modified
ledgers are rejected without automatic migration, deletion, import or reseeding.
Application version 1.0.0 is distinct from preserved scientific v2/v3 profiles.

## Delivery boundaries

No live credentials, orders or transfers were used in testing. Production
enrollment/evidence providers, Linux native execution signing and production
target qualification remain incomplete, so the default release path stays closed
until genuine prerequisites are supplied; no test evidence satisfies them.

macOS production entry points are `./dev.sh build-unsigned` and `./dev.sh build`,
with optional `--preflight`. Both read reviewed publisher metadata from
`~/.devsolux/coinfly` by default and require the same production gates; missing
inputs never select development packaging. See [desktop packaging](packaging/README.md),
[setup and storage](docs/desktop-initialization-and-settings.md) and the
[documentation index](docs/README.md).

## License

Coinfly's source code is released under the [MIT License](LICENSE). The MaleCNS
v1.0 connectome data is not bundled: it is downloaded from the upstream
[FlyEM/Janelia release](https://male-cns.janelia.org/download/) on first use and
remains subject to its publisher's terms.
