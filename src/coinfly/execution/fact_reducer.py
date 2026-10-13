"""Private facts, journal, lots, reservation and acceptance commit atomically."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import localcontext
from typing import Callable

from coinfly.accounting.journal import JournalEntry, JournalFrame, post_tx, utc_now
from coinfly.accounting.lots import apply_lots_tx
from coinfly.accounting.reservations import consume_for_fill_tx, release_tx
from coinfly.domain.context import ContextKey, DomainError, ProductTrack
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.evidence import alarm_tx, event_tx, fence_authority_tx
from coinfly.execution.intent_store import get_tx
from coinfly.execution.models import Fill, IntentRecord, Resolution, Side, Submission
from coinfly.storage.writer import Writer


@dataclass(frozen=True)
class FillResult:
    fill_id: str
    applied: bool
    journal_id: str | None
    fact_sequence: int
    incident: str | None = None


def _increment_fact_tx(connection: sqlite3.Connection, context: ContextKey) -> int:
    connection.execute(
        "INSERT INTO ex_fact_state VALUES(?,1) ON CONFLICT(context_id) DO UPDATE SET sequence=sequence+1",
        (context.identity,),
    )
    return int(
        connection.execute(
            "SELECT sequence FROM ex_fact_state WHERE context_id=?", (context.identity,)
        ).fetchone()[0]
    )


def _accept_tx(
    connection: sqlite3.Connection, record: IntentRecord, order_id: str, evidence_id: str
) -> str | None:
    incident: str | None = None
    if record.submission == Submission.REJECTED:
        incident = "ACCEPTANCE_AFTER_REJECTION"
        # New authoritative acceptance invalidates the prior terminal/no-creation
        # conclusion. Restore the executable remainder's reservation before commit.
        connection.execute("UPDATE ex_outbox SET terminal=0 WHERE intent_id=?", (record.intent.intent_id,))
        consume_for_fill_tx(connection, record.intent, record.filled_quantity)
    if record.venue_order_id is not None and record.venue_order_id != order_id:
        incident = "CONFLICTING_ORDER_IDENTITY"
    if incident:
        fence_authority_tx(connection, record.intent.context, incident, evidence_id)
    connection.execute(
        "UPDATE ex_outbox SET submission='ACKNOWLEDGED',resolution='RESOLVED',venue_order_id=COALESCE(venue_order_id,?),updated_at=? WHERE intent_id=?",
        (order_id, utc_now(), record.intent.intent_id),
    )
    # The create uncertainty is resolved by acceptance; any incident fence remains.
    connection.execute(
        "UPDATE ex_slot_fences SET active=0 WHERE intent_id=? AND reason='UNKNOWN_SUBMISSION'",
        (record.intent.intent_id,),
    )
    return incident


def apply_fill_tx(connection: sqlite3.Connection, fill: Fill) -> FillResult:
    existing = connection.execute(
        "SELECT economic_hash,journal_id FROM ex_fills WHERE fill_id=?", (fill.fill_id,)
    ).fetchone()
    if existing is not None:
        if existing["economic_hash"] != fill.economic_hash:
            fence_authority_tx(connection, fill.context, "CONFLICTING_FILL_FACT", fill.evidence_id)
            return FillResult(fill.fill_id, False, str(existing["journal_id"]), 0, "CONFLICTING_FILL_FACT")
        return FillResult(fill.fill_id, False, str(existing["journal_id"]), 0)
    record: IntentRecord | None = None
    if fill.intent_id is not None:
        record = get_tx(connection, fill.intent_id)
        intent = record.intent
        if (
            intent.context != fill.context
            or intent.scope_id != fill.scope_id
            or intent.profile_version != fill.profile_version
            or intent.instrument != fill.instrument
            or intent.base_asset != fill.base_asset
            or intent.quote_asset != fill.quote_asset
            or intent.side != fill.side
            or intent.contract_multiplier != fill.contract_multiplier
        ):
            raise DomainError("FILL_CONTEXT_MISMATCH")
    order = connection.execute(
        "SELECT intent_id FROM ex_orders WHERE authority_id=? AND environment=? AND venue_id=? AND account_id=? AND instrument=? AND venue_order_id=?",
        (*fill.namespace[:5], fill.venue_order_id),
    ).fetchone()
    if order is not None and order["intent_id"] != fill.intent_id:
        fence_authority_tx(connection, fill.context, "CONFLICTING_FILL_ATTRIBUTION", fill.evidence_id)
        return FillResult(fill.fill_id, False, None, 0, "CONFLICTING_FILL_ATTRIBUTION")
    incident: str | None = None
    if record is None:
        incident = "UNATTRIBUTED_FILL"
        fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
    else:
        incident = _accept_tx(connection, record, fill.venue_order_id, fill.evidence_id)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cf_slots'"
        ).fetchone():
            slot = connection.execute(
                "SELECT engine_state,tombstoned,stop_proof FROM cf_slots WHERE slot_id=? AND context_id=?",
                (record.intent.slot_id, fill.context.identity),
            ).fetchone()
            if slot is not None and (
                slot["engine_state"] == "STOPPED" or slot["tombstoned"] or slot["stop_proof"] is not None
            ):
                incident = "LATE_SLOT_EXPOSURE"
                connection.execute(
                    "UPDATE cf_slots SET stop_proof=NULL,scheduler_enabled=0,revision=revision+1 WHERE slot_id=? AND context_id=?",
                    (record.intent.slot_id, fill.context.identity),
                )
                fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
    retired = connection.execute(
        "SELECT valid FROM ex_context_retirements WHERE context_id=?", (fill.context.identity,)
    ).fetchone()
    if retired is not None:
        incident = "LATE_FILL_AFTER_RETIREMENT"
        connection.execute(
            "UPDATE ex_context_retirements SET valid=0 WHERE context_id=?", (fill.context.identity,)
        )
        fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
    if fill.fee and fill.fee_asset != fill.quote_asset:
        incident = "NONQUOTE_FEE_RECONCILIATION_REQUIRED"
        fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
    sequence = event_tx(
        connection,
        fill.context,
        fill.scope_id,
        "FILL_APPLIED" if record else "FILL_SUSPENSE",
        fill.intent_id or fill.fill_id,
        fill.evidence_id,
    )
    change = apply_lots_tx(connection, fill, sequence) if record is not None else None
    if change is not None and fill.context.product_track == ProductTrack.SPOT and change.quantity_after < 0:
        incident = "NEGATIVE_SPOT_INVENTORY"
        fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
    with localcontext(financial_context()) as decimal_context:
        decimal_context.prec = 256
        signed = fill.quantity if fill.side == Side.BUY else -fill.quantity
        prefix = "assets" if record else "venue_suspense"
        entries: list[JournalEntry] = []
        if fill.context.product_track == ProductTrack.SPOT:
            entries.extend(
                (
                    JournalEntry(account=f"{prefix}:inventory", asset=fill.base_asset, amount=signed),
                    JournalEntry(account="venue:trade_clearing", asset=fill.base_asset, amount=-signed),
                    JournalEntry(
                        account=f"{prefix}:cash", asset=fill.quote_asset, amount=-signed * fill.price
                    ),
                    JournalEntry(
                        account="venue:trade_clearing", asset=fill.quote_asset, amount=signed * fill.price
                    ),
                )
            )
        else:
            # Derivative notional is a position memo, never spendable spot cash.
            contract_asset = "CONTRACT:" + fill.instrument.upper()
            entries.extend(
                (
                    JournalEntry(account=f"{prefix}:contracts", asset=contract_asset, amount=signed),
                    JournalEntry(account="venue:contract_clearing", asset=contract_asset, amount=-signed),
                )
            )
            if change is not None and change.realized_pnl:
                entries.extend(
                    (
                        JournalEntry(
                            account="assets:cash", asset=fill.quote_asset, amount=change.realized_pnl
                        ),
                        JournalEntry(
                            account="income:realized_derivative",
                            asset=fill.quote_asset,
                            amount=-change.realized_pnl,
                        ),
                    )
                )
        if fill.fee:
            entries.extend(
                (
                    JournalEntry(account="expenses:trading_fees", asset=fill.fee_asset, amount=fill.fee),
                    JournalEntry(account=f"{prefix}:cash", asset=fill.fee_asset, amount=-fill.fee),
                )
            )
        journal_id = post_tx(
            connection,
            JournalFrame(
                context=fill.context,
                scope_id=fill.scope_id,
                event_kind="VENUE_FILL",
                evidence_id=fill.fill_id,
                entries=tuple(entries),
            ),
        )
        connection.execute(
            "INSERT INTO ex_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill.fill_id,
                *fill.namespace[:5],
                fill.trade_id,
                fill.venue_order_id,
                fill.context.identity,
                fill.scope_id,
                fill.intent_id,
                fill.model_dump_json(),
                fill.economic_hash,
                journal_id,
                utc_now(),
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO ex_orders VALUES(?,?,?,?,?,?,?,?)",
            (*fill.namespace[:5], fill.venue_order_id, fill.intent_id, fill.context.identity),
        )
        if record is not None:
            total = record.filled_quantity + fill.quantity
            if total > record.intent.quantity:
                incident = "ORDER_OVERFILLED"
                fence_authority_tx(connection, fill.context, incident, fill.evidence_id)
            terminal = total >= record.intent.quantity
            connection.execute(
                "UPDATE ex_outbox SET filled_quantity=?,terminal=?,updated_at=? WHERE intent_id=?",
                (str(total), int(terminal), utc_now(), fill.intent_id),
            )
            consume_for_fill_tx(connection, record.intent, total)
        if change is not None:
            connection.execute(
                "INSERT INTO ex_lot_movements VALUES(?,?,?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.context.identity,
                    str(change.quantity_before),
                    str(change.quantity_after),
                    str(change.average_before),
                    str(change.average_after),
                    str(change.realized_pnl),
                ),
            )
    return FillResult(fill.fill_id, True, journal_id, _increment_fact_tx(connection, fill.context), incident)


def acknowledge_tx(
    connection: sqlite3.Connection, intent_id: str, venue_order_id: str, evidence_id: str
) -> IntentRecord:
    record = get_tx(connection, intent_id)
    _accept_tx(connection, record, venue_order_id, evidence_id)
    event_tx(
        connection,
        record.intent.context,
        record.intent.scope_id,
        "ORDER_ACKNOWLEDGED",
        intent_id,
        evidence_id,
    )
    return get_tx(connection, intent_id)


def reject_tx(
    connection: sqlite3.Connection, intent_id: str, evidence_id: str, *, proven_no_creation: bool
) -> IntentRecord:
    record = get_tx(connection, intent_id)
    if record.submission == Submission.ACKNOWLEDGED:
        fence_authority_tx(connection, record.intent.context, "REJECTION_AFTER_ACCEPTANCE", evidence_id)
        event_tx(
            connection,
            record.intent.context,
            record.intent.scope_id,
            "CONFLICTING_REJECTION",
            intent_id,
            evidence_id,
        )
        return record
    if not proven_no_creation:
        raise DomainError("AUTHORITATIVE_NONCREATION_REQUIRED")
    connection.execute(
        "UPDATE ex_outbox SET submission='REJECTED',resolution='RESOLVED',terminal=1,updated_at=? WHERE intent_id=?",
        (utc_now(), intent_id),
    )
    release_tx(connection, intent_id)
    connection.execute(
        "UPDATE ex_slot_fences SET active=0 WHERE intent_id=? AND reason='UNKNOWN_SUBMISSION'", (intent_id,)
    )
    event_tx(
        connection, record.intent.context, record.intent.scope_id, "ORDER_REJECTED", intent_id, evidence_id
    )
    return get_tx(connection, intent_id)


def unknown_tx(connection: sqlite3.Connection, intent_id: str, evidence_id: str) -> IntentRecord:
    record = get_tx(connection, intent_id)
    if record.submission != Submission.PENDING_SUBMISSION:
        event_tx(
            connection,
            record.intent.context,
            record.intent.scope_id,
            "LATE_TRANSPORT_AMBIGUITY",
            intent_id,
            evidence_id,
        )
        return record
    if record.dispatch.value != "DISPATCH_STARTED":
        raise DomainError("UNKNOWN_REQUIRES_DISPATCH_MARKER")
    connection.execute(
        "UPDATE ex_outbox SET resolution='UNKNOWN',updated_at=? WHERE intent_id=?", (utc_now(), intent_id)
    )
    connection.execute(
        "INSERT INTO ex_slot_fences VALUES(?,?,?,?,1) ON CONFLICT(intent_id) DO UPDATE SET active=1",
        (intent_id, record.intent.context.authority_id, record.intent.context.identity, "UNKNOWN_SUBMISSION"),
    )
    alarm_tx(connection, record.intent.context, intent_id, "UNKNOWN_SUBMISSION", evidence_id)
    event_tx(
        connection,
        record.intent.context,
        record.intent.scope_id,
        "UNKNOWN_SUBMISSION",
        intent_id,
        evidence_id,
    )
    return get_tx(connection, intent_id)


def reject_unsent_tx(connection: sqlite3.Connection, intent_id: str, evidence_id: str) -> IntentRecord:
    record = get_tx(connection, intent_id)
    if record.submission == Submission.ACKNOWLEDGED:
        event_tx(
            connection,
            record.intent.context,
            record.intent.scope_id,
            "NO_SEND_AFTER_PRIVATE_ACCEPTANCE",
            intent_id,
            evidence_id,
        )
        return record
    return reject_tx(connection, intent_id, evidence_id, proven_no_creation=True)


def terminal_tx(
    connection: sqlite3.Connection, intent_id: str, evidence_id: str, *, complete_fills: bool
) -> IntentRecord:
    record = get_tx(connection, intent_id)
    if (
        record.submission != Submission.ACKNOWLEDGED
        or record.resolution == Resolution.UNKNOWN
        or not complete_fills
    ):
        raise DomainError("TERMINAL_ORDER_AND_COMPLETE_FILL_EVIDENCE_REQUIRED")
    if record.terminal and record.reservation_remaining == 0:
        # Replayed terminal evidence is not a new financial fact. In particular,
        # repeated market snapshots must not invalidate an in-flight stop proof.
        return record
    connection.execute(
        "UPDATE ex_outbox SET terminal=1,updated_at=? WHERE intent_id=?", (utc_now(), intent_id)
    )
    release_tx(connection, intent_id)
    event_tx(
        connection, record.intent.context, record.intent.scope_id, "TERMINAL_PROVEN", intent_id, evidence_id
    )
    _increment_fact_tx(connection, record.intent.context)
    return get_tx(connection, intent_id)


class FactReducer:
    def __init__(self, writer: Writer, provisional_fence: Callable[[str, str], None] | None = None) -> None:
        self.writer = writer
        self.provisional_fence = provisional_fence

    def apply_fill(self, fill: Fill) -> FillResult:
        try:
            result = self.writer.call(lambda connection: apply_fill_tx(connection, fill))
        except Exception:
            if self.provisional_fence:
                self.provisional_fence(fill.context.authority_id, "FILL_PERSISTENCE_FAILED")
            raise
        if result.incident and self.provisional_fence:
            self.provisional_fence(fill.context.authority_id, result.incident)
        return result

    def acknowledge(self, intent_id: str, venue_order_id: str, evidence_id: str) -> IntentRecord:
        return self.writer.call(
            lambda connection: acknowledge_tx(connection, intent_id, venue_order_id, evidence_id)
        )

    def reject(self, intent_id: str, evidence_id: str, *, proven_no_creation: bool) -> IntentRecord:
        return self.writer.call(
            lambda connection: reject_tx(
                connection, intent_id, evidence_id, proven_no_creation=proven_no_creation
            )
        )

    def unknown(self, intent_id: str, evidence_id: str) -> IntentRecord:
        current = self.writer.call(lambda connection: get_tx(connection, intent_id))
        if current.submission == Submission.PENDING_SUBMISSION and self.provisional_fence:
            self.provisional_fence(current.intent.context.authority_id, "UNKNOWN_SUBMISSION")
        return self.writer.call(lambda connection: unknown_tx(connection, intent_id, evidence_id))

    def terminal(self, intent_id: str, evidence_id: str, *, complete_fills: bool) -> IntentRecord:
        return self.writer.call(
            lambda connection: terminal_tx(connection, intent_id, evidence_id, complete_fills=complete_fills)
        )

    def reject_unsent(self, intent_id: str, evidence_id: str) -> IntentRecord:
        """Called only by the transport owner when this invocation did not begin."""
        return self.writer.call(lambda connection: reject_unsent_tx(connection, intent_id, evidence_id))
