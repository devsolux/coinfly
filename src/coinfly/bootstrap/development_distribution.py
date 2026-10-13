"""Package-owned development descriptors, never Ed25519 publisher authority.

The exact descriptors select the existing source-lock/paper composition. They
contain no invented dataset hashes, publisher keys, signatures or readiness.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from coinfly import __application_id__, __version__

from .development import lock_digest
from .distribution import MANIFEST_FILENAME, ROOT_FILENAME, _read_metadata, publisher_directory
from .manifest import REQUIRED_FILES, BootstrapError, canonical_json, strict_json


def development_documents() -> dict[str, dict[str, object]]:
    root: dict[str, object] = {
        "schema_version": 1,
        "distribution_kind": "local-development",
        "verification": "LOCAL_SOURCE_LOCKS",
        "source_locks_sha256": lock_digest(),
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "is_development_build": True,
        "application_version": __version__,
        "bundle_identifiers": {"desktop": __application_id__, "worker": f"{__application_id__}.worker"},
        "environment": "SANDBOX",
        "run_profile": "PAPER",
        "release": "malecns-v1.0",
        "model_profile": "coinfly-dual-compartment-v1",
        "dataset_files": sorted(REQUIRED_FILES),
        "root_sha256": hashlib.sha256(canonical_json(root)).hexdigest(),
    }
    return {ROOT_FILENAME: root, MANIFEST_FILENAME: manifest}


def is_development_distribution(directory: Path | None = None) -> bool:
    """Recognize and fully validate the sealed descriptor pair, or fail closed.

    Only a missing root or a different root kind defers to the publisher loader.
    Runtime callers never consult environment variables or mutable user metadata.
    """
    directory = publisher_directory() if directory is None else directory
    try:
        root = strict_json(_read_metadata(directory, ROOT_FILENAME, "PUBLISHER_TRUST_ROOT_MISSING"))
    except BootstrapError as error:
        if error.code == "PUBLISHER_TRUST_ROOT_MISSING":
            return False
        raise
    if not isinstance(root, dict) or root.get("distribution_kind") != "local-development":
        return False
    manifest = strict_json(_read_metadata(directory, MANIFEST_FILENAME, "SIGNED_MANIFEST_MISSING"))
    actual = {ROOT_FILENAME: root, MANIFEST_FILENAME: manifest}
    if canonical_json(actual) != canonical_json(development_documents()):
        raise BootstrapError("DEVELOPMENT_DISTRIBUTION_INVALID")
    return True
