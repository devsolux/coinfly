"""Synthetic transport fixtures test orchestration, not full-graph fitness."""

import asyncio
import errno
import hashlib
import json
import ssl
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from coinfly.bootstrap.installer import HTTPSDownloader, InstallControls, download_with_retries
from coinfly.bootstrap.manager import BootstrapManager, BootstrapState
from coinfly.bootstrap.manifest import AssetSpec, BootstrapError
from coinfly.paths import ApplicationPaths
from tests.bootstrap.test_installer_boundaries import FakeDownloader, Response, tiny_fixture
from tests.bootstrap.test_signed_manifests import Ed25519PrivateKey, fixture_root


def test_signed_startup_provisions_missing_cache_and_reverifies_offline(tmp_path):
    verified, contents = tiny_fixture()
    downloader = FakeDownloader(contents)
    manager = BootstrapManager(tmp_path, fixture_root(Ed25519PrivateKey.generate()),
                               publisher_manifest=b"unit fixture", downloader=downloader)
    # The production metadata and array validators retain their independent tests.
    with (patch("coinfly.bootstrap.installer.DatasetInstaller._trusted_metadata", return_value=verified),
          patch("coinfly.bootstrap.installer.verify_graph"),
          patch("coinfly.bootstrap.installer.verify_neurons")):
        assert asyncio.run(manager.ensure()).ready
        assert downloader.calls == 5
        dataset = manager.verified_dataset()
        assert dataset.directory.is_relative_to(ApplicationPaths.for_home(tmp_path).datasets)
        assert asyncio.run(manager.ensure()).ready
        assert downloader.calls == 5
        dataset.path("graph.npz").write_bytes(b"damaged")
        result = asyncio.run(manager.ensure())
        assert result.state == BootstrapState.BLOCKED
        assert result.reason == "DATA_LENGTH_MISMATCH"
        assert downloader.calls == 5


def test_storage_denial_is_reported_before_dataset_network_io(tmp_path, monkeypatch, capsys):
    downloader = Mock()
    manager = BootstrapManager(tmp_path, fixture_root(Ed25519PrivateKey.generate()), downloader=downloader)
    original = Path.mkdir

    def denied(path, *args, **kwargs):
        if path.name == "datasets":
            raise PermissionError("PRIVATE_CANARY")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    result = asyncio.run(manager.ensure())
    assert result.reason == "STORAGE_PERMISSION_DENIED"
    assert result.detail and "permissions" in result.detail
    assert not result.ready
    downloader.download.assert_not_called()
    output = capsys.readouterr().err
    assert "PRIVATE_CANARY" not in output
    assert json.loads(output)["blocking_codes"] == [result.reason]


def test_truncated_stream_resumes_with_bounded_retry_and_progress(tmp_path):
    payload = b"abcdef"
    asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz", bytes=len(payload),
                      sha256=hashlib.sha256(payload).hexdigest())
    downloader = HTTPSDownloader(tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    opener = Mock()
    opener.open.side_effect = [
        Response(b"abc", asset.url, headers={"Content-Length": "6"}),
        Response(b"def", asset.url, status=206,
                 headers={"Content-Length": "3", "Content-Range": "bytes 3-5/6"}),
    ]
    downloader._opener = opener
    controls = InstallControls()
    events = []
    destination = tmp_path / "graph.partial"
    with patch.object(controls.cancelled, "wait", return_value=False) as backoff:
        download_with_retries(downloader, asset, destination, controls,
                              lambda *event: events.append(event), completed=10, total=16)
    assert destination.read_bytes() == payload
    assert opener.open.call_args_list[1].args[0].get_header("Range") == "bytes=3-"
    assert events[-1] == ("DOWNLOADING", "graph.npz", 16, 16)
    assert sum(event[0] == "RETRYING" for event in events) == 1
    backoff.assert_called_once_with(1)


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError(errno.ENOSPC, "full")])
def test_filesystem_failures_are_not_retried_as_network_failures(tmp_path, failure):
    asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz", bytes=6, sha256="a" * 64)
    downloader = Mock()
    downloader.download.side_effect = failure
    with pytest.raises(type(failure)):
        download_with_retries(downloader, asset, tmp_path / "graph.partial", InstallControls(),
                              lambda *_: None, completed=0, total=6)
    downloader.download.assert_called_once()


def test_transient_network_retries_are_bounded_and_cancellable(tmp_path):
    asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz", bytes=6, sha256="a" * 64)
    downloader = Mock()
    downloader.download.side_effect = urllib.error.URLError("synthetic offline")
    controls = InstallControls()
    with patch.object(controls.cancelled, "wait", return_value=False):
        with pytest.raises(BootstrapError, match="DATASET_DOWNLOAD_FAILED"):
            download_with_retries(downloader, asset, tmp_path / "graph.partial", controls,
                                  lambda *_: None, completed=0, total=6)
    assert downloader.download.call_count == 3
    controls.cancelled.set()
    with pytest.raises(BootstrapError, match="BOOTSTRAP_CANCELLED"):
        download_with_retries(downloader, asset, tmp_path / "graph.partial", controls,
                              lambda *_: None, completed=0, total=6)
    assert downloader.download.call_count == 3


def test_coalesced_progress_retains_every_retry(tmp_path):
    verified, contents = tiny_fixture()

    class NoDelayControls(InstallControls):
        def __init__(self):
            super().__init__()
            self.cancelled.wait = Mock(return_value=False)

    class RetryingDownloader(FakeDownloader):
        failures = 0

        def download(self, asset, destination, progress, controls):
            if self.failures < 2:
                self.failures += 1
                raise urllib.error.URLError("transient unit fixture")
            super().download(asset, destination, progress, controls)

    manager = BootstrapManager(tmp_path, fixture_root(Ed25519PrivateKey.generate()),
                               downloader=RetryingDownloader(contents))
    with (patch("coinfly.bootstrap.installer.DatasetInstaller._trusted_metadata", return_value=verified),
          patch("coinfly.bootstrap.installer.verify_graph"),
          patch("coinfly.bootstrap.installer.verify_neurons"),
          patch("coinfly.bootstrap.manager.InstallControls", NoDelayControls)):
        result = asyncio.run(manager.ensure())
    assert result.ready and result.retries == 2
