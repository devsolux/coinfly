"""Shadow orchestration accepts market data, never production account state."""

from datetime import datetime
from typing import Protocol

from pydantic import Field

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, FrozenModel, RunProfile
from coinfly.simulation.paper_matching import DepthLevel, PaperFill, PaperMatcher


class MarketFrame(FrozenModel):
    instrument_id: str
    bids: tuple[DepthLevel, ...] = Field(max_length=1000)
    asks: tuple[DepthLevel, ...] = Field(max_length=1000)
    as_of: datetime
    sequence: int = Field(ge=0)


class PublicMarketFeed(Protocol):
    async def next_frame(self) -> MarketFrame: ...


class ShadowRunner:
    """The feed capability exposes one read; the only execution object is local."""

    def __init__(self, config: EngineConfig, market_feed: PublicMarketFeed, matcher: PaperMatcher) -> None:
        if config.environment != Environment.SANDBOX or config.run_profile != RunProfile.SHADOW:
            raise DomainError("SHADOW_REQUIRES_SANDBOX_PROFILE")
        if matcher.context.environment != Environment.SANDBOX:
            raise DomainError("SHADOW_LIVE_MUTATION_FORBIDDEN")
        if (matcher.context.authority_id != config.authority_id
                or matcher.context.product_track != config.product_track
                or matcher.context.broker_profile_id != config.broker_profile_id):
            raise DomainError("SHADOW_CONTEXT_MISMATCH")
        self.config = config
        self._feed = market_feed
        self._matcher = matcher

    async def consume_once(self) -> tuple[PaperFill, ...]:
        frame = await self._feed.next_frame()
        return self._matcher.update_book(
            frame.instrument_id, bids=frame.bids, asks=frame.asks, now=frame.as_of, sequence=frame.sequence,
        )
