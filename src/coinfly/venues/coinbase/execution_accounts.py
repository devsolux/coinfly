"""Authoritative pagination for one explicitly delegated execution portfolio.

Account identity is retained before currency aggregation. A missing cursor,
repeated page, unknown portfolio, or malformed value is never completeness.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Mapping, Protocol

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import financial_context
from coinfly.venues.coinbase.read_only_accounts import _amount


class AccountsTransport(Protocol):
    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ExecutionAccounts:
    # Do not publish account identifiers in UI events or generic diagnostics.
    accounts: tuple[tuple[str, str, Decimal, Decimal], ...]
    balances: tuple[tuple[str, Decimal, Decimal], ...]
    pages: int


def collect_accounts(transport: AccountsTransport, portfolio_id: str, *, page_limit: int = 250,
                     maximum_pages: int = 40) -> ExecutionAccounts:
    if not portfolio_id or not 1 <= page_limit <= 250 or not 1 <= maximum_pages <= 100:
        raise DomainError("ACCOUNT_SCOPE_OR_PAGE_SIZE_INVALID")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    ids: set[str] = set()
    accounts: list[tuple[str, str, Decimal, Decimal]] = []
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    with localcontext(financial_context(256)):
        for index in range(maximum_pages):
            query = {"limit": str(page_limit)}
            if cursor:
                query["cursor"] = cursor
            page = transport.get("/accounts", query=query)
            rows, has_next = page.get("accounts"), page.get("has_next")
            if not isinstance(rows, list) or len(rows) > page_limit or type(has_next) is not bool:
                raise DomainError("ACCOUNT_PAGE_INVALID")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise DomainError("ACCOUNT_ROW_INVALID")
                identity, scope = row.get("uuid"), row.get("retail_portfolio_id")
                if not isinstance(identity, str) or not identity or identity in ids:
                    raise DomainError("ACCOUNT_ID_INVALID_OR_DUPLICATE")
                ids.add(identity)
                if not isinstance(scope, str) or not scope:
                    raise DomainError("ACCOUNT_SCOPE_UNVERIFIED")
                if scope != portfolio_id:
                    continue  # Explicitly outside the delegated book, never pooled.
                currency = row.get("currency")
                if not isinstance(currency, str) or not currency:
                    raise DomainError("ACCOUNT_BALANCE_INVALID")
                if type(row.get("active")) is not bool or type(row.get("ready")) is not bool:
                    raise DomainError("ACCOUNT_STATUS_UNVERIFIED")
                available = _amount(row.get("available_balance"), currency)
                held = _amount(row.get("hold"), currency)
                if not row["active"] or not row["ready"]:
                    held += available
                    available = Decimal(0)
                accounts.append((identity, currency, available, held))
                previous = totals.get(currency, (Decimal(0), Decimal(0)))
                totals[currency] = (previous[0] + available, previous[1] + held)
            if not has_next:
                return ExecutionAccounts(tuple(accounts), tuple((asset, *values) for asset, values in sorted(totals.items())), index + 1)
            next_cursor = page.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096 or next_cursor in seen_cursors:
                raise DomainError("ACCOUNT_PAGINATION_INCOMPLETE")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    raise DomainError("ACCOUNT_PAGES_EXHAUSTED")
