"""Adversarial exact-accounting checks; all identities and amounts are synthetic."""

from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError

from coinfly.accounting.journal import JournalEntry, JournalFrame
from coinfly.domain.decimal_math import exact_decimal
from tests.execution.helpers import context


def test_journal_cannot_hide_small_imbalance_between_large_entries():
    large = Decimal("9" * 100 + "E+100")
    with pytest.raises(ValidationError, match="balance"):
        JournalFrame(
            context=context(), scope_id="synthetic-book", event_kind="TEST",
            evidence_id="synthetic-wide-exponents",
            entries=tuple(JournalEntry(account=f"test:{i}", asset="USD", amount=value)
                          for i, value in enumerate((large, Decimal("1E-100"), large.copy_negate()))),
        )


def test_decimal_contract_does_not_inherit_ambient_traps_or_exponent_limits():
    @exact_decimal
    def calculation():
        return Decimal("1E+20") * 10, Decimal(1) / 3

    reference = calculation()
    with localcontext() as hostile:
        hostile.prec = 2
        hostile.Emax = 9
        hostile.traps[Inexact] = True
        assert calculation() == reference
        assert hostile.prec == 2 and hostile.traps[Inexact]


@pytest.mark.parametrize("number", ["0.123456789012345678", "1.0", "1e-18", "NaN", "Infinity", "true"])
def test_decimal_json_rejects_binary_numbers_before_precision_is_lost(number):
    from coinfly.execution.models import Fill, OrderIntent, Reservation
    from coinfly.strategy.contracts import InstrumentWeight
    from tests.execution.helpers import fill_for, intent

    order = intent()
    models = [
        (order, "quantity"),
        (fill_for(order), "price"),
        (Reservation(asset="USD", amount=Decimal(1)), "amount"),
        (JournalEntry(account="assets:cash", asset="USD", amount=Decimal(1)), "amount"),
        (InstrumentWeight(instrument_id="BTC-USD", weight=Decimal(1)), "weight"),
    ]
    assert isinstance(models[0][0], OrderIntent) and isinstance(models[1][0], Fill)
    for model, field in models:
        payload = model.model_dump_json()
        old = f'"{field}":"{getattr(model, field)}"'
        assert old in payload
        with pytest.raises(ValidationError):
            type(model).model_validate_json(payload.replace(old, f'"{field}":{number}'))
        assert type(model).model_validate_json(payload) == model


def test_exact_decimal_json_strings_integers_and_nested_containers():
    from coinfly.domain.context import FrozenModel
    from coinfly.domain.decimal_math import ExactDecimal

    class Amounts(FrozenModel):
        optional: ExactDecimal | None
        balances: tuple[tuple[str, ExactDecimal], ...]
        by_asset: dict[str, ExactDecimal]

    model = Amounts.model_validate_json(
        '{"optional":null,"balances":[["USD","0.123456789012345678"]],'
        '"by_asset":{"BTC":12345678901234567890}}'
    )
    assert model.balances[0][1] == Decimal("0.123456789012345678")
    assert model.by_asset["BTC"] == Decimal("12345678901234567890")
    assert Amounts.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        Amounts.model_validate_json('{"optional":null,"balances":[],"by_asset":{"USD":0.1}}')


def test_writer_decimal_context_isolated_between_transactions(tmp_path):
    from decimal import getcontext

    from coinfly.storage.writer import Writer

    with Writer(tmp_path / "synthetic.sqlite") as writer:
        def poison(_connection):
            getcontext().prec = 2
            getcontext().Emax = 9
            getcontext().traps[Inexact] = True
        writer.call(poison)
        def exact(_connection):
            return Decimal("10000000000000000000") + Decimal("0.000000000000000001")
        assert writer.call(exact) == Decimal("10000000000000000000.000000000000000001")
        def fail(_connection):
            poison(_connection)
            raise ValueError("synthetic rollback")
        with pytest.raises(ValueError):
            writer.call(fail)
        assert writer.call(exact) == Decimal("10000000000000000000.000000000000000001")


def test_partial_fill_reservation_does_not_create_avoidable_rounding_dust(tmp_path):
    from coinfly.execution.fact_reducer import FactReducer
    from coinfly.execution.intent_store import IntentStore
    from coinfly.execution.models import Reservation
    from coinfly.storage.writer import Writer
    from tests.execution.helpers import fill_for, intent

    with Writer(tmp_path / "synthetic.sqlite") as writer:
        order = intent(quantity="3")
        store = IntentStore(writer)
        store.prepare(order, Reservation(asset="USD", amount=Decimal("0.3")))
        reducer = FactReducer(writer)
        reducer.apply_fill(fill_for(order, quantity="2", price="0.1", fee="0"))
        assert store.get(order.intent_id).reservation_remaining == Decimal("0.1")
        reducer.apply_fill(fill_for(order, trade="last", quantity="1", price="0.1", fee="0"))
        assert store.get(order.intent_id).reservation_remaining == 0


def test_many_microfills_late_fill_and_reopen_conserve_exact_cash(tmp_path):
    from coinfly.accounting.journal import Journal
    from coinfly.domain.decimal_math import financial_context
    from coinfly.execution.fact_reducer import FactReducer
    from coinfly.execution.intent_store import IntentStore
    from coinfly.execution.models import Reservation, Side
    from coinfly.execution.recovery import context_fact_sequence_tx, record_retirement_tx
    from coinfly.storage.writer import Writer
    from tests.execution.helpers import fill_for, intent

    path = tmp_path / "synthetic.sqlite"
    order = intent(quantity="0.000000000000000201")
    price = "12345678901234567890.123456789012345678"
    with Writer(path) as writer:
        store, reducer = IntentStore(writer), FactReducer(writer)
        store.prepare(order, Reservation(asset="USD", amount=Decimal("3000")))
        with localcontext() as ambient:
            ambient.prec = 2
            for index in range(200):
                reducer.apply_fill(fill_for(order, trade=f"micro-{index}", quantity="1e-18", price=price, fee="1e-18"))
        reducer.terminal(order.intent_id, "cancel-complete", complete_fills=True)
        closing = intent(revision=2, side=Side.SELL, quantity="0.000000000000000200")
        store.prepare(closing, Reservation(asset="BTC", amount=closing.quantity))
        reducer.apply_fill(fill_for(closing, quantity=str(closing.quantity), price=price, fee="0"))
        writer.call(lambda c: record_retirement_tx(c, order.context, "synthetic-stop", context_fact_sequence_tx(c, order.context)))
    late = fill_for(order, trade="late-micro", quantity="1e-18", price=price, fee="1e-18")
    with Writer(path) as writer:
        reducer = FactReducer(writer)
        assert reducer.apply_fill(late).incident == "LATE_FILL_AFTER_RETIREMENT"
        assert not reducer.apply_fill(late).applied
        journal = Journal(writer)
        with localcontext(financial_context(512)):
            expected = -Decimal("1e-18") * Decimal(price) - Decimal("201e-18")
        assert journal.balance(order.context, "book", "assets:cash", "USD") == expected
        assert journal.balance(order.context, "book", "assets:inventory", "BTC") == Decimal("1e-18")
        assert journal.balance(context(epoch=2), "different-book", "assets:cash", "USD") == 0
        journal.verify()


def test_risk_checks_cannot_round_away_small_limit_breaches():
    from coinfly.domain.context import DomainError
    from coinfly.risk.exposure import require_concentration
    from coinfly.risk.margin_models import MarginScenario

    with localcontext() as ambient:
        ambient.prec = 1
        with pytest.raises(DomainError, match="CONCENTRATION_LIMIT"):
            require_concentration({"a": Decimal("0.100000000000000001"), "b": Decimal("0.2")},
                                  {"a": "group", "b": "group"}, {"group": Decimal("0.3")})
        margin = MarginScenario(evidence_id="synthetic", eligible_equity=Decimal(1),
                                initial_margin=Decimal("0.111111111111111111"),
                                maintenance_margin=Decimal(0), incremental_reservations=Decimal(0),
                                initial_buffer=Decimal("0.89"), maintenance_buffer=Decimal(0))
        with pytest.raises(DomainError, match="INITIAL_MARGIN_HEADROOM"):
            margin.require_safe()
