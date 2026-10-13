"""Native credential persistence with no file, environment, or CLI fallback.

The production factory loads only a release-pinned bridge from this package.
The native bridge checks the signed worker identity before every vault operation.
Bounded Python buffers are wiped best-effort; this is not a claim that Python or
OpenSSL makes all temporary secret material non-exportable or locked in RAM.
"""

import ctypes
import platform
import sys
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator, Protocol

from coinfly.domain.context import ContextKey, DomainError
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose, CredentialReference
from coinfly.security.release_gates import NATIVE_BRIDGE_SHA256

MAX_SECRET_BYTES = 2400  # Below Windows Credential Manager's 2560-byte blob limit.
_DIGEST_BYTES = 32


class NativeVaultBridge(Protocol):
    """Internal worker-owned native bridge, injected only by trusted composition."""

    def check_identity(self) -> None: ...
    def put(self, binding_id: str, metadata_digest: bytes, secret: bytearray) -> None: ...
    def read(self, binding_id: str, metadata_digest: bytes) -> bytearray: ...
    def delete(self, binding_id: str, metadata_digest: bytes) -> None: ...


def wipe(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


class _CtypesVaultBridge:
    def __init__(self, binary: Path) -> None:
        self._library = ctypes.CDLL(str(binary))
        byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
        self._library.coinfly_vault_check_identity.argtypes = []
        self._library.coinfly_vault_check_identity.restype = ctypes.c_int
        self._library.coinfly_vault_put.argtypes = [
            ctypes.c_char_p,
            byte_pointer,
            byte_pointer,
            ctypes.c_size_t,
        ]
        self._library.coinfly_vault_read.argtypes = [
            ctypes.c_char_p,
            byte_pointer,
            byte_pointer,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._library.coinfly_vault_delete.argtypes = [ctypes.c_char_p, byte_pointer]
        for name in ("put", "read", "delete"):
            getattr(self._library, f"coinfly_vault_{name}").restype = ctypes.c_int
        self.check_identity()

    @staticmethod
    def _status(result: int) -> None:
        if result != 0:
            # Never propagate native payloads, target names, key IDs, or values.
            raise DomainError("KEYSTORE_UNAVAILABLE" if result != -2 else "CREDENTIAL_REALM_MISMATCH")

    def check_identity(self) -> None:
        if self._library.coinfly_vault_check_identity() != 0:
            raise DomainError("NATIVE_SIGNING_UNCERTIFIED")

    @staticmethod
    def _metadata(value: bytes) -> ctypes.Array[ctypes.c_ubyte]:
        if len(value) != _DIGEST_BYTES:
            raise DomainError("CREDENTIAL_METADATA_INVALID")
        return (ctypes.c_ubyte * _DIGEST_BYTES).from_buffer_copy(value)

    def put(self, binding_id: str, metadata_digest: bytes, secret: bytearray) -> None:
        self.check_identity()
        source = (ctypes.c_ubyte * len(secret)).from_buffer(secret)
        self._status(
            self._library.coinfly_vault_put(
                binding_id.encode("ascii"), self._metadata(metadata_digest), source, len(secret)
            )
        )

    def read(self, binding_id: str, metadata_digest: bytes) -> bytearray:
        self.check_identity()
        buffer = bytearray(MAX_SECRET_BYTES)
        output = (ctypes.c_ubyte * MAX_SECRET_BYTES).from_buffer(buffer)
        size = ctypes.c_size_t(MAX_SECRET_BYTES)
        try:
            self._status(
                self._library.coinfly_vault_read(
                    binding_id.encode("ascii"), self._metadata(metadata_digest), output, ctypes.byref(size)
                )
            )
            if not 0 < size.value <= MAX_SECRET_BYTES:
                raise DomainError("KEYSTORE_PAYLOAD_INVALID")
            result = buffer[: size.value]
            return result
        finally:
            wipe(buffer)

    def delete(self, binding_id: str, metadata_digest: bytes) -> None:
        self.check_identity()
        self._status(
            self._library.coinfly_vault_delete(binding_id.encode("ascii"), self._metadata(metadata_digest))
        )


class NativeKeystore:
    """No public unscoped get/export. Enroll stage→verify→activate in the worker.

    SQL stores CredentialReference only. If verification/receipt persistence fails,
    a staged native item remains inactive; recovery can reverify or discard it.
    Active state is checked from the caller's durable reference on every borrow.
    """

    def __init__(self, bridge: NativeVaultBridge) -> None:
        self._bridge = bridge
        self._lock = threading.RLock()
        self._retired: set[str] = set()
        self._bridge.check_identity()

    def stage(self, metadata: CredentialMetadata, secret: bytearray) -> CredentialReference:
        if not 0 < len(secret) <= MAX_SECRET_BYTES:
            wipe(secret)
            raise DomainError("CREDENTIAL_SIZE_INVALID")
        try:
            with self._lock:
                self._bridge.check_identity()
                self._bridge.put(metadata.binding_id, metadata.digest, secret)
                return CredentialReference(metadata=metadata, active=False)
        finally:
            wipe(secret)

    def verify_staged(
        self, reference: CredentialReference, verifier: "SecretVerifier"
    ) -> CredentialReference:
        with self._lock:
            self._bridge.check_identity()
            secret = self._bridge.read(reference.metadata.binding_id, reference.metadata.digest)
            try:
                verifier.verify(reference.metadata, secret)
            finally:
                wipe(secret)
            return CredentialReference(metadata=reference.metadata, active=True)

    @contextmanager
    def borrow(
        self,
        reference: CredentialReference,
        context: ContextKey,
        *,
        portfolio_id: str,
        purpose: CredentialPurpose,
        version: int,
    ) -> Iterator[bytearray]:
        with self._lock:
            if not reference.active or context.identity in self._retired:
                raise DomainError("CREDENTIAL_INACTIVE")
            reference.metadata.require_match(
                context, portfolio_id=portfolio_id, purpose=purpose, version=version
            )
            self._bridge.check_identity()
            secret = self._bridge.read(reference.metadata.binding_id, reference.metadata.digest)
            try:
                yield secret
            finally:
                wipe(secret)

    def retire_context(self, context: ContextKey) -> None:
        with self._lock:
            self._retired.add(context.identity)

    def discard_staged(self, reference: CredentialReference) -> None:
        if reference.active:
            raise DomainError("ACTIVE_CREDENTIAL_DELETE_FORBIDDEN")
        with self._lock:
            self._bridge.delete(reference.metadata.binding_id, reference.metadata.digest)


class SecretVerifier(Protocol):
    def verify(self, metadata: CredentialMetadata, secret: bytearray) -> None: ...


def open_native_keystore() -> NativeKeystore:
    system = platform.system()
    machine = platform.machine().lower()
    if (system, machine) not in (("Darwin", "arm64"), ("Windows", "amd64"), ("Windows", "x86_64")):
        raise DomainError("NATIVE_KEYSTORE_UNSUPPORTED_PLATFORM")
    target = "macos-arm64" if system == "Darwin" else "windows-amd64"
    expected = NATIVE_BRIDGE_SHA256.get(target)
    if expected is None:
        raise DomainError("NATIVE_SIGNING_UNCERTIFIED")
    filename = "libcoinfly_vault.dylib" if system == "Darwin" else "coinfly_vault.dll"
    binary = _packaged_bridge(filename)
    if sha256(binary.read_bytes()).hexdigest() != expected:
        raise DomainError("NATIVE_BRIDGE_INTEGRITY_FAILED")
    return NativeKeystore(_CtypesVaultBridge(binary))


def _packaged_bridge(filename: str) -> Path:
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if not isinstance(bundle, str) or not Path(bundle).is_absolute():
            raise DomainError("NATIVE_BRIDGE_UNAVAILABLE")
        package_root = Path(bundle).resolve()
        # PyInstaller can relocate package resources through internal symlinks.
        # Resolve the binary from the bootloader root, never the cwd or PATH.
        candidate = package_root / "coinfly" / "security" / "_native" / filename
    else:
        package_root = Path(__file__).resolve().parent
        candidate = package_root / "_native" / filename
        if candidate.is_symlink():
            raise DomainError("NATIVE_BRIDGE_UNAVAILABLE")
    try:
        binary = candidate.resolve(strict=True)
        if not binary.is_relative_to(package_root) or not binary.is_file():
            raise DomainError("NATIVE_BRIDGE_UNAVAILABLE")
    except (OSError, RuntimeError):
        raise DomainError("NATIVE_BRIDGE_UNAVAILABLE") from None
    return binary
