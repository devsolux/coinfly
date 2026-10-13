import sqlite3
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from coinfly.ipc.protocol import RPCError
from coinfly.reporting.history_service import (
    HistoryCapture,
    HistoryQuery,
    HistoryRecord,
    HistoryService,
    WriterHistorySource,
)


def record(index: int, *, environment: str = "SANDBOX", closed: bool = False, profit: str = "0") -> HistoryRecord:
    return HistoryRecord(index, f"event-{index}", "2026-09-18T12:00:00+00:00", "authority-1", environment,
                         "EPISODE_CLOSED" if closed else "DEPOSIT", "CLOSED" if closed else "OBSERVED",
                         closed_episode=closed, matched_pnl=Decimal(profit))


class Source:
    def __init__(self, records):
        self.records = list(records)

    def snapshot(self, query):
        return HistoryCapture(max((row.sequence for row in self.records), default=0), tuple(self.records))


class FrozenHistoryTests(unittest.TestCase):
    def test_ten_records_keyset_backwards_last_and_short_page(self) -> None:
        for count in (0, 1, 10, 11, 25, 99):
            with self.subTest(count=count):
                source = Source(record(index) for index in range(1, count + 1))
                service = HistoryService(source)
                first = page = service.open(HistoryQuery("authority-1", "SANDBOX"))
                ids = []
                pages = []
                while True:
                    pages.append(page)
                    ids.extend(row["event_id"] for row in page["rows"])
                    if not page["next_cursor"]:
                        break
                    self.assertEqual(len(page["rows"]), 10)
                    page = service.page(page["snapshot_id"], cursor=page["next_cursor"])
                self.assertEqual(ids, [f"event-{index}" for index in range(count, 0, -1)])
                self.assertEqual(service.page(first["snapshot_id"], direction="last")["rows"], pages[-1]["rows"])
                if len(pages) > 1:
                    back = service.page(page["snapshot_id"], cursor=page["previous_cursor"], direction="previous")
                    self.assertEqual(back["rows"], pages[-2]["rows"])

    def test_rows_counts_realm_and_totals_frozen_together(self) -> None:
        win = replace(record(1, closed=True, profit="10"), fees=Decimal("2"), funding=Decimal("-1"), charges=Decimal("0.5"))
        loss = record(2, closed=True, profit="-4")
        deposit = record(3, profit="1000")
        live_profit = record(4, environment="LIVE", closed=True, profit="500")
        source = Source([win, loss, deposit, live_profit])
        service = HistoryService(source)
        first = service.open(HistoryQuery("authority-1", "SANDBOX", "TRADE_RESULTS"))
        self.assertEqual(first["total_count"], 2)
        self.assertEqual(first["totals"], {"profit": "6.5", "loss": "-4", "net": "2.5", "pending_charges": 0})
        source.records.append(record(5, closed=True, profit="100"))
        repeated = service.page(first["snapshot_id"], direction="first")
        self.assertEqual(first, repeated)
        self.assertEqual(service.open(HistoryQuery("authority-1", "SANDBOX", "TRADE_RESULTS"))["totals"]["net"], "102.5")

    def test_cursor_cross_snapshot_tampering_expiry_and_duplicate_evidence(self) -> None:
        now = [0.0]
        source = Source(record(index) for index in range(1, 21))
        service = HistoryService(source, ttl_seconds=5, clock=lambda: now[0])
        first = service.open(HistoryQuery("authority-1", "SANDBOX"))
        other = service.open(HistoryQuery("authority-1", "SANDBOX"))
        with self.assertRaises(RPCError):
            service.page(other["snapshot_id"], cursor=first["next_cursor"])
        with self.assertRaises(RPCError):
            service.page(first["snapshot_id"], cursor="tampered")
        now[0] = 6
        with self.assertRaisesRegex(RPCError, "HISTORY_SNAPSHOT_EXPIRED"):
            service.page(first["snapshot_id"])
        source.records.append(source.records[0])
        with self.assertRaisesRegex(RPCError, "DUPLICATE_HISTORY_ID"):
            service.open(HistoryQuery("authority-1", "SANDBOX"))

    def test_decimal_totals_do_not_round_at_the_ambient_context_precision(self) -> None:
        large = replace(record(1, closed=True, profit="123456789012345678901234567890.123456789"),
                        fees=Decimal("0.000000001"))
        service = HistoryService(Source([large]))
        page = service.open(HistoryQuery("authority-1", "SANDBOX", "TRADE_RESULTS"))
        self.assertEqual(page["totals"]["net"], "123456789012345678901234567890.123456788")


class WriterSourceTests(unittest.TestCase):
    def test_writer_schema_events_and_closed_results_share_frozen_realm_watermark(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        schema = Path(__file__).resolve().parents[2] / "src/coinfly/storage/schema.sql"
        connection.executescript(schema.read_text())
        for index, environment, kind in ((1, "SANDBOX", "EPISODE_CLOSED"), (2, "SANDBOX", "DEPOSIT"),
                                          (3, "LIVE", "EPISODE_CLOSED")):
            connection.execute("INSERT INTO ex_events VALUES(?,?,?,?,?,?,?,?,?)",
                               (index, "authority-1", environment, "context", "book", kind, f"e{index}",
                                f"proof{index}", "2026-09-18T12:00:00+00:00"))
        connection.execute("INSERT INTO ex_closed_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (1, "authority-1", "SANDBOX", "context", "book", "episode-1", "BTC-USD",
                            "2026-09-18T12:00:00+00:00", "10", "1", "-2", "0.5", 1))
        class LocalWriter:
            def call(self, operation):
                connection.execute("BEGIN")
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
        try:
            service = HistoryService(WriterHistorySource(LocalWriter()))
            page = service.open(HistoryQuery("authority-1", "SANDBOX"))
            self.assertEqual(page["ledger_sequence"], 3)
            self.assertEqual(page["total_count"], 2)
            self.assertEqual(page["totals"]["net"], "6.5")
            self.assertEqual(page["totals"]["pending_charges"], 1)
            self.assertEqual({row["environment"] for row in page["rows"]}, {"SANDBOX"})
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
