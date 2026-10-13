"""Live arming: an explicit, bounded mandate plus every external prerequisite, named.

Nothing here opens live trading. It records what a real-money qualification run
would need and reports, for the current configuration, which artifacts are
present and which are still external. The list is ordered the way the arming
workflow consumes it; the first unsatisfied item is the next thing to obtain.
There is no override, no test switch and no way to mark an item satisfied from
the desktop: each one is derived from evidence the worker holds.

A ``LiveMandate`` is the operator's explicit authorization for one authority,
one environment/product, one portfolio, with hard limits that must be at most
the engine configuration's own limits, an expiry, and the literal statement the
operator typed. It is persisted immutably with its digest and referenced by
``automation.enable`` for LIVE contexts (``LIVE_MANDATE_REQUIRED`` otherwise).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, FrozenModel, Identifier, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal
from coinfly.domain.products import ProductSubtype
from coinfly.security.release_gates import live_release_gates
from coinfly.storage.writer import Writer
from coinfly.venues.capability import (
    AccountEvidence,
    CapabilityRequest,
    Category,
    LocalQualifications,
    resolve,
)
from coinfly.venues.coinbase.endpoint_profiles import ENDPOINT_PROFILES

MANDATE_STATEMENT = "I authorize live orders for this authority within these limits"
# A qualification mandate is the one legitimate way out of the circularity
# "arming needs a recorded qualification run, the run needs arming": it arms
# a deliberately tiny, single-instrument, short-lived live context whose only
# purpose is to produce that record. It is never a trading mandate.
QUALIFICATION_STATEMENT = "I authorize a bounded live qualification run for this authority: one instrument, tiny notional, short expiry"
QUALIFICATION_MAX_NOTIONAL = Decimal("25")
QUALIFICATION_MAX_HOURS = 24
MANDATE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cf_live_mandates (mandate_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, digest TEXT NOT NULL, "
    "mandate_json TEXT NOT NULL, recorded_at TEXT NOT NULL, revoked_at TEXT)",
    "CREATE TABLE IF NOT EXISTS cf_live_context_mandates (context_id TEXT PRIMARY KEY, "
    "mandate_id TEXT NOT NULL REFERENCES cf_live_mandates(mandate_id), config_digest TEXT NOT NULL)",
)


class LiveMandate(FrozenModel):
    mandate_id: Identifier = Field(default_factory=lambda: str(uuid4()))
    authority_id: Identifier
    approver: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=400)
    kind: Literal["TRADING", "QUALIFICATION"] = "TRADING"
    environment: Environment = Environment.LIVE
    product_track: ProductTrack
    product_subtype: ProductSubtype
    portfolio_id: Identifier
    allowed_instruments: tuple[Identifier, ...] = Field(min_length=1, max_length=200)
    max_order_notional: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    max_gross_exposure: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    max_daily_loss: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    approved_at: datetime
    expires_at: datetime

    @property
    def expected_statement(self) -> str:
        return QUALIFICATION_STATEMENT if self.kind == "QUALIFICATION" else MANDATE_STATEMENT

    @model_validator(mode="after")
    def bounded_qualification(self) -> LiveMandate:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.approved_at, self.expires_at)):
            raise ValueError("aware mandate timestamps required")
        if self.expires_at <= self.approved_at:
            raise ValueError("positive mandate lifetime required")
        if len(set(self.allowed_instruments)) != len(self.allowed_instruments):
            raise ValueError("unique mandate instruments required")
        if self.kind == "QUALIFICATION":
            if len(self.allowed_instruments) != 1:
                raise ValueError("a qualification mandate names exactly one instrument")
            if self.max_order_notional > QUALIFICATION_MAX_NOTIONAL or self.max_gross_exposure > QUALIFICATION_MAX_NOTIONAL:
                raise ValueError("a qualification mandate stays within the tiny notional bound")
            if self.expires_at - self.approved_at > timedelta(hours=QUALIFICATION_MAX_HOURS):
                raise ValueError("a qualification mandate expires within a day")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json(exclude={"mandate_id"}).encode()).hexdigest()

    def check(self, config: EngineConfig, *, now: datetime) -> tuple[str, ...]:
        """Codes explaining why this mandate does not cover ``config`` now (empty = covers)."""
        codes: list[str] = []
        if self.environment is not Environment.LIVE or config.environment is not Environment.LIVE:
            codes.append("LIVE_MANDATE_ENVIRONMENT_MISMATCH")
        if self.authority_id != config.authority_id:
            codes.append("LIVE_MANDATE_AUTHORITY_MISMATCH")
        if self.product_track is not config.product_track or self.product_subtype is not config.effective_subtype:
            codes.append("LIVE_MANDATE_PRODUCT_MISMATCH")
        if tuple(config.execution_scope) != (self.portfolio_id,):
            codes.append("LIVE_MANDATE_PORTFOLIO_MISMATCH")
        if self.statement.strip() != self.expected_statement:
            codes.append("LIVE_MANDATE_STATEMENT_MISMATCH")
        if not self.approved_at <= now < self.expires_at:
            codes.append("LIVE_MANDATE_EXPIRED")
        limits = config.limits
        if (self.max_order_notional > limits.max_order_notional or self.max_daily_loss > limits.max_daily_loss
                or self.max_gross_exposure > limits.max_gross_exposure):
            codes.append("LIVE_MANDATE_EXCEEDS_CONFIGURED_LIMITS")
        if self.kind == "QUALIFICATION" and (limits.max_order_notional > self.max_order_notional
                                             or limits.max_gross_exposure > self.max_gross_exposure
                                             or limits.max_daily_loss > self.max_daily_loss):
            # The admission actor enforces the engine configuration's limits, so a
            # qualification run is bounded only when the configuration is no wider
            # than the mandate. With the check above this means the limits agree.
            codes.append("LIVE_MANDATE_QUALIFICATION_CONFIG_WIDER")
        return tuple(codes)


class LiveMandateStore:
    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self.writer.call(self._schema)

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        for statement in MANDATE_SCHEMA:
            connection.execute(statement)

    def record(self, mandate: LiveMandate, *, now: datetime) -> LiveMandate:
        if mandate.expires_at <= mandate.approved_at or mandate.expires_at <= now:
            raise DomainError("LIVE_MANDATE_EXPIRED")
        if mandate.statement.strip() != mandate.expected_statement:
            raise DomainError("LIVE_MANDATE_STATEMENT_MISMATCH")
        def save(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO cf_live_mandates VALUES(?,?,?,?,?,NULL)",
                               (mandate.mandate_id, mandate.authority_id, mandate.digest, mandate.model_dump_json(), now.isoformat()))
        try:
            self.writer.call(save)
        except sqlite3.IntegrityError:
            raise DomainError("LIVE_MANDATE_DUPLICATE") from None
        return mandate

    def revoke(self, mandate_id: str, *, now: datetime) -> bool:
        return bool(self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_live_mandates SET revoked_at=? WHERE mandate_id=? AND revoked_at IS NULL", (now.isoformat(), mandate_id)).rowcount))

    def active(self, authority_id: str, *, now: datetime) -> tuple[LiveMandate, ...]:
        rows = self.writer.call(lambda connection: connection.execute(
            "SELECT mandate_json, digest FROM cf_live_mandates WHERE authority_id=? AND revoked_at IS NULL ORDER BY recorded_at",
            (authority_id,)).fetchall())
        mandates = []
        for row in rows:
            mandate = LiveMandate.model_validate_json(row[0])
            if mandate.digest != row[1]:
                raise DomainError("LIVE_MANDATE_TAMPERED")
            if now < mandate.expires_at:
                mandates.append(mandate)
        return tuple(mandates)

    def bind_context(self, config: EngineConfig, context_id: str, *, now: datetime) -> LiveMandate:
        mandate = self.covering(config, now=now)
        if mandate is None:
            raise DomainError("LIVE_MANDATE_REQUIRED")
        def bind(connection: sqlite3.Connection) -> None:
            row = connection.execute("SELECT mandate_id,config_digest FROM cf_live_context_mandates WHERE context_id=?", (context_id,)).fetchone()
            if row is not None and tuple(row) != (mandate.mandate_id, config.digest):
                raise DomainError("LIVE_CONTEXT_MANDATE_CONFLICT")
            connection.execute("INSERT OR IGNORE INTO cf_live_context_mandates VALUES(?,?,?)", (context_id, mandate.mandate_id, config.digest))
        self.writer.call(bind)
        return mandate

    def covering(self, config: EngineConfig, *, now: datetime) -> LiveMandate | None:
        for mandate in self.active(config.authority_id, now=now):
            if not mandate.check(config, now=now):
                return mandate
        return None


def require_order_mandate_tx(connection: sqlite3.Connection, intent: Any, config: EngineConfig,
                             book: Any, *, now: datetime) -> None:
    """Enforce the SAME bound mandate at admission and actual network initiation.

    Revocation/expiry fence entries. The original mandate still bounds proven
    reductions of already-owned positions; it cannot authorize a reversal or a
    new instrument. Admission supplies the independent reduce-only proof.
    """
    from coinfly.domain.decimal_math import financial_context
    from coinfly.execution.models import IntentKind, OrderIntent
    from coinfly.risk.drawdown import DrawdownStore

    if not isinstance(intent, OrderIntent) or intent.context.environment is not Environment.LIVE:
        return
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='cf_live_context_mandates'").fetchone() is None:
        raise DomainError("LIVE_MANDATE_REQUIRED")
    row = connection.execute(
        "SELECT m.mandate_json,m.digest,m.revoked_at,b.config_digest FROM cf_live_context_mandates b "
        "JOIN cf_live_mandates m USING(mandate_id) WHERE b.context_id=?", (intent.context.identity,)
    ).fetchone()
    if row is None:
        raise DomainError("LIVE_MANDATE_REQUIRED")
    mandate = LiveMandate.model_validate_json(row[0])
    if mandate.digest != row[1]:
        raise DomainError("LIVE_MANDATE_TAMPERED")
    if row[3] != config.digest:
        raise DomainError("LIVE_MANDATE_CONFIG_CHANGED")
    increasing = intent.kind is IntentKind.ENTRY
    if increasing and row[2] is not None:
        raise DomainError("LIVE_MANDATE_REVOKED")
    codes = mandate.check(config, now=now if increasing else mandate.approved_at)
    if codes:
        raise DomainError(codes[0])
    if intent.instrument not in mandate.allowed_instruments:
        raise DomainError("LIVE_MANDATE_INSTRUMENT_NOT_PERMITTED")
    with localcontext(financial_context(256)):
        notional = intent.quantity * intent.limit_price * intent.contract_multiplier
        if notional > mandate.max_order_notional:
            raise DomainError("LIVE_MANDATE_ORDER_LIMIT")
        if not increasing:
            return
        marks = {mark.instrument: mark for mark in book.marks}
        gross = Decimal(0)
        for position in connection.execute("SELECT instrument,quantity FROM ex_positions WHERE context_id=? AND scope_id=?",
                                           (intent.context.identity, intent.scope_id)):
            quantity = Decimal(position[1])
            if not quantity:
                continue
            mark = marks.get(position[0])
            if mark is None:
                raise DomainError("POSITION_MARK_UNVERIFIED")
            gross += abs(quantity) * mark.price * mark.contract_multiplier
        for pending in connection.execute(
            "SELECT i.payload_json,o.filled_quantity FROM ex_intents i JOIN ex_outbox o USING(intent_id) "
            "WHERE i.context_id=? AND i.scope_id=? AND o.terminal=0 AND i.intent_id<>?",
            (intent.context.identity, intent.scope_id, intent.intent_id),
        ):
            older = OrderIntent.model_validate_json(pending[0])
            if older.kind is IntentKind.ENTRY:
                mark = marks.get(older.instrument)
                if mark is None:
                    raise DomainError("POSITION_MARK_UNVERIFIED")
                gross += max(Decimal(0), older.quantity - Decimal(pending[1])) * max(mark.price, older.limit_price) * older.contract_multiplier
        candidate_mark = marks.get(intent.instrument)
        if candidate_mark is None:
            raise DomainError("POSITION_MARK_UNVERIFIED")
        candidate_risk = intent.quantity * max(intent.limit_price, candidate_mark.price) * intent.contract_multiplier
        gross_ceiling = book.funded_equity * mandate.max_gross_exposure
        if mandate.kind == "QUALIFICATION":
            # max_gross_exposure is a ratio, NOT a currency amount. The
            # qualification additionally has an absolute aggregate ceiling.
            gross_ceiling = min(gross_ceiling, QUALIFICATION_MAX_NOTIONAL)
        if gross + candidate_risk > gross_ceiling:
            raise DomainError("LIVE_MANDATE_GROSS_LIMIT")
        for scope in (book.scope_id, "authority"):
            loss = DrawdownStore.read_tx(connection, intent.context, scope, book.reporting_currency)
            if loss is None:
                raise DomainError("LOSS_HISTORY_UNVERIFIED")
            if loss.day == now.date().isoformat() and loss.daily_net_result <= -mandate.max_daily_loss:
                raise DomainError("LIVE_MANDATE_LOSS_LIMIT")


class Prerequisite(FrozenModel):
    code: str
    satisfied: bool
    kind: Literal["external", "evidence", "local", "operator"]
    detail: str = Field(max_length=400)


def live_arming_prerequisites(config: EngineConfig, *, now: datetime, evidence: AccountEvidence | None,
                              local: LocalQualifications, credential_bound: bool, dataset_ready: tuple[bool, str],
                              kernel_ready: tuple[bool, str], mandate: LiveMandate | None,
                              qualification_record: str | None = None) -> tuple[Prerequisite, ...]:
    """Every artifact the live arming workflow needs, in the order it is consumed."""
    items: list[Prerequisite] = []
    gates = live_release_gates()
    for gate in gates:
        items.append(Prerequisite(code=gate.code, satisfied=gate.satisfied, kind="external",
                                  detail=f"Release trust anchor: {gate.evidence_required}"))
    items.append(Prerequisite(code="LIVE_ENDPOINT_PROFILE", satisfied=config.endpoint_profile_id in ENDPOINT_PROFILES
                              and ENDPOINT_PROFILES[config.endpoint_profile_id].environment is Environment.LIVE
                              and any(method == "POST" for method, _ in ENDPOINT_PROFILES[config.endpoint_profile_id].routes),
                              kind="local", detail="Configuration names the documented execution endpoint profile (coinbase-advanced-spot-execution)."))
    items.append(Prerequisite(code="EXECUTION_CREDENTIAL_BOUND", satisfied=credential_bound, kind="operator",
                              detail="An EXECUTION-purpose CDP API key is stored in the native keystore and bound to this authority/portfolio "
                                     "(entered in Settings, never in chat, source, or environment)."))
    if evidence is None:
        items.append(Prerequisite(code="ACCOUNT_EVIDENCE_MISSING", satisfied=False, kind="evidence",
                                  detail="Verified service entity, jurisdiction and enrollment evidence from the authenticated account channel."))
        items.append(Prerequisite(code="KEY_PERMISSIONS_UNVERIFIED", satisfied=False, kind="evidence",
                                  detail="GET /key_permissions must show can_trade=true, can_transfer=false and the mandated portfolio."))
    else:
        fresh = evidence.observed_at <= now < evidence.expires_at
        items.append(Prerequisite(code="ACCOUNT_EVIDENCE_FRESH", satisfied=fresh, kind="evidence",
                                  detail=f"Account evidence {evidence.evidence_id} observed {evidence.observed_at.isoformat()}."))
        permissions = evidence.permissions
        ok = permissions is not None and permissions.can_trade and not permissions.can_transfer
        items.append(Prerequisite(code="KEY_PERMISSIONS_VERIFIED", satisfied=ok, kind="evidence",
                                  detail="Trade permission present and transfer permission absent on the signing key."))
    request = CapabilityRequest(environment=config.environment, region_policy=config.region_policy, product_track=config.product_track,
                                product_subtype=config.effective_subtype, run_profile=config.run_profile)
    decision = resolve(request, evidence=evidence, now=now, local=local, gates=gates)
    items.append(Prerequisite(code=f"CAPABILITY_{decision.category.value}", satisfied=decision.category is Category.SUPPORTED, kind="evidence",
                              detail=(decision.explanation + " Codes: " + ", ".join(decision.all_blocking_codes))[:400]))
    items.append(Prerequisite(code="DATASET_READY", satisfied=dataset_ready[0], kind="local", detail=dataset_ready[1]))
    items.append(Prerequisite(code="NATIVE_KERNEL_VERIFIED", satisfied=kernel_ready[0], kind="local", detail=kernel_ready[1]))
    qualifying = mandate is not None and mandate.kind == "QUALIFICATION"
    items.append(Prerequisite(code="LIVE_ADAPTER_QUALIFICATION_RUN", satisfied=qualification_record is not None or qualifying, kind="external",
                              detail=(f"Recorded qualification run {qualification_record}" if qualification_record else
                                      f"Qualification mode under mandate {mandate.mandate_id}: this run produces the record." if qualifying and mandate else
                                      "A separately approved real-money qualification run (small, bounded, observed) with its recorded evidence; "
                                      "development work never performs it. A QUALIFICATION mandate is the only way to arm without the record.")))
    if mandate is None:
        items.append(Prerequisite(code="LIVE_MANDATE_REQUIRED", satisfied=False, kind="operator",
                                  detail="An explicit, unexpired live mandate for this authority, product, portfolio and limits."))
    else:
        codes = mandate.check(config, now=now)
        items.append(Prerequisite(code="LIVE_MANDATE_COVERS_CONFIGURATION" if not codes else codes[0], satisfied=not codes, kind="operator",
                                  detail=f"Mandate {mandate.mandate_id} by {mandate.approver}, expires {mandate.expires_at.isoformat()}."))
    return tuple(items)


def prerequisites_report(items: tuple[Prerequisite, ...]) -> dict[str, Any]:
    missing = [item for item in items if not item.satisfied]
    return {"ready": not missing, "items": [item.model_dump(mode="json") for item in items],
            "next_required": missing[0].code if missing else None,
            "external": [item.code for item in missing if item.kind == "external"],
            "evidence": [item.code for item in missing if item.kind == "evidence"],
            "operator": [item.code for item in missing if item.kind == "operator"],
            "local": [item.code for item in missing if item.kind == "local"],
            "generated_at": datetime.now(timezone.utc).isoformat()}


def mandate_from_params(params: Any, *, authority_id: str, now: datetime) -> LiveMandate:
    try:
        return LiveMandate(
            authority_id=authority_id, approver=str(params["approver"]), statement=str(params["statement"]),
            product_track=ProductTrack(str(params["product_track"])), product_subtype=ProductSubtype(str(params["product_subtype"])),
            portfolio_id=str(params["portfolio_id"]), allowed_instruments=tuple(str(item) for item in params["allowed_instruments"]),
            max_order_notional=Decimal(str(params["max_order_notional"])), max_gross_exposure=Decimal(str(params["max_gross_exposure"])),
            max_daily_loss=Decimal(str(params["max_daily_loss"])), approved_at=now,
            expires_at=datetime.fromisoformat(str(params["expires_at"])),
            kind="QUALIFICATION" if params.get("kind") == "QUALIFICATION" else "TRADING",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("LIVE_MANDATE_INVALID") from error
