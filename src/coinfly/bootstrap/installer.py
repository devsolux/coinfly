"""Bounded raw-file staging, authenticated activation, and verified cache loading."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import shutil
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, ClassVar, Protocol

from coinfly.paths import ApplicationPaths, is_link

from .manifest import (
    MANIFESTS,
    AssetSpec,
    BootstrapError,
    ManifestVerifier,
    TrustRoot,
    VerifiedManifest,
    canonical_json,
    strict_json,
)

CHUNK_BYTES = 1024 * 1024
MAX_EXPANDED_GRAPH_BYTES = 1024**3
_VERIFIED_DATASET_ISSUER = object()


class InstallControls:
    """Thread-safe cooperative cancellation and resource-pressure pauses."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.unpaused = threading.Event()
        self.unpaused.set()
        self.resources_available = threading.Event()
        self.resources_available.set()

    def checkpoint(self) -> None:
        while not self.unpaused.is_set() or not self.resources_available.is_set():
            if self.cancelled.wait(0.05):
                raise BootstrapError("BOOTSTRAP_CANCELLED")
        if self.cancelled.is_set():
            raise BootstrapError("BOOTSTRAP_CANCELLED")


ProgressCallback = Callable[[str, str | None, int, int], None]


class DatasetCapability(Protocol):
    @property
    def total_bytes(self) -> int: ...

    @property
    def digest(self) -> str: ...

    def assert_current(self, now: datetime | None = None) -> None: ...
    def path(self, name: str) -> Path: ...


class Installer(Protocol):
    def check(self, envelope: bytes | None, *, now: datetime,
              controls: InstallControls, progress: ProgressCallback) -> DatasetCapability: ...

    def install(self, envelope: bytes | None, *, now: datetime,
                controls: InstallControls, progress: ProgressCallback) -> DatasetCapability: ...


class Downloader(Protocol):
    def download(self, asset: AssetSpec, destination: Path,
                 progress: Callable[[int], None], controls: InstallControls) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes],
                         code: int, msg: str, headers: object, newurl: str) -> None:
        raise BootstrapError("DATA_REDIRECT_REJECTED")


class HTTPSDownloader:
    """No proxy/netrc/ambient CA configuration, cookies, credentials or redirects."""

    def __init__(self, *, timeout_seconds: float = 15.0, tls_context: ssl.SSLContext | None = None) -> None:
        if not 0 < timeout_seconds <= 30:
            raise BootstrapError("DOWNLOAD_TIMEOUT_INVALID")
        # An explicit dependency-owned CA bundle avoids SSL_CERT_FILE/SSL_CERT_DIR.
        if tls_context is None:
            try:
                import certifi
            except ImportError:
                raise BootstrapError("TLS_CA_BUNDLE_MISSING") from None
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=certifi.where())
        else:
            context = tls_context
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise BootstrapError("TLS_VERIFICATION_REQUIRED")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )
        self._timeout = timeout_seconds

    def download(self, asset: AssetSpec, destination: Path,
                 progress: Callable[[int], None], controls: InstallControls) -> None:
        controls.checkpoint()
        _assert_regular_path(destination, missing_ok=True)
        offset = destination.stat().st_size if destination.exists() else 0
        if offset > asset.bytes:
            raise BootstrapError("STAGED_FILE_OVERSIZED")
        if offset == asset.bytes:
            progress(offset)
            return
        headers = {"User-Agent": "coinfly/1.0.0", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(asset.url, headers=headers, method="GET")
        with self._opener.open(request, timeout=self._timeout) as response:
            if response.geturl() != asset.url:
                raise BootstrapError("DATA_REDIRECT_REJECTED")
            status = response.status
            if status == 206:
                expected = f"bytes {offset}-{asset.bytes - 1}/{asset.bytes}"
                if response.headers.get("Content-Range") != expected:
                    raise BootstrapError("DOWNLOAD_RANGE_INVALID")
            elif status == 200:
                offset = 0
            else:
                raise BootstrapError("DOWNLOAD_STATUS_INVALID")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise BootstrapError("DOWNLOAD_ENCODING_REJECTED")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) != asset.bytes - offset:
                        raise ValueError
                except ValueError:
                    raise BootstrapError("DOWNLOAD_LENGTH_INVALID") from None
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_APPEND if offset else os.O_TRUNC
            fd = os.open(destination, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                received = offset
                while True:
                    controls.checkpoint()
                    # One underlying read keeps cancellation checkpoints useful
                    # even when a server dribbles bytes without closing a body.
                    chunk = response.read1(min(CHUNK_BYTES, asset.bytes - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > asset.bytes:
                        raise BootstrapError("DOWNLOAD_LENGTH_EXCEEDED")
                    output.write(chunk)
                    progress(received)
                output.flush()
                os.fsync(output.fileno())
            if received != asset.bytes:
                raise BootstrapError("DOWNLOAD_TRUNCATED")


def download_with_retries(downloader: Downloader, asset: AssetSpec, destination: Path,
                          controls: InstallControls, progress: ProgressCallback, *,
                          completed: int, total: int) -> None:
    """Resume bounded partial files after transient transport failures only.

    Filesystem errors propagate to storage diagnostics. Invalid lengths/ranges,
    redirects and hash failures never become successful downloads via retries.
    The caller verifies exact length and SHA-256 before activating the asset.
    """
    def transferred(amount: int) -> None:
        progress("DOWNLOADING", asset.path, completed + amount, total)

    for attempt in range(3):
        controls.checkpoint()
        try:
            downloader.download(asset, destination, transferred, controls)
            return
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise BootstrapError("DATASET_DOWNLOAD_FAILED") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            pass
        except BootstrapError as error:
            if error.code != "DOWNLOAD_TRUNCATED":
                raise
        controls.checkpoint()
        if attempt == 2:
            raise BootstrapError("DATASET_DOWNLOAD_FAILED") from None
        progress("RETRYING", asset.path, completed, total)
        if controls.cancelled.wait(2**attempt):
            raise BootstrapError("BOOTSTRAP_CANCELLED")


def _assert_regular_path(path: Path, *, missing_ok: bool = False) -> None:
    if is_link(path):
        raise BootstrapError("DATA_SYMLINK_REJECTED")
    if not path.exists() and missing_ok:
        return
    if not path.is_file():
        raise BootstrapError("DATA_FILE_MISSING_OR_INVALID")
    if path.stat().st_nlink != 1:
        raise BootstrapError("DATA_HARDLINK_REJECTED")


def secure_directory(path: Path) -> None:
    if is_link(path):
        raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not path.is_dir():
        raise BootstrapError("DATA_DIRECTORY_INVALID")
    if sys.platform != "win32":
        if path.stat().st_uid != os.getuid():
            raise BootstrapError("DATA_DIRECTORY_OWNER_MISMATCH")
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, payload: bytes) -> None:
    _assert_regular_path(path, missing_ok=True)
    fd, name = tempfile.mkstemp(prefix=".coinfly-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".installation.lock"
    _assert_regular_path(lock_path, missing_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BootstrapError("BOOTSTRAP_OWNED_BY_OTHER_PROCESS") from None
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BootstrapError("BOOTSTRAP_OWNED_BY_OTHER_PROCESS") from None
        yield
    finally:
        os.close(fd)


def file_sha256(path: Path, controls: InstallControls | None = None) -> str:
    _assert_regular_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            if controls is not None:
                controls.checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def _graph_archive_budget(path: Path) -> None:
    import numpy as np

    expected = json.loads((MANIFESTS / "arrays.lock.json").read_bytes())
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (len(members) != len(expected)
                    or {member.filename for member in members} != {name + ".npy" for name in expected}
                    or sum(member.file_size for member in members) > MAX_EXPANDED_GRAPH_BYTES
                    or any(member.flag_bits & 1 for member in members)):
                raise BootstrapError("GRAPH_ARCHIVE_CONTRACT_INVALID")
            for member in members:
                with archive.open(member) as source:
                    version = np.lib.format.read_magic(source)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(source, max_header_size=16384)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(source, max_header_size=16384)
                    else:
                        raise BootstrapError("GRAPH_ARRAY_HEADER_INVALID")
                    expected_bytes = math.prod(shape) * dtype.itemsize
                    if (dtype.hasobject or len(shape) > 2 or any(dimension < 0 for dimension in shape)
                            or expected_bytes > MAX_EXPANDED_GRAPH_BYTES
                            or expected_bytes != member.file_size - source.tell()):
                        raise BootstrapError("GRAPH_ARRAY_HEADER_INVALID")
    except (OSError, ValueError, zipfile.BadZipFile):
        raise BootstrapError("GRAPH_ARCHIVE_INVALID") from None


def verify_graph(path: Path, controls: InstallControls | None = None) -> None:
    import numpy as np

    _graph_archive_budget(path)
    expected = json.loads((MANIFESTS / "arrays.lock.json").read_bytes())
    shapes = {
        "ptr": (166701,), "post": (25582938,), "weight": (25582938,),
        "ids": (166700,), "retina": (3335,), "uv": (3335, 2),
        "confidence": (3335,), "hexes": (3335, 2), "superclass": (166700,),
    }
    dtypes = {"ptr": "int64", "post": "int32", "weight": "float32", "ids": "int64",
              "retina": "int32", "uv": "float32", "confidence": "float64", "hexes": "float64",
              "lamina": "int32", "sugar": "int32", "superclass": "<U64"}
    try:
        with np.load(path, allow_pickle=False, max_header_size=16384) as graph:
            for name, digest in expected.items():
                if controls is not None:
                    controls.checkpoint()
                array = graph[name]
                if array.dtype != np.dtype(dtypes[name]) or not array.flags.c_contiguous:
                    raise BootstrapError("GRAPH_ARRAY_TYPE_MISMATCH")
                if name in shapes and array.shape != shapes[name]:
                    raise BootstrapError("GRAPH_ARRAY_SHAPE_MISMATCH")
                if name in {"lamina", "sugar"} and (array.ndim != 1 or len(array) > 166700):
                    raise BootstrapError("GRAPH_ARRAY_SHAPE_MISMATCH")
                if hashlib.sha256(array.tobytes()).hexdigest() != digest:
                    raise BootstrapError("GRAPH_ARRAY_DIGEST_MISMATCH")
    except (ValueError, KeyError, OSError, MemoryError, zipfile.BadZipFile):
        raise BootstrapError("GRAPH_ARRAY_INVALID") from None


def verify_neurons(directory: Path, controls: InstallControls) -> None:
    import numpy as np

    try:
        import pyarrow.feather as feather
    except ImportError:
        raise BootstrapError("NEURON_READER_DEPENDENCY_MISSING") from None
    controls.checkpoint()
    try:
        table = feather.read_table(directory / "normalized/neurons.feather",
                                   columns=["source_id", "neurotransmitter"])
        if table.num_rows != 166700:
            raise BootstrapError("NEURON_COUNT_MISMATCH")
        values = table.column("neurotransmitter").to_pylist()
        if any(value is not None and not isinstance(value, str) for value in values):
            raise BootstrapError("NEURON_TRANSMITTER_TYPE_INVALID")
        normalized = ["" if value is None else value for value in values]
        digest = hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()
        locked = json.loads((MANIFESTS / "neurons.lock.json").read_bytes())
        if digest != locked["neurotransmitter_values_sha256"]:
            raise BootstrapError("NEURON_TRANSMITTER_DIGEST_MISMATCH")
        identities = table.column("source_id").to_pylist()
        if any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in identities):
            raise BootstrapError("NEURON_ID_TYPE_INVALID")
        with np.load(directory / "graph.npz", allow_pickle=False) as graph:
            if not np.array_equal(np.asarray(identities, dtype=np.int64), graph["ids"]):
                raise BootstrapError("NEURON_ORDER_MISMATCH")
    except (ValueError, KeyError, OSError):
        raise BootstrapError("NEURON_METADATA_INVALID") from None
    controls.checkpoint()


def verify_dataset(directory: Path, verified: VerifiedManifest, controls: InstallControls,
                   progress: ProgressCallback) -> None:
    if is_link(directory) or is_link(directory.parent) or is_link(directory / "normalized"):
        raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
    total = sum(asset.bytes for asset in verified.manifest.files)
    checked = 0
    for asset in verified.manifest.files:
        controls.checkpoint()
        path = directory / asset.path
        _assert_regular_path(path)
        if path.stat().st_size != asset.bytes:
            raise BootstrapError("DATA_LENGTH_MISMATCH")
        if file_sha256(path, controls) != asset.sha256:
            raise BootstrapError("DATA_DIGEST_MISMATCH")
        checked += asset.bytes
        progress("VERIFYING", asset.path, checked, total)
    progress("PREPARING", None, total, total)
    verify_graph(directory / "graph.npz", controls)
    verify_neurons(directory, controls)


@dataclass(frozen=True)
class VerifiedDataset:
    verification: ClassVar[str] = "PUBLISHER_SIGNED"
    directory: Path
    manifest: VerifiedManifest
    verified_at: datetime
    _issuer: object = field(repr=False)

    @property
    def total_bytes(self) -> int:
        return sum(asset.bytes for asset in self.manifest.manifest.files)

    @property
    def digest(self) -> str:
        return self.manifest.digest

    def assert_current(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if (self._issuer is not _VERIFIED_DATASET_ISSUER
                or not self.manifest.manifest.issued_at <= current < self.manifest.manifest.expires_at
                or current >= self.manifest.trust_expires_at
                or self.directory.name != self.manifest.digest):
            raise BootstrapError("DATASET_READINESS_INVALID_OR_EXPIRED")

    def path(self, name: str) -> Path:
        self.assert_current()
        if name not in {asset.path for asset in self.manifest.manifest.files}:
            raise BootstrapError("DATASET_PATH_NOT_ATTESTED")
        path = self.directory / name
        _assert_regular_path(path)
        if is_link(path.parent) or is_link(self.directory) or is_link(self.directory.parent):
            raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
        return path


class DatasetInstaller:
    def __init__(self, home: Path, trust_root: TrustRoot, *, downloader: Downloader | None = None) -> None:
        self.paths = ApplicationPaths.for_home(home)
        self.home = self.paths.root.parent.parent
        self.root = self.paths.signed_datasets
        self.trust_root = trust_root
        self.verifier = ManifestVerifier(trust_root)
        self.downloader = downloader

    def _prepare_root(self) -> None:
        self.paths.prepare()
        secure_directory(self.root)

    def _trusted_metadata(self, envelope: bytes | None, now: datetime) -> VerifiedManifest:
        path = self.root / ".trusted-state.json"
        version, digest, root_version = 0, None, 0
        if path.exists() or path.is_symlink():
            _assert_regular_path(path)
            state = strict_json(path.read_bytes())
            if (not isinstance(state, dict) or set(state) != {"version", "digest", "root_version"}
                    or type(state["version"]) is not int or state["version"] < 1
                    or type(state["root_version"]) is not int or state["root_version"] < 1
                    or not isinstance(state["digest"], str) or len(state["digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in state["digest"])):
                raise BootstrapError("ROLLBACK_STATE_INVALID")
            version, digest, root_version = state["version"], state["digest"], state["root_version"]
        if root_version > self.trust_root.version:
            raise BootstrapError("TRUST_ROOT_ROLLBACK_REJECTED")
        if envelope is None:
            if digest is None:
                raise BootstrapError("SIGNED_MANIFEST_MISSING")
            cached = self.root / "malecns-v1.0" / digest / ".manifest.json"
            if is_link(cached.parent) or is_link(cached.parent.parent):
                raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
            _assert_regular_path(cached)
            envelope = cached.read_bytes()
        verified = self.verifier.verify(envelope, now=now, minimum_version=version, pinned_digest=digest)
        atomic_write(path, canonical_json({"version": verified.manifest.version,
                                          "digest": verified.digest, "root_version": self.trust_root.version}))
        return verified

    def _result(self, directory: Path, manifest: VerifiedManifest, now: datetime) -> VerifiedDataset:
        finished = datetime.now(timezone.utc)
        if finished >= min(manifest.manifest.expires_at, manifest.trust_expires_at):
            raise BootstrapError("TRUST_METADATA_EXPIRED_DURING_VERIFICATION")
        return VerifiedDataset(directory, manifest, now, _VERIFIED_DATASET_ISSUER)

    def check(self, envelope: bytes | None, *, now: datetime,
              controls: InstallControls, progress: ProgressCallback) -> VerifiedDataset:
        self._prepare_root()
        with installation_lock(self.root):
            verified = self._trusted_metadata(envelope, now)
            final = self.root / verified.manifest.release / verified.digest
            if not final.is_dir():
                raise BootstrapError("DATASET_CACHE_MISSING")
            verify_dataset(final, verified, controls, progress)
            return self._result(final, verified, now)

    def install(self, envelope: bytes | None, *, now: datetime,
                controls: InstallControls, progress: ProgressCallback) -> VerifiedDataset:
        self._prepare_root()
        with installation_lock(self.root):
            verified = self._trusted_metadata(envelope, now)
            release = self.root / verified.manifest.release
            secure_directory(release)
            final = release / verified.digest
            if is_link(final):
                raise BootstrapError("DATA_DIRECTORY_SYMLINK_REJECTED")
            if final.exists():
                try:
                    verify_dataset(final, verified, controls, progress)
                    return self._result(final, verified, now)
                except BootstrapError as error:
                    if error.code in {"BOOTSTRAP_CANCELLED", "NEURON_READER_DEPENDENCY_MISSING"}:
                        raise
            staging_root = self.root / ".staging"
            secure_directory(staging_root)
            staging = staging_root / verified.digest
            secure_directory(staging)
            secure_directory(staging / "normalized")
            total = sum(asset.bytes for asset in verified.manifest.files)
            remaining = sum(max(0, asset.bytes - (staging / asset.path).stat().st_size)
                            if (staging / asset.path).is_file() else asset.bytes
                            for asset in verified.manifest.files)
            if shutil.disk_usage(staging).free < remaining + MAX_EXPANDED_GRAPH_BYTES:
                raise BootstrapError("DATASET_DISK_BUDGET_INSUFFICIENT")
            downloader = self.downloader or HTTPSDownloader()
            completed = 0
            for asset in verified.manifest.files:
                controls.checkpoint()
                target = staging / asset.path
                partial = target.with_name(target.name + ".partial")
                _assert_regular_path(target, missing_ok=True)
                _assert_regular_path(partial, missing_ok=True)
                if target.exists() and target.stat().st_size == asset.bytes and file_sha256(target, controls) == asset.sha256:
                    completed += asset.bytes
                    continue
                if target.exists():
                    target.unlink()
                download_with_retries(downloader, asset, partial, controls, progress,
                                      completed=completed, total=total)
                if partial.stat().st_size != asset.bytes or file_sha256(partial, controls) != asset.sha256:
                    partial.unlink(missing_ok=True)
                    raise BootstrapError("DOWNLOADED_DATA_DIGEST_OR_LENGTH_MISMATCH")
                os.replace(partial, target)
                completed += asset.bytes
            verify_dataset(staging, verified, controls, progress)
            controls.checkpoint()
            if datetime.now(timezone.utc) >= verified.manifest.expires_at:
                raise BootstrapError("MANIFEST_EXPIRED_DURING_INSTALL")
            atomic_write(staging / ".manifest.json", verified.signed_envelope)
            if final.exists():
                quarantine = release / (verified.digest + ".corrupt-" + uuid.uuid4().hex)
                os.replace(final, quarantine)
            os.replace(staging, final)
            _fsync_directory(release)
            return self._result(final, verified, now)
