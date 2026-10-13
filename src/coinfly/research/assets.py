"""Explicit verified data and independently attested prebuilt native code."""

from __future__ import annotations

import ctypes
import hashlib
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from coinfly.bootstrap.installer import DatasetCapability, file_sha256
from coinfly.bootstrap.manifest import BootstrapError

KERNEL_SOURCE = Path(__file__).parent / "neural" / "kernel.cpp"
KERNEL_SOURCE_SHA256 = "e2d4d584f4633613277bb1bf6ea7ba20855c01a332ae2e9344ed7782604c8509"
_NATIVE_ISSUER = object()


@dataclass(frozen=True)
class NeuralPaths:
    dataset: DatasetCapability

    def __post_init__(self) -> None:
        self.dataset.assert_current()

    @property
    def graph(self) -> Path:
        return self.dataset.path("graph.npz")

    @property
    def annotations(self) -> Path:
        return self.dataset.path("annotations.feather")

    @property
    def neurons(self) -> Path:
        return self.dataset.path("normalized/neurons.feather")


@dataclass(frozen=True)
class NativeAttestation:
    binary_sha256: str
    source_sha256: str
    operating_system: str
    architecture: str
    signed_identity: str
    evidence_id: str
    expires_at: datetime


class NativeCodeVerifier(Protocol):
    """Trusted platform verifier, outside model IPC and user-editable configuration.

    Implementations verify the signed application identity and bundled artifact
    provenance on the actual native host. A digest alone is not an attestation.
    """

    def attest(self, path: Path) -> NativeAttestation: ...


@dataclass(frozen=True)
class VerifiedNativeKernel:
    path: Path
    attestation: NativeAttestation
    verifier: NativeCodeVerifier = field(repr=False)
    _issuer: object = field(repr=False)

    def load(self) -> ctypes.CDLL:
        if self._issuer is not _NATIVE_ISSUER:
            raise BootstrapError("NATIVE_KERNEL_UNCERTIFIED")
        # Re-attest at the actual native load boundary, not only at construction.
        current = verify_native_kernel(self.path, self.verifier)
        if current.attestation != self.attestation:
            raise BootstrapError("NATIVE_KERNEL_ATTESTATION_CHANGED")
        return ctypes.CDLL(str(self.path))


def verify_native_kernel(path: Path, verifier: NativeCodeVerifier | None = None) -> VerifiedNativeKernel:
    if verifier is None:
        raise BootstrapError("NATIVE_KERNEL_UNCERTIFIED")
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("NATIVE_KERNEL_FILE_INVALID")
    if hashlib.sha256(KERNEL_SOURCE.read_bytes()).hexdigest() != KERNEL_SOURCE_SHA256:
        raise BootstrapError("NATIVE_KERNEL_SOURCE_CHANGED")
    attestation = verifier.attest(path)
    expected_arch = {"darwin": "arm64", "win32": "amd64", "linux": "amd64"}.get(sys.platform)
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    if machine == "x86_64":
        machine = "amd64"
    if expected_arch is None or machine != expected_arch:
        raise BootstrapError("NATIVE_KERNEL_PLATFORM_UNQUALIFIED")
    if (attestation.operating_system != sys.platform or attestation.architecture != expected_arch
            or attestation.source_sha256 != KERNEL_SOURCE_SHA256
            or not attestation.signed_identity or not attestation.evidence_id
            or attestation.expires_at.tzinfo is None
            or datetime.now(timezone.utc) >= attestation.expires_at
            or file_sha256(path) != attestation.binary_sha256):
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
    return VerifiedNativeKernel(path.resolve(), attestation, verifier, _NATIVE_ISSUER)
