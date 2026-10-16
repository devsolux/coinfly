# Validation evidence

This page records the latest local validation run and what it does **not**
cover. Rerun the commands below to reproduce it; numbers from older runs are not
carried forward.

## Latest run — 2026-10-16

Host: macOS 27.0.1 on Apple Silicon (ARM64), Python 3.14.8 linked to SQLite
3.54.0, locked environment from `uv sync --locked --extra desktop --extra research
--extra training`.

| Check | Command | Result |
| --- | --- | --- |
| Lint | `ruff check src tests scripts packaging dev.py` | Passed |
| Types | `mypy src/coinfly` (strict) | Passed, 201 source files |
| Tests | `python -m pytest -q` | **1285 passed, 14 skipped**, 236 subtests passed |
| Wheel and sdist | `uv build --no-sources`, then `scripts/verify_artifact.py` | Passed |
| Doctor | `scripts/verify_doctor.py` | Passed: research dependencies ready, production blocked (exit 2 by design) |
| Shell scripts | `bash -n` and `shellcheck -S warning` on `dev.sh` and `scripts/*.sh` | Passed |

The 14 skips are expected on this host:

- 12 native desktop smoke tests are opt-in; run them with `./dev.sh test-ui` or
  `python dev.py test-ui` in a logged-in desktop session.
- 1 test needs the real Win32 kernel API (Windows AMD64 only).
- 1 test needs an interpreter linked below the minimum SQLite version.

## Not covered by this run

| Area | Status |
| --- | --- |
| Native desktop smoke tests (`test-ui`) | Not run |
| Full MaleCNS graph (`prepare`, native kernel, full-graph paper run) | Not run; unit tests use small synthetic graphs |
| Windows AMD64 and Linux AMD64 native runs | Not run; the CI workflow is manual-only |
| Signed or unsigned production builds | Not run; see [macOS production builds](devsh-production-builds.md) |
| Live accounts, orders, transfers or enrollment | Never used in testing |

A passing suite establishes the tested engineering contracts only. It does not
certify native signing, Windows IPC, publisher artifacts, account eligibility,
live gateways, full-graph latency, biological fidelity or profitable learning.
See [implementation status](implementation-status.md) for remaining work.
