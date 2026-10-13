# Root cause: `Not initialized` / `FAILED_INITIALIZATION` / `ENGINE_OPERATION_FAILED`

Investigated 2026-09-21 against the actual application data of the affected
desktop (`~/.devsolux/coinfly/storage/development/<checkout-id>/`), the frozen
development bundle in `/Applications/coinfly.app`, and the worker/desktop
sources. Nothing was deleted or reset; every ledger was read in place.

## Symptoms

* Overview showed the effective context as `Not initialized`; Settings showed a
  contradictory environment status.
* `cf_transition_operations` held one operation whose last phases were
  `INITIALIZING_NEW` → `FAILED_INITIALIZATION` 1.7 ms later, error code
  `ENGINE_OPERATION_FAILED`, with no engine schema row written.
* Every retry produced the same collapsed code. The desktop's `switching`
  flag stayed set, which disabled the normal controls, so neither retry nor a
  fresh preview was reachable from the UI.

## Root cause (three independent defects, one visible failure)

1. **Stale worker versus rebuilt desktop.** The frozen bundle's worker
   (started 16:58 UTC) kept serving a desktop rebuilt ~20:33 UTC. PyInstaller
   resolves lazy imports by *file name inside the bundle*, so the old worker
   imported the new desktop's `paper_engine` module tree partially and failed
   inside the engine factory with a plain `ModuleNotFoundError`. The worker had
   no way to say "I am not the same program as you".
2. **Diagnostics collapsed.** `ContextCoordinator._run` converted every
   non-`DomainError` into `ENGINE_OPERATION_FAILED`, discarding the exception
   type chain, the failing stage and whether a retry was safe. `BootstrapError`
   is a `RuntimeError`, not a `DomainError`, so even coded bootstrap failures
   were collapsed.
3. **First activation had no explicit path.** With no previous context the
   coordinator still recorded `STOPPED_OLD`/`RESETTING` phases for a source
   that did not exist, and the desktop treated any non-terminal phase as an
   in-progress switch. A failed first activation therefore looked like a switch
   that never finished.

## Fixes (with regressions)

| Defect | Change | Regression |
| --- | --- | --- |
| Collapsed diagnostics | `TransitionFailure` (stage, stable code, category DOMAIN/CODED/IMPORT/STORAGE/UNEXPECTED, redacted exception chain, `retry_safe`, `corrective_action`) persisted with the operation; the worker log (`logs/worker-<profile>.log`, redacting formatter) records the same record. Exception *messages* are never read: only types, `.code`, ImportError module names and errno symbols. | `tests/control/test_context_coordinator.py` (uncoded factory exception keeps stage/chain/action; coded non-domain error keeps its code), `tests/worker/test_initialization_diagnostics.py` |
| Explicit first activation | `NO_PREVIOUS_CONTEXT` phase instead of fake source phases; failed initialization keeps the same operation id and epoch; retry never reseeds the paper book (`SIMULATION_SEED` count stays 1). | `test_first_activation_records_no_previous_context`, `test_failed_initialize_retry_does_not_bump_epoch` |
| Stale worker | `source_fingerprint()` (SHA-256 over the package sources) in every snapshot as `runtime`; the desktop compares it with its own and shows `WORKER_SOURCE_MISMATCH` with a `Restart stale worker` action (`worker.shutdown` RPC + automatic relaunch). Eager `LocalPaperEngine` import at supervisor start so a broken bundle fails at startup, not inside a transition. | `test_stale_worker_is_detected_and_can_be_restarted` |
| UI semantics | `DesktopState.switching` is true only for in-progress phases; `transition_retryable` and `transition_failure` are separate; Settings shows the same failure (phase · stage · code + corrective action) as Overview. | `tests/ui/test_context_balance_bootstrap.py`, native `tests/ui/test_native_desktop.py` |

## Recovery without destructive resets

* Restart the stale worker from the Overview (or quit and relaunch the app):
  the fresh worker recovers the existing ledgers, marks the authority
  `recovery_required` → attaches the recovered paper engine, and fences
  admissions.
* Retry the failed operation (`operation.retry`) — same operation id, same
  epoch, no new funding.
* If the failure persists, the Overview names the stage, the stable code and
  the corrective action (`ACTION_VERIFY_DATASET`, `ACTION_RESTART_WORKER`,
  `ACTION_RESOLVE_INCIDENTS`, `ACTION_CHECK_STORAGE`, `ACTION_RETRY_OPERATION`,
  `ACTION_INSPECT_WORKER_LOG`).

No step deletes `storage/`, caches or the paper book. Destructive resets are
not part of any recovery path and would require separate explicit approval.
