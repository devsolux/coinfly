"""Source-lock verification for an isolated, explicitly local development worker.

Never creates publisher keys, signed manifests or a publisher-verified capability.
The installed package's original source, array and neuron locks are the authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from coinfly.paths import ApplicationPaths, is_link

from .installer import (
    DatasetCapability,
    Downloader,
    HTTPSDownloader,
    InstallControls,
    Installer,
    ProgressCallback,
    _assert_regular_path,
    _fsync_directory,
    download_with_retries,
    file_sha256,
    installation_lock,
    secure_directory,
    verify_graph,
    verify_neurons,
)
from .manager import BootstrapManager, BootstrapProgress
from .manifest import MANIFESTS, REQUIRED_FILES, AssetSpec, BootstrapError

_LOCAL_ISSUER = object()


def source_assets() -> tuple[AssetSpec, ...]:
    locked = json.loads((MANIFESTS / "sources.lock.json").read_bytes())
    return tuple(AssetSpec(path=name, **value) for name, value in locked.items())


def lock_digest() -> str:
    digest = hashlib.sha256()
    for name in ("sources.lock.json", "arrays.lock.json", "neurons.lock.json"):
        digest.update((MANIFESTS / name).read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceVerifiedDataset:
    directory: Path
    digest: str
    total_bytes: int
    _issuer: object = field(repr=False)

    def assert_current(self, now: datetime | None = None) -> None:
        if self._issuer is not _LOCAL_ISSUER or self.digest != lock_digest() or self.directory.name != self.digest:
            raise BootstrapError("LOCAL_DATASET_READINESS_INVALID")

    def path(self, name: str) -> Path:
        self.assert_current()
        if name not in REQUIRED_FILES:
            raise BootstrapError("DATASET_PATH_NOT_ATTESTED")
        path = self.directory / name
        _assert_regular_path(path)
        if any(is_link(parent) for parent in (path.parent, self.directory, self.directory.parent)):
            raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
        return path


class SourceDatasetInstaller:
    def __init__(self, root: Path | None = None, *, downloader: Downloader | None = None,
                 application: ApplicationPaths | None = None) -> None:
        self.application = application or (ApplicationPaths.for_current_user() if root is None else None)
        if self.application is not None:
            if root is not None and root != self.application.source_datasets:
                raise BootstrapError("DATASET_ROOT_MISMATCH")
            root = self.application.source_datasets
        assert root is not None
        self.root = root.expanduser().absolute()
        self.downloader = downloader
        self.assets = source_assets()
        self.digest = lock_digest()

    def _prepare_root(self) -> None:
        if self.application is not None:
            self.application.prepare()
        # Explicit fixture roots retain their caller-owned parent boundary.
        secure_directory(self.root)

    def _verify_sources(self, directory: Path, controls: InstallControls, progress: ProgressCallback) -> None:
        total, completed = sum(asset.bytes for asset in self.assets), 0
        if is_link(directory) or is_link(directory / "normalized"):
            raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
        for asset in self.assets:
            path = directory / asset.path
            _assert_regular_path(path)
            if path.stat().st_size != asset.bytes or file_sha256(path, controls) != asset.sha256:
                raise BootstrapError("DATA_DIGEST_OR_LENGTH_MISMATCH")
            completed += asset.bytes
            progress("VERIFYING", asset.path, completed, total)

    def _verified(self, directory: Path, controls: InstallControls, progress: ProgressCallback) -> SourceVerifiedDataset:
        self._verify_sources(directory, controls, progress)
        for name in REQUIRED_FILES:
            _assert_regular_path(directory / name)
        progress("VERIFYING", "graph.npz", 0, 0)
        verify_graph(directory / "graph.npz", controls)
        verify_neurons(directory, controls)
        controls.checkpoint()
        return SourceVerifiedDataset(directory, self.digest,
                                     sum((directory / name).stat().st_size for name in REQUIRED_FILES), _LOCAL_ISSUER)

    def check(self, envelope: bytes | None, *, now: datetime,
              controls: InstallControls, progress: ProgressCallback) -> SourceVerifiedDataset:
        self._prepare_root()
        with installation_lock(self.root):
            final = self.root / self.digest
            if not final.exists():
                raise BootstrapError("DATASET_CACHE_MISSING")
            return self._verified(final, controls, progress)

    def install(self, envelope: bytes | None, *, now: datetime,
                controls: InstallControls, progress: ProgressCallback) -> SourceVerifiedDataset:
        from .prepare import prepare_graph

        self._prepare_root()
        with installation_lock(self.root):
            final = self.root / self.digest
            if final.exists() or is_link(final):
                # A changed active cache fails closed; never overwrite operator data.
                return self._verified(final, controls, progress)
            staging = self.root / (self.digest + ".staging")
            secure_directory(staging)
            secure_directory(staging / "normalized")
            total = sum(asset.bytes for asset in self.assets)
            if shutil.disk_usage(staging).free < total + 2 * 1024**3:
                raise BootstrapError("DATASET_DISK_BUDGET_INSUFFICIENT")
            completed = 0
            downloader = self.downloader or HTTPSDownloader()
            for asset in self.assets:
                controls.checkpoint()
                target = staging / asset.path
                partial = target.with_name(target.name + ".partial")
                _assert_regular_path(target, missing_ok=True)
                _assert_regular_path(partial, missing_ok=True)
                if target.exists():
                    if target.stat().st_size != asset.bytes or file_sha256(target, controls) != asset.sha256:
                        raise BootstrapError("DATA_DIGEST_OR_LENGTH_MISMATCH")
                else:
                    download_with_retries(downloader, asset, partial, controls, progress,
                                          completed=completed, total=total)
                    if partial.stat().st_size != asset.bytes or file_sha256(partial, controls) != asset.sha256:
                        partial.unlink()
                        raise BootstrapError("DOWNLOADED_DATA_DIGEST_OR_LENGTH_MISMATCH")
                    os.replace(partial, target)
                completed += asset.bytes
            # Recheck source bytes before handing them to Feather/graph parsers.
            self._verify_sources(staging, controls, progress)
            for name in ("graph.npz", "normalized/neurons.feather"):
                _assert_regular_path(staging / name, missing_ok=True)
            prepare_graph(staging, controls, progress)
            dataset = self._verified(staging, controls, progress)
            for name in ("graph.npz", "normalized/neurons.feather"):
                with (staging / name).open("rb") as source:
                    os.fsync(source.fileno())
            _fsync_directory(staging / "normalized")
            _fsync_directory(staging)
            controls.checkpoint()
            os.replace(staging, final)
            _fsync_directory(self.root)
            return SourceVerifiedDataset(final, dataset.digest, dataset.total_bytes, _LOCAL_ISSUER)


class DevelopmentBootstrapManager(BootstrapManager):
    verification = "LOCAL_SOURCE_LOCKS"

    def __init__(self, root: Path | None = None, *, downloader: Downloader | None = None,
                 application: ApplicationPaths | None = None) -> None:
        self.application = application or (ApplicationPaths.for_current_user() if root is None else None)
        if self.application is not None:
            if root is not None and root != self.application.source_datasets:
                raise BootstrapError("DATASET_ROOT_MISMATCH")
            root = self.application.source_datasets
        assert root is not None
        super().__init__(home=root.parent, downloader=downloader)
        self.root = root
        self.progress = BootstrapProgress(self.progress.state, verification=self.verification)

    def _installer(self) -> Installer:
        return SourceDatasetInstaller(self.root, downloader=self.downloader, application=self.application)

    async def check(self) -> BootstrapProgress:
        # dev.sh explicitly opts into obtaining and preparing the pinned sources.
        return await self.install()

    def verified_dataset(self) -> DatasetCapability:
        dataset = super().verified_dataset()
        if not isinstance(dataset, SourceVerifiedDataset):
            raise BootstrapError("LOCAL_DATASET_READINESS_INVALID")
        return dataset
