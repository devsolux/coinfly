"""Immutable balanced journal; every asset balances independently."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from typing import Annotated
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator, model_validator

from coinfly.domain.context import ContextKey, DomainError, FrozenModel, Identifier
from coinfly.domain.decimal_math import ExactDecimal, financial_context
from coinfly.storage.writer import Writer

Asset = Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Z0-9][A-Z0-9_:./-]*$")]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_decimal(value: Decimal) -> str:
    """Equivalent source scales identify the same exact amount without float math."""
    with localcontext(financial_context(512)):
        return str(value.normalize()) if value else "0"


def finite_decimal(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("a finite Decimal is required")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or len(value.as_tuple().digits) > 100 or not -100 <= exponent <= 100:
        raise ValueError("decimal precision exceeds the accounting contract")
    return value


class JournalEntry(FrozenModel):
    account: Identifier
    asset: Asset
    amount: ExactDecimal

    _finite = field_validator("amount")(finite_decimal)


class JournalFrame(FrozenModel):
    context: ContextKey
    scope_id: Identifier
    event_kind: Identifier
    evidence_id: Identifier
    entries: tuple[JournalEntry, ...] = Field(min_length=2, max_length=1000)

    @model_validator(mode="after")
    def balanced(self) -> JournalFrame:
        # Accepted 100-digit coefficients with exponents -100..100 span 300
        # places; 256 digits can hide an imbalance during cancellation.
        with localcontext(financial_context(512)):
            totals: dict[str, Decimal] = {}
            for entry in self.entries:
                totals[entry.asset] = totals.get(entry.asset, Decimal(0)) + entry.amount
            if any(total != 0 for total in totals.values()):
                raise ValueError("journal must balance independently for every asset")
        return self

    @property
    def fingerprint(self) -> str:
        # Epoch is provenance; the same event cannot create money after a switch.
        payload = {
            "authority": self.context.authority_id,
            "environment": self.context.environment.value,
            "scope": self.scope_id,
            "event": self.event_kind,
            "evidence": self.evidence_id,
            "entries": [
                dict(account=entry.account, asset=entry.asset, amount=canonical_decimal(entry.amount))
                for entry in self.entries
            ],
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def post_tx(connection: sqlite3.Connection, frame: JournalFrame) -> str:
    row = connection.execute(
        "SELECT journal_id,fingerprint FROM ex_journals WHERE authority_id=? AND environment=? "
        "AND scope_id=? AND event_kind=? AND evidence_id=?",
        (
            frame.context.authority_id,
            frame.context.environment.value,
            frame.scope_id,
            frame.event_kind,
            frame.evidence_id,
        ),
    ).fetchone()
    if row is not None:
        if row["fingerprint"] != frame.fingerprint:
            raise DomainError("JOURNAL_EVIDENCE_CONFLICT")
        return str(row["journal_id"])
    journal_id = str(uuid4())
    connection.execute(
        "INSERT INTO ex_journals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            journal_id,
            frame.context.authority_id,
            frame.context.environment.value,
            frame.scope_id,
            frame.context.identity,
            frame.context.model_dump_json(),
            frame.event_kind,
            frame.evidence_id,
            frame.fingerprint,
            utc_now(),
        ),
    )
    connection.executemany(
        "INSERT INTO ex_entries VALUES(?,?,?,?,?)",
        [
            (journal_id, index, entry.account, entry.asset, str(entry.amount))
            for index, entry in enumerate(frame.entries)
        ],
    )
    return journal_id


class Journal:
    def __init__(self, writer: Writer) -> None:
        self.writer = writer

    def post(self, frame: JournalFrame) -> str:
        return self.writer.call(lambda connection: post_tx(connection, frame))

    def balance(self, context: ContextKey, scope_id: str, account: str, asset: str) -> Decimal:
        def query(connection: sqlite3.Connection) -> Decimal:
            rows = connection.execute(
                "SELECT e.amount FROM ex_entries e JOIN ex_journals j USING(journal_id) "
                "WHERE j.authority_id=? AND j.environment=? AND j.scope_id=? AND e.account=? AND e.asset=?",
                (context.authority_id, context.environment.value, scope_id, account, asset),
            ).fetchall()
            with localcontext(financial_context(512)):
                return sum((Decimal(row[0]) for row in rows), Decimal(0))

        return self.writer.call(query)

    def verify(self) -> None:
        def check(connection: sqlite3.Connection) -> None:
            totals: dict[tuple[str, str], Decimal] = {}
            with localcontext(financial_context(512)):
                for row in connection.execute("SELECT journal_id,asset,amount FROM ex_entries"):
                    key = (str(row[0]), str(row[1]))
                    totals[key] = totals.get(key, Decimal(0)) + Decimal(row[2])
                if any(totals.values()):
                    raise DomainError("JOURNAL_UNBALANCED")

        self.writer.call(check)
