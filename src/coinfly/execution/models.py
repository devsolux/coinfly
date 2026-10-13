"""Immutable execution DTOs with no raw credentials or arbitrary payload fields."""

from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

from pydantic import Field, field_validator

from coinfly.accounting.journal import Asset, canonical_decimal, finite_decimal
from coinfly.domain.context import ContextKey, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IntentKind(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    PROTECTION = "PROTECTION"


class Submission(StrEnum):
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"


class DispatchState(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    DISPATCH_STARTED = "DISPATCH_STARTED"


class Resolution(StrEnum):
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"
    RESOLVED = "RESOLVED"


class OrderIntent(FrozenModel):
    intent_id: Identifier = Field(default_factory=lambda: str(uuid4()))
    context: ContextKey
    scope_id: Identifier
    profile_version: int = Field(ge=1)
    signer_version: int = Field(ge=0)
    slot_id: Identifier
    allocation_revision: int = Field(ge=0)
    instrument: Identifier
    base_asset: Asset
    quote_asset: Asset
    side: Side
    kind: IntentKind = IntentKind.ENTRY
    quantity: ExactDecimal = Field(gt=0, max_digits=38, decimal_places=18, allow_inf_nan=False)
    limit_price: ExactDecimal = Field(gt=0, max_digits=38, decimal_places=18, allow_inf_nan=False)
    contract_multiplier: ExactDecimal = Field(
        default=Decimal(1), gt=0, max_digits=20, decimal_places=18, allow_inf_nan=False
    )
    child_index: int = Field(default=0, ge=0)

    @property
    def payload_hash(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def semantic_key(self) -> str:
        values = (
            self.context.authority_id,
            self.context.environment.value,
            self.context.product_track.value,
            self.scope_id,
            self.slot_id,
            self.allocation_revision,
            self.instrument,
            self.child_index,
            self.kind.value,
        )
        return sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


class Reservation(FrozenModel):
    asset: Asset
    amount: ExactDecimal = Field(gt=0, max_digits=76, allow_inf_nan=False)

    _finite = field_validator("amount")(finite_decimal)


class IntentRecord(FrozenModel):
    intent: OrderIntent
    submission: Submission
    dispatch: DispatchState
    resolution: Resolution
    venue_order_id: str | None
    filled_quantity: ExactDecimal
    terminal: bool
    reservation_remaining: ExactDecimal


class Fill(FrozenModel):
    context: ContextKey
    scope_id: Identifier
    profile_version: int = Field(default=1, ge=1)
    venue_id: Identifier
    account_id: Identifier
    instrument: Identifier
    trade_id: Identifier
    venue_order_id: Identifier
    evidence_id: Identifier
    intent_id: Identifier | None = None
    base_asset: Asset
    quote_asset: Asset
    side: Side
    quantity: ExactDecimal = Field(gt=0, max_digits=38, decimal_places=18, allow_inf_nan=False)
    price: ExactDecimal = Field(gt=0, max_digits=38, decimal_places=18, allow_inf_nan=False)
    fee: ExactDecimal = Field(default=Decimal(0), ge=0, max_digits=38, decimal_places=18, allow_inf_nan=False)
    fee_asset: Asset
    contract_multiplier: ExactDecimal = Field(
        default=Decimal(1), gt=0, max_digits=20, decimal_places=18, allow_inf_nan=False
    )

    @property
    def namespace(self) -> tuple[str, ...]:
        return (
            self.context.authority_id,
            self.context.environment.value,
            self.venue_id,
            self.account_id,
            self.instrument,
            self.trade_id,
        )

    @property
    def fill_id(self) -> str:
        return sha256(json.dumps(self.namespace, separators=(",", ":")).encode()).hexdigest()

    @property
    def economic_hash(self) -> str:
        # Context epoch/API observation/evidence IDs are not venue trade identity.
        values = (
            self.namespace,
            self.venue_order_id,
            self.base_asset,
            self.quote_asset,
            self.side.value,
            canonical_decimal(self.quantity),
            canonical_decimal(self.price),
            canonical_decimal(self.fee),
            self.fee_asset,
            canonical_decimal(self.contract_multiplier),
        )
        return sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
