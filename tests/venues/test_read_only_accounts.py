import unittest
from datetime import datetime, timezone
from decimal import Decimal

from coinfly.accounts.inventory import AccountKind
from coinfly.accounts.source_registry import SourceScope
from coinfly.domain.context import DomainError, Environment
from coinfly.venues.coinbase.read_only_accounts import AdvancedTradeReadOnlyAccounts

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def row(account="wallet-1", **changes):
    result = dict(
        uuid=account,
        currency="USD",
        retail_portfolio_id="portfolio",
        type="FIAT",
        active=True,
        ready=True,
        available_balance={"currency": "USD", "value": "99.0000000000000001"},
        hold={"currency": "USD", "value": "1"},
    )
    result.update(changes)
    return result


def scope():
    return SourceScope(
        source_id="advanced",
        scope_id="observed-portfolio",
        authority_id="owner",
        environment=Environment.LIVE,
        portfolio_ids=("portfolio",),
        service_entity="Synthetic entity for test",
        source_api="advanced-trade",
        readable_kinds=(AccountKind.FIAT, AccountKind.WALLET),
        evidence_id="scope-evidence",
    )


class Transport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def read_accounts_page(self, *, cursor, limit):
        self.calls.append((cursor, limit))
        result = self.pages[cursor]
        if isinstance(result, Exception):
            raise result
        return result


class AccountReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_page_is_explicit_and_balance_decimal_preserved(self):
        transport = Transport(
            {
                None: {"accounts": [row()], "has_next": True, "cursor": "page-2"},
                "page-2": {"accounts": [row("wallet-2")], "has_next": False},
            }
        )
        adapter = AdvancedTradeReadOnlyAccounts(transport, scope(), clock=lambda: NOW)
        first = await adapter.read_page(None)
        self.assertFalse(first.complete)
        self.assertEqual(first.next_cursor, "page-2")
        second = await adapter.read_page(first.next_cursor)
        self.assertTrue(second.complete)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(transport.calls, [(None, 250), ("page-2", 250)])
        self.assertEqual(first.observations[0].quantity, Decimal("100.0000000000000001"))
        self.assertEqual(first.observations[0].order_holds, Decimal("1"))
        self.assertFalse(scope().discovery_complete)
        self.assertFalse(hasattr(adapter, "create_order"))
        self.assertFalse(hasattr(adapter, "transfer"))

    async def test_pagination_failure_is_never_complete_or_zero(self):
        for bad in (
            {"accounts": [], "has_next": True},
            {"accounts": [], "has_next": True, "cursor": "same"},
            {"accounts": [], "has_next": "false"},
        ):
            adapter = AdvancedTradeReadOnlyAccounts(Transport({"same": bad}), scope())
            with self.assertRaises(DomainError):
                await adapter.read_page("same")

    async def test_wrong_owner_scope_and_float_balances_are_not_observations(self):
        for bad in (
            row(retail_portfolio_id="other"),
            row(available_balance={"currency": "USD", "value": 99.0}),
            row(available_balance={"currency": "USD", "value": "NaN"}),
            row(hold={"currency": "EUR", "value": "1"}),
            row(type="VAULT"),
        ):
            adapter = AdvancedTradeReadOnlyAccounts(
                Transport({None: {"accounts": [bad], "has_next": False}}), scope()
            )
            with self.assertRaises(DomainError):
                await adapter.read_page(None)

    async def test_duplicate_account_and_partial_timeout_do_not_invent_completion(self):
        transport = Transport({None: {"accounts": [row(), row()], "has_next": False}})
        with self.assertRaisesRegex(DomainError, "DUPLICATE"):
            await AdvancedTradeReadOnlyAccounts(transport, scope()).read_page(None)
        transport = Transport({None: TimeoutError("synthetic read failure")})
        with self.assertRaises(TimeoutError):
            await AdvancedTradeReadOnlyAccounts(transport, scope()).read_page(None)


if __name__ == "__main__":
    unittest.main()
