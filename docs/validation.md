# Verification

Install the locked desktop, research and training dependencies before running the
suite. `python dev.py setup` prepares the native development environment on macOS ARM64,
Windows AMD64 or Linux AMD64; macOS also retains `./dev.sh setup`.

```sh
./dev.sh check
./dev.sh test-ui
./dev.sh prepare
```

`check` runs shell syntax, Ruff, strict mypy and the test suite. Native widget
tests are opt-in through `test-ui`. `prepare` verifies the real full MaleCNS cache
or downloads and prepares the pinned sources when absent; it succeeds only at
READY. Unit tests use small, explicitly synthetic graphs and paper execution.

Portable source checks (including Windows PowerShell):

```sh
uv sync --locked --extra desktop --extra research --extra training
uv run ruff check src tests scripts
uv run mypy src/coinfly
uv run python -m pytest -q
uv run python scripts/verify_doctor.py
```

The suite covers Decimal accounting, durable order intent, unknown outcomes,
context fencing/recovery, current snapshot contracts, exact checkpoint provenance,
source and signed dataset integrity, native home paths, neural rules, causal reward
and experimental evaluation. The final root SQL schema is required runtime package data at `storage/schema.sql`.
Component contracts are final DDL, not ordered upgrade migrations. Old or unknown
ledgers are rejected without modification.

## Package identity and contents

```sh
uv build --no-sources
uv run python scripts/verify_artifact.py
uv run python scripts/build_release_archive.py
```

Build outputs go to `dist/`. The source archive excludes generated outputs,
operator data, credentials and caches. Application metadata is `coinfly`, `1.0.0`,
`com.devsolux.coinfly`; Windows uses the required four-part version `1.0.0.0`.
`./dev.sh clean` removes the environment and generated development files while
preserving application data both outside the checkout and in root data/storage/
datasets/models/runs/logs/cache directories. It also preserves `.git`, `.agents`,
`.codex`, `.env` files and lockfiles, and does not follow directory symlinks.
Cleanup regression tests operate only on disposable project fixtures.

For macOS production prerequisites, run:

```sh
./dev.sh build-unsigned --preflight
./dev.sh build --preflight
```

The corresponding builds omit `--preflight`. Both modes use the shared production
gates; the signed mode additionally validates the environment-resolved identity,
notary profile, signing, notarization and stapling. No signing flags are required.
Unset/empty signing values use the exact defaults in `dev.sh`; unsigned mode
ignores these inherited values. Missing publisher metadata never falls back.
See [native release assembly](../packaging/README.md) and the
[current change's issue/validation ledger](devsh-production-builds.md).

The regression suite exercises real Bash default resolution/quoting/exit statuses,
nonrecursive wrappers, mode-specific credential access, exact toolchain matching,
fixed deployment targets, common-stage failures and transactional publication.
External build tools in orchestration tests are fixtures. They do not establish
actual frozen application, minimum-OS, full-graph or distribution qualification.

## Qualification limits

A passing suite establishes the tested engineering contracts. It does not certify
native signing, Windows IPC, publisher artifacts, real account eligibility, live
gateways, full-graph latency, biological fidelity or profitable learning. The CI
configuration defines cross-platform jobs; local checks do not establish that
remote jobs ran. Use [native release assembly](../packaging/README.md) and
[current capabilities](implementation-status.md) to assess remaining work.

Synthetic performance harnesses are available in `scripts/benchmark_coinfly.py`,
`benchmark_training.py`, and `benchmark_plasticity.py`. Generate fresh measurements
for the qualified hardware and configuration; do not treat isolated-kernel
throughput as full application performance. No live orders are needed for checks.
