from __future__ import annotations

import hashlib
import io
import ssl
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from coinfly.bootstrap.installer import (
    DatasetInstaller,
    HTTPSDownloader,
    InstallControls,
    _graph_archive_budget,
)
from coinfly.bootstrap.manifest import AssetSpec, BootstrapError, DatasetManifest, VerifiedManifest
from tests.bootstrap.test_signed_manifests import NOW, Ed25519PrivateKey, fixture_root


class Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str, *, status: int = 200, headers: dict | None = None) -> None:
        super().__init__(payload)
        self.url = url
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}

    def geturl(self) -> str:
        return self.url


class Opener:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.requests = []

    def open(self, request, timeout: float) -> Response:
        self.requests.append(request)
        return self.response


class FakeDownloader:
    def __init__(self, contents: dict[str, bytes], *, corrupt: bool = False) -> None:
        self.contents = contents
        self.corrupt = corrupt
        self.calls = 0

    def download(self, asset, destination, progress, controls) -> None:
        controls.checkpoint()
        self.calls += 1
        content = self.contents[asset.path]
        destination.write_bytes(b"x" * len(content) if self.corrupt else content)
        progress(len(content))


def tiny_fixture() -> tuple[VerifiedManifest, dict[str, bytes]]:
    """Installer unit fixture only; production verifier rejects these source hashes."""
    names = ["annotations.feather", "neurotransmitters.feather", "edges.feather",
             "graph.npz", "normalized/neurons.feather"]
    contents = {name: (name + " fixture").encode() for name in names}
    assets = tuple(AssetSpec(path=name, url="https://fixture.invalid/" + name,
                             bytes=len(contents[name]), sha256=hashlib.sha256(contents[name]).hexdigest())
                   for name in names)
    manifest = DatasetManifest.model_construct(schema_version=1, version=1, release="malecns-v1.0",
                                              model_profile="coinfly-dual-compartment-v1",
                                              issued_at=NOW, expires_at=NOW.replace(year=NOW.year + 1), files=assets)
    return VerifiedManifest(manifest, "a" * 64, b"unit fixture", 1, manifest.expires_at), contents


class DownloaderBoundaryTests(unittest.TestCase):
    def test_resume_uses_exact_range_and_preserves_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graph.partial"
            path.write_bytes(b"abc")
            asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz",
                              bytes=6, sha256=hashlib.sha256(b"abcdef").hexdigest())
            downloader = HTTPSDownloader(tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
            opener = Opener(Response(b"def", asset.url, status=206,
                                     headers={"Content-Length": "3", "Content-Range": "bytes 3-5/6"}))
            downloader._opener = opener
            downloader.download(asset, path, lambda _: None, InstallControls())
            self.assertEqual(path.read_bytes(), b"abcdef")
            self.assertEqual(opener.requests[0].get_header("Range"), "bytes=3-")

    def test_range_ignored_restarts_at_zero_and_redirects_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graph.partial"
            path.write_bytes(b"abc")
            asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz", bytes=6, sha256="a" * 64)
            downloader = HTTPSDownloader(tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
            downloader._opener = Opener(Response(b"abcdef", asset.url))
            downloader.download(asset, path, lambda _: None, InstallControls())
            self.assertEqual(path.read_bytes(), b"abcdef")
            path.unlink()
            downloader._opener = Opener(Response(b"abcdef", "https://other.invalid/data"))
            with self.assertRaisesRegex(BootstrapError, "REDIRECT_REJECTED"):
                downloader.download(asset, path, lambda _: None, InstallControls())
            self.assertFalse(path.exists())

    def test_chunked_oversized_response_never_exceeds_signed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graph.partial"
            asset = AssetSpec(path="graph.npz", url="https://fixture.invalid/graph.npz", bytes=3, sha256="a" * 64)
            downloader = HTTPSDownloader(tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
            downloader._opener = Opener(Response(b"abcdef", asset.url, headers={"Transfer-Encoding": "chunked"}))
            with self.assertRaisesRegex(BootstrapError, "LENGTH_EXCEEDED"):
                downloader.download(asset, path, lambda _: None, InstallControls())
            self.assertLessEqual(path.stat().st_size, 3)

    def test_archive_paths_are_not_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "graph.npz"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../kernel.py", "unexpected executable")
            with self.assertRaisesRegex(BootstrapError, "ARCHIVE_CONTRACT_INVALID"):
                _graph_archive_budget(archive_path)


class InstallerTransactionTests(unittest.TestCase):
    def test_atomic_activation_full_hash_recheck_and_cached_no_network(self) -> None:
        verified, contents = tiny_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            downloader = FakeDownloader(contents)
            installer = DatasetInstaller(Path(temporary), fixture_root(Ed25519PrivateKey.generate()), downloader=downloader)
            stages = []
            with patch.object(installer, "_trusted_metadata", return_value=verified), \
                    patch("coinfly.bootstrap.installer.verify_graph") as graph, \
                    patch("coinfly.bootstrap.installer.verify_neurons") as neurons:
                dataset = installer.install(b"fixture", now=NOW, controls=InstallControls(),
                                            progress=lambda stage, *_: stages.append(stage))
                self.assertEqual(downloader.calls, 5)
                self.assertEqual(dataset.directory.name, verified.digest)
                self.assertTrue(graph.called and neurons.called)
                self.assertEqual(stages[-1], "PREPARING")
                installer.check(None, now=NOW, controls=InstallControls(), progress=lambda *_: None)
                self.assertEqual(downloader.calls, 5)
                (dataset.directory / "edges.feather").write_bytes(b"corrupted")
                with self.assertRaisesRegex(BootstrapError, "LENGTH_MISMATCH"):
                    installer.check(None, now=NOW, controls=InstallControls(), progress=lambda *_: None)

    def test_corrupt_download_and_cancel_never_activate(self) -> None:
        verified, contents = tiny_fixture()
        for cancelled in [False, True]:
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as temporary:
                installer = DatasetInstaller(Path(temporary), fixture_root(Ed25519PrivateKey.generate()),
                                             downloader=FakeDownloader(contents, corrupt=True))
                controls = InstallControls()
                if cancelled:
                    controls.cancelled.set()
                with patch.object(installer, "_trusted_metadata", return_value=verified), self.assertRaises(BootstrapError):
                    installer.install(b"fixture", now=NOW, controls=controls, progress=lambda *_: None)
                self.assertFalse((installer.root / verified.manifest.release / verified.digest).exists())


if __name__ == "__main__":
    unittest.main()
