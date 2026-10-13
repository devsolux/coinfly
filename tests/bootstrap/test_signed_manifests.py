from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from coinfly.bootstrap.installer import DatasetInstaller, InstallControls
from coinfly.bootstrap.manifest import (
    MANIFESTS,
    BootstrapError,
    ManifestVerifier,
    TrustedKey,
    TrustRoot,
    canonical_json,
)

NOW = datetime.now(timezone.utc)


def fixture_root(key: Ed25519PrivateKey) -> TrustRoot:
    return TrustRoot(1, (TrustedKey("test-publisher", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),),
                     1, ("https://storage.googleapis.com", "https://fixture.invalid"), NOW + timedelta(days=2))


def fixture_payload() -> dict:
    original = json.loads((MANIFESTS / "sources.lock.json").read_bytes())
    files = [{"path": name, **entry} for name, entry in original.items()]
    files += [{"path": name, "url": "https://fixture.invalid/" + name, "bytes": 100,
               "sha256": "a" * 64} for name in ["graph.npz", "normalized/neurons.feather"]]
    return {"schema_version": 1, "version": 1, "release": "malecns-v1.0",
            "model_profile": "coinfly-dual-compartment-v1", "issued_at": (NOW - timedelta(hours=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(), "files": files}


def sign(key: Ed25519PrivateKey, payload: dict) -> bytes:
    return canonical_json({"signed": payload, "signatures": [{
        "key_id": "test-publisher", "signature": base64.b64encode(key.sign(canonical_json(payload))).decode(),
    }]})


class SignedManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.root = fixture_root(self.key)
        self.verifier = ManifestVerifier(self.root)

    def test_signature_source_locks_and_complete_file_contract(self) -> None:
        payload = fixture_payload()
        result = self.verifier.verify(sign(self.key, payload), now=NOW)
        self.assertEqual(result.digest, hashlib.sha256(canonical_json(payload)).hexdigest())
        self.assertEqual(sum(asset.bytes for asset in result.manifest.files), 1109008294)

    def test_changed_signed_bytes_or_untrusted_key_fail(self) -> None:
        envelope = json.loads(sign(self.key, fixture_payload()))
        envelope["signed"]["version"] = 2
        with self.assertRaisesRegex(BootstrapError, "SIGNATURE_INVALID"):
            self.verifier.verify(canonical_json(envelope), now=NOW)
        with self.assertRaisesRegex(BootstrapError, "SIGNATURE_INVALID"):
            self.verifier.verify(sign(Ed25519PrivateKey.generate(), fixture_payload()), now=NOW)

    def test_original_sources_cannot_be_replaced_even_by_release_signer(self) -> None:
        payload = fixture_payload()
        payload["files"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(BootstrapError, "MANIFEST_CONTRACT_INVALID"):
            self.verifier.verify(sign(self.key, payload), now=NOW)

    def test_code_archives_and_incomplete_graphs_are_rejected(self) -> None:
        for modification in ["../kernel.so", "graph.pkl", "archive.zip", "normalized/../graph.npz"]:
            payload = fixture_payload()
            payload["files"][-1]["path"] = modification
            with self.subTest(path=modification), self.assertRaisesRegex(BootstrapError, "CONTRACT_INVALID"):
                self.verifier.verify(sign(self.key, payload), now=NOW)
        payload = fixture_payload()
        payload["files"].pop()
        with self.assertRaises(BootstrapError):
            self.verifier.verify(sign(self.key, payload), now=NOW)

    def test_no_unknown_origin_http_or_embedded_credentials(self) -> None:
        for url in ["https://untrusted.invalid/data", "http://fixture.invalid/data",
                    "https://user:password@fixture.invalid/data", "https://fixture.invalid:444/data"]:
            payload = fixture_payload()
            payload["files"][-1]["url"] = url
            with self.subTest(url=url), self.assertRaises(BootstrapError):
                self.verifier.verify(sign(self.key, payload), now=NOW)

    def test_expiry_rollback_and_equivocation(self) -> None:
        payload = fixture_payload()
        envelope = sign(self.key, payload)
        verified = self.verifier.verify(envelope, now=NOW)
        with self.assertRaisesRegex(BootstrapError, "ROLLBACK"):
            self.verifier.verify(envelope, now=NOW, minimum_version=2)
        with self.assertRaisesRegex(BootstrapError, "NOT_CURRENT"):
            self.verifier.verify(envelope, now=NOW + timedelta(days=1))
        payload["files"][-1]["sha256"] = "b" * 64
        with self.assertRaisesRegex(BootstrapError, "EQUIVOCATION"):
            self.verifier.verify(sign(self.key, payload), now=NOW, minimum_version=1,
                                 pinned_digest=verified.digest)

    def test_duplicate_signatures_do_not_meet_threshold(self) -> None:
        envelope = json.loads(sign(self.key, fixture_payload()))
        envelope["signatures"].append(copy.deepcopy(envelope["signatures"][0]))
        with self.assertRaisesRegex(BootstrapError, "DUPLICATE_SIGNATURE"):
            self.verifier.verify(canonical_json(envelope), now=NOW)

    def test_duplicate_json_keys_are_not_normalized_away(self) -> None:
        envelope = sign(self.key, fixture_payload()).replace(b'"version":1', b'"version":0,"version":1')
        with self.assertRaisesRegex(BootstrapError, "DUPLICATE_KEY"):
            self.verifier.verify(envelope, now=NOW)

    def test_verified_metadata_floor_survives_incomplete_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            installer = DatasetInstaller(home, self.root)
            payload = fixture_payload()
            payload["version"] = 2
            with self.assertRaisesRegex(BootstrapError, "CACHE_MISSING"):
                installer.check(sign(self.key, payload), now=NOW, controls=InstallControls(), progress=lambda *_: None)
            with self.assertRaisesRegex(BootstrapError, "ROLLBACK"):
                installer.check(sign(self.key, fixture_payload()), now=NOW, controls=InstallControls(),
                                progress=lambda *_: None)
            self.assertEqual(installer.root, home.resolve() / ".devsolux/coinfly/datasets/malecns/signed")
            self.assertFalse((installer.root / "malecns-v1.0").exists())


if __name__ == "__main__":
    unittest.main()
