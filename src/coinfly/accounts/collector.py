"""Read-only paginated collection; partial failures are explicit observations."""

import asyncio
from datetime import datetime, timezone
from typing import Callable

from coinfly.accounts.coverage import ScopeScan
from coinfly.accounts.inventory import AccountObservation
from coinfly.accounts.source_registry import SourceRegistry, SourceScope
from coinfly.domain.context import DomainError, FrozenModel
from coinfly.network.rate_governor import Priority, RateGovernor


class CollectionBatch(FrozenModel):
    observations: tuple[AccountObservation, ...]
    scans: tuple[ScopeScan, ...]


class AccountCollector:
    def __init__(
        self, registry: SourceRegistry, governor: RateGovernor, *,
        max_pages: int = 1000, max_observations: int = 100000,
        page_timeout: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        import math

        if max_pages < 1 or max_observations < 1 or not math.isfinite(page_timeout) or page_timeout <= 0:
            raise ValueError("positive bounded collector budgets required")
        self.registry = registry
        self.governor = governor
        self.max_pages = max_pages
        self.max_observations = max_observations
        self.page_timeout = page_timeout
        self._clock = clock

    async def collect(self, *, protective_refresh: bool = False) -> CollectionBatch:
        observations: list[AccountObservation] = []
        scans: list[ScopeScan] = []
        priority = Priority.RECONCILIATION if protective_refresh else Priority.OBSERVATION
        for scope in self.registry.sources:
            rows, scan = await self._collect_scope(scope, priority)
            if len(observations) + len(rows) > self.max_observations:
                scans.append(ScopeScan(
                    scope_id=scope.scope_id, completed=False, pages=scan.pages,
                    observed_at=self._clock(), issue="OBSERVATION_BATCH_LIMIT",
                ))
                continue
            observations.extend(rows)
            scans.append(scan)
        return CollectionBatch(observations=tuple(observations), scans=tuple(scans))

    async def _collect_scope(
        self, scope: SourceScope, priority: Priority
    ) -> tuple[tuple[AccountObservation, ...], ScopeScan]:
        rows: list[AccountObservation] = []
        pages = 0
        issue: str | None = None
        complete = False
        reader = self.registry.reader(scope.scope_id)
        if not scope.supported:
            issue = "SOURCE_UNSUPPORTED"
        elif not scope.accessible or reader is None:
            issue = "SOURCE_INACCESSIBLE"
        else:
            cursor: str | None = None
            cursors: set[str] = set()
            identities: dict[str, AccountObservation] = {}
            try:
                while pages < self.max_pages:
                    await self.governor.acquire(priority)
                    page = await asyncio.wait_for(reader.read_page(cursor), timeout=self.page_timeout)
                    pages += 1
                    if len(rows) + len(page.observations) > self.max_observations:
                        raise DomainError("OBSERVATION_BATCH_LIMIT")
                    for row in page.observations:
                        self._validate(scope, row)
                        prior = identities.get(row.observation_id)
                        if prior is not None:
                            if prior != row:
                                raise DomainError("OBSERVATION_ID_CONFLICT")
                            continue
                        identities[row.observation_id] = row
                        rows.append(row)
                    if page.complete:
                        complete = True
                        break
                    if page.next_cursor is None:
                        issue = "PAGINATION_INCOMPLETE"
                        break
                    if page.next_cursor in cursors:
                        raise DomainError("PAGINATION_CURSOR_LOOP")
                    cursors.add(page.next_cursor)
                    cursor = page.next_cursor
                if not complete and issue is None:
                    issue = "PAGINATION_LIMIT"
            except DomainError as exc:
                issue = exc.code
            except (TimeoutError, OSError):
                issue = "SOURCE_UNAVAILABLE"
            except Exception:
                # No exception text crosses the observation channel (HTTP errors may contain secrets).
                issue = "SOURCE_PROTOCOL_ERROR"
        return tuple(rows), ScopeScan(
            scope_id=scope.scope_id, completed=complete, pages=pages, observed_at=self._clock(),
            max_sequence=max((row.sequence for row in rows), default=0), issue=issue,
        )

    def _validate(self, scope: SourceScope, row: AccountObservation) -> None:
        if (row.authority_id != scope.authority_id or row.environment != scope.environment
                or row.scope_id != scope.scope_id or row.source_id != scope.source_id):
            raise DomainError("OBSERVATION_REALM_MISMATCH")
        if row.kind not in scope.readable_kinds:
            raise DomainError("ACCOUNT_KIND_NOT_CERTIFIED")
        if row.portfolio_id is not None and row.portfolio_id not in scope.portfolio_ids:
            raise DomainError("OBSERVATION_PORTFOLIO_MISMATCH")
        if row.as_of > self._clock():
            raise DomainError("FUTURE_ACCOUNT_OBSERVATION")
