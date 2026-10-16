# ADR 0005 — Exclusive engine context

Status: accepted architecture; local paper lifecycle implemented and tested; native/live deployment qualification incomplete.

An account authority owns at most one mutation-capable engine context. `ContextKey` consists of `authority_id`, `environment`, `product_track`, `broker_profile_id` and a monotonic `epoch`. Multiple slots may operate within its selected track. Read-only observation of other approved accounts/products does not gain mutation authority. The authority lock is independent of aliases, track, environment and run directory; a separate host requires independent fencing before takeover.

The GUI proposes a change through a worker-owned preview. The worker persists the preview/configuration digest, expected revision and epoch, and request receipt. Apply accepts one immutable proposal, fences new source admissions and advances the transition epoch before asynchronous drain work begins. A repeated request resolves to its existing operation; a competing structural change conflicts. The effective source context remains visible and retains only the permissions needed to resolve its obligations.

| Durable phase | Meaning and authority boundary |
|---|---|
| `REQUESTED`, `QUIESCING` | Proposed configuration exists separately. New entry/model admission is fenced; source work is quiesced. |
| `DRAINING`, `RECONCILING` | Source-owned orders and positions are resolved through source-bound routes. Slippage, participation and deadline constraints still apply. |
| `STOPPED_OLD` | Fresh closedness evidence and mutation retirement have committed. The source has no remaining mutation authority. |
| `RESETTING`, `INITIALIZING_NEW` | Clear only transient decisions, queues and grants. Construct the target from its own financial truth after the source barrier. |
| `READY_UNARMED` | The target is now effective, with admissions fenced. Explicit revision/epoch-bound Arm is separate. |
| `BLOCKED_DRAIN` | Source obligations or stop evidence are unresolved. The target has not been activated. |
| `FAILED_INITIALIZATION` | The source has retired or no source existed; target creation failed. Remain stopped and fenced without automatic rollback or rearming. |

`StopCertificate` requires exact zero residual delegated positions, terminal related orders, no unresolved potentially accepted creates, no unapplied observed fills, complete fresh scope/account watermarks and fenced dispatch tasks. `RetirementEvidence` separately confirms transports and signing handles retired, tasks joined or fenced, and grants revoked. The writer rechecks the source fact sequence and valid retirement immediately before committing the target. An expired retired-source proof may be refreshed through read-only evidence only while retirement remains valid and the source fact sequence is unchanged, without restoring source mutation rights.

Financial history, balances, realized daily loss, drawdown and uncertain reservations survive handover. A newly discovered attributable source fill is booked in its original realm; it invalidates the source retirement proof and fences authority admissions, including an already initialized target. Deduplicated repeats do not create another financial fact. The system does not start another mutation engine to conceal the conflict. Autonomous broker recovery for this case is not integrated in this release; the durable incident remains visible for a qualified recovery path.

Slot lifecycle uses the same principle. Start enables scheduling without manufacturing a trade. Stop persists manual exclusion. Delete tombstones only after proof. Instrument Edit creates a durable replacement operation, drains the source, then atomically tombstones it and creates a new STOPPED slot if the context, reservation and stop facts still match. The replacement does not inherit a running scheduler or financial identity. Pending replacement targets exclude competing creates/edits; failed inserts roll back the source tombstone. Book/profile changes belong to the context transaction.

Implementation is in [context_coordinator.py](../../src/coinfly/control/context_coordinator.py), [context_interlock.py](../../src/coinfly/risk/context_interlock.py), [stop_certificate.py](../../src/coinfly/risk/stop_certificate.py), [slot_service.py](../../src/coinfly/control/slot_service.py) and [paper_engine.py](../../src/coinfly/worker/paper_engine.py). Evidence is in [test_context_coordinator.py](../../tests/control/test_context_coordinator.py), [test_stop_proof.py](../../tests/risk/test_stop_proof.py), [test_slot_service.py](../../tests/control/test_slot_service.py), [test_ledger_crash_and_late_fill.py](../../tests/faults/test_ledger_crash_and_late_fill.py) and [test_paper_worker_flow.py](../../tests/integration/test_paper_worker_flow.py).

The current integrated lifecycle is local paper. Flat Spot↔Perpetual context transitions are tested. Paper perpetual dispatch is admitted only against the bound synthetic `PaperLinearMarginModel` of the local book (otherwise `PERPETUAL_MARGIN_UNCERTIFIED`); no live derivative margin is certified. A local process lock does not establish account-wide exclusivity against independent clients, and fixture stop proofs do not certify an exchange evidence producer. Operational handling is described in [context-switch-and-late-fill.md](../runbooks/context-switch-and-late-fill.md).
