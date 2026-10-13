"""Priority token bucket: display scans cannot consume the protection reserve."""

import asyncio
import heapq
import math
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from coinfly.domain.context import DomainError


class Priority(IntEnum):
    DRAIN = 0
    UNKNOWN_RESOLUTION = 1
    RECONCILIATION = 2
    PERMISSION = 3
    EXECUTION = 4
    OBSERVATION = 5
    SCAN = 6
    BULK = 7


@dataclass(order=True)
class _Waiter:
    priority: Priority
    sequence: int
    cost: float = field(compare=False)


class RateGovernor:
    """In-process hierarchy for a single worker event loop.

    Production transports must also enforce the certified account/endpoint limits.
    Permits are consumed, never refunded merely because an outcome is UNKNOWN.
    """

    def __init__(
        self, *, rate_per_second: float = 10.0, capacity: float = 10.0,
        protection_reserve: float = 2.0, max_waiters: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not all(math.isfinite(x) for x in (rate_per_second, capacity, protection_reserve)):
            raise ValueError("finite rate budgets required")
        if rate_per_second <= 0 or capacity <= 0 or not 0 <= protection_reserve < capacity:
            raise ValueError("invalid rate budget")
        if max_waiters < 1:
            raise ValueError("positive queue bound required")
        self.rate_per_second = rate_per_second
        self.capacity = capacity
        self.protection_reserve = protection_reserve
        self.max_waiters = max_waiters
        self._clock = clock
        self._tokens = capacity
        self._last_refill = clock()
        self._condition = asyncio.Condition()
        self._waiters: list[_Waiter] = []
        self._sequence = 0
        self._closed = False
        self._broadcasts: set[asyncio.Task[None]] = set()
        self.bulk_paused = False
        self.freshness_valid = True

    async def acquire(
        self, priority: Priority, *, cost: float = 1.0, deadline: float | None = None
    ) -> None:
        if not isinstance(priority, Priority) or not math.isfinite(cost) or cost <= 0:
            raise ValueError("valid priority and positive finite cost required")
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("finite monotonic deadline required")
        reserve = 0.0 if priority <= Priority.RECONCILIATION else self.protection_reserve
        if cost > self.capacity - reserve:
            raise DomainError("RATE_COST_EXCEEDS_CAPACITY")
        async with self._condition:
            if self._closed:
                raise DomainError("RATE_GOVERNOR_CLOSED")
            queue_reserve = min(16, max(1, self.max_waiters // 4)) if self.max_waiters > 1 else 0
            queue_limit = self.max_waiters if priority <= Priority.RECONCILIATION else self.max_waiters - queue_reserve
            if len(self._waiters) >= queue_limit:
                raise DomainError("RATE_QUEUE_FULL")
            self._sequence += 1
            waiter = _Waiter(priority, self._sequence, cost)
            heapq.heappush(self._waiters, waiter)
            self._condition.notify_all()
            try:
                while True:
                    now = self._clock()
                    if deadline is not None and now >= deadline:
                        raise DomainError("RATE_DEADLINE_EXCEEDED")
                    if self._closed:
                        raise DomainError("RATE_GOVERNOR_CLOSED")
                    if priority == Priority.EXECUTION and not self.freshness_valid:
                        raise DomainError("RECONCILIATION_REQUIRED")
                    self._refill(now)
                    paused = priority == Priority.BULK and self.bulk_paused
                    if self._waiters[0] is waiter and self._tokens >= cost + reserve and not paused:
                        self._tokens -= cost
                        heapq.heappop(self._waiters)
                        self._condition.notify_all()
                        return
                    wait_seconds = max(0.001, (cost + reserve - self._tokens) / self.rate_per_second)
                    wait_seconds = min(0.1, wait_seconds)
                    if self._waiters[0] is not waiter or paused:
                        wait_seconds = 0.1
                    if deadline is not None:
                        wait_seconds = min(wait_seconds, max(0.001, deadline - now))
                    await self._wait_or_timeout(wait_seconds)
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    heapq.heapify(self._waiters)
                    self._condition.notify_all()

    async def _wait_or_timeout(self, seconds: float) -> None:
        """Bounded condition wait without cancelling ``Condition.wait``.

        ``asyncio.wait_for(condition.wait(), timeout)`` cancels the wait while it
        re-acquires the lock; on Python 3.11 that cancellation can be swallowed
        and a later cancellation of the acquiring task then never completes.
        A timer that schedules a plain broadcast is version-independent, and the
        caller re-checks its state after every (possibly spurious) wake-up.
        """
        loop = asyncio.get_running_loop()
        timer = loop.call_later(seconds, self._schedule_broadcast, loop)
        try:
            await self._condition.wait()
        finally:
            timer.cancel()

    def _schedule_broadcast(self, loop: asyncio.AbstractEventLoop) -> None:
        task = loop.create_task(self._broadcast())
        self._broadcasts.add(task)  # keep a strong reference until it completes
        task.add_done_callback(self._broadcasts.discard)

    async def _broadcast(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def set_resource_pressure(self, active: bool) -> None:
        async with self._condition:
            self.bulk_paused = active
            self._condition.notify_all()

    async def invalidate_after_wake(self) -> None:
        async with self._condition:
            self.freshness_valid = False
            self._condition.notify_all()

    async def mark_reconciled(self) -> None:
        async with self._condition:
            self.freshness_valid = True
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        self._last_refill = now
