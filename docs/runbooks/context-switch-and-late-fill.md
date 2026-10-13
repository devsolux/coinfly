# Context switch and late-fill runbook

This source release has a tested local paper lifecycle. It has no enabled live gateway, certified native signer or automated broker recovery service. The steps below describe implemented operator commands and the evidence needed for release integrations; they do not enable live execution. See [implementation status](../implementation-status.md), [ADR 0005](../adr/0005-exclusive-engine-context.md).

For a normal structural change:

1. Inspect the **effective** authority, environment, track, profile and epoch alongside the requested settings. Review existing slots, unknown orders, alarms and source obligations. The requested selection has no execution authority.
2. Use the UI preview/apply workflow. `context.preview` checks the proposed configuration and returns a worker-owned preview. Confirm the source/target and protected drain policy. `context.apply` binds the preview, configuration revision, epoch and request identity and returns a durable operation ID.
3. Follow that operation through `operation.get`. A lost response is resolved using its operation ID or original request ID. A second Apply is not a replacement for resolving the first request.
4. During `QUIESCING`, `DRAINING` and `RECONCILING`, keep the worker running. Its source-bound accounting, reconciliation and protection remain responsible for the old context. In a future certified Live→Sandbox handover, any source drain orders would still be real live orders until the source retires.
5. Treat `READY_UNARMED` as completion of the structural handover. Inspect the new effective context and its independent book state. Arm requires separate explicit confirmation with the current revision and epoch. Old proposals and pending UI commands cannot arm the new context.

| Observed condition | Required response |
|---|---|
| `BLOCKED_DRAIN` / unresolved create | Preserve the reservation, fence and operation. Obtain source-bound order/history/account evidence through reconciliation. `operation.retry` retries lifecycle work; it does not blindly resend an uncertain create. |
| `DRAIN_FRESH_MARKET_REQUIRED` | Wait for a qualified fresh source snapshot. The paper worker currently has no autonomous feed to repair this condition; integration tests supply explicit snapshots. Do not synthesize a price. |
| `DRAIN_DUST_OR_LIQUIDITY_BLOCKED`, `DRAIN_AWAITING_LIQUIDITY`, or deadline exceeded | Keep the residual quantity visible. Preserve approved price/liquidity limits; a deadline cannot make an unfilled position zero. |
| Incomplete or stale stop evidence before retirement | Reconcile and produce fresh complete evidence before the stop barrier. No target construction is permitted while proof remains incomplete. |
| Expired proof after retirement, with retirement still valid and source fact sequence unchanged | Refresh through read-only source evidence without regaining mutation rights. |
| Changed source fact sequence or invalidated retirement after the stop barrier | Preserve the authority fence and follow the late-fact recovery procedure. A fresh certificate alone cannot repair invalidated retirement. |
| `FAILED_INITIALIZATION` | Remain stopped and fenced. Correct the target's actual missing prerequisite, then explicitly retry the existing operation. The source does not automatically resume. |
| Missing/corrupt MaleCNS assets | New initialization and Arm remain blocked. `state.snapshot`, history, `safety.pause_entries`, `safety.stop` and reconciliation remain available. Cancelling a download does not stop the engine. |
| `PERPETUAL_MARGIN_UNCERTIFIED` | The current worker cannot safely dispatch this track. Preserve the gate; a tested synthetic margin component is not the required integrated risk evidence. |

`safety.pause_entries` persists an admission fence. `safety.stop` requests protected slot drains or joins/retries the current context operation while the supervisor stays alive. Existing slot Stop, Delete or Edit operations are joined, and a blocked slot does not prevent independent slots from receiving their stop commands. Inspect both the returned operations and per-slot errors; failed commands retain their identity and produce an alarm/history event when persistence is available. A Stop response acknowledges the operation, not guaranteed immediate flatness. Start enables slot scheduling without forcing a purchase. Delete and instrument Edit leave the source visible while draining; after proof, Edit creates a new STOPPED replacement and requires a separate Start. Follow the existing operation rather than repeatedly issuing conflicting edits.

If a newly discovered old-context fill appears after retirement:

1. The fact reducer retains its original context/realm, deduplicates it, books the financial fact and invalidates source closedness. Target admissions are immediately fenced and a sticky incident records the source obligation. An unattributed authenticated fill remains in suspense and also fences risk.
2. Review the incident, originating intent/order/trade identity, old proof/fact sequence, current target obligations and source scope. Preserve both histories. A tombstoned slot with invalidated proof is included when its original context is listed and remains auditable in source history/incidents. The current desktop slot table lists the effective context, so a retired-source slot need not appear in that table.
3. Keep admissions paused. Acknowledging an alarm records acknowledgment only; it does not clear the fence. Do not clear SQL rows, reset the book, reassign the fill to the target or launch another mutation worker.
4. A qualified recovery integration must first quiesce/drain the current context under its own permissions and then establish whether one source recovery context can safely be authorized. That automatic broker recovery integration is absent here; unresolved external obligations require the broker's authenticated operational process. Selecting GLOBAL or SANDBOX does not supply missing permission.

After a crash, the worker converts possibly dispatched work into UNKNOWN where required, restores the committed paper book without reseeding, discards market freshness, and starts unarmed/fenced. A database restore cannot rewind venue activity. External-host fencing and reconciliation of activity missing from a restored database are mandatory prerequisites for a future live recovery; no source command currently certifies them.

The September audit makes repeated terminal evidence idempotent: a price update
alone cannot advance the financial fact sequence for an already terminal order.
An actual new fill still advances it and invalidates old proof. Context and slot
drains share one planner within each paper engine; they cannot cancel one another's
protective orders while planning concurrently. A matching or persistence failure
restores the committed financial state, discards matcher liquidity freshness and
fences the authority. Inspect the incident and obtain fresh market evidence before
an authorized recovery; do not clear the fence merely to retry a create.

Completed operation futures and retired engines are released from memory only
after lifecycle completion. Durable receipts, immutable source snapshots, fills
and stop evidence remain available. Late-fill accounting uses the original
persisted context and does not depend on retaining a retired engine object.

Trade Results requires closed-episode evidence separately from booked fills or administrative STOPPED state. The current [paper finalizer](../../src/coinfly/accounting/paper_episodes.py) supports exact SANDBOX/SPOT USD round trips only. Partial or unresolved episodes return `PAPER_EPISODE_NOT_SETTLED`; unsupported reporting currency returns `PAPER_REPORTING_CURRENCY_UNSUPPORTED`, with no closed row inserted. Do not fill the report by inferring charge completeness from flatness or a snapshot. The local matcher has explicit quote fees and no deferred funding/charges; that assumption does not extend to live brokers or perpetuals. Conflicting closure evidence creates an incident and preserves immutable history. See [test_paper_episodes.py](../../tests/accounting/test_paper_episodes.py).

Implementation references: [context_coordinator.py](../../src/coinfly/control/context_coordinator.py), [reconciler.py](../../src/coinfly/execution/reconciler.py), [fact_reducer.py](../../src/coinfly/execution/fact_reducer.py), [slot_service.py](../../src/coinfly/control/slot_service.py) and [supervisor.py](../../src/coinfly/worker/supervisor.py). Executable scenarios are in [test_context_coordinator.py](../../tests/control/test_context_coordinator.py), [test_ledger_crash_and_late_fill.py](../../tests/faults/test_ledger_crash_and_late_fill.py), [test_slot_service.py](../../tests/control/test_slot_service.py) and [test_paper_worker_flow.py](../../tests/integration/test_paper_worker_flow.py).
