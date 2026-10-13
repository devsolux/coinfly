"""Release-owned publisher metadata, independent of OS signing certificates.

Only the reviewed application package supplies a root. Never search the working
directory, user data, network or environment for runtime trust anchors.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field, ValidationError

from coinfly.domain.context import FrozenModel

from .manifest import (
    MAX_METADATA_BYTES,
    BootstrapError,
    ManifestVerifier,
    TrustedKey,
    TrustRoot,
    canonical_json,
    strict_json,
)

ROOT_FILENAME = "trust-root.json"
MANIFEST_FILENAME = "manifest.json"


class PublisherKey(FrozenModel):
    key_id: str
    public_key_hex: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublisherRoot(FrozenModel):
    version: int
    keys: tuple[PublisherKey, ...]
    threshold: int
    allowed_origins: tuple[str, ...]
    expires_at: datetime
    minimum_manifest_version: int = 1

    def trusted(self) -> TrustRoot:
        return TrustRoot(self.version,
                         tuple(TrustedKey(key.key_id, bytes.fromhex(key.public_key_hex)) for key in self.keys),
                         self.threshold, self.allowed_origins, self.expires_at, self.minimum_manifest_version)


def publisher_directory() -> Path:
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if not isinstance(bundle, str) or not Path(bundle).is_absolute():
            raise BootstrapError("PACKAGED_RESOURCE_ROOT_INVALID")
        return Path(bundle) / "coinfly" / "bootstrap" / "publisher"
    return Path(__file__).resolve().parent / "publisher"


def _read_metadata(directory: Path, name: str, missing: str) -> bytes:
    try:
        # Bound allocations even for corrupted package resources. PyInstaller
        # may use bundle-internal resource symlinks on macOS.
        with (directory / name).open("rb") as stream:
            payload = stream.read(MAX_METADATA_BYTES + 1)
    except FileNotFoundError:
        raise BootstrapError(missing) from None
    except OSError:
        raise BootstrapError("PUBLISHER_METADATA_UNREADABLE") from None
    if len(payload) > MAX_METADATA_BYTES:
        raise BootstrapError("METADATA_TOO_LARGE")
    return payload


def load_distribution(directory: Path | None = None, *,
                      now: datetime | None = None) -> tuple[TrustRoot, bytes]:
    """Validate packaged metadata; explicit paths are for trusted build tooling.

    This validates signatures and source locks, not downloaded data readiness or
    native-vault qualification. The installer still verifies every dataset byte.
    """
    directory = publisher_directory() if directory is None else directory.expanduser().resolve()
    from .development_distribution import is_development_distribution

    if is_development_distribution(directory):
        raise BootstrapError("DEVELOPMENT_DISTRIBUTION_ONLY")
    payload = _read_metadata(directory, ROOT_FILENAME, "PUBLISHER_TRUST_ROOT_MISSING")
    try:
        root = PublisherRoot.model_validate_json(canonical_json(strict_json(payload))).trusted()
    except (ValidationError, ValueError, TypeError):
        raise BootstrapError("TRUST_ROOT_INVALID") from None
    envelope = _read_metadata(directory, MANIFEST_FILENAME, "SIGNED_MANIFEST_MISSING")
    ManifestVerifier(root).verify(envelope, now=now or datetime.now(timezone.utc))
    return root, envelope
