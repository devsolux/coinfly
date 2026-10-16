"""Reward windows for the research controller, built from the paper execution ledger.

The experimental controller learns from ``RewardWindow`` evidence: contiguous,
equal-duration accounting windows with booked net P&L, every execution's cost
(reference vs fill price, booked fee, latency) and the exposure credits that
name the originating admitted decisions. In the study code a replay
environment produces them; here the worker produces them from its own ledger:

* **Equity samples.** After every tick the worker records ``(sample_at,
  funded_equity)``. The equity *at* a window boundary ``E`` is the last sample
  at or before ``E`` — the paper book changes only inside ticks, so this is
  exact, not interpolated.
* **Windows.** ``[start, start + interval)`` from the session start; a window
  closes at the first sample past its end with ``pnl = equity(E) −
  capital`` and ``capital`` of the next window ``= equity(E)``, so windows are
  contiguous in time *and* in equity. A gap without ticks closes the skipped
  windows with zero P&L (nothing happened in the ledger); nothing is invented.
* **Executions.** Fills booked inside ``(start, end]``, with the decision-time
  reference price, the fill price, the booked fee and every timestamp of the
  decision → submission → fill chain (``ExecutionCost`` enforces causality).
* **Credits.** Exposure attribution to originating admitted decisions: the
  cost basis of every open lot (by the decision that opened it) plus the
  executed notional of the window's fills (by their decision), as fractions
  rounded *down* so they never exceed one. Protective drains have no decision
  and earn no credit.
* **Delivery.** Closed windows are handed to the controller strictly in order,
  one per observation, and marked ``FED`` with the decision that consumed
  them. A backlog is measured and reported — never dropped. A window fed later
  than the declared credit horizon is still fed and counted as
  ``horizon_exceeded``; the controller reports the expired credits itself.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any

from coinfly.domain.context import ContextKey, DomainError
from coinfly.domain.decimal_math import financial_context
from coinfly.execution.models import Fill
from coinfly.research.reward import DecisionCredit, ExecutionCost, RewardWindow, TakerFees
from coinfly.storage.component_schema import install_component_schema
from coinfly.storage.writer import Writer

REWARD_SCHEMA_COMPONENT = "reward_ledger"
REWARD_SCHEMA: tuple[str, ...] = (
    'CREATE TABLE IF NOT EXISTS cf_reward_windows (context_id TEXT NOT NULL, session_id TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, capital TEXT NOT NULL, equity_end TEXT, pnl TEXT, available_at TEXT, state TEXT NOT NULL, fed_decision_id TEXT, fed_at TEXT, delay_seconds REAL, horizon_exceeded INTEGER NOT NULL DEFAULT 0, credits_json TEXT, PRIMARY KEY(context_id, session_id, window_start))',
)
MAX_CREDITS = 1024
FRACTION_QUANTUM = Decimal("1e-18")


@dataclass(frozen=True)
class EquitySample:
    at: datetime
    funded_equity: Decimal


@dataclass(frozen=True)
class PendingWindow:
    window: RewardWindow
    delay_seconds: float
    horizon_exceeded: bool
    credits_truncated: int


class RewardLedger:
    """One research session's reward windows for one context (single slot, one causal core)."""

    def __init__(self, writer: Writer, *, context: ContextKey, session_id: str, interval_seconds: float,
                 currency: str, fee_rate: Decimal, credit_horizon_seconds: float, started_at: datetime) -> None:
        if interval_seconds <= 0 or credit_horizon_seconds <= 0:
            raise DomainError("REWARD_INTERVAL_INVALID")
        if started_at.tzinfo is None:
            raise DomainError("AWARE_TIMESTAMP_REQUIRED")
        self.writer = writer
        self.context = context
        self.session_id = session_id
        self.interval = timedelta(seconds=interval_seconds)
        self.interval_seconds = interval_seconds
        self.currency = currency
        self.credit_horizon_seconds = credit_horizon_seconds
        self.started_at = started_at
        # The paper venue's declared, simulated fee rate: explicit evidence, not a market tier.
        self.fees = TakerFees(venue="coinbase-advanced", rate=fee_rate, effective_at=started_at, observed_at=started_at,
                              expires_at=started_at + timedelta(days=3650), evidence_id=f"PAPER_SIMULATED_FEE_RATE:{fee_rate}")
        self._samples: list[EquitySample] = []
        self._boundary: EquitySample | None = None  # equity at the last closed boundary (or the session start)
        self._open_start: datetime | None = None
        self._open_capital: Decimal | None = None
        self.windows_closed = 0
        self.windows_fed = 0
        self.horizon_exceeded = 0
        self.writer.call(lambda connection: install_component_schema(connection, REWARD_SCHEMA_COMPONENT, REWARD_SCHEMA))

    # ------------------------------------------------------------------ sampling
    def sample(self, at: datetime, funded_equity: Decimal) -> int:
        """Record the post-tick equity; close every window whose end has passed. Returns windows closed."""
        if at.tzinfo is None:
            raise DomainError("AWARE_TIMESTAMP_REQUIRED")
        if not isinstance(funded_equity, Decimal) or not funded_equity.is_finite():
            raise DomainError("REWARD_CAPITAL_INVALID")
        if self._samples and at < self._samples[-1].at:
            raise DomainError("REWARD_SAMPLE_TIME_REGRESSION")
        if self._open_start is None:
            if funded_equity <= 0:
                raise DomainError("REWARD_CAPITAL_INVALID")
            self._open_start, self._open_capital = at, funded_equity
            self._boundary = EquitySample(at, funded_equity)
            self._persist_open(at, funded_equity)
        self._samples.append(EquitySample(at, funded_equity))
        closed = 0
        while self._open_start is not None and at >= self._open_start + self.interval:
            end = self._open_start + self.interval
            inside = [sample for sample in self._samples if sample.at <= end]
            # No sample inside a skipped window: the ledger did not move, the
            # boundary equity carries over (nothing is interpolated).
            boundary = max(inside, key=lambda sample: sample.at) if inside else self._boundary
            assert boundary is not None
            self._close(end, boundary.funded_equity, available_at=at)
            closed += 1
            if boundary.funded_equity <= 0:
                # An insolvent paper account ends the research session; it is not clipped to survival.
                self._open_start = None
                raise DomainError("REWARD_CAPITAL_INVALID")
            self._open_start, self._open_capital, self._boundary = end, boundary.funded_equity, boundary
            self._persist_open(end, boundary.funded_equity)
            self._samples = [sample for sample in self._samples if sample.at > end]
        return closed

    def _persist_open(self, start: datetime, capital: Decimal) -> None:
        self.writer.call(lambda connection: connection.execute(
            "INSERT OR IGNORE INTO cf_reward_windows (context_id, session_id, window_start, window_end, capital, state) VALUES(?,?,?,?,?,'OPEN')",
            (self.context.identity, self.session_id, start.isoformat(), (start + self.interval).isoformat(), str(capital))).rowcount)

    def _close(self, end: datetime, equity_end: Decimal, *, available_at: datetime) -> None:
        assert self._open_start is not None and self._open_capital is not None
        with localcontext(financial_context(512)):
            pnl = equity_end - self._open_capital
        start = self._open_start
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_reward_windows SET equity_end=?, pnl=?, available_at=?, state='CLOSED' WHERE context_id=? AND session_id=? AND window_start=?",
            (str(equity_end), str(pnl), available_at.isoformat(), self.context.identity, self.session_id, start.isoformat())).rowcount)
        self.windows_closed += 1

    # ------------------------------------------------------------------ delivery
    def backlog(self, now: datetime) -> dict[str, Any]:
        rows = self.writer.call(lambda connection: connection.execute(
            "SELECT window_end, available_at FROM cf_reward_windows WHERE context_id=? AND session_id=? AND state='CLOSED' ORDER BY window_start",
            (self.context.identity, self.session_id)).fetchall())
        oldest = datetime.fromisoformat(rows[0][0]) if rows else None
        return {"pending_windows": len(rows), "oldest_delay_seconds": (now - oldest).total_seconds() if oldest else 0.0,
                "windows_closed": self.windows_closed, "windows_fed": self.windows_fed, "horizon_exceeded": self.horizon_exceeded}

    def next_pending(self, decision_at: datetime) -> PendingWindow | None:
        """The oldest closed window available at or before the decision, in order."""
        row = self.writer.call(lambda connection: connection.execute(
            "SELECT window_start, window_end, capital, pnl, available_at FROM cf_reward_windows WHERE context_id=? AND session_id=? "
            "AND state='CLOSED' ORDER BY window_start LIMIT 1", (self.context.identity, self.session_id)).fetchone())
        if row is None:
            return None
        start, end, available = (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1]), datetime.fromisoformat(row[4]))
        if available > decision_at:
            return None
        capital, pnl = Decimal(row[2]), Decimal(row[3])
        executions = self._executions(start, end, available)
        credits, truncated = self._credits(start, end)
        window = RewardWindow(start_at=start, end_at=end, available_at=available, currency=self.currency, pnl=pnl, capital=capital,
                              basis="net_execution", executions=tuple(executions), credits=tuple(credits))
        delay = (decision_at - end).total_seconds()
        return PendingWindow(window=window, delay_seconds=delay, horizon_exceeded=delay > self.credit_horizon_seconds,
                             credits_truncated=truncated)

    def mark_fed(self, pending: PendingWindow, *, decision_id: str, now: datetime) -> None:
        start = pending.window.start_at
        self.writer.call(lambda connection: connection.execute(
            "UPDATE cf_reward_windows SET state='FED', fed_decision_id=?, fed_at=?, delay_seconds=?, horizon_exceeded=?, credits_json=? "
            "WHERE context_id=? AND session_id=? AND window_start=? AND state='CLOSED'",
            (decision_id, now.isoformat(), pending.delay_seconds, int(pending.horizon_exceeded),
             json.dumps([{"decision_at": credit.decision_at.isoformat(), "fraction": str(credit.fraction)} for credit in pending.window.credits]),
             self.context.identity, self.session_id, start.isoformat())).rowcount)
        self.windows_fed += 1
        self.horizon_exceeded += int(pending.horizon_exceeded)

    # ------------------------------------------------------------------ evidence
    def _decision_rows(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """intent_id → decision timing/reference (from the automation order table)."""
        result: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
                "SELECT intent_id, decision_id, decided_at, reference_price, reference_at, reference_available_at "
                "FROM cf_automation_orders WHERE context_id=? AND decided_at IS NOT NULL", (self.context.identity,)):
            result[str(row[0])] = {"decision_id": str(row[1]), "decided_at": datetime.fromisoformat(row[2]),
                                   "reference_price": Decimal(row[3]) if row[3] else None,
                                   "reference_at": datetime.fromisoformat(row[4]) if row[4] else None,
                                   "reference_available_at": datetime.fromisoformat(row[5]) if row[5] else None}
        return result

    def _executions(self, start: datetime, end: datetime, available_at: datetime) -> list[ExecutionCost]:
        def read(connection: sqlite3.Connection) -> list[ExecutionCost]:
            decisions = self._decision_rows(connection)
            costs: list[ExecutionCost] = []
            for row in connection.execute(
                    "SELECT f.fill_id, f.payload_json, f.created_at, f.intent_id, i.created_at FROM ex_fills f "
                    "LEFT JOIN ex_intents i USING(intent_id) WHERE f.context_id=? AND f.created_at>? AND f.created_at<=? ORDER BY f.created_at",
                    (self.context.identity, start.isoformat(), end.isoformat())):
                fill = Fill.model_validate_json(row[1])
                filled_at = datetime.fromisoformat(row[2])
                decision = decisions.get(str(row[3])) if row[3] else None
                if decision is None or decision["reference_price"] is None:
                    # A protective drain or an unlabeled fill: its cost is real,
                    # but it has no decision-time reference. Book it at its own
                    # fill price (zero shortfall) with the submission time.
                    submitted = datetime.fromisoformat(row[4]) if row[4] else filled_at
                    reference = fill.price
                    reference_at = reference_available = decision_at = min(submitted, filled_at)
                else:
                    submitted = datetime.fromisoformat(row[4]) if row[4] else decision["decided_at"]
                    reference = decision["reference_price"]
                    decision_at = decision["decided_at"]
                    reference_at = min(decision["reference_at"] or decision_at, decision_at)
                    reference_available = min(max(decision["reference_available_at"] or reference_at, reference_at), decision_at)
                    submitted = max(submitted, decision_at)
                if fill.quote_asset != self.currency:
                    raise DomainError("REWARD_CURRENCY_MISMATCH")
                costs.append(ExecutionCost(
                    fill_id=fill.fill_id, currency=fill.quote_asset, side=fill.side.value, quantity=fill.quantity,
                    multiplier=fill.contract_multiplier, reference_price=reference, fill_price=fill.price,
                    decision_at=decision_at, reference_at=reference_at, reference_available_at=reference_available,
                    submitted_at=submitted, filled_at=max(filled_at, submitted), available_at=available_at, fees=self.fees,
                    actual_fee=fill.fee if fill.fee_asset == fill.quote_asset else Decimal(0)))
            return costs
        return self.writer.call(read)

    def _credits(self, start: datetime, end: datetime) -> tuple[list[DecisionCredit], int]:
        def read(connection: sqlite3.Connection) -> tuple[list[DecisionCredit], int]:
            decisions = self._decision_rows(connection)
            weights: dict[datetime, Decimal] = {}
            with localcontext(financial_context(512)):
                for row in connection.execute(
                        "SELECT lot_id, quantity_remaining, price, multiplier FROM ex_open_lots WHERE context_id=? AND quantity_remaining<>'0'",
                        (self.context.identity,)):
                    opening = connection.execute("SELECT intent_id FROM ex_fills WHERE fill_id=?", (row[0],)).fetchone()
                    decision = decisions.get(str(opening[0])) if opening and opening[0] else None
                    if decision is None or decision["decided_at"] >= end:
                        continue
                    weight = abs(Decimal(row[1])) * Decimal(row[2]) * Decimal(row[3])
                    weights[decision["decided_at"]] = weights.get(decision["decided_at"], Decimal(0)) + weight
                for row in connection.execute(
                        "SELECT payload_json, intent_id FROM ex_fills WHERE context_id=? AND created_at>? AND created_at<=?",
                        (self.context.identity, start.isoformat(), end.isoformat())):
                    decision = decisions.get(str(row[1])) if row[1] else None
                    if decision is None or decision["decided_at"] >= end:
                        continue
                    fill = Fill.model_validate_json(row[0])
                    weight = fill.quantity * fill.price * fill.contract_multiplier
                    weights[decision["decided_at"]] = weights.get(decision["decided_at"], Decimal(0)) + weight
                total = sum(weights.values(), Decimal(0))
                if total <= 0:
                    return [], 0
                ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
                truncated = max(0, len(ranked) - MAX_CREDITS)
                credits = []
                for decided_at, weight in ranked[:MAX_CREDITS]:
                    fraction = (weight / total).quantize(FRACTION_QUANTUM, rounding=ROUND_DOWN)
                    if fraction > 0:
                        credits.append(DecisionCredit(decision_at=decided_at, fraction=fraction))
                credits.sort(key=lambda credit: credit.decision_at)
                return credits, truncated
        return self.writer.call(read)
