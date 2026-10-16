# Initialization failures: `Not initialized` / `FAILED_INITIALIZATION` / `ENGINE_OPERATION_FAILED`

This runbook explains a known failure pattern of context initialization, the
defects behind it, and how to recover without deleting or resetting any ledger.

## Symptoms

* Overview shows the effective context as `Not initialized`, while Settings
  shows a contradictory environment status.
* `cf_transition_operations` holds an operation whose last phases are
  `INITIALIZING_NEW` → `FAILED_INITIALIZATION` within milliseconds, with no
  engine schema row written.
* Every retry produces the same collapsed error code, and the controls stay
  disabled because the desktop believes a switch is still in progress.

## Causes (three independent defects, one visible failure)

1. **Stale worker versus rebuilt desktop.** A worker started from an older
   build keeps serving a rebuilt desktop. PyInstaller resolves lazy imports by
   file name inside the bundle, so the old worker can import part of the new
   module tree and fail inside the engine factory with a plain
   `ModuleNotFoundError`.
2. **Collapsed diagnostics.** The coordinator converted every non-`DomainError`
   into `ENGINE_OPERATION_FAILED`, discarding the exception type chain, the
   failing stage and whether a retry was safe. `BootstrapError` is a
   `RuntimeError`, so coded bootstrap failures were collapsed too.
3. **No explicit first-activation path.** Without a previous context the
   coordinator recorded `STOPPED_OLD`/`RESETTING` phases for a source that did
   not exist, and the desktop treated any non-terminal phase as an in-progress
   switch.

## Fixes and regression tests

| Defect | Change | Regression |
| --- | --- | --- |
| Collapsed diagnostics | `TransitionFailure` (stage, stable code, category DOMAIN/CODED/IMPORT/STORAGE/UNEXPECTED, redacted exception chain, `retry_safe`, `corrective_action`) is persisted with the operation and written to the worker log (`logs/worker-<profile>.log`, redacting formatter). Exception *messages* are never read: only types, `.code`, ImportError module names and errno symbols. | `tests/control/test_context_coordinator.py`, `tests/worker/test_initialization_diagnostics.py` |
| First activation | A `NO_PREVIOUS_CONTEXT` phase replaces fake source phases; a failed initialization keeps the same operation id and epoch; a retry never reseeds the paper book (`SIMULATION_SEED` count stays 1). | `test_first_activation_has_explicit_no_previous_context_path`, `test_failed_initialize_records_stage_and_retry_recovers_without_new_epoch` |
| Stale worker | Every snapshot carries `source_fingerprint()` (SHA-256 over the package sources) as `runtime`; the desktop compares it with its own, shows `STALE WORKER` and offers *Restart stale worker* (`worker.shutdown` RPC plus automatic relaunch). The supervisor imports `LocalPaperEngine` eagerly, so a broken bundle fails at startup rather than inside a transition. | `test_snapshot_carries_runtime_identity_for_stale_worker_detection` |
| UI semantics | `DesktopState.switching` is true only for in-progress phases; `transition_retryable` and `transition_failure` are separate; Settings shows the same failure (phase · stage · code + corrective action) as Overview. | `tests/ui/test_context_balance_bootstrap.py`, native `tests/ui/test_native_desktop.py` |

## Recovery without destructive resets

* Restart the stale worker from the Overview (or quit and relaunch the app).
  The fresh worker recovers the existing ledgers, marks the authority
  `recovery_required`, attaches the recovered paper engine and fences
  admissions.
* Retry the failed operation (`operation.retry`): same operation id, same
  epoch, no new funding.
* If the failure persists, the Overview names the stage, the stable code and
  the corrective action (`ACTION_VERIFY_DATASET`, `ACTION_RESTART_WORKER`,
  `ACTION_RESOLVE_INCIDENTS`, `ACTION_CHECK_STORAGE`, `ACTION_RETRY_OPERATION`,
  `ACTION_INSPECT_WORKER_LOG`).

No step deletes `storage/`, caches or the paper book. Destructive resets are
not part of any recovery path and require separate explicit approval.
