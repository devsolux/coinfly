"""Export a complete frozen IPC report on an I/O thread, with atomic completion."""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from coinfly.ipc.protocol import RPCError


def _cell(value: Any, *, money: bool = False) -> str:
    if value is None:
        return ""
    text = str(value)
    if money:
        try:
            if not Decimal(text).is_finite():
                raise InvalidOperation()
            return text
        except InvalidOperation as exc:
            raise RPCError("INVALID_EXPORT_MONEY") from exc
    # Account/event identifiers cannot become spreadsheet formulas on opening.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_frozen_report(snapshot_id: str, destination: Path,
                         fetch: Callable[[str | None, str], Mapping[str, Any]]) -> int:
    """fetch(cursor, direction) must use history.page for the same snapshot ID."""
    first = page = fetch(None, "first")
    count = first.get("total_count")
    if type(count) is not int or not 0 <= count <= 100_000:
        raise RPCError("INVALID_EXPORT_COUNT")
    binding = (snapshot_id, first.get("ledger_sequence"), first.get("environment"), first.get("totals"))
    descriptor, temporary = tempfile.mkstemp(prefix=".coinfly-export-", suffix=".csv", dir=destination.parent)
    written, seen = 0, set()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("snapshot_id", "ledger_sequence", "environment", "event_id", "occurred_at_utc",
                             "instrument", "event", "status", "closed_net", "pending_charges",
                             "report_profit", "report_loss", "report_net"))
            while True:
                current = (page.get("snapshot_id"), page.get("ledger_sequence"), page.get("environment"), page.get("totals"))
                rows = page.get("rows", ())
                if (current != binding or page.get("total_count") != count or page.get("frozen") is not True
                        or page.get("page_size") != 10 or len(rows) > 10):
                    raise RPCError("EXPORT_SNAPSHOT_CHANGED")
                for row in rows:
                    event_id = row.get("event_id")
                    if not isinstance(event_id, str) or event_id in seen or row.get("environment") != binding[2]:
                        raise RPCError("EXPORT_RECORD_INVALID")
                    seen.add(event_id)
                    totals = page["totals"]
                    values = (snapshot_id, page["ledger_sequence"], page["environment"], event_id,
                              row.get("occurred_at"), row.get("instrument"), row.get("kind"), row.get("status"),
                              row.get("closed_net"), row.get("pending_charges"), totals.get("profit"), totals.get("loss"), totals.get("net"))
                    writer.writerow(_cell(value, money=index in (8, 10, 11, 12)) for index, value in enumerate(values))
                    written += 1
                cursor = page.get("next_cursor")
                if cursor is None:
                    break
                if len(rows) != 10 or written >= count:
                    raise RPCError("EXPORT_PAGINATION_INVALID")
                page = fetch(cursor, "next")
            if written != count:
                raise RPCError("EXPORT_COUNT_MISMATCH")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return written
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
