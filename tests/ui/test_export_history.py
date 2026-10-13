import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from coinfly.ipc.protocol import RPCError
from coinfly.reporting.history_service import HistoryCapture, HistoryQuery, HistoryRecord, HistoryService
from coinfly.ui.export_history import export_frozen_report
from coinfly.ui.ipc_client import DesktopClient
from coinfly.ui.render_scheduler import RenderQueue


class ExportTests(unittest.TestCase):
    def test_full_frozen_report_exports_every_page_and_escapes_formula_cells(self) -> None:
        class Source:
            def snapshot(self, query):
                return HistoryCapture(11, tuple(HistoryRecord(index, f"id-{index}", "2026-09-18T00:00:00Z",
                    "owner", "SANDBOX", "=untrusted", "CLOSED", closed_episode=True, matched_pnl=Decimal("-1.2"))
                    for index in range(1, 12)))
        service = HistoryService(Source())
        first = service.open(HistoryQuery("owner", "SANDBOX"))
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.csv"
            count = export_frozen_report(first["snapshot_id"], destination,
                        lambda cursor, direction: service.page(first["snapshot_id"], cursor=cursor, direction=direction))
            self.assertEqual(count, 11)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 11)
            self.assertEqual({row["environment"] for row in rows}, {"SANDBOX"})
            self.assertEqual({row["event"] for row in rows}, {"'=untrusted"})
            self.assertEqual({row["closed_net"] for row in rows}, {"-1.2"})
            self.assertEqual({row["report_net"] for row in rows}, {"-13.2"})

    def test_incomplete_report_does_not_replace_existing_destination(self) -> None:
        page = {"snapshot_id": "snapshot", "ledger_sequence": 2, "environment": "LIVE", "totals": {},
                "total_count": 1, "frozen": True, "page_size": 10, "rows": [], "next_cursor": None}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.csv"
            destination.write_text("existing complete report")
            with self.assertRaisesRegex(RPCError, "EXPORT_COUNT_MISMATCH"):
                export_frozen_report("snapshot", destination, lambda *_: page)
            self.assertEqual(destination.read_text(), "existing complete report")
            self.assertEqual(len(tuple(Path(temporary).iterdir())), 1)

    def test_history_queue_cannot_consume_reserved_safety_capacity(self) -> None:
        client = DesktopClient(RenderQueue(), Path("/unused/worker.sock"))
        for _ in range(56):
            client.submit("history.open", {}, epoch=1)
        with self.assertRaisesRegex(RPCError, "COMMAND_QUEUE_FULL"):
            client.submit("history.open", {}, epoch=1)
        request_id = client.submit("safety.stop", {}, epoch=1)
        _, _, first = client._commands.get_nowait()
        self.assertEqual(first.request_id, request_id)
        self.assertEqual(first.method, "safety.stop")
