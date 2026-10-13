"""Resource resolution never replaces release pins or native identity checks."""

import hashlib

import pytest

from coinfly.domain.context import DomainError
from coinfly.security import keystore


def test_frozen_bridge_resolves_inside_bundle_and_rejects_external_link(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    directory = bundle / "coinfly/security/_native"
    directory.mkdir(parents=True)
    binary = directory / "libcoinfly_vault.dylib"
    binary.write_bytes(b"synthetic native fixture")
    monkeypatch.setattr(keystore.sys, "frozen", True, raising=False)
    monkeypatch.setattr(keystore.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.chdir(tmp_path)
    assert keystore._packaged_bridge(binary.name) == binary.resolve()
    binary.unlink()
    outside = tmp_path / "outside.dylib"
    outside.write_bytes(b"synthetic external fixture")
    binary.symlink_to(outside)
    with pytest.raises(DomainError, match="NATIVE_BRIDGE_UNAVAILABLE"):
        keystore._packaged_bridge(binary.name)


def test_frozen_path_does_not_bypass_qualification_or_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(keystore.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(keystore.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(keystore, "NATIVE_BRIDGE_SHA256", {})
    monkeypatch.setattr(keystore.sys, "frozen", True, raising=False)
    monkeypatch.setattr(keystore.sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(DomainError, match="NATIVE_SIGNING_UNCERTIFIED"):
        keystore.open_native_keystore()
    binary = tmp_path / "coinfly/security/_native/libcoinfly_vault.dylib"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"tampered fixture")
    monkeypatch.setattr(keystore, "NATIVE_BRIDGE_SHA256", {"macos-arm64": hashlib.sha256(b"expected").hexdigest()})
    with pytest.raises(DomainError, match="NATIVE_BRIDGE_INTEGRITY_FAILED"):
        keystore.open_native_keystore()
