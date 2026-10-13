"""Strict, deployment-pinned Ed25519 metadata for data-only MaleCNS releases.

No publisher root or signed release is bundled. Provisioning a trusted root is
an independent release operation; a downloaded manifest cannot appoint its key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, ValidationError, field_validator, model_validator

from coinfly.domain.context import FrozenModel

MANIFESTS = Path(__file__).parent.parent / "research" / "manifests"
REQUIRED_FILES = frozenset({
    "annotations.feather", "neurotransmitters.feather", "edges.feather",
    "graph.npz", "normalized/neurons.feather",
})
MAX_METADATA_BYTES = 128 * 1024
MAX_DATA_BYTES = 8 * 1024**3


class BootstrapError(RuntimeError):
    """Stable, nonsensitive failure code suitable for a desktop projection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("METADATA_DUPLICATE_KEY")
        result[key] = value
    return result


def strict_json(payload: bytes) -> Any:
    if len(payload) > MAX_METADATA_BYTES:
        raise BootstrapError("METADATA_TOO_LARGE")
    try:
        return json.loads(payload, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise BootstrapError("METADATA_INVALID_JSON") from None


def https_origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise BootstrapError("DATA_ORIGIN_INVALID") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or port not in (None, 443)
            or any(ord(c) < 33 or ord(c) > 126 for c in url)):
        raise BootstrapError("DATA_ORIGIN_INVALID")
    return "https://" + parts.hostname.lower()


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: bytes

    def __post_init__(self) -> None:
        if (not isinstance(self.key_id, str) or type(self.public_key) is not bytes
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", self.key_id) or len(self.public_key) != 32):
            raise BootstrapError("TRUST_KEY_INVALID")
        Ed25519PublicKey.from_public_bytes(self.public_key)


@dataclass(frozen=True)
class TrustRoot:
    """Pinned by the signed application/deployment, never supplied by a dataset."""

    version: int
    keys: tuple[TrustedKey, ...]
    threshold: int
    allowed_origins: tuple[str, ...]
    expires_at: datetime
    minimum_manifest_version: int = 1

    def __post_init__(self) -> None:
        if (type(self.keys) is not tuple or type(self.allowed_origins) is not tuple
                or type(self.version) is not int or self.version < 1
                or type(self.threshold) is not int or not 1 <= self.threshold <= len(self.keys)
                or len({key.key_id for key in self.keys}) != len(self.keys)
                or type(self.minimum_manifest_version) is not int or self.minimum_manifest_version < 1
                or not self.allowed_origins or self.expires_at.tzinfo is None):
            raise BootstrapError("TRUST_ROOT_INVALID")
        if any(https_origin(origin) != origin for origin in self.allowed_origins):
            raise BootstrapError("TRUST_ORIGIN_INVALID")


class AssetSpec(FrozenModel):
    path: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=10, max_length=2048)
    bytes: int = Field(gt=0, le=MAX_DATA_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def data_only_path(cls, value: str) -> str:
        if value not in REQUIRED_FILES:
            raise ValueError("only the five certified raw/derived data files are permitted")
        return value


class DatasetManifest(FrozenModel):
    schema_version: Literal[1]
    version: int = Field(ge=1)
    release: Literal["malecns-v1.0"]
    model_profile: Literal["coinfly-dual-compartment-v1"]
    issued_at: datetime
    expires_at: datetime
    files: tuple[AssetSpec, ...]

    @field_validator("issued_at", "expires_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC-qualified timestamp required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def retained_profile(self) -> "DatasetManifest":
        if (len(self.files) != len(REQUIRED_FILES)
                or {asset.path for asset in self.files} != REQUIRED_FILES):
            raise ValueError("complete unmodified source and derived data set required")
        if self.expires_at <= self.issued_at or sum(asset.bytes for asset in self.files) > MAX_DATA_BYTES:
            raise ValueError("invalid release interval or data budget")
        original = json.loads((MANIFESTS / "sources.lock.json").read_bytes())
        for asset in self.files:
            if asset.path in original:
                expected = original[asset.path]
                if asset.sha256 != expected["sha256"] or asset.bytes != expected["bytes"]:
                    raise ValueError("original MaleCNS source digest and length must be preserved")
        return self


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: DatasetManifest
    digest: str
    signed_envelope: bytes
    root_version: int
    trust_expires_at: datetime


class ManifestVerifier:
    def __init__(self, trust_root: TrustRoot) -> None:
        self.trust_root = trust_root

    def verify(self, envelope: bytes, *, now: datetime,
               minimum_version: int = 0, pinned_digest: str | None = None) -> VerifiedManifest:
        root = self.trust_root
        if now.tzinfo is None or now.utcoffset() is None:
            raise BootstrapError("CLOCK_NOT_UTC_QUALIFIED")
        if now >= root.expires_at:
            raise BootstrapError("PUBLISHER_TRUST_ROOT_EXPIRED")
        raw = strict_json(envelope)
        if not isinstance(raw, dict) or set(raw) != {"signed", "signatures"}:
            raise BootstrapError("MANIFEST_ENVELOPE_INVALID")
        signed, signatures = raw["signed"], raw["signatures"]
        if not isinstance(signed, dict) or not isinstance(signatures, list) or len(signatures) > 32:
            raise BootstrapError("MANIFEST_ENVELOPE_INVALID")
        payload = canonical_json(signed)
        keys = {key.key_id: key.public_key for key in root.keys}
        accepted: set[str] = set()
        seen: set[str] = set()
        for signature in signatures:
            if (not isinstance(signature, dict) or set(signature) != {"key_id", "signature"}
                    or not isinstance(signature["key_id"], str)
                    or not isinstance(signature["signature"], str)):
                raise BootstrapError("MANIFEST_SIGNATURE_INVALID")
            key_id = signature["key_id"]
            if key_id in seen:
                raise BootstrapError("MANIFEST_DUPLICATE_SIGNATURE")
            seen.add(key_id)
            if key_id not in keys:
                continue
            try:
                sig = base64.b64decode(signature["signature"], validate=True)
                Ed25519PublicKey.from_public_bytes(keys[key_id]).verify(sig, payload)
            except (ValueError, InvalidSignature):
                raise BootstrapError("MANIFEST_SIGNATURE_INVALID") from None
            accepted.add(key_id)
        if len(accepted) < root.threshold:
            raise BootstrapError("MANIFEST_SIGNATURE_THRESHOLD_NOT_MET")
        try:
            manifest = DatasetManifest.model_validate_json(payload)
        except ValidationError:
            raise BootstrapError("MANIFEST_CONTRACT_INVALID") from None
        if not manifest.issued_at <= now < manifest.expires_at:
            raise BootstrapError("MANIFEST_NOT_CURRENT")
        floor = max(root.minimum_manifest_version, minimum_version)
        digest = hashlib.sha256(payload).hexdigest()
        if manifest.version < floor:
            raise BootstrapError("MANIFEST_ROLLBACK_REJECTED")
        if manifest.version == minimum_version and pinned_digest is not None and digest != pinned_digest:
            raise BootstrapError("MANIFEST_VERSION_EQUIVOCATION")
        for asset in manifest.files:
            if https_origin(asset.url) not in root.allowed_origins:
                raise BootstrapError("DATA_ORIGIN_NOT_ALLOWLISTED")
        return VerifiedManifest(manifest, digest, envelope, root.version, root.expires_at)
