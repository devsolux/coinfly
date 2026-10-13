"""Exact financial observations, independently identified from financial events."""

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from coinfly.domain.context import Environment, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal


class AccountKind(StrEnum):
    WALLET = "WALLET"
    FIAT = "FIAT"
    VAULT = "VAULT"
    MARGIN = "MARGIN"
    SUMMARY = "SUMMARY"


class BalanceSemantics(StrEnum):
    SETTLED = "SETTLED"
    EQUITY = "EQUITY"


class AccountObservation(FrozenModel):
    observation_id: Identifier
    source_id: Identifier
    scope_id: Identifier
    authority_id: Identifier
    environment: Environment
    account_id: Identifier
    portfolio_id: Identifier | None = None
    currency: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    quantity: ExactDecimal = Field(allow_inf_nan=False, max_digits=38, decimal_places=18)
    as_of: datetime
    sequence: int = Field(ge=0)
    kind: AccountKind = AccountKind.WALLET
    semantics: BalanceSemantics = BalanceSemantics.SETTLED
    unsettled_perp_upl: ExactDecimal = Field(default=Decimal(0), allow_inf_nan=False, max_digits=38, decimal_places=18)
    liabilities: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    order_holds: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    initial_margin: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    custody_restrictions: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    pending_transfers: ExactDecimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    spot_unrealized_pnl: ExactDecimal = Field(default=Decimal(0), allow_inf_nan=False, max_digits=38, decimal_places=18)
    native_available: ExactDecimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    constraints_complete: bool = False
    holds_overlap_initial_margin: bool | None = None

    @field_validator("as_of")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware observation time required")
        return value.astimezone(timezone.utc)

    @property
    @exact_decimal
    def settled_quantity(self) -> Decimal:
        if self.semantics == BalanceSemantics.EQUITY:
            return self.quantity - self.unsettled_perp_upl
        return self.quantity


class AccountPage(FrozenModel):
    observations: tuple[AccountObservation, ...] = Field(max_length=10000)
    next_cursor: str | None = Field(default=None, max_length=4096)
    complete: bool = True

    @model_validator(mode="after")
    def pagination_contract(self) -> "AccountPage":
        if self.complete and self.next_cursor is not None:
            raise ValueError("complete page must have no next cursor")
        if self.next_cursor == "":
            raise ValueError("empty cursor is not a pagination token")
        return self


class PriceMark(FrozenModel):
    currency: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    reporting_currency: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    as_of: datetime
    source_id: Identifier
    validated: bool = False

    @field_validator("as_of")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware mark time required")
        return value.astimezone(timezone.utc)
