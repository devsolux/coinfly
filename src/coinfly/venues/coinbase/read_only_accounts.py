"""Advanced Trade pagination normalized into explicit scoped observations.

This is a read adapter, not a wallet sweep or a proof of account-wide coverage.
The separate Track/vault and active perpetual APIs require their own certification.
"""

import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Mapping, Protocol

from coinfly.accounts.inventory import AccountKind, AccountObservation, AccountPage
from coinfly.accounts.source_registry import SourceScope
from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import financial_context


class AccountsReadTransport(Protocol):
    async def read_accounts_page(self, *, cursor: str | None, limit: int) -> Mapping[str, Any]: ...


def _amount(raw: Any, currency: str) -> Decimal:
    if not isinstance(raw, dict) or raw.get("currency") != currency or not isinstance(raw.get("value"), str):
        raise DomainError("ACCOUNT_BALANCE_INVALID")
    value = raw["value"]
    if len(value) > 100:
        raise DomainError("ACCOUNT_BALANCE_INVALID")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise DomainError("ACCOUNT_BALANCE_INVALID") from None
    if not amount.is_finite() or amount < 0:
        raise DomainError("ACCOUNT_BALANCE_INVALID")
    amount_parts = amount.as_tuple()
    if (
        len(amount_parts.digits) > 64
        or not isinstance(amount_parts.exponent, int)
        or not -36 <= amount_parts.exponent <= 36
    ):
        raise DomainError("ACCOUNT_BALANCE_INVALID")
    return amount


class AdvancedTradeReadOnlyAccounts:
    def __init__(
        self,
        transport: AccountsReadTransport,
        scope: SourceScope,
        *,
        page_size: int = 250,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not 1 <= page_size <= 250 or len(scope.portfolio_ids) != 1:
            raise DomainError("ACCOUNT_SCOPE_OR_PAGE_SIZE_INVALID")
        if not scope.supported or not scope.accessible:
            raise DomainError("ACCOUNT_SOURCE_UNAVAILABLE")
        if not set(scope.readable_kinds).issubset({AccountKind.WALLET, AccountKind.FIAT}):
            raise DomainError("ADVANCED_TRADE_KIND_UNCERTIFIED")
        self._transport = transport
        self._scope = scope
        self._page_size = page_size
        self._clock = clock
        self._sequence = 0

    async def read_page(self, cursor: str | None) -> AccountPage:
        if cursor is not None and (not cursor or len(cursor) > 4096):
            raise DomainError("ACCOUNT_CURSOR_INVALID")
        page = await self._transport.read_accounts_page(cursor=cursor, limit=self._page_size)
        rows, has_next = page.get("accounts"), page.get("has_next")
        if not isinstance(rows, list) or type(has_next) is not bool or len(rows) > self._page_size:
            raise DomainError("ACCOUNT_PAGE_INVALID")
        next_cursor = page.get("cursor") if has_next else None
        if has_next and (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > 4096
            or next_cursor == cursor
        ):
            raise DomainError("ACCOUNT_PAGINATION_INCOMPLETE")
        received_at = self._clock()
        self._sequence += 1
        page_sequence = self._sequence
        observed: list[AccountObservation] = []
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DomainError("ACCOUNT_ROW_INVALID")
            account_id, currency = row.get("uuid"), row.get("currency")
            portfolio = row.get("retail_portfolio_id")
            if not isinstance(account_id, str) or not account_id or account_id in ids:
                raise DomainError("ACCOUNT_ID_INVALID_OR_DUPLICATE")
            if not isinstance(currency, str) or portfolio != self._scope.portfolio_ids[0]:
                raise DomainError("ACCOUNT_SCOPE_MISMATCH")
            available, hold = (
                _amount(row.get("available_balance"), currency),
                _amount(row.get("hold"), currency),
            )
            account_type = row.get("type")
            kind = (
                {"FIAT": AccountKind.FIAT, "CRYPTO": AccountKind.WALLET}.get(account_type)
                if isinstance(account_type, str)
                else None
            )
            if kind is None or kind not in self._scope.readable_kinds:
                raise DomainError("ACCOUNT_KIND_UNCERTIFIED")
            if type(row.get("active")) is not bool or type(row.get("ready")) is not bool:
                raise DomainError("ACCOUNT_STATUS_UNVERIFIED")
            try:
                with localcontext(financial_context()) as decimal_context:
                    decimal_context.prec = 150
                    settled_quantity = available + hold
                observed.append(
                    AccountObservation(
                        observation_id=secrets.token_hex(16),
                        source_id=self._scope.source_id,
                        scope_id=self._scope.scope_id,
                        authority_id=self._scope.authority_id,
                        environment=self._scope.environment,
                        account_id=account_id,
                        portfolio_id=portfolio,
                        currency=currency,
                        quantity=settled_quantity,
                        as_of=received_at,
                        sequence=page_sequence,
                        kind=kind,
                        order_holds=hold,
                        native_available=available if row["active"] and row["ready"] else None,
                    )
                )
            except Exception:
                raise DomainError("ACCOUNT_ROW_INVALID") from None
            ids.add(account_id)
        return AccountPage(observations=tuple(observed), next_cursor=next_cursor, complete=not has_next)
