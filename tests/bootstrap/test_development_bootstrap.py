"""Small transport fixtures exercise local staging; full array locks are never changed."""

import asyncio
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from coinfly.bootstrap.development import (
    DevelopmentBootstrapManager,
    SourceDatasetInstaller,
    SourceVerifiedDataset,
)
from coinfly.bootstrap.installer import InstallControls, VerifiedDataset
from coinfly.bootstrap.manager import BootstrapManager, BootstrapState
from coinfly.bootstrap.manifest import AssetSpec, BootstrapError
from tests.bootstrap.test_installer_boundaries import FakeDownloader


@pytest.fixture
def sources(tmp_path):
    contents = {name: (name + " local fixture").encode()
                for name in ("annotations.feather", "neurotransmitters.feather", "edges.feather")}
    assets = tuple(AssetSpec(path=name, url="https://fixture.invalid/" + name, bytes=len(value),
                            sha256=hashlib.sha256(value).hexdigest()) for name, value in contents.items())
    with patch("coinfly.bootstrap.development.source_assets", return_value=assets):
        yield tmp_path, contents


def prepare_fixture(directory, controls, progress):
    controls.checkpoint()
    (directory / "graph.npz").write_bytes(b"synthetic graph fixture")
    (directory / "normalized/neurons.feather").write_bytes(b"synthetic neuron fixture")


def operation(installer, method="install", controls=None):
    return getattr(installer, method)(None, now=datetime.now(timezone.utc),
                                      controls=controls or InstallControls(), progress=lambda *_: None)


def test_local_install_verifies_then_activates_and_rechecks_offline(sources):
    root, contents = sources
    downloader = FakeDownloader(contents)
    installer = SourceDatasetInstaller(root / "malecns", downloader=downloader)
    with (patch("coinfly.bootstrap.prepare.prepare_graph", side_effect=prepare_fixture) as prepare,
          patch("coinfly.bootstrap.development.verify_graph") as graph,
          patch("coinfly.bootstrap.development.verify_neurons") as neurons):
        dataset = operation(installer)
        assert isinstance(dataset, SourceVerifiedDataset)
        assert not isinstance(dataset, VerifiedDataset)
        assert dataset.directory.name == installer.digest
        dataset.assert_current()
        assert dataset.path("graph.npz").is_file()
        with pytest.raises(BootstrapError, match="PATH_NOT_ATTESTED"):
            dataset.path("../kernel.so")
        assert downloader.calls == 3
        assert prepare.call_count == graph.call_count == neurons.call_count == 1
        assert operation(installer, "check") == dataset
        assert downloader.calls == 3
        assert graph.call_count == neurons.call_count == 2
        (dataset.directory / "annotations.feather").write_bytes(b"changed")
        with pytest.raises(BootstrapError, match="DATA_DIGEST_OR_LENGTH_MISMATCH"):
            operation(installer, "check")


def test_bad_source_never_reaches_parser_or_ready(sources):
    root, contents = sources
    manager = DevelopmentBootstrapManager(root / "malecns", downloader=FakeDownloader(contents, corrupt=True))
    with patch("coinfly.bootstrap.prepare.prepare_graph") as prepare:
        result = asyncio.run(manager.check())
    assert result.state == BootstrapState.BLOCKED
    assert result.verification == "LOCAL_SOURCE_LOCKS"
    assert result.reason == "DOWNLOADED_DATA_DIGEST_OR_LENGTH_MISMATCH"
    prepare.assert_not_called()
    with pytest.raises(BootstrapError, match="DATASET_NOT_READY"):
        manager.verified_dataset()


def test_derived_verification_failure_and_cancel_never_activate(sources):
    root, contents = sources
    installer = SourceDatasetInstaller(root / "malecns", downloader=FakeDownloader(contents))
    with (patch("coinfly.bootstrap.prepare.prepare_graph", side_effect=prepare_fixture),
          patch("coinfly.bootstrap.development.verify_graph", side_effect=BootstrapError("GRAPH_ARRAY_DIGEST_MISMATCH"))):
        with pytest.raises(BootstrapError, match="GRAPH_ARRAY_DIGEST_MISMATCH"):
            operation(installer)
        assert not (installer.root / installer.digest).exists()
        controls = InstallControls()
        controls.cancelled.set()
        with pytest.raises(BootstrapError, match="BOOTSTRAP_CANCELLED"):
            operation(installer, controls=controls)
        assert not (installer.root / installer.digest).exists()


def test_local_manager_auto_installs_without_changing_signed_startup(sources):
    root, contents = sources
    manager = DevelopmentBootstrapManager(root / "malecns", downloader=FakeDownloader(contents))
    with (patch("coinfly.bootstrap.prepare.prepare_graph", side_effect=prepare_fixture),
          patch("coinfly.bootstrap.development.verify_graph"),
          patch("coinfly.bootstrap.development.verify_neurons")):
        result = asyncio.run(manager.check())
    assert result.ready and result.verification == "LOCAL_SOURCE_LOCKS"
    assert result.manifest_digest == manager.verified_dataset().digest
    signed = asyncio.run(BootstrapManager(home=root).check())
    assert not signed.ready and signed.reason == "PUBLISHER_TRUST_ROOT_MISSING"
    assert signed.verification == "PUBLISHER_SIGNED"
    assert not (root / ".devsolux").exists()


def test_source_capability_cannot_be_forged_or_reused_after_locks_change(sources):
    root, contents = sources
    dataset = SourceVerifiedDataset(root, "a" * 64, 1, object())
    with pytest.raises(BootstrapError, match="LOCAL_DATASET_READINESS_INVALID"):
        dataset.assert_current()
    installer = SourceDatasetInstaller(root / "malecns", downloader=FakeDownloader(contents))
    with (patch("coinfly.bootstrap.prepare.prepare_graph", side_effect=prepare_fixture),
          patch("coinfly.bootstrap.development.verify_graph"),
          patch("coinfly.bootstrap.development.verify_neurons")):
        dataset = operation(installer)
    with patch("coinfly.bootstrap.development.lock_digest", return_value="b" * 64):
        with pytest.raises(BootstrapError, match="LOCAL_DATASET_READINESS_INVALID"):
            dataset.path("graph.npz")


def test_staging_symlink_is_rejected_before_download(sources):
    root, contents = sources
    downloader = FakeDownloader(contents)
    installer = SourceDatasetInstaller(root / "malecns", downloader=downloader)
    installer.root.mkdir()
    (installer.root / (installer.digest + ".staging")).symlink_to(root, target_is_directory=True)
    with pytest.raises(BootstrapError, match="DATA_DIRECTORY_SYMLINK_REJECTED"):
        operation(installer)
    assert downloader.calls == 0


def test_development_state_and_socket_are_isolated_and_live_is_rejected(tmp_path, monkeypatch):
    from coinfly.config.models import default_config
    from coinfly.development import DevelopmentWorker, development_root, development_socket
    from coinfly.domain.context import DomainError, Environment, RunProfile
    from coinfly.ipc.transport_unix import default_socket_path
    from coinfly.paths import ApplicationPaths
    from coinfly.worker.paths import WorkerPaths

    application = ApplicationPaths.for_home(tmp_path)
    monkeypatch.setattr(ApplicationPaths, "for_current_user", lambda: application)
    root = development_root(tmp_path / "checkout")
    paths = WorkerPaths(root)
    assert paths.database.is_relative_to(application.storage / "development")
    assert not (tmp_path / "checkout").exists()
    assert development_socket(tmp_path) != default_socket_path()
    assert development_socket(tmp_path) != development_socket(tmp_path / "another-checkout")
    config = default_config().model_copy(update={"environment": Environment.LIVE, "run_profile": RunProfile.STANDARD})
    with pytest.raises(DomainError, match="DEVELOPMENT_PAPER_ONLY"):
        DevelopmentWorker._validate_candidate(object(), config)


def test_dev_launcher_selects_local_profile_and_retains_signed_entry_point(tmp_path, monkeypatch):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    root = Path(__file__).resolve().parents[2]
    launcher = (root / "dev.sh").read_text()
    assert '-m coinfly.development desktop' in launcher
    assert '-m coinfly.development worker' in launcher
    assert '-m coinfly.development prepare' in launcher
    from coinfly.cli import main
    with patch("coinfly.cli._launch_desktop", return_value=0) as desktop:
        assert main(["desktop"]) == 0
    desktop.assert_called_once()


def test_default_source_download_bootstraps_fresh_home_off_thread(sources, monkeypatch):
    from coinfly.paths import ApplicationPaths

    home, contents = sources
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: home)
    entered, proceed = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    paths = ApplicationPaths.for_home(home)

    class SlowDownloader(FakeDownloader):
        def download(self, asset, destination, progress, controls):
            assert threading.get_ident() != main_thread
            assert all(path.is_dir() for path in paths.directories)
            assert destination.is_relative_to(paths.datasets)
            entered.set()
            assert proceed.wait(2)
            super().download(asset, destination, progress, controls)

    downloader = SlowDownloader(contents)
    manager = DevelopmentBootstrapManager(downloader=downloader)
    assert not paths.root.exists()

    async def run():
        operation = asyncio.create_task(manager.ensure())
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set() and not operation.done()
            # The loop remains responsive while the network thread is blocked.
            await asyncio.sleep(0)
            proceed.set()
            return await asyncio.wait_for(operation, 2)
        finally:
            proceed.set()
            manager.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    with (patch("coinfly.bootstrap.prepare.prepare_graph", side_effect=prepare_fixture),
          patch("coinfly.bootstrap.development.verify_graph"),
          patch("coinfly.bootstrap.development.verify_neurons")):
        result = asyncio.run(run())
        assert result.ready and result.bytes_completed == result.bytes_total > 0
        assert manager.verified_dataset().path("graph.npz").is_relative_to(paths.datasets)
        assert asyncio.run(manager.ensure()).ready
    assert downloader.calls == 3
