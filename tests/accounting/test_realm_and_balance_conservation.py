from __future__ import annotations

import sqlite3
from decimal import Decimal

from pydantic import ValidationError

from coinfly.accounting.journal import Journal, JournalEntry, JournalFrame
from coinfly.domain.context import DomainError, Environment
from coinfly.execution.models import Reservation
from tests.execution.helpers import LedgerCase, context, fill_for, intent


class JournalTests(LedgerCase):
    def frame(self, amount: str = "0.000000000000000001") -> JournalFrame:
        return JournalFrame(
            context=context(),
            scope_id="book",
            event_kind="DEPOSIT",
            evidence_id="deposit-proof",
            entries=(
                JournalEntry(account="assets:cash", asset="USD", amount=Decimal(amount)),
                JournalEntry(account="equity:external_flows", asset="USD", amount=-Decimal(amount)),
            ),
        )

    def test_exact_balance_and_evidence_idempotency(self) -> None:
        journal = Journal(self.writer)
        identifier = journal.post(self.frame())
        self.assertEqual(journal.post(self.frame()), identifier)
        self.assertEqual(
            journal.balance(context(), "book", "assets:cash", "USD"), Decimal("0.000000000000000001")
        )
        journal.verify()
        with self.assertRaises(DomainError):
            journal.post(self.frame("2"))

    def test_each_asset_balances_independently(self) -> None:
        with self.assertRaises(ValidationError):
            JournalFrame(
                context=context(),
                scope_id="book",
                event_kind="INVALID",
                evidence_id="bad",
                entries=(
                    JournalEntry(account="assets:cash", asset="USD", amount=Decimal(1)),
                    JournalEntry(account="assets:cash", asset="BTC", amount=Decimal(-1)),
                ),
            )

    def test_journal_immutable_even_with_sql_mutation(self) -> None:
        Journal(self.writer).post(self.frame())
        for sql in ("UPDATE ex_entries SET amount='7'", "DELETE FROM ex_entries", "DELETE FROM ex_journals"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.writer.call(lambda connection: connection.execute(sql))

    def test_realm_balance_isolation_and_exact_trade_entries(self) -> None:
        paper = self.prepare(intent(quantity="0.0001"))
        live = self.prepare(intent(quantity="0.0001", environment=Environment.LIVE))
        self.reducer.apply_fill(fill_for(paper, quantity="0.0001", price="12345.678901", fee="0.000000001"))
        self.reducer.apply_fill(fill_for(live, quantity="0.0001", price="20000", fee="0"))
        journal = Journal(self.writer)
        self.assertEqual(
            journal.balance(paper.context, "book", "assets:cash", "USD"), Decimal("-1.2345678911")
        )
        self.assertEqual(journal.balance(live.context, "book", "assets:cash", "USD"), Decimal("-2"))
        journal.verify()

    def test_intent_conflict_cannot_reserve_twice(self) -> None:
        order = self.prepare(intent())
        equivalent = intent()
        record = self.store.prepare(equivalent, Reservation(asset="USD", amount=Decimal("200")))
        self.assertEqual(record.intent.intent_id, order.intent_id)
        with self.assertRaises(DomainError):
            self.store.prepare(intent(quantity="3"), Reservation(asset="USD", amount=Decimal("300")))
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute("SELECT COUNT(*) FROM ex_reservations").fetchone()[0]
            ),
            1,
        )

    def test_nonquote_fee_requires_reconciliation_before_admission(self) -> None:
        order = self.prepare(intent())
        fill = fill_for(order).model_copy(update={"fee_asset": "BTC", "fee": Decimal("0.001")})
        result = self.reducer.apply_fill(fill)
        self.assertEqual(result.incident, "NONQUOTE_FEE_RECONCILIATION_REQUIRED")
        self.assertEqual(
            self.writer.call(
                lambda connection: connection.execute(
                    "SELECT COUNT(*) FROM ex_authority_fences WHERE active=1"
                ).fetchone()[0]
            ),
            1,
        )

    def test_fifo_realized_pnl_is_exact_and_fees_separate(self) -> None:
        buy1 = self.prepare(intent(revision=1, quantity="1"))
        buy2 = self.prepare(intent(revision=2, quantity="1"))
        from coinfly.execution.models import Side

        sell = self.prepare(intent(revision=3, quantity="1.5", side=Side.SELL))
        self.reducer.apply_fill(fill_for(buy1, trade="one", quantity="1", price="100"))
        self.reducer.apply_fill(fill_for(buy2, trade="two", quantity="1", price="110"))
        self.reducer.apply_fill(fill_for(sell, trade="three", quantity="1.5", price="120", fee="0.3"))
        row = self.writer.call(
            lambda connection: dict(connection.execute("SELECT * FROM ex_positions").fetchone())
        )
        self.assertEqual(Decimal(row["quantity"]), Decimal("0.5"))
        self.assertEqual(Decimal(row["average_price"]), Decimal("110"))
        self.assertEqual(Decimal(row["realized_pnl"]), Decimal("25"))
        self.assertEqual(Decimal(row["fees_quote"]), Decimal("0.5"))
        Journal(self.writer).verify()

    def test_fifo_lots_never_match_between_execution_scopes(self) -> None:
        from coinfly.execution.models import Side

        first = self.prepare(intent(quantity="1"))
        second = intent(quantity="1", revision=2).model_copy(update={"scope_id": "other-book"})
        self.prepare(second)
        sale = self.prepare(intent(quantity="1", revision=3, side=Side.SELL))
        self.reducer.apply_fill(fill_for(first, trade="scope-a", quantity="1", price="100"))
        self.reducer.apply_fill(fill_for(second, trade="scope-b", quantity="1", price="1000"))
        self.reducer.apply_fill(fill_for(sale, trade="scope-a-exit", quantity="1", price="120"))
        rows = self.writer.call(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    "SELECT scope_id,quantity,realized_pnl FROM ex_positions ORDER BY scope_id"
                )
            ]
        )
        self.assertEqual(
            rows,
            [
                {"scope_id": "book", "quantity": "0", "realized_pnl": "20"},
                {"scope_id": "other-book", "quantity": "1", "realized_pnl": "0"},
            ],
        )
