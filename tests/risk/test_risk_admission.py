from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from coinfly.config.models import EngineConfig, RiskLimits
from coinfly.control.slot_service import SlotRecord, SlotService, SlotStopProof
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack, RegionPolicy
from coinfly.execution.models import OrderIntent, Side
from coinfly.risk.admission_actor import ActiveBookSnapshot, AdmissionActor, AvailableAsset, ExecutionMark
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.risk.drain_policy import DrainPolicy
from coinfly.risk.margin_models import MarginReductionEvidence, MarginScenario, ReductionScenario
from coinfly.storage.writer import Writer


class NoTradeSlotDriver:
    def start(self, slot: SlotRecord) -> None:
        return None

    def quiesce(self, slot: SlotRecord) -> None:
        return None

    def drain(self, slot: SlotRecord, policy: DrainPolicy) -> None:
        raise DomainError("TEST_NO_EXECUTION")

    def reconcile(self, slot: SlotRecord) -> SlotStopProof:
        raise DomainError("TEST_NO_EVIDENCE")


class RiskAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.writer = Writer(Path(self.temp.name) / "risk.sqlite")
        self.interlock = ContextInterlock(self.writer)
        self.actor = AdmissionActor(self.writer, self.interlock, fee_buffer_fraction=Decimal(0))
        self.context = ContextKey(authority_id="authority", environment=Environment.SANDBOX, product_track=ProductTrack.SPOT, broker_profile_id="paper-local", epoch=1)
        self.config = EngineConfig(authority_id="authority", region_policy=RegionPolicy.GLOBAL, execution_scope=("book",),
                                   limits=RiskLimits(max_order_notional=Decimal(100), max_gross_exposure=Decimal(10)))
        self.writer.call(lambda connection: connection.execute(
            "INSERT INTO cf_authorities(authority_id,effective_context,effective_config,effective_revision,transition_epoch,engine_state,armed,admissions_fenced) VALUES(?,?,?,?,?,'ACTIVE',1,0)",
            ("authority", self.context.model_dump_json(), self.config.model_dump_json(), 1, 1),
        ).rowcount)
        self.slots = SlotService(self.writer, self.interlock, NoTradeSlotDriver())
        self.slots.create(slot_id="slot", context=self.context, scope_id="book", instrument="BTC-USD", expected_config_revision=1, request_id="create")
        self.slots.start("slot", expected_revision=1, expected_epoch=1, request_id="start")
        now = datetime.now(timezone.utc)
        self.actor.drawdown.record(self.context, "book", equity=Decimal(100), evidence_id="initial", observed_at=now-timedelta(seconds=2))
        self.snapshot = ActiveBookSnapshot(
            context=self.context, scope_id="book", evidence_id="paper-account-1", observed_at=now, expires_at=now+timedelta(seconds=30),
            fact_sequence=0, reporting_currency="USD", funded_equity=Decimal(100), eligible_collateral=Decimal(100),
            available_assets=(AvailableAsset(asset="USD", amount=Decimal(100)), AvailableAsset(asset="BTC", amount=Decimal(0))),
            marks=(ExecutionMark(instrument="BTC-USD", price=Decimal(10)),),
            complete_execution_scope=True, authoritative_account_evidence=True, conflicting_external_activity=False,
        )
        self.actor.observe(self.snapshot)

    def tearDown(self) -> None:
        self.slots.close()
        self.writer.close()
        self.temp.cleanup()

    def intent(self, quantity: str = "6", revision: int = 1) -> OrderIntent:
        return OrderIntent(context=self.context, scope_id="book", profile_version=1, signer_version=0, slot_id="slot",
                           allocation_revision=revision, instrument="BTC-USD", base_asset="BTC", quote_asset="USD",
                           side=Side.BUY, quantity=Decimal(quantity), limit_price=Decimal(10))

    def test_simultaneous_last_collateral_admissions_reserve_once(self) -> None:
        barrier = threading.Barrier(2)
        def submit(revision: int) -> str:
            barrier.wait()
            try:
                self.actor.admit(self.intent(revision=revision))
                return "accepted"
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, (1, 2)))
        self.assertEqual(results.count("accepted"), 1)
        self.assertIn("INSUFFICIENT_ACTIVE_BOOK_FUNDS", results)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM ex_intents").fetchone()[0]), 1)
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_risk_grants").fetchone()[0]), 1)

    def test_duplicate_semantic_request_does_not_reserve_twice(self) -> None:
        first = self.actor.admit(self.intent())
        duplicate = self.actor.admit(self.intent())
        self.assertEqual(first.intent.intent_id, duplicate.intent.intent_id)
        self.interlock.validate(first.intent)
        with self.assertRaisesRegex(DomainError, "SEMANTIC_INTENT_CONFLICT"):
            self.actor.admit(self.intent(quantity="5"))

    def test_native_hold_and_local_reservation_are_not_counted_twice(self) -> None:
        first = self.actor.admit(self.intent())
        snapshot = self.snapshot.model_copy(update={
            "evidence_id": "paper-account-2", "observed_at": datetime.now(timezone.utc),
            "available_assets": (AvailableAsset(asset="USD", amount=Decimal(40)),),
            "reservations_in_remote_holds": (first.intent.intent_id,),
        })
        self.actor.observe(snapshot)
        second = self.actor.admit(self.intent(quantity="4", revision=2))
        self.assertEqual(second.reservation_remaining, Decimal(40))
        with self.assertRaisesRegex(DomainError, "RISK_GRANT_STALE"):
            self.interlock.validate(first.intent)

    def test_fact_after_admission_invalidates_final_send(self) -> None:
        record = self.actor.admit(self.intent())
        self.writer.call(lambda connection: connection.execute("INSERT INTO ex_fact_state VALUES(?,1)", (self.context.identity,)).rowcount)
        sent: list[str] = []
        with self.assertRaisesRegex(DomainError, "RISK_GRANT_BEHIND_FACTS"):
            self.interlock.run_at_send_boundary(record.intent, lambda: sent.append("sent"))
        self.assertEqual(sent, [])

    def test_pause_wins_race_before_actual_network_invocation(self) -> None:
        record = self.actor.admit(self.intent())
        sent: list[str] = []
        with self.interlock.lock:
            self.writer.call(lambda connection: connection.execute("UPDATE cf_authorities SET admissions_fenced=1,armed=0 WHERE authority_id='authority'").rowcount)
        with self.assertRaisesRegex(DomainError, "UNARMED_OR_TRANSITIONING"):
            self.interlock.run_at_send_boundary(record.intent, lambda: sent.append("sent"))
        self.assertEqual(sent, [])

    def test_send_boundary_serializes_with_switch_fencing(self) -> None:
        record = self.actor.admit(self.intent())
        entered, release, fenced = threading.Event(), threading.Event(), threading.Event()
        def invoke() -> None:
            entered.set()
            self.assertTrue(release.wait(2))
        def switch() -> None:
            with self.interlock.lock:
                self.writer.call(lambda connection: connection.execute("UPDATE cf_authorities SET admissions_fenced=1,armed=0").rowcount)
                fenced.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            send_future = executor.submit(self.interlock.run_at_send_boundary, record.intent, invoke)
            self.assertTrue(entered.wait(2))
            fence_future = executor.submit(switch)
            self.assertFalse(fenced.wait(0.02))
            release.set()
            send_future.result(2)
            fence_future.result(2)
        self.assertTrue(fenced.is_set())

    def test_loss_update_revokes_previously_valid_grant(self) -> None:
        record = self.actor.admit(self.intent())
        self.actor.drawdown.record(self.context, "book", equity=Decimal(70), evidence_id="loss", observed_at=datetime.now(timezone.utc), realized_pnl=Decimal(-30))
        with self.assertRaisesRegex(DomainError, "LOSS_LIMIT_INVALIDATES_GRANT"):
            self.interlock.validate(record.intent)

    def test_unknown_fence_cannot_clear_an_unrelated_incident(self) -> None:
        self.interlock.provisional_fence("authority", "LATE_EXPOSURE")
        self.interlock.provisional_fence("authority", "UNKNOWN_SUBMISSION")
        with self.assertRaisesRegex(DomainError, "INCIDENT_REQUIRES_RECOVERY"):
            self.interlock.reconcile_provisional_unknown(self.context)
        with self.assertRaisesRegex(DomainError, "AUTHORITY_FENCED"):
            self.actor.admit(self.intent())

    def test_oversized_observation_or_stale_snapshot_cannot_authorize_spending(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ActiveBookSnapshot.model_validate({**self.snapshot.model_dump(), "consolidated_equity": Decimal(1000000)})
        stale = self.snapshot.model_copy(update={"observed_at": datetime.now(timezone.utc)-timedelta(minutes=1)})
        with self.assertRaisesRegex(DomainError, "EXECUTION_BOOK_STALE"):
            self.actor.observe(stale)

    def test_drawdown_survives_epoch_reset_and_cash_deposit(self) -> None:
        store = self.actor.drawdown
        now = datetime.now(timezone.utc)
        deposited = store.record(self.context, "book", equity=Decimal(200), external_flow=Decimal(100), evidence_id="deposit", observed_at=now)
        self.assertEqual(deposited.unit_nav, Decimal(1))
        fallen = store.record(self.context.model_copy(update={"epoch": 2}), "book", equity=Decimal(160), evidence_id="decline", observed_at=now+timedelta(seconds=1))
        self.assertEqual(fallen.drawdown_fraction, Decimal("0.2"))
        self.assertEqual(fallen.high_water_nav, Decimal(1))
        self.assertEqual(store.read(self.context, "book").drawdown_fraction, Decimal("0.2"))
        self.assertIsNone(store.read(self.context.model_copy(update={"environment": Environment.LIVE}), "book"))

    def test_margin_scenarios_require_both_initial_and_maintenance_headroom(self) -> None:
        values = dict(evidence_id=str(uuid4()), eligible_equity=Decimal(100), initial_margin=Decimal(80),
                      maintenance_margin=Decimal(90), incremental_reservations=Decimal(10), initial_buffer=Decimal(10), maintenance_buffer=Decimal(10))
        scenario = MarginScenario(**values)
        scenario.require_safe()
        self.assertEqual(scenario.maintenance_utilization, Decimal("0.9"))
        with self.assertRaisesRegex(DomainError, "INITIAL_MARGIN_HEADROOM"):
            scenario.model_copy(update={"incremental_reservations": Decimal(11)}).require_safe()
        with self.assertRaisesRegex(DomainError, "MAINTENANCE_MARGIN_HEADROOM"):
            scenario.model_copy(update={"maintenance_margin": Decimal(91)}).require_safe()

    def test_distressed_margin_close_can_improve_risk_without_initial_headroom(self) -> None:
        before = MarginScenario(evidence_id="stress-before", eligible_equity=Decimal(50), initial_margin=Decimal(100),
                                maintenance_margin=Decimal(80), incremental_reservations=Decimal(0), initial_buffer=Decimal(0), maintenance_buffer=Decimal(0))
        after = before.model_copy(update={"evidence_id": "stress-after", "eligible_equity": Decimal(49),
                                           "initial_margin": Decimal(60), "maintenance_margin": Decimal(40)})
        with self.assertRaises(DomainError):
            after.require_safe()
        reduction = MarginReductionEvidence(evidence_id="certified-reduction", venue_reduce_only_enforced=True,
                                             contract_units_verified=True, all_fill_scenarios_covered=True,
                                             scenarios=(ReductionScenario(before=before, after=after),))
        reduction.require_safe_reduction()
        with self.assertRaisesRegex(DomainError, "REDUCTION_MARGIN_UNCERTIFIED"):
            reduction.model_copy(update={"venue_reduce_only_enforced": False}).require_safe_reduction()
        with self.assertRaisesRegex(DomainError, "REDUCTION_WORSENS_STRESSED_MARGIN"):
            ReductionScenario(before=before, after=after.model_copy(update={"eligible_equity": Decimal(0)})).require_nonworsening()

    def test_daily_loss_and_its_receipt_roll_back_with_failed_accounting(self) -> None:
        store = self.actor.drawdown
        now = datetime.now(timezone.utc)
        def failed_batch(connection):
            store.record_tx(connection, self.context, "book", equity=Decimal(95), realized_pnl=Decimal(-5),
                            evidence_id="fill-batch", observed_at=now)
            raise DomainError("SIMULATED_LEDGER_FAILURE")
        with self.assertRaisesRegex(DomainError, "SIMULATED_LEDGER_FAILURE"):
            self.writer.call(failed_batch)
        self.assertEqual(store.read(self.context, "book").daily_net_result, Decimal(0))
        self.assertEqual(self.writer.call(lambda connection: connection.execute("SELECT COUNT(*) FROM cf_drawdown_samples WHERE evidence_id='fill-batch'").fetchone()[0]), 0)
        state = self.writer.call(lambda connection: store.record_tx(
            connection, self.context, "book", equity=Decimal(95), realized_pnl=Decimal(-5), evidence_id="fill-batch", observed_at=now,
        ))
        self.assertEqual(state.daily_net_result, Decimal(-5))
        repeated = store.record(self.context, "book", equity=Decimal(95), realized_pnl=Decimal(-5), evidence_id="fill-batch", observed_at=now)
        self.assertEqual(repeated.daily_net_result, Decimal(-5))
