"""Real local-matcher evidence, without any claim of live fee completeness."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from coinfly.accounting.paper_episodes import (
    PaperEpisodeFinalizer,
    PaperFinalizationProof,
    PaperFinalizationResult,
)
from coinfly.domain.context import DomainError, Environment, ProductTrack
from coinfly.execution.fact_reducer import apply_fill_tx, terminal_tx
from coinfly.execution.models import Fill, OrderIntent, Side
from coinfly.reporting.history_service import HistoryQuery, HistoryService, WriterHistorySource
from coinfly.simulation.paper_matching import (
    DepthLevel,
    PaperFill,
    PaperInstrument,
    PaperMatcher,
    PaperOrderRequest,
)
from coinfly.simulation.paper_matching import Side as PaperSide
from coinfly.storage.writer import Writer
from tests.execution.helpers import LedgerCase, context, fill_for, intent


class PaperEpisodeTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.context = context()
        self.matcher = PaperMatcher(
            self.context,
            (
                PaperInstrument(
                    instrument_id="BTC-USD",
                    base_currency="BTC",
                    quote_currency="USD",
                    product_track=ProductTrack.SPOT,
                ),
            ),
            initial_balances={"USD": Decimal(1000)},
        )
        self.writer.call(
            lambda connection: (
                connection.execute(
                    "CREATE TABLE wp_paper_books(authority_id TEXT,environment TEXT,scope_id TEXT,product_track TEXT,context_json TEXT,state_json TEXT)"
                ).rowcount
            )
        )
        self.writer.call(self.persist_tx)

    def persist_tx(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM wp_paper_books")
        connection.execute(
            "INSERT INTO wp_paper_books VALUES(?,?,?,?,?,?)",
            (
                self.context.authority_id,
                "SANDBOX",
                "book",
                "SPOT",
                self.context.model_dump_json(),
                json.dumps(self.matcher.dump_state(), sort_keys=True),
            ),
        )

    def submit(self, *, revision: int, quantity: str, side: Side) -> OrderIntent:
        order = intent(revision=revision, quantity=quantity, side=side).model_copy(
            update={"limit_price": Decimal(200) if side == Side.BUY else Decimal(1)}
        )
        self.prepare(order)
        self.matcher.submit(
            PaperOrderRequest(
                client_order_id=order.intent_id,
                instrument_id="BTC-USD",
                side=PaperSide(side.value),
                quantity=order.quantity,
                limit_price=order.limit_price,
                reduce_only=side == Side.SELL,
            ),
            now=datetime.now(timezone.utc),
        )
        return order

    def feed(self, bid: str, ask: str) -> PaperFinalizationResult:
        fills = self.matcher.update_book(
            "BTC-USD",
            bids=(DepthLevel(price=Decimal(bid), quantity=Decimal(1000)),),
            asks=(DepthLevel(price=Decimal(ask), quantity=Decimal(1000)),),
            now=datetime.now(timezone.utc),
        )
        prefix = self.context.identity[:20] + "-"

        def commit(connection: sqlite3.Connection) -> PaperFinalizationResult:
            for local in fills:
                apply_fill_tx(connection, self.fact(local))
            for order in self.matcher.orders:
                if order.terminal:
                    terminal_tx(
                        connection,
                        order.client_order_id,
                        prefix + "terminal-" + order.order_id,
                        complete_fills=True,
                    )
            self.persist_tx(connection)
            return PaperEpisodeFinalizer.finalize_tx(
                connection, PaperEpisodeFinalizer.capture(self.matcher, "book")
            )

        return self.writer.call(commit)

    def fact(self, local: PaperFill) -> Fill:
        prefix = self.context.identity[:20] + "-"
        return Fill(
            context=self.context,
            scope_id="book",
            venue_id="coinfly-paper",
            account_id="book",
            instrument=local.instrument_id,
            trade_id=prefix + local.trade_id,
            venue_order_id=prefix + local.order_id,
            evidence_id=prefix + local.trade_id,
            intent_id=local.client_order_id,
            base_asset="BTC",
            quote_asset="USD",
            side=Side(local.side.value),
            quantity=local.quantity,
            price=local.price,
            fee=local.fee,
            fee_asset=local.fee_currency,
        )

    def rows(self) -> list[dict[str, object]]:
        return self.writer.call(
            lambda connection: [
                dict(row) for row in connection.execute("SELECT * FROM ex_closed_results ORDER BY sequence")
            ]
        )

    def test_round_trip_posts_exact_closed_result_and_real_history(self) -> None:
        self.submit(revision=1, quantity="2", side=Side.BUY)
        opened = self.feed("99", "100")
        self.assertEqual(opened.episode_ids, ())
        self.assertEqual(opened.pending_instruments, ("BTC-USD",))
        self.submit(revision=2, quantity="2", side=Side.SELL)
        closed = self.feed("105", "106")
        self.assertEqual(len(closed.episode_ids), 1)
        row = self.rows()[0]
        self.assertEqual(Decimal(str(row["matched_pnl"])), Decimal(10))
        self.assertEqual(Decimal(str(row["fees"])), Decimal("0.410"))
        self.assertEqual(row["funding"], "0")
        self.assertEqual(row["charges"], "0")
        self.assertEqual(row["pending_charges"], 0)
        report = HistoryService(WriterHistorySource(self.writer)).open(
            HistoryQuery(self.context.authority_id, "SANDBOX", "TRADE_RESULTS")
        )
        self.assertEqual(report["total_count"], 1)
        self.assertEqual(Decimal(report["totals"]["net"]), Decimal("9.590"))

    def test_partial_closes_do_not_finalize_then_multiple_round_trips_are_distinct(self) -> None:
        self.submit(revision=1, quantity="2", side=Side.BUY)
        self.feed("99", "100")
        self.submit(revision=2, quantity="1", side=Side.SELL)
        self.assertFalse(self.feed("105", "106").episode_ids)
        self.assertFalse(self.rows())
        self.submit(revision=3, quantity="1", side=Side.SELL)
        first = self.feed("106", "107")
        self.assertEqual(len(first.episode_ids), 1)
        self.submit(revision=4, quantity="1", side=Side.BUY)
        self.feed("99", "100")
        self.submit(revision=5, quantity="1", side=Side.SELL)
        second = self.feed("110", "111")
        self.assertEqual(len(second.episode_ids), 1)
        self.assertNotEqual(first.episode_ids, second.episode_ids)
        self.assertEqual(len(self.rows()), 2)

    def test_duplicate_finalization_after_reopen_preserves_exact_economics(self) -> None:
        self.submit(revision=1, quantity="1", side=Side.BUY)
        self.feed("99", "100")
        self.submit(revision=2, quantity="1", side=Side.SELL)
        self.feed("110", "111")
        before = self.rows()
        state = self.matcher.dump_state()
        self.writer.close()
        self.writer = Writer(self.path)
        self.matcher = PaperMatcher.restore(self.context, self.matcher.instruments, state)
        result = self.writer.call(
            lambda connection: PaperEpisodeFinalizer.finalize_tx(
                connection, PaperEpisodeFinalizer.capture(self.matcher, "book")
            )
        )
        self.assertEqual(result.episode_ids, ())
        self.assertIsNone(result.incident_code)
        self.assertEqual(self.rows(), before)

    def test_unsettled_order_defers_a_closed_candidate(self) -> None:
        self.submit(revision=1, quantity="1", side=Side.BUY)
        self.feed("99", "100")
        self.submit(revision=2, quantity="1", side=Side.SELL)
        # A prepared create whose outcome is not terminal blocks certification.
        self.prepare(intent(revision=3, quantity="1"))
        result = self.feed("110", "111")
        self.assertEqual(result.pending_instruments, ("BTC-USD",))
        self.assertFalse(self.rows())

    def test_late_old_fill_fences_and_never_rewrites_closed_results(self) -> None:
        opening = self.submit(revision=1, quantity="1", side=Side.BUY)
        self.feed("99", "100")
        self.submit(revision=2, quantity="1", side=Side.SELL)
        self.feed("110", "111")
        before = self.rows()
        late = fill_for(opening, trade="late-unmodeled-fill", quantity="0.1").model_copy(
            update={
                "venue_id": "coinfly-paper",
                "account_id": "book",
                "venue_order_id": self.context.identity[:20] + "-paper-order-1",
            }
        )
        self.reducer.apply_fill(late)
        result = self.writer.call(
            lambda connection: PaperEpisodeFinalizer.finalize_tx(
                connection, PaperEpisodeFinalizer.capture(self.matcher, "book")
            )
        )
        self.assertEqual(result.incident_code, "PAPER_EPISODE_FACT_COVERAGE_MISMATCH")
        self.assertEqual(self.rows(), before)
        self.assertGreater(
            self.writer.call(
                lambda connection: int(
                    connection.execute("SELECT COUNT(*) FROM ex_authority_fences WHERE active=1").fetchone()[
                        0
                    ]
                )
            ),
            0,
        )

    def test_unpersisted_financial_state_cannot_certify_any_result(self) -> None:
        self.submit(revision=1, quantity="1", side=Side.BUY)
        proof = PaperEpisodeFinalizer.capture(self.matcher, "book")
        result = self.writer.call(lambda connection: PaperEpisodeFinalizer.finalize_tx(connection, proof))
        self.assertEqual(result.incident_code, "PAPER_EPISODE_STATE_MISMATCH")
        self.assertFalse(self.rows())

    def test_corrupted_lot_cost_cannot_create_certified_profit(self) -> None:
        self.submit(revision=1, quantity="1", side=Side.BUY)
        self.feed("99", "100")
        self.writer.call(lambda connection: connection.execute("UPDATE ex_open_lots SET price='80'").rowcount)
        self.submit(revision=2, quantity="1", side=Side.SELL)
        result = self.feed("105", "106")
        self.assertEqual(result.incident_code, "PAPER_EPISODE_PNL_RECONCILIATION_MISMATCH")
        self.assertFalse(self.rows())
        self.assertGreater(
            self.writer.call(
                lambda connection: int(
                    connection.execute("SELECT COUNT(*) FROM ex_authority_fences WHERE active=1").fetchone()[
                        0
                    ]
                )
            ),
            0,
        )

    def test_consistently_wrong_intent_and_fill_asset_cannot_certify_the_matcher(self) -> None:
        order = intent(quantity="1").model_copy(update={"base_asset": "ETH", "limit_price": Decimal(200)})
        self.prepare(order)
        self.matcher.submit(
            PaperOrderRequest(
                client_order_id=order.intent_id,
                instrument_id="BTC-USD",
                side=PaperSide.BUY,
                quantity=Decimal(1),
                limit_price=Decimal(200),
            ),
            now=datetime.now(timezone.utc),
        )
        local = self.matcher.update_book(
            "BTC-USD",
            bids=(DepthLevel(price=Decimal(99), quantity=Decimal(100)),),
            asks=(DepthLevel(price=Decimal(100), quantity=Decimal(100)),),
            now=datetime.now(timezone.utc),
        )[0]

        def commit(connection: sqlite3.Connection) -> PaperFinalizationResult:
            apply_fill_tx(connection, self.fact(local).model_copy(update={"base_asset": "ETH"}))
            terminal_tx(connection, order.intent_id, "wrong-asset-terminal", complete_fills=True)
            self.persist_tx(connection)
            return PaperEpisodeFinalizer.finalize_tx(
                connection, PaperEpisodeFinalizer.capture(self.matcher, "book")
            )

        result = self.writer.call(commit)
        self.assertEqual(result.incident_code, "PAPER_EPISODE_INSTRUMENT_CONTRACT_MISMATCH")
        self.assertFalse(self.rows())

    def test_unissued_proof_and_non_spot_realms_are_rejected(self) -> None:
        from dataclasses import replace

        invalid = PaperFinalizationProof(self.context, "book", "{}", object())
        with self.assertRaisesRegex(DomainError, "PAPER_EPISODE_PROOF_UNVERIFIED"):
            self.writer.call(lambda connection: PaperEpisodeFinalizer.finalize_tx(connection, invalid))
        proof = PaperEpisodeFinalizer.capture(self.matcher, "book")
        wrong_realm = replace(
            proof, context=self.context.model_copy(update={"environment": Environment.LIVE})
        )
        with self.assertRaisesRegex(DomainError, "PAPER_EPISODES_REQUIRE_SANDBOX_SPOT"):
            self.writer.call(lambda connection: PaperEpisodeFinalizer.finalize_tx(connection, wrong_realm))

    def test_non_usd_reporting_is_explicitly_pending(self) -> None:
        self.matcher = PaperMatcher(
            self.context,
            (
                PaperInstrument(
                    instrument_id="BTC-EUR",
                    base_currency="BTC",
                    quote_currency="EUR",
                    product_track=ProductTrack.SPOT,
                ),
            ),
            initial_balances={"EUR": Decimal(1000)},
        )
        self.writer.call(self.persist_tx)
        result = self.writer.call(
            lambda connection: PaperEpisodeFinalizer.finalize_tx(
                connection, PaperEpisodeFinalizer.capture(self.matcher, "book")
            )
        )
        self.assertEqual(result.pending_reason, "PAPER_REPORTING_CURRENCY_UNSUPPORTED")
        self.assertFalse(self.rows())
