"""The paper matcher's single monotonic clock and its validate-before-mutate guarantees.

Regression cover for the venue clock mixing feed stamps with wall time: a depth
snapshot may be observed earlier than the matcher's latest event, so the matcher
takes an event clock (``now``) separately from the observation time
(``observed_at``) and a failed validation never changes any state.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from coinfly.domain.context import DomainError
from coinfly.simulation.paper_matching import PaperMatcher, PaperOrderStatus, Side
from tests.simulation.test_stateful_paper_matching import NOW, context, depth, instrument, request

D = Decimal


class PaperMatcherClockTests(unittest.TestCase):
    def matcher(self) -> PaperMatcher:
        return PaperMatcher(context(), (instrument(),), initial_balances={"USD": D("1000")})

    def test_last_time_follows_the_latest_accepted_event(self) -> None:
        matcher = self.matcher()
        self.assertIsNone(matcher.last_time)
        matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "5")), now=NOW)
        self.assertEqual(matcher.last_time, NOW)
        matcher.submit(request(), now=NOW + timedelta(seconds=2))
        self.assertEqual(matcher.last_time, NOW + timedelta(seconds=2))

    def test_book_keeps_its_observation_time_when_the_event_clock_is_ahead(self) -> None:
        matcher = self.matcher()
        clock = NOW + timedelta(seconds=10)
        order = matcher.submit(request(quantity="1"), now=clock)
        # Observed 7 s before the event clock: older than the 5 s book age, so it must not fill.
        fills = matcher.update_book("BTC-USD", bids=depth(("98", "5")), asks=depth(("99", "5")), now=clock,
                                    observed_at=NOW + timedelta(seconds=3))
        self.assertEqual(fills, ())
        self.assertEqual(matcher.order(order.order_id).status, PaperOrderStatus.OPEN)
        self.assertEqual(matcher.last_time, clock)
        # Observed 1 s before the event clock: fresh, so the same resting order fills.
        fills = matcher.update_book("BTC-USD", bids=depth(("98", "5")), asks=depth(("99", "5")), now=clock,
                                    observed_at=NOW + timedelta(seconds=9))
        self.assertEqual([fill.quantity for fill in fills], [D("1")])
        self.assertEqual(matcher.last_time, clock)

    def test_an_observation_cannot_be_stamped_after_the_event_clock(self) -> None:
        matcher = self.matcher()
        before = matcher.dump_state()
        with self.assertRaisesRegex(DomainError, "PAPER_OBSERVATION_AFTER_CLOCK"):
            matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "5")), now=NOW,
                                observed_at=NOW + timedelta(seconds=1))
        self.assertEqual(matcher.dump_state(), before)

    def test_the_event_clock_still_never_runs_backwards(self) -> None:
        matcher = self.matcher()
        matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "5")), now=NOW)
        with self.assertRaisesRegex(DomainError, "PAPER_TIME_REGRESSION"):
            matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "5")),
                                now=NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(DomainError, "PAPER_TIME_REGRESSION"):
            matcher.submit(request(), now=NOW - timedelta(seconds=1))


class PaperMatcherValidationTests(unittest.TestCase):
    def matcher(self) -> PaperMatcher:
        matcher = PaperMatcher(context(), (instrument(),), initial_balances={"USD": D("1000")})
        matcher.update_book("BTC-USD", bids=depth(("99", "5")), asks=depth(("100", "5")), now=NOW)
        return matcher

    def test_rejected_submit_changes_nothing(self) -> None:
        matcher = self.matcher()
        before = matcher.dump_state()
        later = NOW + timedelta(seconds=3)
        cases = (
            (request("tick", price="100.005"), "PAPER_INVALID_INCREMENT"),
            (request("funds", quantity="100000"), "PAPER_INSUFFICIENT_FUNDS"),
            (request("inventory", side=Side.SELL), "PAPER_INSUFFICIENT_INVENTORY"),
            (request("reduce", side=Side.BUY, reduce_only=True), "PAPER_REDUCE_ONLY_VIOLATION"),
        )
        for bad, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                matcher.submit(bad, now=later)
            self.assertEqual(matcher.dump_state(), before)

    def test_validate_book_raises_without_touching_state(self) -> None:
        matcher = self.matcher()
        before = matcher.dump_state()
        later = NOW + timedelta(seconds=1)
        cases = (
            ({"bids": depth(("100", "1")), "asks": depth(("99", "1"))}, "PAPER_CROSSED_BOOK"),
            ({"bids": depth(("98", "1"), ("99", "1")), "asks": depth(("100", "1"))}, "PAPER_INVALID_BID_DEPTH"),
            ({"bids": depth(("99", "1")), "asks": depth(("101", "1"), ("100", "1"))}, "PAPER_INVALID_ASK_DEPTH"),
        )
        for book, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                matcher.validate_book("BTC-USD", now=later, **book)
            self.assertEqual(matcher.dump_state(), before)
        with self.assertRaisesRegex(DomainError, "PAPER_UNKNOWN_INSTRUMENT"):
            matcher.validate_book("ETH-USD", bids=depth(("99", "1")), asks=depth(("100", "1")), now=later)
        with self.assertRaisesRegex(DomainError, "PAPER_TIME_REGRESSION"):
            matcher.validate_book("BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")),
                                  now=NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(DomainError, "PAPER_STALE_BOOK_SEQUENCE"):
            matcher.validate_book("BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")), now=later, sequence=0)
        self.assertEqual(matcher.dump_state(), before)

    def test_validate_book_reports_the_sequence_update_book_will_take(self) -> None:
        matcher = self.matcher()
        later = NOW + timedelta(seconds=1)
        self.assertEqual(matcher.validate_book("BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")), now=later), 1)
        self.assertEqual(matcher.validate_book("BTC-USD", bids=depth(("99", "1")), asks=depth(("100", "1")),
                                               now=later, sequence=7), 7)
        self.assertEqual(matcher.last_time, NOW)  # validation alone never advances the clock


if __name__ == "__main__":
    unittest.main()
