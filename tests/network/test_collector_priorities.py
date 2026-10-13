import asyncio
import time
import unittest

from coinfly.domain.context import DomainError
from coinfly.network.rate_governor import Priority, RateGovernor


class RateGovernorTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_traffic_cannot_consume_drain_reserve(self):
        governor = RateGovernor(rate_per_second=0.001, capacity=3, protection_reserve=1)
        await governor.acquire(Priority.OBSERVATION, cost=2)
        display = asyncio.create_task(governor.acquire(Priority.SCAN))
        await asyncio.sleep(0)
        await asyncio.wait_for(governor.acquire(Priority.DRAIN), timeout=0.1)
        self.assertFalse(display.done())
        display.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await display
        await governor.close()

    async def test_unknown_resolution_precedes_queued_scans(self):
        governor = RateGovernor(rate_per_second=100, capacity=1, protection_reserve=0)
        await governor.acquire(Priority.DRAIN)
        received = []

        async def acquire(priority):
            await governor.acquire(priority)
            received.append(priority)

        scan = asyncio.create_task(acquire(Priority.SCAN))
        unknown = asyncio.create_task(acquire(Priority.UNKNOWN_RESOLUTION))
        await asyncio.gather(scan, unknown)
        self.assertEqual(received, [Priority.UNKNOWN_RESOLUTION, Priority.SCAN])

    async def test_deadline_and_wake_require_reconciliation(self):
        governor = RateGovernor(rate_per_second=0.001, capacity=2, protection_reserve=1)
        await governor.acquire(Priority.OBSERVATION)
        with self.assertRaisesRegex(DomainError, "RATE_DEADLINE_EXCEEDED"):
            await governor.acquire(Priority.OBSERVATION, deadline=time.monotonic() + 0.02)
        await governor.invalidate_after_wake()
        with self.assertRaisesRegex(DomainError, "RECONCILIATION_REQUIRED"):
            await governor.acquire(Priority.EXECUTION)
        await governor.acquire(Priority.RECONCILIATION)
        await governor.mark_reconciled()
        self.assertTrue(governor.freshness_valid)

    async def test_resource_pressure_pauses_bulk_but_not_protection(self):
        governor = RateGovernor()
        await governor.set_resource_pressure(True)
        bulk = asyncio.create_task(governor.acquire(Priority.BULK))
        await asyncio.sleep(0)
        await governor.acquire(Priority.DRAIN)
        self.assertFalse(bulk.done())
        await governor.set_resource_pressure(False)
        await asyncio.wait_for(bulk, timeout=0.1)

    async def test_queue_has_reserved_space_for_protection(self):
        governor = RateGovernor(rate_per_second=0.001, capacity=2, protection_reserve=1, max_waiters=2)
        await governor.acquire(Priority.OBSERVATION)
        scan = asyncio.create_task(governor.acquire(Priority.SCAN))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(DomainError, "RATE_QUEUE_FULL"):
            await governor.acquire(Priority.OBSERVATION)
        await asyncio.wait_for(governor.acquire(Priority.DRAIN), timeout=0.1)
        scan.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await scan
