"""Worker-side bootstrap progress; disk/network work never runs on the UI loop."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from coinfly.paths import StorageInitializationError, native_user_home

from .installer import DatasetCapability, DatasetInstaller, Downloader, InstallControls, Installer
from .manifest import BootstrapError, TrustRoot


class BootstrapState(StrEnum):
    CHECKING = "CHECKING"
    MISSING = "MISSING"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    PREPARING = "PREPARING"
    PAUSED = "PAUSED"
    READY = "READY"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class BootstrapProgress:
    state: BootstrapState
    reason: str | None = None
    asset: str | None = None
    bytes_completed: int = 0
    bytes_total: int = 0
    bytes_per_second: float = 0.0
    retries: int = 0
    manifest_digest: str | None = None
    verification: str = "PUBLISHER_SIGNED"
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == BootstrapState.READY


class BootstrapManager:
    """A missing trust root is a release blocker, never permission to fabricate it.

    Subscribe on the worker event loop. Notifications are coalesced immutable
    values. No safety actor, guardian, writer, or running drain is owned here.
    """

    verification = "PUBLISHER_SIGNED"

    def __init__(self, home: Path | None = None, trust_root: TrustRoot | None = None,
                 publisher_manifest: bytes | None = None, *, downloader: Downloader | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.home = home if home is not None else native_user_home()
        self.trust_root = trust_root
        self.publisher_manifest = publisher_manifest
        self.downloader = downloader
        self.clock = clock
        self.progress = BootstrapProgress(BootstrapState.CHECKING)
        self.dataset: DatasetCapability | None = None
        self._controls = InstallControls()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[BootstrapProgress]] = set()
        self._started = 0.0
        self._retries = 0
        self._pending_lock = threading.Lock()
        self._pending_progress: tuple[str, str | None, int, int] | None = None
        self._pending_retries = 0
        self._progress_scheduled = False
        self._resume_progress: BootstrapProgress | None = None

    def subscribe(self) -> asyncio.Queue[BootstrapProgress]:
        queue: asyncio.Queue[BootstrapProgress] = asyncio.Queue(maxsize=1)
        queue.put_nowait(self.progress)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BootstrapProgress]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, progress: BootstrapProgress) -> None:
        progress = replace(progress, verification=self.verification)
        self.progress = progress
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(progress)

    def pause(self) -> None:
        self._controls.unpaused.clear()
        if self._lock.locked():
            if self.progress.state != BootstrapState.PAUSED:
                self._resume_progress = self.progress
            self._publish(replace(self.progress, state=BootstrapState.PAUSED, reason="PAUSED_BY_USER"))

    def resume(self) -> None:
        self._controls.unpaused.set()
        if self.progress.state == BootstrapState.PAUSED and self._resume_progress is not None:
            self._publish(self._resume_progress)
        self._resume_progress = None

    def set_resource_pressure(self, active: bool) -> None:
        if active:
            self._controls.resources_available.clear()
        else:
            self._controls.resources_available.set()

    def cancel(self) -> None:
        self._controls.cancelled.set()

    def verified_dataset(self) -> DatasetCapability:
        if not self.progress.ready or self.dataset is None:
            raise BootstrapError("DATASET_NOT_READY")
        self.dataset.assert_current(self.clock())
        return self.dataset

    async def check(self) -> BootstrapProgress:
        return await self._run(install=False)

    async def ensure(self) -> BootstrapProgress:
        """Verify cached assets, automatically provision only a missing cache."""
        return await self._run(install=False, auto_install=True)

    async def install(self) -> BootstrapProgress:
        return await self._run(install=True)

    async def retry(self) -> BootstrapProgress:
        if self._lock.locked() and self.progress.state == BootstrapState.PAUSED:
            self.resume()
            return self.progress
        return await self.install()

    def _installer(self) -> Installer:
        if self.trust_root is None:
            from .distribution import load_distribution

            self.trust_root, self.publisher_manifest = load_distribution(now=self.clock())
        return DatasetInstaller(self.home, self.trust_root, downloader=self.downloader)

    def _advance(self, stage: str, asset: str | None, completed: int, total: int) -> None:
        if stage == "RETRYING":
            state = BootstrapState.DOWNLOADING
        else:
            state = BootstrapState(stage)
        elapsed = max(time.monotonic() - self._started, 0.001)
        progress = BootstrapProgress(state, asset=asset, bytes_completed=completed,
                                     bytes_total=total, bytes_per_second=completed / elapsed,
                                     retries=self._retries)
        if not self._controls.unpaused.is_set():
            self._resume_progress = progress
            progress = replace(progress, state=BootstrapState.PAUSED, reason="PAUSED_BY_USER")
        self._publish(progress)

    def _flush_progress(self) -> None:
        with self._pending_lock:
            update = self._pending_progress
            retries = self._pending_retries
            self._pending_retries = 0
            self._pending_progress = None
            self._progress_scheduled = False
        if update is not None and self._lock.locked():
            self._retries += retries
            self._advance(*update)

    async def _run(self, *, install: bool, auto_install: bool = False) -> BootstrapProgress:
        if self._lock.locked():
            raise BootstrapError("BOOTSTRAP_BUSY")
        async with self._lock:
            self.dataset = None
            self._controls = InstallControls()
            self._resume_progress = None
            self._retries = 0
            self._started = time.monotonic()
            self._publish(BootstrapProgress(BootstrapState.CHECKING))
            loop = asyncio.get_running_loop()

            def notify(stage: str, asset: str | None, done: int, total: int) -> None:
                # At most one thread-to-loop callback is outstanding even when
                # hashing/download throughput exceeds the display cadence.
                with self._pending_lock:
                    if stage == "RETRYING":
                        self._pending_retries += 1
                    self._pending_progress = (stage, asset, done, total)
                    if self._progress_scheduled:
                        return
                    self._progress_scheduled = True
                loop.call_soon_threadsafe(self._flush_progress)

            def provision() -> DatasetCapability:
                # Construction, path preparation, hashing and network I/O all
                # belong to this thread, never the event loop or UI thread.
                installer = self._installer()
                operation = installer.install if install else installer.check
                try:
                    return operation(self.publisher_manifest, now=self.clock(),
                                     controls=self._controls, progress=notify)
                except BootstrapError as error:
                    if not auto_install or error.code != "DATASET_CACHE_MISSING":
                        raise
                    self._controls.checkpoint()
                    return installer.install(self.publisher_manifest, now=self.clock(),
                                             controls=self._controls, progress=notify)

            task = asyncio.create_task(asyncio.to_thread(provision))
            try:
                self.dataset = await asyncio.shield(task)
            except asyncio.CancelledError:
                self._controls.cancelled.set()
                try:
                    await asyncio.shield(task)
                except (BootstrapError, StorageInitializationError, OSError):
                    pass
                self._publish(BootstrapProgress(BootstrapState.CANCELLED, "BOOTSTRAP_CANCELLED"))
                raise
            except BootstrapError as error:
                self._flush_progress()
                state = (BootstrapState.CANCELLED if error.code == "BOOTSTRAP_CANCELLED"
                         else BootstrapState.MISSING if error.code == "DATASET_CACHE_MISSING"
                         else BootstrapState.BLOCKED)
                self._publish(BootstrapProgress(state, error.code, retries=self._retries))
                return self.progress
            except (StorageInitializationError, OSError) as error:
                from coinfly.operations.qualification import report_storage_failure

                self._flush_progress()
                failure = (error if isinstance(error, StorageInitializationError)
                           else StorageInitializationError.from_os_error(error))
                report_storage_failure(failure)
                self._publish(BootstrapProgress(BootstrapState.BLOCKED, failure.code,
                                                retries=self._retries, detail=failure.detail))
                return self.progress
            except Exception:
                self._publish(BootstrapProgress(BootstrapState.BLOCKED, "BOOTSTRAP_DEPENDENCY_OR_IO_FAILURE"))
                return self.progress
            # Drain a coalesced final chunk before READY so retry counts survive
            # a fast transfer that completes before its scheduled UI callback.
            self._flush_progress()
            total = self.dataset.total_bytes
            self._publish(BootstrapProgress(BootstrapState.READY, bytes_completed=total,
                                           bytes_total=total, retries=self._retries,
                                           manifest_digest=self.dataset.digest))
            return self.progress
