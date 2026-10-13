import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.risk.stop_certificate import PositionResidual, ScopeWatermark, StopCertificate


class StopProofTests(unittest.TestCase):
    def test_missing_or_stale_evidence_cannot_certify_stop(self) -> None:
        now = datetime.now(timezone.utc)
        context = ContextKey(authority_id="account", environment=Environment.SANDBOX, product_track=ProductTrack.SPOT, broker_profile_id="paper-local", epoch=1)
        watermark = ScopeWatermark(scope_id="book", evidence_id="complete-account", account_observed_at=now,
                                   account_stream_sequence=10, applied_stream_sequence=10, history_complete=True, account_complete=True)
        proof = StopCertificate(proof_id="proof", context=context, execution_scope=("book",), issued_at=now, expires_at=now+timedelta(seconds=10),
                                fact_sequence=4, watermarks=(watermark,), residual_positions=(), outstanding_order_ids=(), unresolved_create_ids=(),
                                unapplied_fill_count=0, all_delegated_positions_covered=True, all_related_orders_terminal=True,
                                dispatch_tasks_fenced=True, task_fence_evidence_id="retirement")
        proof.require_valid(context, ("book",), now)
        poisoned = (
            {"all_delegated_positions_covered": False}, {"all_related_orders_terminal": False}, {"dispatch_tasks_fenced": False},
            {"outstanding_order_ids": ("open",)}, {"unresolved_create_ids": ("unknown",)}, {"unapplied_fill_count": 1},
            {"residual_positions": (PositionResidual(scope_id="book", instrument="BTC-USD", quantity=Decimal("0.000001")),)},
            {"watermarks": (watermark.model_copy(update={"history_complete": False}),)},
            {"watermarks": (watermark.model_copy(update={"applied_stream_sequence": 9}),)},
            {"watermarks": (watermark.model_copy(update={"account_observed_at": now-timedelta(minutes=1)}),)},
            {"execution_scope": ("book", "missing")}, {"expires_at": now},
        )
        for changes in poisoned:
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                proof.model_copy(update=changes).require_valid(context, ("book",), now)
