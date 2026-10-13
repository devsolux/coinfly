"""Crash-window recovery for a retired source and an unpublished paper target.

The shared fixture provides only explicit synthetic market inputs and a test
readiness sentinel. It does not certify the neural dataset or a live gateway.
"""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from unittest.mock import patch

from coinfly.accounting.journal import Journal
from coinfly.config.models import default_config
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import DomainError, RegionPolicy
from coinfly.risk.stop_certificate import StopCertificate
from coinfly.worker.paper_engine import LocalPaperEngine
from tests.integration.test_paper_worker_flow import WorkerFixture


class PaperStagedRecoveryTests(WorkerFixture):
    def stage_failed_rebind(self):
        source, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.buy_fixture(source)
        self.market(source, "BTC-USD", "110.00", "110.02")
        status = self.worker.coordinator.status(self.worker.authority_id)
        config = default_config().model_copy(update={"region_policy": RegionPolicy.GLOBAL})
        preview = self.worker.coordinator.preview(config, expected_revision=status.effective_revision,
                                                  expected_epoch=status.transition_epoch)
        original_initialize = LocalPaperEngine.initialize

        def fail_after_persist(engine, candidate, context):
            original_initialize(engine, candidate, context)
            raise DomainError("FIXTURE_POST_INITIALIZE_FAILURE")

        with patch.object(LocalPaperEngine, "initialize", fail_after_persist):
            operation = self.worker.coordinator.apply(
                preview.preview_id, preview.config_digest, expected_revision=status.effective_revision,
                expected_epoch=status.transition_epoch, request_id="fixture-staged-target")
            operation = self.worker.coordinator.wait(operation.operation_id)
        self.assertEqual(operation.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(operation.error_code, "FIXTURE_POST_INITIALIZE_FAILURE")
        self.assertFalse(source.matcher.open_orders)
        self.assertEqual(source.matcher.balances["BTC"], 0)
        self.assertGreater(source.matcher.balances["USD"], Decimal(100))
        return source, operation

    def stored_book(self):
        return self.worker.writer.call(lambda connection: tuple(connection.execute(
            "SELECT context_json,state_json FROM wp_paper_books").fetchone()))

    def restart(self):
        self.worker.close()
        self.worker = self.new_worker()

    def expire_source_proof(self, operation):
        raw = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT stop_proof FROM cf_transition_operations WHERE operation_id=?",
            (operation.operation_id,)).fetchone()[0])
        proof = StopCertificate.model_validate_json(raw)
        expired = proof.model_copy(update={
            "issued_at": proof.issued_at - timedelta(minutes=2),
            "expires_at": proof.expires_at - timedelta(minutes=2),
            "watermarks": tuple(item.model_copy(update={
                "account_observed_at": item.account_observed_at - timedelta(minutes=2),
            }) for item in proof.watermarks),
        })
        self.worker.writer.call(lambda connection: connection.execute(
            "UPDATE cf_transition_operations SET stop_proof=? WHERE operation_id=?",
            (expired.model_dump_json(), operation.operation_id)).rowcount)
        return proof

    def test_post_initialize_failure_recovers_retired_source_without_reseeding(self):
        source, operation = self.stage_failed_rebind()
        expected_cash = source.matcher.balances["USD"]
        staged_book = self.stored_book()
        archive = self.worker.writer.call(lambda connection: dict(connection.execute(
            "SELECT * FROM wp_paper_archives WHERE context_id=?", (source.context.identity,)).fetchone()))
        self.restart()
        status = self.worker.coordinator.status(self.worker.authority_id)
        self.assertFalse(status.recovery_required)
        self.assertFalse(status.armed)
        self.assertEqual(status.effective_context, source.context)
        recovered_source = self.worker._engines[source.context.identity]
        self.assertTrue(recovered_source._retired)
        self.assertTrue(recovered_source._quiesced)
        self.assertFalse(recovered_source._armed)
        self.assertEqual(recovered_source.matcher.balances["USD"], expected_cash)
        with self.assertRaises(DomainError):
            recovered_source.arm()
        with self.assertRaises(DomainError):
            recovered_source._cancel()
        self.assertEqual(self.stored_book(), staged_book)
        self.worker.coordinator.retry(operation.operation_id)
        final = self.worker.coordinator.wait(operation.operation_id)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())
        target = self.worker._engines[final.target_context.identity]
        self.assertEqual(target.matcher.balances["USD"], expected_cash)
        self.assertFalse(target._armed)
        self.assertFalse(target.matcher.orders)
        self.assertEqual(self.stored_book(), staged_book)
        self.assertEqual(self.worker.writer.call(lambda connection: dict(connection.execute(
            "SELECT * FROM wp_paper_archives WHERE context_id=?", (source.context.identity,)).fetchone())), archive)
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0]), 1)
        Journal(self.worker.writer).verify()

    def test_expired_proof_refreshes_archived_source_without_redrain_or_retirement(self):
        source, operation = self.stage_failed_rebind()
        proof = self.expire_source_proof(operation)
        staged_book = self.stored_book()
        self.restart()
        recovered_source = self.worker._engines[source.context.identity]
        with (
            patch.object(recovered_source, "drain", side_effect=AssertionError("retired source redrain")),
            patch.object(recovered_source, "retire", side_effect=AssertionError("second retirement")),
            patch.object(recovered_source, "reconcile", wraps=recovered_source.reconcile) as reconcile,
        ):
            self.worker.coordinator.retry(operation.operation_id)
            final = self.worker.coordinator.wait(operation.operation_id)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())
        reconcile.assert_called_once()
        self.assertEqual(self.stored_book(), staged_book)
        fresh_raw = self.worker.writer.call(lambda connection: connection.execute(
            "SELECT stop_proof FROM cf_transition_operations WHERE operation_id=?",
            (operation.operation_id,)).fetchone()[0])
        self.assertNotEqual(StopCertificate.model_validate_json(fresh_raw).proof_id, proof.proof_id)
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_context_retirements WHERE context_id=?",
            (source.context.identity,)).fetchone()[0]), 1)

    def assert_factory_failure_restart_recovers_retirement(self, *, expired):
        source, _ = self.initialize()
        self.arm()
        self.open_slot()
        self.buy_fixture(source)
        self.market(source, "BTC-USD", "110.00", "110.02")
        status = self.worker.coordinator.status(self.worker.authority_id)
        preview = self.worker.coordinator.preview(default_config(), expected_revision=status.effective_revision,
                                                  expected_epoch=status.transition_epoch)

        def fail_factory(config, context):
            raise DomainError("FIXTURE_FACTORY_FAILURE")

        with patch.object(self.worker, "_injected_factory", fail_factory):
            operation = self.worker.coordinator.apply(
                preview.preview_id, preview.config_digest, expected_revision=status.effective_revision,
                expected_epoch=status.transition_epoch, request_id="fixture-factory-failure")
            operation = self.worker.coordinator.wait(operation.operation_id)
        self.assertEqual(operation.phase, TransitionPhase.FAILED_INITIALIZATION)
        self.assertEqual(operation.error_code, "FIXTURE_FACTORY_FAILURE")
        expected_cash = source.matcher.balances["USD"]
        source_book = self.stored_book()
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM wp_paper_archives").fetchone()[0]), 0)
        if expired:
            self.expire_source_proof(operation)
        self.restart()
        status = self.worker.coordinator.status(self.worker.authority_id)
        self.assertFalse(status.recovery_required)
        self.assertFalse(status.armed)
        recovered_source = self.worker._engines[source.context.identity]
        self.assertTrue(recovered_source._retired)
        self.assertTrue(recovered_source._quiesced)
        self.assertEqual(self.stored_book(), source_book)
        with (
            patch.object(recovered_source, "drain", side_effect=AssertionError("retired source redrain")),
            patch.object(recovered_source, "retire", side_effect=AssertionError("second retirement")),
            patch.object(recovered_source, "reconcile", wraps=recovered_source.reconcile) as reconcile,
        ):
            self.worker.coordinator.retry(operation.operation_id)
            final = self.worker.coordinator.wait(operation.operation_id)
        self.assertEqual(final.phase, TransitionPhase.READY_UNARMED, final.model_dump_json())
        self.assertEqual(reconcile.call_count, 1 if expired else 0)
        target = self.worker._engines[final.target_context.identity]
        self.assertEqual(target.matcher.balances["USD"], expected_cash)
        self.assertFalse(target._armed)
        self.assertFalse(target.matcher.orders)
        self.assertEqual(self.worker.writer.call(lambda connection: connection.execute(
            "SELECT COUNT(*) FROM ex_journals WHERE event_kind='SIMULATION_SEED'").fetchone()[0]), 1)
        Journal(self.worker.writer).verify()

    def test_factory_failure_restart_recovers_retired_current_book(self):
        self.assert_factory_failure_restart_recovers_retirement(expired=False)

    def test_expired_factory_failure_restart_refreshes_retired_current_book(self):
        self.assert_factory_failure_restart_recovers_retirement(expired=True)

    def test_archive_is_immutable_in_the_normal_writer_domain(self):
        self.stage_failed_rebind()
        for statement in ("UPDATE wp_paper_archives SET state_json='{}'", "DELETE FROM wp_paper_archives"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.worker.writer.call(lambda connection: connection.execute(statement).rowcount)

    def assert_corruption_fenced(self, mutate):
        source, operation = self.stage_failed_rebind()
        # Simulate offline database corruption past the normal immutable trigger.
        def corrupt(connection):
            connection.execute("DROP TRIGGER wp_paper_archives_no_update")
            mutate(connection, source, operation)
        self.worker.writer.call(corrupt)
        staged_book = self.stored_book()
        self.restart()
        status = self.worker.coordinator.status(self.worker.authority_id)
        self.assertTrue(status.recovery_required)
        self.assertFalse(status.armed)
        self.assertTrue(self.worker.interlock.is_provisionally_fenced(self.worker.authority_id))
        self.assertNotIn(source.context.identity, self.worker._engines)
        self.assertEqual(self.stored_book(), staged_book)

    def test_archive_hash_corruption_cannot_reconstruct_source(self):
        self.assert_corruption_fenced(lambda connection, source, operation: connection.execute(
            "UPDATE wp_paper_archives SET state_sha256=?", ("0" * 64,)))

    def test_archive_target_context_mismatch_cannot_reconstruct_source(self):
        self.assert_corruption_fenced(lambda connection, source, operation: connection.execute(
            "UPDATE wp_paper_archives SET target_context_json=?",
            (operation.target_context.model_copy(update={"epoch": 999}).model_dump_json(),)))

    def test_changed_source_fact_sequence_cannot_reconstruct_source(self):
        self.assert_corruption_fenced(lambda connection, source, operation: connection.execute(
            "UPDATE ex_fact_state SET sequence=sequence+1 WHERE context_id=?", (source.context.identity,)))

    def test_rehashed_archive_cannot_change_settled_source_capital(self):
        def mutate(connection, source, operation):
            state = json.loads(connection.execute("SELECT state_json FROM wp_paper_archives").fetchone()[0])
            for field in ("balances", "initial_balances"):
                state[field] = [[currency, str(Decimal(amount) + 1) if currency == "USD" else amount]
                                for currency, amount in state[field]]
            raw = json.dumps(state, sort_keys=True)
            connection.execute("UPDATE wp_paper_archives SET state_json=?,state_sha256=?",
                               (raw, sha256(raw.encode()).hexdigest()))
        self.assert_corruption_fenced(mutate)

    def test_changed_staged_target_cash_cannot_reconstruct_source(self):
        def mutate(connection, source, operation):
            state = json.loads(connection.execute("SELECT state_json FROM wp_paper_books").fetchone()[0])
            for field in ("balances", "initial_balances"):
                state[field] = [[currency, str(Decimal(amount) + 1) if currency == "USD" else amount]
                                for currency, amount in state[field]]
            state["flat_rebind_provenance"]["settled_balances"] = state["initial_balances"]
            connection.execute("UPDATE wp_paper_books SET state_json=?", (json.dumps(state, sort_keys=True),))
        self.assert_corruption_fenced(mutate)
