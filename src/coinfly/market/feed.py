"""Market feed protocol and the two credential-free local feeds.

``MarketFeed`` is owned by the worker's automation controller. A feed stores the
latest observation per instrument (bounded), reports its own health, and is
retired with the context that subscribed it: a retired feed delivers nothing to
a replacement context because ``stop()`` revokes every later ``latest()``.

* ``SyntheticFixtureFeed`` — deterministic scripted books for tests and offline
  demonstrations. Its observations are labeled SYNTHETIC_FIXTURE everywhere.
* ``ReplayFeed`` — replays a recorded JSON-lines file (one MarketObservation per
  line) in order, keyed to the injected clock. Labeled REPLAY_FILE with the
  recording's digest.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from coinfly.domain.context import DomainError
from coinfly.domain.products import ProductSubtype
from coinfly.market.observations import (
    DataSource,
    DepthLevelObservation,
    FeedHealth,
    FeedState,
    InstrumentCatalogEntry,
    MarketObservation,
)

MAX_TRACKED_INSTRUMENTS = 200
MAX_REPLAY_BYTES = 64 * 1024 * 1024


class MarketFeed(Protocol):
    source: DataSource

    def start(self, instruments: Sequence[str]) -> None: ...
    def stop(self) -> None: ...
    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]: ...
    def latest(self, instrument: str) -> MarketObservation | None: ...
    def health(self) -> FeedHealth: ...
    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        """Advance any internal source and return observations produced now."""
        ...


class _ObservationStore:
    """Bounded latest-observation cache shared by every feed implementation."""

    def __init__(self, source: DataSource) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._latest: dict[str, MarketObservation] = {}
        self._stopped = False
        self._subscribed: tuple[str, ...] = ()
        self._last_message_at: datetime | None = None
        self._gaps = 0
        self._reconnects = 0
        self._resyncs = 0
        self._overflows = 0
        self._state = FeedState.STOPPED
        self._detail = ""
        self._sequence: dict[str, Any] = {}

    def record(self, observation: MarketObservation) -> bool:
        with self._lock:
            if self._stopped:
                return False
            previous = self._latest.get(observation.instrument)
            if previous is not None and observation.sequence <= previous.sequence:
                return False  # out-of-order or duplicate: ignore, never regress
            if previous is None and len(self._latest) >= MAX_TRACKED_INSTRUMENTS:
                raise DomainError("FEED_INSTRUMENT_LIMIT")
            if previous is not None and observation.sequence > previous.sequence + 1:
                self._gaps += 1
            self._latest[observation.instrument] = observation
            self._last_message_at = observation.received_at
            return True

    def latest(self, instrument: str) -> MarketObservation | None:
        with self._lock:
            if self._stopped:
                return None
            return self._latest.get(instrument)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._state = FeedState.STOPPED
            self._latest.clear()

    def set_state(self, state: FeedState, detail: str = "") -> None:
        with self._lock:
            if not self._stopped:
                self._state, self._detail = state, detail[:240]

    def note_reconnect(self) -> None:
        with self._lock:
            self._reconnects += 1

    def note_resync(self) -> None:
        with self._lock:
            self._resyncs += 1

    def note_overflow(self) -> None:
        with self._lock:
            self._overflows += 1

    def set_sequence(self, description: dict[str, Any]) -> None:
        with self._lock:
            self._sequence = dict(description)

    def invalidate(self, detail: str) -> None:
        """Books can no longer be trusted (gap/overflow): drop them until the resync delivers new ones."""
        with self._lock:
            self._latest.clear()
            if not self._stopped:
                self._state, self._detail = FeedState.DEGRADED, detail[:240]

    def subscribe(self, instruments: Sequence[str]) -> None:
        with self._lock:
            if self._stopped:
                raise DomainError("FEED_STOPPED")
            self._subscribed = tuple(dict.fromkeys(instruments))

    @property
    def subscribed(self) -> tuple[str, ...]:
        with self._lock:
            return self._subscribed

    def health(self) -> FeedHealth:
        with self._lock:
            return FeedHealth(source=self.source, state=self._state, last_message_at=self._last_message_at,
                              sequence_gaps=self._gaps, reconnects=self._reconnects, subscribed=self._subscribed,
                              detail=self._detail, resynchronizations=self._resyncs,
                              instrument_observed_at={name: item.observed_at for name, item in self._latest.items()},
                              sequence=dict(self._sequence), overflows=self._overflows)


def fixture_catalog(instruments: Iterable[str], *, now: datetime, quote: str = "USD",
                    subtype: ProductSubtype = ProductSubtype.SPOT,
                    source: DataSource = DataSource.SYNTHETIC_FIXTURE) -> tuple[InstrumentCatalogEntry, ...]:
    entries = []
    for instrument in instruments:
        base, _, found_quote = instrument.partition("-")
        entries.append(InstrumentCatalogEntry(
            instrument_id=instrument, base_currency=base, quote_currency=found_quote or quote, subtype=subtype,
            base_increment=Decimal("0.00000001"), quote_increment=Decimal("0.01"), base_min_size=Decimal("0.00000001"),
            quote_min_size=Decimal("1"), volume_24h=Decimal("1000000"), observed_at=now, source=source,
        ))
    return tuple(entries)


PathFunction = Callable[[int], tuple[Decimal, Decimal]]


class SyntheticFixtureFeed:
    """Deterministic books from explicit price paths; never presented as market data."""

    source = DataSource.SYNTHETIC_FIXTURE

    def __init__(self, paths: dict[str, PathFunction], *, quote: str = "USD", depth_quantity: Decimal = Decimal("1000"),
                 subtype: ProductSubtype = ProductSubtype.SPOT, catalog_overrides: dict[str, dict[str, object]] | None = None) -> None:
        if not paths:
            raise ValueError("at least one scripted instrument required")
        self._paths = dict(paths)
        self._quote = quote
        self._depth_quantity = depth_quantity
        self._subtype = subtype
        self._overrides = catalog_overrides or {}
        self._store = _ObservationStore(self.source)
        self._tick = 0
        self._catalog_time = datetime.now(timezone.utc)

    def start(self, instruments: Sequence[str]) -> None:
        unknown = [name for name in instruments if name not in self._paths]
        if unknown:
            raise DomainError("FEED_INSTRUMENT_UNKNOWN")
        self._store.subscribe(instruments)
        self._store.set_state(FeedState.CONNECTED, "scripted fixture")

    def stop(self) -> None:
        self._store.stop()

    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]:
        entries = []
        for entry in fixture_catalog(self._paths, now=self._catalog_time, quote=self._quote, subtype=self._subtype):
            overrides = self._overrides.get(entry.instrument_id)
            entries.append(entry.model_copy(update=overrides) if overrides else entry)
        return tuple(entries)

    def latest(self, instrument: str) -> MarketObservation | None:
        return self._store.latest(instrument)

    def health(self) -> FeedHealth:
        return self._store.health()

    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        produced = []
        self._tick += 1
        for instrument in self._store.subscribed:
            bid, ask = self._paths[instrument](self._tick)
            observation = MarketObservation(
                instrument=instrument,
                bids=(DepthLevelObservation(price=bid, quantity=self._depth_quantity),
                      DepthLevelObservation(price=bid - Decimal("0.01"), quantity=self._depth_quantity)),
                asks=(DepthLevelObservation(price=ask, quantity=self._depth_quantity),
                      DepthLevelObservation(price=ask + Decimal("0.01"), quantity=self._depth_quantity)),
                observed_at=now, received_at=now, sequence=self._tick, source=self.source,
                lineage=f"fixture tick {self._tick}",
            )
            if self._store.record(observation):
                produced.append(observation)
        return tuple(produced)


def record_observation_line(observation: MarketObservation) -> str:
    return json.dumps(observation.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class ReplayFeed:
    """Replays a recording strictly in order; observation times drive availability.

    A recording must be chronological and each instrument's sequence must
    strictly increase; otherwise it is refused at load rather than replayed
    with records silently dropped.
    """

    source = DataSource.REPLAY_FILE

    def __init__(self, path: Path, *, catalog: tuple[InstrumentCatalogEntry, ...] | None = None) -> None:
        self._path = Path(path)
        if self._path.is_symlink() or not self._path.is_file():
            raise DomainError("REPLAY_FILE_INVALID")
        if self._path.stat().st_size > MAX_REPLAY_BYTES:
            raise DomainError("REPLAY_FILE_TOO_LARGE")
        raw = self._path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        self._observations: list[MarketObservation] = []
        for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed = MarketObservation.model_validate_json(line)
            except ValueError:
                raise DomainError("REPLAY_LINE_INVALID", f"REPLAY_LINE_INVALID: line {number}") from None
            # A recording keeps its original source label in lineage; the feed
            # that replays it is labeled as a replay so nobody mistakes it for live.
            self._observations.append(parsed.model_copy(update={
                "source": self.source, "lineage": f"replay of {parsed.source.value}: {parsed.lineage}"[:200]}))
        if any(later.observed_at < earlier.observed_at
               for earlier, later in zip(self._observations, self._observations[1:])):
            raise DomainError("REPLAY_NOT_CHRONOLOGICAL")
        # The store ignores a sequence that does not advance, so a restart (or a
        # repeat) would silently swallow every later record of that instrument.
        last_sequence: dict[str, int] = {}
        for item in self._observations:
            previous = last_sequence.get(item.instrument)
            if previous is not None and item.sequence <= previous:
                raise DomainError("REPLAY_SEQUENCE_NOT_MONOTONIC",
                                  f"REPLAY_SEQUENCE_NOT_MONOTONIC: {item.instrument} sequence {item.sequence} after {previous}")
            last_sequence[item.instrument] = item.sequence
        self._cursor = 0
        self._catalog = catalog
        self._store = _ObservationStore(self.source)

    @property
    def path(self) -> Path:
        return self._path

    def start(self, instruments: Sequence[str]) -> None:
        self._store.subscribe(instruments)
        self._store.set_state(FeedState.CONNECTED, f"replay {self.digest[:12]}")

    def stop(self) -> None:
        self._store.stop()

    def catalog(self) -> tuple[InstrumentCatalogEntry, ...]:
        if self._catalog is not None:
            return self._catalog
        names = tuple(dict.fromkeys(item.instrument for item in self._observations))
        first = self._observations[0].observed_at if self._observations else datetime.now(timezone.utc)
        return fixture_catalog(names, now=first, source=self.source)

    def latest(self, instrument: str) -> MarketObservation | None:
        return self._store.latest(instrument)

    def health(self) -> FeedHealth:
        return self._store.health()

    def poll(self, now: datetime) -> tuple[MarketObservation, ...]:
        produced = []
        subscribed = set(self._store.subscribed)
        while self._cursor < len(self._observations) and self._observations[self._cursor].observed_at <= now:
            observation = self._observations[self._cursor]
            self._cursor += 1
            if observation.instrument in subscribed and self._store.record(
                    observation.model_copy(update={"received_at": max(observation.received_at, observation.observed_at)})):
                produced.append(observation)
        if self._cursor >= len(self._observations):
            self._store.set_state(FeedState.DEGRADED, "replay exhausted")
        return tuple(produced)

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._observations)

    @property
    def remaining(self) -> int:
        return len(self._observations) - self._cursor


def stale_after(observation: MarketObservation | None, now: datetime, max_age: timedelta) -> bool:
    return observation is None or not observation.two_sided or observation.age(now) > max_age or observation.age(now) < timedelta(0)
