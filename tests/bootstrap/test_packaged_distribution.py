"""Ephemeral publisher fixtures exercise packaging; no production trust is issued."""

import asyncio
import json
import sys

import pytest

from coinfly.bootstrap.distribution import load_distribution, publisher_directory
from coinfly.bootstrap.manager import BootstrapManager
from coinfly.bootstrap.manifest import BootstrapError
from coinfly.operations.qualification import _publisher_check
from tests.bootstrap.test_signed_manifests import (
    NOW,
    Ed25519PrivateKey,
    fixture_payload,
    fixture_root,
    sign,
)


def write_distribution(directory):
    directory.mkdir(parents=True)
    key = Ed25519PrivateKey.generate()
    root = fixture_root(key)
    metadata = {"version": root.version, "keys": [
        {"key_id": item.key_id, "public_key_hex": item.public_key.hex()} for item in root.keys],
        "threshold": root.threshold, "allowed_origins": list(root.allowed_origins),
        "expires_at": root.expires_at.isoformat(), "minimum_manifest_version": root.minimum_manifest_version}
    (directory / "trust-root.json").write_text(json.dumps(metadata))
    (directory / "manifest.json").write_bytes(sign(key, fixture_payload()))
    return directory


def test_frozen_metadata_is_absolute_and_independent_of_working_directory(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    directory = write_distribution(bundle / "coinfly/bootstrap/publisher")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.chdir(tmp_path)
    assert publisher_directory() == directory
    root, envelope = load_distribution(now=NOW)
    assert root.keys[0].key_id == "test-publisher"
    assert envelope == (directory / "manifest.json").read_bytes()
    assert _publisher_check().satisfied


def test_default_bootstrap_loads_package_metadata_before_streaming(tmp_path, monkeypatch):
    directory = write_distribution(tmp_path / "publisher")
    monkeypatch.setattr("coinfly.bootstrap.distribution.publisher_directory", lambda: directory)
    manager = BootstrapManager(tmp_path, clock=lambda: NOW)
    installer = manager._installer()
    assert manager.trust_root is not None
    assert manager.publisher_manifest == (directory / "manifest.json").read_bytes()
    assert installer is not None


@pytest.mark.parametrize("failure,code", [
    ("missing-root", "PUBLISHER_TRUST_ROOT_MISSING"),
    ("missing-manifest", "SIGNED_MANIFEST_MISSING"),
    ("tampered", "MANIFEST_SIGNATURE_INVALID"),
    ("expired", "PUBLISHER_TRUST_ROOT_EXPIRED"),
    ("extra-field", "TRUST_ROOT_INVALID"),
    ("duplicate-key", "METADATA_DUPLICATE_KEY"),
])
def test_broken_packaged_metadata_blocks_without_downloading(tmp_path, monkeypatch, failure, code):
    directory = write_distribution(tmp_path / "publisher")
    root = directory / "trust-root.json"
    manifest = directory / "manifest.json"
    if failure == "missing-root":
        root.unlink()
    elif failure == "missing-manifest":
        manifest.unlink()
    elif failure == "tampered":
        data = json.loads(manifest.read_bytes())
        data["signed"]["version"] += 1
        manifest.write_text(json.dumps(data))
    else:
        data = json.loads(root.read_bytes())
        if failure == "expired":
            data["expires_at"] = "2000-01-01T00:00:00Z"
        elif failure == "extra-field":
            data["private_key"] = "MUST_NOT_BE_EMBEDDED"
        root.write_text(json.dumps(data))
        if failure == "duplicate-key":
            root.write_text(root.read_text().replace('{"version": 1', '{"version": 2, "version": 1'))
    monkeypatch.setattr("coinfly.bootstrap.distribution.publisher_directory", lambda: directory)
    class NoNetwork:
        def download(self, *_args, **_kwargs):
            pytest.fail("Invalid publisher metadata reached the downloader")
    result = asyncio.run(BootstrapManager(tmp_path, downloader=NoNetwork(), clock=lambda: NOW).ensure())
    assert not result.ready and result.reason == code
    assert not _publisher_check().satisfied


def test_missing_bundle_metadata_never_uses_cwd_or_user_storage(tmp_path, monkeypatch):
    write_distribution(tmp_path / "coinfly/bootstrap/publisher")
    write_distribution(tmp_path / ".devsolux/coinfly")
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty-bundle"), raising=False)
    with pytest.raises(BootstrapError, match="PUBLISHER_TRUST_ROOT_MISSING"):
        load_distribution()
    monkeypatch.setattr(sys, "_MEIPASS", "relative")
    with pytest.raises(BootstrapError, match="PACKAGED_RESOURCE_ROOT_INVALID"):
        load_distribution()
