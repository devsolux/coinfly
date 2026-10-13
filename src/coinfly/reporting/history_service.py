"""Frozen ten-row reports with immutable keyset cursors and exact Decimal P&L."""

from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Protocol, TypeVar

from coinfly.domain.decimal_math import financial_context
from coinfly.ipc.protocol import RPCError

PAGE_SIZE = 10
ZERO = Decimal("0")


def _exact_sum(values: tuple[Decimal, ...] | list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    precision = max(28, max(value.adjusted() for value in values)
                    - min(int(value.as_tuple().exponent) for value in values) + len(str(len(values))) + 4)
    if precision > 4096:
        raise RPCError("HISTORY_MONEY_RANGE")
    with localcontext(financial_context()) as context:
        context.prec = precision
        return sum(values, ZERO)


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    authority_id: str
    environment: str
    mode: str = "ALL_EVENTS"
    scope_id: str | None = None
    instrument: str | None = None
    search: str = ""

    def __post_init__(self) -> None:
        if self.environment not in ("LIVE", "SANDBOX") or self.mode not in ("ALL_EVENTS", "TRADE_RESULTS"):
            raise RPCError("INVALID_HISTORY_QUERY")
        if not self.authority_id or len(self.authority_id) > 160 or len(self.search) > 120:
            raise RPCError("INVALID_HISTORY_QUERY")
        if any(value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 160)
               for value in (self.scope_id, self.instrument)):
            raise RPCError("INVALID_HISTORY_QUERY")


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    sequence: int
    event_id: str
    occurred_at: str
    authority_id: str
    environment: str
    kind: str
    status: str
    scope_id: str | None = None
    instrument: str | None = None
    slot_id: str | None = None
    closed_episode: bool = False
    matched_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    charges: Decimal = ZERO
    pending_charges: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 0 or not self.event_id or self.environment not in ("LIVE", "SANDBOX"):
            raise RPCError("INVALID_HISTORY_RECORD")
        try:
            stamp = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RPCError("INVALID_HISTORY_TIMESTAMP") from exc
        if stamp.tzinfo is None:
            raise RPCError("INVALID_HISTORY_TIMESTAMP")
        object.__setattr__(self, "occurred_at", stamp.astimezone(timezone.utc).isoformat(timespec="microseconds"))
        if any(not isinstance(value, Decimal) or not value.is_finite()
               for value in (self.matched_pnl, self.fees, self.funding, self.charges)):
            raise RPCError("INVALID_HISTORY_MONEY")
        if self.fees < 0 or self.charges < 0:
            raise RPCError("INVALID_HISTORY_MONEY")

    @property
    def closed_net(self) -> Decimal | None:
        if not self.closed_episode:
            return None
        return _exact_sum((self.matched_pnl, self.fees.copy_negate(), self.funding, self.charges.copy_negate()))

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.occurred_at, self.sequence, self.event_id)

    def as_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "event_id": self.event_id, "occurred_at": self.occurred_at,
                "authority_id": self.authority_id, "environment": self.environment, "kind": self.kind,
                "status": self.status, "scope_id": self.scope_id, "instrument": self.instrument,
                "slot_id": self.slot_id, "closed_net": str(self.closed_net) if self.closed_net is not None else None,
                "pending_charges": self.pending_charges}


@dataclass(frozen=True, slots=True)
class HistoryCapture:
    ledger_sequence: int
    records: tuple[HistoryRecord, ...]


class HistorySource(Protocol):
    def snapshot(self, query: HistoryQuery) -> HistoryCapture:
        """Capture rows and ledger watermark atomically on the worker side."""
        ...


@dataclass(frozen=True, slots=True)
class _Snapshot:
    snapshot_id: str
    ledger_sequence: int
    query: HistoryQuery
    created_at: float
    records: tuple[HistoryRecord, ...]  # ascending; pages reverse exact keysets
    keys: tuple[tuple[str, int, str], ...]
    profit: Decimal
    loss: Decimal
    net: Decimal
    pending_charges: int


class HistoryService:
    def __init__(self, source: HistorySource, *, ttl_seconds: float = 900.0,
                 max_snapshots: int = 16, max_records: int = 100_000,
                 max_total_records: int = 200_000, clock: Callable[[], float] = time.monotonic) -> None:
        if ttl_seconds <= 0 or min(max_snapshots, max_records, max_total_records) <= 0:
            raise ValueError("Report limits must be positive")
        self.source, self.ttl_seconds, self.clock = source, ttl_seconds, clock
        self.max_snapshots, self.max_records, self.max_total_records = max_snapshots, max_records, max_total_records
        self._snapshots: dict[str, _Snapshot] = {}
        self._cursor_key = secrets.token_bytes(32)
        self._lock = threading.RLock()

    def open(self, query: HistoryQuery) -> dict[str, Any]:
        capture = self.source.snapshot(query)
        if len(capture.records) > self.max_records:
            raise RPCError("HISTORY_SCOPE_TOO_LARGE")
        # The service independently validates the provider's realm boundary.
        rows = tuple(record for record in capture.records
                     if record.authority_id == query.authority_id and record.environment == query.environment
                     and record.sequence <= capture.ledger_sequence
                     and (query.mode != "TRADE_RESULTS" or record.closed_episode)
                     and (query.scope_id is None or record.scope_id == query.scope_id)
                     and (query.instrument is None or record.instrument == query.instrument)
                     and (not query.search or query.search.casefold() in
                          " ".join((record.kind, record.status, record.instrument or "", record.event_id)).casefold()))
        if len({record.event_id for record in rows}) != len(rows):
            raise RPCError("DUPLICATE_HISTORY_ID")
        ordered = tuple(sorted(rows, key=lambda item: item.key))
        results = [record.closed_net for record in ordered if record.closed_episode]
        profit = _exact_sum([value for value in results if value is not None and value > 0])
        loss = _exact_sum([value for value in results if value is not None and value < 0])
        snapshot = _Snapshot(uuid.uuid4().hex, capture.ledger_sequence, query, self.clock(), ordered,
                             tuple(record.key for record in ordered), profit, loss, _exact_sum((profit, loss)),
                             sum(record.pending_charges for record in ordered if record.closed_episode))
        with self._lock:
            self._expire()
            if len(self._snapshots) >= self.max_snapshots or sum(len(s.records) for s in self._snapshots.values()) + len(rows) > self.max_total_records:
                raise RPCError("HISTORY_SNAPSHOT_LIMIT")
            self._snapshots[snapshot.snapshot_id] = snapshot
        return self.page(snapshot.snapshot_id)

    def _expire(self) -> None:
        cutoff = self.clock() - self.ttl_seconds
        for key in tuple(self._snapshots):
            if self._snapshots[key].created_at <= cutoff:
                del self._snapshots[key]

    def close(self, snapshot_id: str) -> None:
        with self._lock:
            self._snapshots.pop(snapshot_id, None)

    def _cursor(self, snapshot: _Snapshot, key: tuple[str, int, str]) -> str:
        payload = json.dumps([snapshot.snapshot_id, *key], separators=(",", ":")).encode()
        signature = hmac.digest(self._cursor_key, payload, hashlib.sha256)
        return base64.urlsafe_b64encode(payload + signature).decode()

    def _parse_cursor(self, snapshot: _Snapshot, cursor: str) -> tuple[str, int, str]:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise RPCError("INVALID_HISTORY_CURSOR")
        try:
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, hashlib.sha256)):
                raise ValueError("Invalid cursor signature")
            snapshot_id, timestamp, sequence, event_id = json.loads(payload)
            if snapshot_id != snapshot.snapshot_id or type(sequence) is not int:
                raise ValueError("Cursor binding mismatch")
            key = (timestamp, sequence, event_id)
            index = bisect.bisect_left(snapshot.keys, key)
            if index == len(snapshot.keys) or snapshot.keys[index] != key:
                raise ValueError("Unknown cursor key")
            return key
        except (ValueError, TypeError, UnicodeError) as exc:
            raise RPCError("INVALID_HISTORY_CURSOR") from exc

    def page(self, snapshot_id: str, *, cursor: str | None = None, direction: str = "next") -> dict[str, Any]:
        if direction not in {"first", "previous", "next", "last"}:
            raise RPCError("INVALID_HISTORY_DIRECTION")
        with self._lock:
            self._expire()
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise RPCError("HISTORY_SNAPSHOT_EXPIRED")
            total = len(snapshot.records)
            end = total
            if direction == "last":
                end = (total % PAGE_SIZE or PAGE_SIZE) if total else 0
            elif cursor is not None and direction in {"next", "previous"}:
                key = self._parse_cursor(snapshot, cursor)
                if direction == "next":
                    end = bisect.bisect_left(snapshot.keys, key)
                else:
                    end = min(total, bisect.bisect_right(snapshot.keys, key) + PAGE_SIZE)
            start = max(0, end - PAGE_SIZE)
            rows = tuple(reversed(snapshot.records[start:end]))
            before = total - end
            return {"snapshot_id": snapshot.snapshot_id, "ledger_sequence": snapshot.ledger_sequence,
                    "authority_id": snapshot.query.authority_id, "environment": snapshot.query.environment,
                    "mode": snapshot.query.mode, "page_size": PAGE_SIZE,
                    "page_number": before // PAGE_SIZE + 1 if total else 1,
                    "page_count": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
                    "total_count": total, "first_row": before + 1 if rows else 0,
                    "last_row": before + len(rows), "rows": [row.as_dict() for row in rows],
                    "previous_cursor": self._cursor(snapshot, rows[0].key) if rows and end < total else None,
                    "next_cursor": self._cursor(snapshot, rows[-1].key) if rows and start > 0 else None,
                    "totals": {"profit": str(snapshot.profit), "loss": str(snapshot.loss), "net": str(snapshot.net),
                               "pending_charges": snapshot.pending_charges},
                    "frozen": True}


T = TypeVar("T")


class Writer(Protocol):
    def call(self, operation: Callable[[Any], T]) -> T: ...


class WriterHistorySource:
    """Worker-only bridge; writer.call captures one immutable SQL view.

    Closed results require explicit ex_closed_results evidence. Open/flat
    positions, deposits and rejected unfilled orders never become trade results.
    """

    def __init__(self, writer: Writer, *, max_records: int = 100_000) -> None:
        self.writer, self.max_records = writer, max_records

    def snapshot(self, query: HistoryQuery) -> HistoryCapture:
        def read(connection: Any) -> tuple[int, tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]:
            sequence = int(connection.execute("SELECT COALESCE(MAX(sequence),0) FROM ex_events").fetchone()[0])
            events: tuple[tuple[Any, ...], ...] = ()
            results: tuple[tuple[Any, ...], ...] = ()
            if query.mode == "ALL_EVENTS":
                rows = connection.execute(
                    "SELECT sequence,authority_id,environment,scope_id,event_kind,entity_id,evidence_id,created_at "
                    "FROM ex_events WHERE authority_id=? AND environment=? AND sequence<=? ORDER BY sequence DESC LIMIT ?",
                    (query.authority_id, query.environment, sequence, self.max_records + 1)).fetchall()
                events = tuple(tuple(row) for row in rows)
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ex_closed_results'").fetchone()
            if table is not None:
                rows = connection.execute(
                    "SELECT sequence,authority_id,environment,scope_id,episode_id,instrument,closed_at,matched_pnl,fees,funding,charges,pending_charges "
                    "FROM ex_closed_results WHERE authority_id=? AND environment=? AND sequence<=? ORDER BY sequence DESC LIMIT ?",
                    (query.authority_id, query.environment, sequence, self.max_records + 1)).fetchall()
                results = tuple(tuple(row) for row in rows)
            if max(len(events), len(results)) > self.max_records:
                raise RPCError("HISTORY_SCOPE_TOO_LARGE")
            return sequence, events, results
        sequence, events, results = self.writer.call(read)
        # Parsing, Decimal calculations and sorting happen after the short
        # writer transaction has released its queue slot for protective work.
        closed_sequences = {int(row[0]) for row in results}
        records = [HistoryRecord(int(row[0]), f"event-{row[0]}", row[7], row[1], row[2], row[4],
                                 row[4], scope_id=row[3], slot_id=row[5]) for row in events
                   if int(row[0]) not in closed_sequences]
        for row in results:
            records.append(HistoryRecord(int(row[0]), f"closed-{row[4]}-{row[0]}", row[6], row[1], row[2],
                                         "EPISODE_CLOSED", "CLOSED", scope_id=row[3], instrument=row[5],
                                         slot_id=row[4], closed_episode=True, matched_pnl=Decimal(row[7]),
                                         fees=Decimal(row[8]), funding=Decimal(row[9]), charges=Decimal(row[10]),
                                         pending_charges=bool(row[11])))
        if len(records) > self.max_records:
            raise RPCError("HISTORY_SCOPE_TOO_LARGE")
        return HistoryCapture(sequence, tuple(records))
