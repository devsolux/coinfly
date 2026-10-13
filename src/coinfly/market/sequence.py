"""Dual-contract WebSocket sequence tracking: per connection or per product, decided by evidence.

Coinbase's channel references (level2, heartbeats; accessed 2026-09-22) define
``sequence_num`` as a *per-connection* message sequence to detect dropped or
out-of-order messages; the WebSocket overview page says sequence numbers are
increasing integers *for each product*. The two statements disagree
(DOCUMENT_CONFLICT in ``docs/evidence-matrix.md``). This tracker does not pick
one on faith: every message is judged against both contracts, the contract
that the stream actually follows is detected from evidence and reported, and
only an anomaly under the detected contract (or under both while undecided)
forces a resynchronization.

Verdicts:

* ``ACCEPT`` — consistent with the (detected or still plausible) contract.
* ``DUPLICATE`` — the same sequence for the same channel/product again: not
  applied, counted.
* ``OUT_OF_ORDER`` — older than what this channel/product already delivered:
  not applied, counted; past a bounded tolerance the stream resynchronizes.
* ``GAP`` — messages were missed under the contract: the book can no longer be
  trusted, the caller resynchronizes (reconnect → fresh snapshot).
* ``RESET`` — the sequence restarted (a new server-side stream on the same
  connection): treated as a gap, the caller resynchronizes.

Each connection is a new *generation*: the tracker is reset on reconnect and
observations carry the generation in their lineage so a book from a previous
connection is never mistaken for the current one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

EVIDENCE_TO_LOCK = 16
OUT_OF_ORDER_TOLERANCE = 8


class SequenceContract(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    PER_CONNECTION = "PER_CONNECTION"
    PER_PRODUCT = "PER_PRODUCT"


class SequenceVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    GAP = "GAP"
    RESET = "RESET"


@dataclass
class SequenceTracker:
    generation: int = 0
    contract: SequenceContract = SequenceContract.UNDETERMINED
    _expected: int | None = None
    _last_by_key: dict[tuple[str, str], int] = field(default_factory=dict)
    _connection_evidence: int = 0
    _product_evidence: int = 0
    _out_of_order_streak: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    resets: int = 0
    messages: int = 0

    def new_connection(self) -> int:
        """Reset for a new connection; the contract detection starts over (a peer may change)."""
        self.generation += 1
        self.contract = SequenceContract.UNDETERMINED
        self._expected = None
        self._last_by_key = {}
        self._connection_evidence = self._product_evidence = 0
        self._out_of_order_streak = 0
        return self.generation

    def observe(self, sequence: int, *, channel: str, product: str | None) -> SequenceVerdict:
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("non-negative integer sequence required")
        key = (channel, product or "*")
        self.messages += 1
        last = self._last_by_key.get(key)
        if self._expected is None:
            self._expected = sequence + 1
            self._last_by_key[key] = sequence
            return SequenceVerdict.ACCEPT
        if last is not None and sequence == last:
            self.duplicates += 1
            return SequenceVerdict.DUPLICATE
        if last is not None and sequence < last:
            if sequence == 0 and last > 0:
                self.resets += 1
                return SequenceVerdict.RESET
            self.out_of_order += 1
            self._out_of_order_streak += 1
            if self._out_of_order_streak > OUT_OF_ORDER_TOLERANCE:
                self.gaps += 1
                return SequenceVerdict.GAP
            return SequenceVerdict.OUT_OF_ORDER
        self._out_of_order_streak = 0
        connection_ok = sequence == self._expected
        product_ok = last is not None and sequence == last + 1
        if self.contract is SequenceContract.PER_CONNECTION:
            verdict = SequenceVerdict.ACCEPT if connection_ok else SequenceVerdict.GAP
        elif self.contract is SequenceContract.PER_PRODUCT:
            verdict = SequenceVerdict.ACCEPT if (product_ok or last is None) else SequenceVerdict.GAP
        else:
            # Undetermined: collect evidence; an anomaly under BOTH contracts is a gap.
            if connection_ok:
                self._connection_evidence += 1
            if product_ok and not connection_ok:
                self._product_evidence += 1
            if connection_ok or product_ok or last is None:
                verdict = SequenceVerdict.ACCEPT
            else:
                verdict = SequenceVerdict.GAP
            if self._connection_evidence >= EVIDENCE_TO_LOCK and self._product_evidence == 0:
                self.contract = SequenceContract.PER_CONNECTION
            elif self._product_evidence >= EVIDENCE_TO_LOCK:
                self.contract = SequenceContract.PER_PRODUCT
        if verdict is SequenceVerdict.GAP:
            self.gaps += 1
            return verdict
        if sequence < self._expected and last is None:
            # First message of a product under a per-product stream (or an
            # interleaved connection counter): record it, do not treat it as
            # a regression of the connection counter.
            self._last_by_key[key] = sequence
            return verdict
        self._expected = max(self._expected, sequence + 1)
        self._last_by_key[key] = sequence
        return verdict

    def describe(self) -> dict[str, Any]:
        return {"generation": self.generation, "contract": self.contract.value, "messages": self.messages,
                "duplicates": self.duplicates, "out_of_order": self.out_of_order, "gaps": self.gaps, "resets": self.resets,
                "connection_evidence": self._connection_evidence, "product_evidence": self._product_evidence}
