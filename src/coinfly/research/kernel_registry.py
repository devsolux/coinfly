"""Legitimate native kernel verification workflows; no attestation is ever invented.

Two producers can attest ``libcoinfly_neural`` on this host:

* ``LocalDevelopmentKernelVerifier`` — the explicit, reviewed local build
  (``./dev.sh kernel`` → ``scripts/build_kernel.py``): CMake compiles the pinned
  ``kernel.cpp`` (source SHA-256 must equal ``KERNEL_SOURCE_SHA256``), the
  script records a development attestation JSON (binary hash, source hash,
  platform, compiler, code-signing identity or ``adhoc``, expiry) under
  ``models/kernels/`` and the worker verifies that record on every load.
  The resulting capability is ``LOCAL_DEVELOPMENT_KERNEL``: it enables the
  preserved controller for paper research and satisfies NO release gate.
* ``BundledKernelVerifier`` — a signed application bundle ships the dylib plus
  the attestation the release build wrote before signing; verification checks
  the bundle's code signature (``codesign --verify --strict``) and that the
  signing team matches the running bundle.

Application startup never compiles anything. A missing or stale attestation
is reported as ``NATIVE_KERNEL_UNAVAILABLE`` with the exact reason.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from coinfly.bootstrap.installer import file_sha256
from coinfly.bootstrap.manifest import BootstrapError
from coinfly.research.assets import (
    KERNEL_SOURCE_SHA256,
    NativeAttestation,
    VerifiedNativeKernel,
    verify_native_kernel,
)

ATTESTATION_SCHEMA_VERSION = 1
MAX_ATTESTATION_BYTES = 16 * 1024
DEVELOPMENT_IDENTITY_PREFIX = "local-development:"
LIBRARY_NAMES = {"darwin": "libcoinfly_neural.dylib", "win32": "coinfly_neural.dll", "linux": "libcoinfly_neural.so"}


def library_name() -> str:
    return LIBRARY_NAMES.get(sys.platform, "libcoinfly_neural.so")


def host_architecture() -> str:
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(machine, machine)


def _read_attestation(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_MISSING")
    if path.stat().st_size > MAX_ATTESTATION_BYTES:
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID") from None
    if not isinstance(document, dict) or document.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
    return document


def _attestation_from(document: dict[str, Any]) -> NativeAttestation:
    try:
        expires = datetime.fromisoformat(str(document["expires_at"]))
        return NativeAttestation(
            binary_sha256=str(document["binary_sha256"]), source_sha256=str(document["source_sha256"]),
            operating_system=str(document["operating_system"]), architecture=str(document["architecture"]),
            signed_identity=str(document["signed_identity"]), evidence_id=str(document["evidence_id"]), expires_at=expires,
        )
    except (KeyError, TypeError, ValueError):
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID") from None


class LocalDevelopmentKernelVerifier:
    """Reads the attestation ``scripts/build_kernel.py`` wrote beside the binary."""

    capability = "LOCAL_DEVELOPMENT_KERNEL"

    def attest(self, path: Path) -> NativeAttestation:
        document = _read_attestation(path.with_suffix(path.suffix + ".attestation.json"))
        attestation = _attestation_from(document)
        if not attestation.signed_identity.startswith(DEVELOPMENT_IDENTITY_PREFIX):
            raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
        if document.get("kind") != "local-development" or document.get("compiled_at_startup") is not False:
            raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
        return attestation


class BundledKernelVerifier:
    """Signed-bundle verification via the platform code-signing tool (macOS)."""

    capability = "BUNDLED_SIGNED_KERNEL"

    def __init__(self, bundle_root: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self._bundle_root = bundle_root.resolve()
        self._runner = runner or (lambda *args, **kwargs: subprocess.run(*args, **kwargs))

    def _team(self, path: Path) -> str:
        if shutil.which("codesign") is None:
            raise BootstrapError("NATIVE_KERNEL_SIGNATURE_UNVERIFIABLE")
        try:
            self._runner(["codesign", "--verify", "--strict", str(path)], check=True, capture_output=True, text=True, timeout=30)
            info = self._runner(["codesign", "-dv", "--verbose=2", str(path)], check=True, capture_output=True, text=True, timeout=30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            raise BootstrapError("NATIVE_KERNEL_SIGNATURE_INVALID") from None
        for line in (info.stderr + info.stdout).splitlines():
            if line.startswith("TeamIdentifier="):
                return line.split("=", 1)[1].strip()
        raise BootstrapError("NATIVE_KERNEL_SIGNATURE_INVALID")

    def attest(self, path: Path) -> NativeAttestation:
        resolved = path.resolve()
        if not resolved.is_relative_to(self._bundle_root):
            raise BootstrapError("NATIVE_KERNEL_FILE_INVALID")
        document = _read_attestation(resolved.with_suffix(resolved.suffix + ".attestation.json"))
        attestation = _attestation_from(document)
        if document.get("kind") != "signed-bundle":
            raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")
        team = self._team(resolved)
        if not team or attestation.signed_identity != f"developer-id:{team}":
            raise BootstrapError("NATIVE_KERNEL_SIGNATURE_INVALID")
        return attestation


def write_development_attestation(binary: Path, *, compiler: str, signed_identity: str = DEVELOPMENT_IDENTITY_PREFIX + "adhoc",
                                  validity: timedelta = timedelta(days=180), now: datetime | None = None) -> Path:
    """Called by the explicit build script only; never by application startup."""
    from uuid import uuid4

    if not signed_identity.startswith(DEVELOPMENT_IDENTITY_PREFIX):
        raise ValueError("development attestations carry a local-development identity")
    now = now or datetime.now(timezone.utc)
    document = {
        "schema_version": ATTESTATION_SCHEMA_VERSION, "kind": "local-development", "compiled_at_startup": False,
        "binary_sha256": file_sha256(binary), "source_sha256": KERNEL_SOURCE_SHA256,
        "operating_system": sys.platform, "architecture": host_architecture(), "signed_identity": signed_identity,
        "evidence_id": f"local-kernel-{uuid4().hex}", "built_at": now.isoformat(), "expires_at": (now + validity).isoformat(),
        "compiler": compiler[:200], "capability": LocalDevelopmentKernelVerifier.capability,
        "note": "Local research capability only. Satisfies no release gate and no live qualification.",
    }
    target = binary.with_suffix(binary.suffix + ".attestation.json")
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target


class KernelRegistry:
    """Locates and verifies the native kernel for this installation, on demand."""

    def __init__(self, kernels_directory: Path, *, bundle_root: Path | None = None,
                 verifier_factory: Callable[[Path], Any] | None = None) -> None:
        self._kernels = kernels_directory
        self._bundle_root = bundle_root
        self._verifier_factory = verifier_factory
        self._cached: VerifiedNativeKernel | None = None
        self._last_code = "NATIVE_KERNEL_UNAVAILABLE"
        self._capability: str | None = None

    def candidates(self) -> tuple[tuple[Path, Any], ...]:
        found: list[tuple[Path, Any]] = []
        name = library_name()
        if self._bundle_root is not None:
            from coinfly.bootstrap.development_distribution import is_development_distribution

            metadata = self._bundle_root / "coinfly" / "bootstrap" / "publisher"
            development = is_development_distribution(metadata)
            bundled = self._bundle_root / "coinfly" / "research" / "neural" / "_native" / name
            if bundled.is_file():
                # Exact package-owned development descriptors select a local
                # capability only. They never satisfy a publisher/live gate.
                if development:
                    found.append((bundled, LocalDevelopmentKernelVerifier()))
                else:
                    found.append((bundled, BundledKernelVerifier(self._bundle_root)))
            if not development:
                # Production must not turn a missing/invalid bundled kernel into
                # a local development capability (including unsigned production).
                return tuple(found)
        local = self._kernels / KERNEL_SOURCE_SHA256[:16] / name
        if local.is_file():
            verifier = self._verifier_factory(local) if self._verifier_factory else LocalDevelopmentKernelVerifier()
            found.append((local, verifier))
        return tuple(found)

    def verified(self) -> VerifiedNativeKernel | None:
        """Re-verify on every call; a changed binary or expired attestation fails closed."""
        codes: list[str] = []
        try:
            candidates = self.candidates()
        except BootstrapError as error:
            self._cached, self._capability, self._last_code = None, None, error.code
            return None
        for path, verifier in candidates:
            try:
                kernel = verify_native_kernel(path, verifier)
            except BootstrapError as error:
                codes.append(error.code)
                continue
            self._cached = kernel
            self._capability = getattr(verifier, "capability", "UNKNOWN")
            self._last_code = "NATIVE_KERNEL_VERIFIED"
            return kernel
        self._cached = None
        self._capability = None
        self._last_code = codes[0] if codes else "NATIVE_KERNEL_UNAVAILABLE"
        return None

    def readiness(self) -> tuple[bool, str]:
        kernel = self.verified()
        return kernel is not None, self._last_code

    @property
    def capability(self) -> str | None:
        return self._capability

    def describe(self) -> dict[str, Any]:
        ready, code = self.readiness()
        try:
            candidates = [str(path.name) for path, _ in self.candidates()]
        except BootstrapError:
            candidates = []  # readiness retains the precise invalid-metadata code.
        return {"ready": ready, "code": code, "capability": self._capability,
                "expected_source_sha256": KERNEL_SOURCE_SHA256,
                "candidates": candidates,
                "build_command": "./dev.sh kernel"}
