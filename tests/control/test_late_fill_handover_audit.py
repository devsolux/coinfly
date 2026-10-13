"""Deterministic barrier races; no market connection or live gateway exists."""

from decimal import Decimal
from threading import Event

import pytest
from tests.control.test_context_coordinator import FixtureEngine
from tests.execution.helpers import fill_for, intent

from coinfly.config.models import EngineConfig
from coinfly.control.context_coordinator import ContextCoordinator
from coinfly.control.transition_state import TransitionPhase
from coinfly.domain.context import DomainError
from coinfly.execution.evidence import authority_fenced_tx
from coinfly.execution.fact_reducer import FactReducer
from coinfly.execution.intent_store import IntentStore
from coinfly.execution.models import Reservation, Side
from coinfly.risk.context_interlock import ContextInterlock
from coinfly.storage.writer import Writer


def test_late_source_fill_during_target_initialization_prevents_handover(tmp_path):
    entered, release = Event(), Event()
    with Writer(tmp_path / "synthetic.sqlite") as writer:
        interlock = ContextInterlock(writer)
        engines = []
        def factory(config, context):
            engine = FixtureEngine(writer, context, [])
            if context.epoch == 2:
                entered.set()
                assert release.wait(5), "test did not release initialization barrier"
            engines.append(engine)
            return engine
        coordinator = ContextCoordinator(writer, interlock, factory)
        config = EngineConfig(authority_id="owner", execution_scope=("paper-book",))
        def apply(request):
            status = coordinator.status("owner")
            preview = coordinator.preview(config, expected_revision=status.effective_revision,
                                          expected_epoch=status.transition_epoch)
            return coordinator.apply(preview.preview_id, preview.config_digest,
                                     expected_revision=preview.expected_revision,
                                     expected_epoch=preview.expected_epoch, request_id=request)
        try:
            first = apply("first")
            assert coordinator.wait(first.operation_id).phase == TransitionPhase.READY_UNARMED
            reducer = FactReducer(writer, provisional_fence=interlock.provisional_fence)
            store = IntentStore(writer)
            buy = intent(quantity="2").model_copy(update={"scope_id": "paper-book"})
            sell = intent(quantity="1", side=Side.SELL, revision=2).model_copy(update={"scope_id": "paper-book"})
            for order in (buy, sell):
                store.prepare(order, Reservation(asset="USD", amount=Decimal("200")))
            reducer.apply_fill(fill_for(buy, quantity="1", trade="initial-buy"))
            reducer.terminal(buy.intent_id, "synthetic-cancel", complete_fills=True)
            reducer.apply_fill(fill_for(sell, quantity="1", trade="initial-sell"))
            second = apply("second")
            assert entered.wait(5)
            late = fill_for(buy, quantity="1", trade="late-buy")
            result = reducer.apply_fill(late)
            assert result.incident == "LATE_FILL_AFTER_RETIREMENT"
            release.set()
            failed = coordinator.wait(second.operation_id)
            assert failed.phase == TransitionPhase.FAILED_INITIALIZATION
            assert failed.error_code == "AUTHORITY_FENCED"
            status = coordinator.status("owner")
            assert status.effective_context == first.target_context
            assert not status.armed and status.admissions_fenced
            assert writer.call(lambda c: authority_fenced_tx(c, "owner"))
            assert not reducer.apply_fill(late).applied
            positions = writer.call(lambda c: [tuple(row) for row in c.execute(
                "SELECT context_id,quantity FROM ex_positions")])
            assert positions == [(buy.context.identity, "1")]
            with pytest.raises(DomainError):
                coordinator.arm("owner", expected_revision=1, expected_epoch=2, request_id="unsafe")
        finally:
            release.set()
            coordinator.close()
