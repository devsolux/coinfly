# Refactor evidence — 1.0.0 fresh project

This file records the **current refactor session only**. It replaces stale claims
from the uploaded working documents, including a prior pass-through Numba shim.
No such shim was used here. The original uploaded archive remains unchanged.

Host: Linux AMD64, Python 3.13.5, linked SQLite 3.46.1; real Numba 0.65.1 and
NumPy 2.3.5. These are available container packages, **not the full locked release
environment**. An offline `pip install --no-deps --no-build-isolation -e .`
registered the actual source for isolated Python subprocesses; it did not install
or qualify locked dependencies. The production SQLite >=3.51.3 check is unchanged.

## Executed evidence

| Scope | Result | Limit |
| --- | --- | --- |
| Consolidated component regression command in delivery report | **317 passed, 1 skipped, 150 subtests passed** in 25.17 s | Overlapping earlier runs are not added to this count; native Win32 test skipped on Linux |
| Full test collection | **888 collected; 8 collection errors** | Missing `polars`; complete suite was not executed |
| Representative worker lifecycle test | Fails at `SQLITE_VERSION_UNSUPPORTED` before worker startup | Linked SQLite is 3.46.1; no production gate bypass |
| Linux C++ propagator build | CMake 3.31.6 / GNU C++ 14.2 compiled the preserved source | Development artifact only |
| Native library verification/load | `verify_native_kernel` + `LocalDevelopmentKernelVerifier`, then actual `ctypes.CDLL` load and `memory_advance` export resolution passed | No full-graph invocation, latency claim or native release attestation |
| Scientific preservation | All four research JSON manifests and `neural/kernel.cpp` byte-identical to input Git baseline | Python orchestration intentionally changed where described |
| Live domain regressions | Real domain/engine methods with isolated SQL and bounded HTTP doubles | Inline transaction fixture is not production Writer/WAL or real exchange evidence |
| Native desktop and real account operation | Not run | CustomTkinter/locked research dependencies/target OS and live enrollment not available |
| Ruff / strict mypy | Not run | Tools absent; syntax checks and selected tests are not substitutes |

## Implementation status by refactor area

| Area | Status | Evidence location |
| --- | --- | --- |
| Strategy capital and instrument-scoped proposals | Implemented, component regressions executed | `tests/refactor/test_capital_and_scope.py` |
| Deferred-frame cleanup and budget fairness | Implemented, focused actual-branch regression executed | `tests/refactor/test_accounts_maintenance_and_autopilot.py` |
| Final schema contracts / rejection of old or changed structure | Implemented, component SQL tests executed | `tests/storage/test_component_schema.py`, `tests/refactor/test_desktop_and_schema.py` |
| Live mandate limits/revocation and retained cancellation reservations | Implemented, domain regressions executed | `tests/refactor/test_live_boundaries.py` |
| Continuous maintenance / scoped account and open-order reads | Implemented, isolated parser/engine tests executed | `worker/maintenance.py`, `execution_accounts.py`, refactor tests |
| Positive Live service composition and supervisor recovery | Implemented; production integration not exercised | `venues/coinbase/live_services.py`, `worker/supervisor.py` |
| Windows IPC | Implemented; actual Win32 execution not exercised | `ipc/windows_native.py`, `ipc/transport_named_pipe.py` |
| Linux native model | Built, verified and loaded locally | `scripts/build_kernel.py`, `native/CMakeLists.txt` |
| Linux setup keyring / portable desktop packages | Source implemented; target desktop not exercised | `security/desktop_credentials.py`, `packaging/desktop/` |
| Separate NeuralHost process | Not implemented | Existing in-worker compute architecture retained |
| Production Live enrollment/evidence provider | Not implemented | Trusted injection interface exists; default remains closed |
| Live derivatives / Linux execution-vault bridge | Not implemented | Existing explicit release/capability failures retained |
| Full-graph desktop paper, native-platform soak and live qualification | Not performed | Required follow-on acceptance work, not inferred from fixtures |

See the external validation ZIP for logs and native-source hashes. Source artifacts
contain no account secrets, ledger data, compiled kernel or invented qualification.
