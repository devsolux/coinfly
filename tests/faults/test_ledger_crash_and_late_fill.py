from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from coinfly.domain.context import DomainError
from coinfly.execution.evidence import authority_fenced_tx
from coinfly.execution.models import Resolution, Side
from coinfly.execution.reconciler import Reconciler
from coinfly.execution.recovery import context_fact_sequence_tx, record_retirement_tx
from coinfly.storage.writer import Writer
from tests.execution.helpers import LedgerCase, fill_for, intent


class CrashTests(LedgerCase):
    def test_writer_wal_full_and_serialized_atomic_transactions(self) -> None:
        self.assertEqual(
            self.writer.call(lambda connection: connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "wal",
        )
        self.assertEqual(
            self.writer.call(lambda connection: connection.execute("PRAGMA synchronous").fetchone()[0]), 2
        )
        self.writer.call(
            lambda connection: connection.execute("CREATE TABLE counter(value INTEGER)").rowcount
        )
        self.writer.call(lambda connection: connection.execute("INSERT INTO counter VALUES(0)").rowcount)

        def increment(_: int) -> None:
            self.writer.call(
                lambda connection: connection.execute("UPDATE counter SET value=value+1").rowcount
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(80)))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT value FROM counter").fetchone()[0]
            ),
            80,
        )

    def test_exception_rolls_back_all_domain_changes(self) -> None:
        import sqlite3

        from coinfly.execution.intent_store import prepare_tx
        from coinfly.execution.models import Reservation

        order = intent()

        def fail_after_prepare(connection: sqlite3.Connection) -> None:
            prepare_tx(connection, order, Reservation(asset="USD", amount=Decimal(200)))
            raise OSError("simulated disk failure")

        with self.assertRaises(OSError):
            self.writer.call(fail_after_prepare)
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]
            ),
            0,
        )

    def test_failed_fill_commit_rolls_back_ack_lots_and_fences_memory(self) -> None:
        import sqlite3
        from unittest.mock import patch

        from coinfly.execution.fact_reducer import FactReducer
        from coinfly.execution.models import Submission

        order = self.prepare(intent())
        fences: list[tuple[str, str]] = []
        reducer = FactReducer(
            self.writer, provisional_fence=lambda authority, reason: fences.append((authority, reason))
        )
        with patch(
            "coinfly.execution.fact_reducer.post_tx", side_effect=sqlite3.OperationalError("disk full")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                reducer.apply_fill(fill_for(order))
        record = self.store.get(order.intent_id)
        self.assertEqual(record.submission, Submission.PENDING_SUBMISSION)
        self.assertEqual(record.reservation_remaining, Decimal(200))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_positions").fetchone()[0]
            ),
            0,
        )
        self.assertEqual(fences, [(order.context.authority_id, "FILL_PERSISTENCE_FAILED")])
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_reservations").fetchone()[0]
            ),
            1,
        )

    def test_real_process_exit_after_marker_recovers_unknown_without_replay(self) -> None:
        self.writer.close()
        source = """
import os, sys
from decimal import Decimal
from coinfly.storage.writer import Writer
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Reservation
from coinfly.execution.outbox import claim_dispatch_tx
from tests.execution.helpers import intent
w = Writer(sys.argv[1])
i = intent()
IntentStore(w).prepare(i, Reservation(asset='USD', amount=Decimal(200)))
w.call(lambda c: claim_dispatch_tx(c, i.intent_id, i.payload_hash))
os._exit(23)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = (
            str(Path(__file__).resolve().parents[2] / "src")
            + os.pathsep
            + str(Path(__file__).resolve().parents[2])
        )
        result = subprocess.run(
            [sys.executable, "-c", source, str(self.path)],
            env=environment,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        self.writer = Writer(self.path)
        recovered = Reconciler(self.writer).recover()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].resolution, Resolution.UNKNOWN)
        self.assertEqual(recovered[0].reservation_remaining, Decimal(200))

    def test_late_fill_invalidates_retirement_and_never_books_in_target(self) -> None:
        buy = self.prepare(intent(quantity="2"))
        self.reducer.apply_fill(fill_for(buy, trade="first", quantity="1"))
        self.reducer.terminal(buy.intent_id, "cancel-and-complete-history", complete_fills=True)
        sell = self.prepare(intent(quantity="1", side=Side.SELL, revision=2))
        self.reducer.apply_fill(fill_for(sell, trade="exit", quantity="1"))
        fact_seq = self.writer.call(lambda connection: context_fact_sequence_tx(connection, buy.context))
        self.writer.call(
            lambda connection: record_retirement_tx(connection, buy.context, "stop-certificate", fact_seq)
        )
        late = self.reducer.apply_fill(fill_for(buy, trade="late", quantity="1"))
        self.assertEqual(late.incident, "LATE_FILL_AFTER_RETIREMENT")
        self.assertTrue(
            self.writer.call(lambda connection: authority_fenced_tx(connection, buy.context.authority_id))
        )
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT valid FROM ex_context_retirements").fetchone()[
                    0
                ]
            ),
            0,
        )
        rows = self.writer.call(
            lambda connection: [
                dict(row) for row in connection.execute("SELECT context_id,quantity FROM ex_positions")
            ]
        )
        self.assertEqual(rows, [{"context_id": buy.context.identity, "quantity": "1"}])
        with self.assertRaises(DomainError):
            self.prepare(intent(revision=3))

    def test_unsupported_sqlite_fails_closed(self) -> None:
        from unittest.mock import patch

        with patch("coinfly.storage.writer.sqlite3.sqlite_version_info", (3, 50, 0)):
            with self.assertRaisesRegex(DomainError, "SQLite"):
                Writer(self.path.parent / "old-sqlite.db")

    def test_duplicate_local_writer_for_same_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "WRITER_PATH_ALREADY_OWNED"):
            Writer(self.path)

    def test_late_fill_in_deleted_slot_invalidates_stop_proof(self) -> None:
        from coinfly.control.slot_service import SlotService

        order = self.prepare(intent())
        self.writer.call(SlotService._schema)
        self.writer.call(
            lambda connection: (
                connection.execute(
                    "INSERT INTO cf_slots VALUES(?,?,?,?,?,?,1,'STOPPED',0,1,NULL,'old-proof')",
                    (
                        order.slot_id,
                        order.context.identity,
                        order.context.authority_id,
                        order.context.model_dump_json(),
                        order.scope_id,
                        order.instrument,
                    ),
                ).rowcount
            )
        )
        result = self.reducer.apply_fill(fill_for(order))
        self.assertEqual(result.incident, "LATE_SLOT_EXPOSURE")
        row = self.writer.call(
            lambda connection: dict(
                connection.execute(
                    "SELECT stop_proof,scheduler_enabled,tombstoned,revision FROM cf_slots"
                ).fetchone()
            )
        )
        self.assertEqual(row, {"stop_proof": None, "scheduler_enabled": 0, "tombstoned": 1, "revision": 2})
        self.assertTrue(
            self.writer.call(lambda connection: authority_fenced_tx(connection, order.context.authority_id))
        )
