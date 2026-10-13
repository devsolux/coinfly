"""Completeness is evidence about discoverable scope, never a UI preference."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from coinfly.domain.context import FrozenModel, Identifier


class CoverageStatus(StrEnum):
    COMPLETE_VERIFIED = "COMPLETE_VERIFIED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    INCONSISTENT = "INCONSISTENT"
    UNKNOWN_UNIVERSE = "UNKNOWN_UNIVERSE"


class ScopeScan(FrozenModel):
    scope_id: Identifier
    completed: bool
    pages: int = Field(ge=0)
    observed_at: datetime
    max_sequence: int = Field(default=0, ge=0)
    issue: str | None = None


class CoverageReport(FrozenModel):
    status: CoverageStatus
    included_scopes: tuple[str, ...]
    expected_scopes: tuple[str, ...]
    unsupported_scopes: tuple[str, ...] = ()
    inaccessible_scopes: tuple[str, ...] = ()
    incomplete_scopes: tuple[str, ...] = ()
    missing_marks: tuple[str, ...] = ()
    ambiguous_accounts: tuple[str, ...] = ()
    stale_scopes: tuple[str, ...] = ()
    stale_marks: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    observation_start: datetime | None
    observation_end: datetime | None
    source_sequences: tuple[tuple[str, int], ...]
    universe_verified: bool

    @property
    def is_complete(self) -> bool:
        return self.status == CoverageStatus.COMPLETE_VERIFIED
