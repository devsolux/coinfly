"""Finalize only fully reconciled round trips of the built-in USD spot simulator.

This local matcher charges its explicit quote fee at each fill and models no
funding or deferred charges. Its complete persisted order/fill inventory is the
evidence domain; none of those assumptions extends to a live broker or futures.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from hashlib import sha256
from typing import Any

from coinfly.accounting.journal import canonical_decimal
from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.evidence import authority_fenced_tx, event_tx, fence_authority_tx
from coinfly.execution.intent_store import get_tx
from coinfly.execution.models import Fill, IntentKind, Resolution, Side, Submission
from coinfly.simulation.paper_matching import PaperFill, PaperInstrument, PaperMatcher, PaperOrder

_PROOF_ISSUER = object()
_MODEL_CONTRACT = "coinfly-local-usd-spot-v1"


@dataclass(frozen=True)
class PaperFinalizationProof:
    context: ContextKey
    scope_id: str
    state_json: str
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class PaperFinalizationResult:
    episode_ids: tuple[str, ...] = ()
    pending_instruments: tuple[str, ...] = ()
    incident_code: str | None = None
    pending_reason: str | None = None


@dataclass(frozen=True)
class _Fact:
    fill: Fill
    before: Decimal
    after: Decimal
    matched_pnl: Decimal
    closed_at: str


def _financial_state(state: dict[str, Any]) -> dict[str, Any]:
    # Quote books are deliberately discarded on restart; financial facts are not.
    return {key: value for key, value in state.items() if key != "books"}


class PaperEpisodeFinalizer:
    @staticmethod
    def capture(matcher: PaperMatcher, scope_id: str) -> PaperFinalizationProof:
        """Capture under the engine lock, then finalize in its persistence commit."""
        if type(matcher) is not PaperMatcher:
            raise DomainError("PAPER_EPISODE_MODEL_UNVERIFIED")
        if (
            matcher.context.environment != Environment.SANDBOX
            or matcher.context.product_track != ProductTrack.SPOT
        ):
            raise DomainError("PAPER_EPISODES_REQUIRE_SANDBOX_SPOT")
        if not scope_id or len(scope_id) > 160:
            raise DomainError("PAPER_EPISODE_SCOPE_INVALID")
        return PaperFinalizationProof(
            matcher.context,
            scope_id,
            json.dumps(matcher.dump_state(), sort_keys=True, separators=(",", ":")),
            _PROOF_ISSUER,
        )

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ex_paper_episode_proofs (episode_id TEXT PRIMARY KEY,context_id TEXT NOT NULL,"
            "scope_id TEXT NOT NULL,instrument TEXT NOT NULL,currency TEXT NOT NULL,financial_hash TEXT NOT NULL,"
            "fill_ids_json TEXT NOT NULL,intent_ids_json TEXT NOT NULL,closed_fill_id TEXT NOT NULL,"
            "snapshot_digest TEXT NOT NULL,model_contract TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS ex_paper_episode_proofs_no_update BEFORE UPDATE ON ex_paper_episode_proofs BEGIN SELECT RAISE(ABORT,'immutable paper episode proof'); END"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS ex_paper_episode_proofs_no_delete BEFORE DELETE ON ex_paper_episode_proofs BEGIN SELECT RAISE(ABORT,'immutable paper episode proof'); END"
        )

    @staticmethod
    def _incident(
        connection: sqlite3.Connection, proof: PaperFinalizationProof, code: str
    ) -> PaperFinalizationResult:
        evidence_id = sha256((proof.state_json + code).encode()).hexdigest()
        fence_authority_tx(connection, proof.context, code, evidence_id)
        event_tx(
            connection,
            proof.context,
            proof.scope_id,
            "PAPER_EPISODE_INCIDENT",
            proof.context.identity,
            evidence_id,
        )
        return PaperFinalizationResult(incident_code=code)

    @classmethod
    def finalize_tx(
        cls, connection: sqlite3.Connection, proof: PaperFinalizationProof
    ) -> PaperFinalizationResult:
        """Call after local fills, terminal evidence and matcher state are persisted.

        A conflict commits a durable authority fence/alarm and returns an incident;
        the caller must not roll that transaction back merely because of the code.
        Pending episodes produce no financial result. No network I/O is performed.
        """
        if proof._issuer is not _PROOF_ISSUER:
            raise DomainError("PAPER_EPISODE_PROOF_UNVERIFIED")
        if (
            proof.context.environment != Environment.SANDBOX
            or proof.context.product_track != ProductTrack.SPOT
        ):
            raise DomainError("PAPER_EPISODES_REQUIRE_SANDBOX_SPOT")
        cls._schema(connection)
        state = json.loads(proof.state_json)
        instruments = tuple(
            PaperInstrument.model_validate_json(json.dumps(item)) for item in state["instruments"]
        )
        contracts = {item.instrument_id: item for item in instruments}
        if len(contracts) != len(instruments) or any(
            item.product_track != ProductTrack.SPOT or item.contract_multiplier != 1 for item in instruments
        ):
            return cls._incident(connection, proof, "PAPER_EPISODE_INSTRUMENT_CONTRACT_MISMATCH")
        currencies = {item.quote_currency for item in instruments}
        if currencies != {"USD"}:
            return PaperFinalizationResult(pending_reason="PAPER_REPORTING_CURRENCY_UNSUPPORTED")
        stored = connection.execute(
            "SELECT context_json,state_json FROM wp_paper_books WHERE authority_id=? AND environment='SANDBOX' AND scope_id=? AND product_track='SPOT'",
            (proof.context.authority_id, proof.scope_id),
        ).fetchone()
        if stored is None or ContextKey.model_validate_json(stored["context_json"]) != proof.context:
            return cls._incident(connection, proof, "PAPER_EPISODE_STATE_UNPERSISTED")
        if _financial_state(json.loads(stored["state_json"])) != _financial_state(state):
            return cls._incident(connection, proof, "PAPER_EPISODE_STATE_MISMATCH")
        orders = tuple(PaperOrder.model_validate_json(json.dumps(value)) for value in state["orders"])
        local_fills = tuple(PaperFill.model_validate_json(json.dumps(value)) for value in state["fills"])
        prefix = proof.context.identity[:20] + "-"
        order_by_intent = {order.client_order_id: order for order in orders}
        source_fills = {prefix + fill.trade_id: fill for fill in local_fills}
        if len(order_by_intent) != len(orders) or len(source_fills) != len(local_fills):
            return cls._incident(connection, proof, "PAPER_EPISODE_DUPLICATE_SOURCE")
        rows = connection.execute(
            "SELECT f.fill_id,f.payload_json,l.quantity_before,l.quantity_after,l.realized_pnl,e.sequence "
            "FROM ex_fills f LEFT JOIN ex_lot_movements l USING(fill_id) LEFT JOIN ex_events e "
            "ON e.event_kind='FILL_APPLIED' AND e.entity_id=f.intent_id AND e.evidence_id=json_extract(f.payload_json,'$.evidence_id') "
            "WHERE f.context_id=? AND f.scope_id=? ORDER BY e.sequence,f.fill_id",
            (proof.context.identity, proof.scope_id),
        ).fetchall()
        if len(rows) != len(local_fills) or len({row["fill_id"] for row in rows}) != len(rows):
            return cls._incident(connection, proof, "PAPER_EPISODE_FACT_COVERAGE_MISMATCH")
        groups: dict[str, list[_Fact]] = {}
        for row in rows:
            fill = Fill.model_validate_json(row["payload_json"])
            local_fill = source_fills.get(fill.trade_id)
            order = order_by_intent.get(fill.intent_id or "")
            if (
                local_fill is None
                or order is None
                or row["quantity_before"] is None
                or row["sequence"] is None
            ):
                return cls._incident(connection, proof, "PAPER_EPISODE_UNATTRIBUTED_FACT")
            if (
                fill.context != proof.context
                or fill.scope_id != proof.scope_id
                or fill.venue_id != "coinfly-paper"
                or fill.account_id != proof.scope_id
                or fill.venue_order_id != prefix + local_fill.order_id
                or fill.intent_id != local_fill.client_order_id
                or order.order_id != local_fill.order_id
                or fill.instrument != local_fill.instrument_id
                or fill.side.value != local_fill.side.value
                or fill.quantity != local_fill.quantity
                or fill.price != local_fill.price
                or fill.fee != local_fill.fee
                or fill.fee_asset != local_fill.fee_currency
                or fill.quote_asset != "USD"
                or fill.fee_asset != "USD"
                or fill.contract_multiplier != 1
            ):
                return cls._incident(connection, proof, "PAPER_EPISODE_ECONOMICS_MISMATCH")
            record = get_tx(connection, fill.intent_id)
            if record.intent.context != proof.context or record.intent.scope_id != proof.scope_id:
                return cls._incident(connection, proof, "PAPER_EPISODE_INTENT_REALM_MISMATCH")
            contract = contracts.get(fill.instrument)
            if (
                contract is None
                or fill.base_asset != contract.base_currency
                or fill.quote_asset != contract.quote_currency
                or record.intent.base_asset != contract.base_currency
                or record.intent.quote_asset != contract.quote_currency
                or record.intent.contract_multiplier != contract.contract_multiplier
            ):
                return cls._incident(connection, proof, "PAPER_EPISODE_INSTRUMENT_CONTRACT_MISMATCH")
            groups.setdefault(fill.instrument, []).append(
                _Fact(
                    fill,
                    Decimal(row["quantity_before"]),
                    Decimal(row["quantity_after"]),
                    Decimal(row["realized_pnl"]),
                    local_fill.as_of.isoformat(timespec="microseconds"),
                )
            )
        pending = {order.instrument_id for order in orders if not order.terminal}
        records = connection.execute(
            "SELECT i.intent_id FROM ex_intents i WHERE i.context_id=? AND i.scope_id=?",
            (proof.context.identity, proof.scope_id),
        ).fetchall()
        if not set(order_by_intent).issubset({str(raw[0]) for raw in records}):
            return cls._incident(connection, proof, "PAPER_EPISODE_UNATTRIBUTED_ORDER")
        for raw in records:
            record = get_tx(connection, str(raw[0]))
            local_order = order_by_intent.get(record.intent.intent_id)
            contract = contracts.get(record.intent.instrument)
            if (
                contract is None
                or record.intent.base_asset != contract.base_currency
                or record.intent.quote_asset != contract.quote_currency
                or record.intent.contract_multiplier != contract.contract_multiplier
            ):
                return cls._incident(connection, proof, "PAPER_EPISODE_INSTRUMENT_CONTRACT_MISMATCH")
            if record.submission == Submission.ACKNOWLEDGED:
                if (
                    local_order is None
                    or record.venue_order_id != prefix + local_order.order_id
                    or record.filled_quantity != local_order.filled_quantity
                    or record.intent.instrument != local_order.instrument_id
                    or record.intent.quantity != local_order.quantity
                    or record.intent.limit_price != local_order.request.limit_price
                    or record.intent.side.value != local_order.side.value
                    or (record.intent.kind != IntentKind.ENTRY) != local_order.request.reduce_only
                ):
                    return cls._incident(connection, proof, "PAPER_EPISODE_ORDER_EVIDENCE_MISMATCH")
            elif local_order is not None:
                return cls._incident(connection, proof, "PAPER_EPISODE_ORDER_STATE_CONFLICT")
            if (
                record.resolution == Resolution.UNKNOWN
                or not record.terminal
                or record.reservation_remaining != 0
            ):
                pending.add(record.intent.instrument)
        if authority_fenced_tx(connection, proof.context.authority_id):
            return PaperFinalizationResult(
                pending_instruments=tuple(sorted(groups)), pending_reason="AUTHORITY_FENCED"
            )
        finalized: list[str] = []
        candidates: list[tuple[str, list[_Fact]]] = []
        for instrument, facts in groups.items():
            quantity = Decimal(0)
            episode: list[_Fact] = []
            retired_intents: set[str] = set()
            for fact in facts:
                with localcontext(financial_context()) as decimal_context:
                    decimal_context.prec = 256
                    expected_after = fact.before + (
                        fact.fill.quantity if fact.fill.side == Side.BUY else -fact.fill.quantity
                    )
                if (
                    fact.before != quantity
                    or fact.before < 0
                    or fact.after < 0
                    or fact.after != expected_after
                ):
                    return cls._incident(connection, proof, "PAPER_EPISODE_LOT_CHAIN_CONFLICT")
                if fact.fill.intent_id in retired_intents:
                    return cls._incident(connection, proof, "LATE_PAPER_EPISODE_FACT")
                episode.append(fact)
                quantity = fact.after
                if not quantity:
                    candidates.append((instrument, episode))
                    retired_intents.update(
                        item.fill.intent_id for item in episode if item.fill.intent_id is not None
                    )
                    episode = []
            if episode:
                pending.add(instrument)
        # Validate all prior immutable episode economics before inserting any new
        # result, so a late conflict cannot produce a partially certified report.
        prepared: list[tuple[str, str, list[_Fact], str, Decimal, Decimal]] = []
        for instrument, facts in candidates:
            episode_id = sha256(
                json.dumps(
                    (
                        _MODEL_CONTRACT,
                        proof.context.identity,
                        proof.scope_id,
                        instrument,
                        facts[0].fill.fill_id,
                    ),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            with localcontext(financial_context()) as decimal_context:
                decimal_context.prec = 256
                matched = sum((fact.matched_pnl for fact in facts), Decimal(0))
                fees = sum((fact.fill.fee for fact in facts), Decimal(0))
                # A complete flat-to-flat spot episode has independently known
                # price P&L from immutable cashflows. Do not certify a corrupted
                # mutable lot projection merely because its movements were saved.
                cashflow_pnl = sum(
                    (
                        fact.fill.quantity
                        * fact.fill.price
                        * (Decimal(1) if fact.fill.side == Side.SELL else Decimal(-1))
                        for fact in facts
                    ),
                    Decimal(0),
                )
            if matched != cashflow_pnl:
                return cls._incident(connection, proof, "PAPER_EPISODE_PNL_RECONCILIATION_MISMATCH")
            financial_hash = sha256(
                json.dumps(
                    [
                        (
                            fact.fill.fill_id,
                            fact.fill.economic_hash,
                            canonical_decimal(fact.before),
                            canonical_decimal(fact.after),
                            canonical_decimal(fact.matched_pnl),
                        )
                        for fact in facts
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            previous = connection.execute(
                "SELECT financial_hash FROM ex_paper_episode_proofs WHERE episode_id=?", (episode_id,)
            ).fetchone()
            if previous is not None:
                if previous[0] != financial_hash:
                    return cls._incident(connection, proof, "PAPER_CLOSED_RESULT_CONFLICT")
                result = connection.execute(
                    "SELECT * FROM ex_closed_results WHERE episode_id=?", (episode_id,)
                ).fetchone()
                if (
                    result is None
                    or result["authority_id"] != proof.context.authority_id
                    or result["environment"] != "SANDBOX"
                    or result["context_id"] != proof.context.identity
                    or result["scope_id"] != proof.scope_id
                    or result["instrument"] != instrument
                    or Decimal(result["matched_pnl"]) != matched
                    or Decimal(result["fees"]) != fees
                    or Decimal(result["funding"]) != 0
                    or Decimal(result["charges"]) != 0
                    or result["pending_charges"]
                ):
                    return cls._incident(connection, proof, "PAPER_CLOSED_RESULT_CONFLICT")
                continue
            if connection.execute(
                "SELECT 1 FROM ex_closed_results WHERE episode_id=?", (episode_id,)
            ).fetchone():
                return cls._incident(connection, proof, "PAPER_CLOSED_RESULT_PROOF_MISSING")
            if instrument not in pending:
                prepared.append((episode_id, instrument, facts, financial_hash, matched, fees))
        snapshot_digest = sha256(proof.state_json.encode()).hexdigest()
        for episode_id, instrument, facts, financial_hash, matched, fees in prepared:
            sequence = event_tx(
                connection, proof.context, proof.scope_id, "EPISODE_CLOSED", episode_id, financial_hash
            )
            connection.execute(
                "INSERT INTO ex_closed_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    proof.context.authority_id,
                    "SANDBOX",
                    proof.context.identity,
                    proof.scope_id,
                    episode_id,
                    instrument,
                    facts[-1].closed_at,
                    str(matched),
                    str(fees),
                    "0",
                    "0",
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO ex_paper_episode_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    episode_id,
                    proof.context.identity,
                    proof.scope_id,
                    instrument,
                    "USD",
                    financial_hash,
                    json.dumps([fact.fill.fill_id for fact in facts]),
                    json.dumps(sorted({fact.fill.intent_id for fact in facts})),
                    facts[-1].fill.fill_id,
                    snapshot_digest,
                    _MODEL_CONTRACT,
                ),
            )
            finalized.append(episode_id)
        return PaperFinalizationResult(
            tuple(finalized),
            tuple(sorted(pending)),
            pending_reason="PAPER_EPISODE_NOT_SETTLED" if pending else None,
        )
