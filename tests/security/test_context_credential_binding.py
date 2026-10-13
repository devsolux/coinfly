import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from coinfly.domain.context import ContextKey, DomainError, Environment, ProductTrack
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose
from coinfly.security.keystore import NativeKeystore, open_native_keystore
from coinfly.security.signing_gateway import ES256SecretVerifier, SigningGateway, SigningRequest
from coinfly.venues.coinbase.capability_handshake import KeyPermissions, PermissionEvidence
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


class SyntheticVault:
    """No production fallback. Synthetic credentials remain in this test process."""

    def __init__(self):
        self.items = {}
        self.reads = 0
        self.locked = False
        self.last_borrow = None

    def check_identity(self):
        if self.locked:
            raise DomainError("KEYSTORE_UNAVAILABLE")

    def put(self, binding_id, metadata_digest, secret):
        if binding_id in self.items:
            raise DomainError("CREDENTIAL_ALREADY_EXISTS")
        self.items[binding_id] = (metadata_digest, bytes(secret))

    def read(self, binding_id, metadata_digest):
        self.reads += 1
        expected, secret = self.items[binding_id]
        if expected != metadata_digest:
            raise DomainError("CREDENTIAL_REALM_MISMATCH")
        self.last_borrow = bytearray(secret)
        return self.last_borrow

    def delete(self, binding_id, metadata_digest):
        if self.items[binding_id][0] != metadata_digest:
            raise DomainError("CREDENTIAL_REALM_MISMATCH")
        del self.items[binding_id]


class Authority:
    def __init__(self):
        self.lock = RLock()
        self.allowed = True
        self.draining = False
        self.calls = 0

    def require_authorized(self, request, *, now):
        self.calls += 1
        if not self.allowed or (self.draining and not request.drain_only):
            raise DomainError("AUTHORITY_FENCED")


    def run_at_send_boundary(self, request, invoke):
        with self.lock:
            self.require_authorized(request, now=NOW)
            return invoke()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.context = ContextKey(
            authority_id="owner",
            environment=Environment.LIVE,
            product_track=ProductTrack.SPOT,
            broker_profile_id="profile",
            epoch=8,
        )
        self.metadata = CredentialMetadata(
            binding_id="a" * 32,
            environment=Environment.LIVE,
            verified_profile_id="profile",
            authority_id="owner",
            portfolio_id="portfolio",
            purpose=CredentialPurpose.EXECUTION,
            version=1,
        )
        self.bridge = SyntheticVault()
        self.keystore = NativeKeystore(self.bridge)
        generated = ec.generate_private_key(ec.SECP256R1())
        pem = generated.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        self.secret = bytearray(
            json.dumps({"key_name": "organizations/test/apiKeys/test", "private_key": pem}).encode()
        )
        self.staged = self.keystore.stage(self.metadata, self.secret)
        self.active = self.keystore.verify_staged(self.staged, ES256SecretVerifier())
        self.permissions = PermissionEvidence(
            binding_id=self.metadata.binding_id,
            credential_version=1,
            permissions=KeyPermissions(
                can_view=True,
                can_trade=True,
                can_transfer=False,
                portfolio_uuid="portfolio",
                portfolio_type="CONSUMER",
            ),
            observed_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            response_sha256="1" * 64,
        )
        self.authority = Authority()
        self.gateway = SigningGateway(
            context=self.context,
            endpoint=endpoint_profile("coinbase-advanced-spot"),
            credential=self.active,
            profile_version=1,
            permissions=self.permissions,
            keystore=self.keystore,
            authority=self.authority,
            clock=lambda: NOW,
        )

    def request(self, **updates):
        values = dict(
            context=self.context,
            endpoint_profile_id="coinbase-advanced-spot",
            method="GET",
            path="/accounts",
            purpose=CredentialPurpose.EXECUTION,
            credential_version=1,
            profile_version=1,
            request_id="b" * 32,
        )
        values.update(updates)
        return SigningRequest(**values)

    def test_enrollment_and_borrow_buffers_are_wiped(self):
        self.assertTrue(all(value == 0 for value in self.secret))
        self.assertTrue(all(value == 0 for value in self.bridge.last_borrow))
        with self.keystore.borrow(
            self.active,
            self.context,
            portfolio_id="portfolio",
            purpose=CredentialPurpose.EXECUTION,
            version=1,
        ) as secret:
            self.assertIn(b"PRIVATE KEY", secret)
        self.assertTrue(all(value == 0 for value in secret))

    def test_sandbox_and_wrong_purpose_do_not_read_native_secret(self):
        reads = self.bridge.reads
        sandbox = self.context.model_copy(update={"environment": Environment.SANDBOX})
        for context, purpose in (
            (sandbox, CredentialPurpose.EXECUTION),
            (self.context, CredentialPurpose.OBSERVATION),
        ):
            with self.subTest(context=context, purpose=purpose):
                with self.assertRaisesRegex(DomainError, "CREDENTIAL_REALM_MISMATCH"):
                    with self.keystore.borrow(
                        self.active, context, portfolio_id="portfolio", purpose=purpose, version=1
                    ):
                        self.fail("realm must never reach a secret")
        self.assertEqual(reads, self.bridge.reads)

    def test_staged_item_cannot_sign_and_metadata_is_native_bound(self):
        with self.assertRaisesRegex(DomainError, "CREDENTIAL_INACTIVE"):
            with self.keystore.borrow(
                self.staged,
                self.context,
                portfolio_id="portfolio",
                purpose=CredentialPurpose.EXECUTION,
                version=1,
            ):
                pass
        changed = self.metadata.model_copy(update={"portfolio_id": "other"})
        with self.assertRaisesRegex(DomainError, "CREDENTIAL_REALM_MISMATCH"):
            self.bridge.read(changed.binding_id, changed.digest)

    def test_final_send_revalidates_a_post_sign_fence(self):
        request = self.request()
        authorization = self.gateway.sign(request)
        self.assertNotIn(authorization.token, repr(authorization))
        self.authority.allowed = False
        with self.assertRaisesRegex(DomainError, "AUTHORITY_FENCED"):
            self.gateway.send(request, authorization, lambda: None)

    def test_final_send_rechecks_native_vault_access(self):
        request = self.request()
        authorization = self.gateway.sign(request)
        self.bridge.locked = True
        with self.assertRaisesRegex(DomainError, "KEYSTORE_UNAVAILABLE"):
            self.gateway.send(request, authorization, lambda: None)

    def test_token_is_one_use_and_bound_to_query(self):
        request = self.request(query=(("limit", "10"),))
        authorization = self.gateway.sign(request)
        changed = self.request(query=(("limit", "250"),))
        with self.assertRaisesRegex(DomainError, "SIGNED_REQUEST_MISMATCH"):
            self.gateway.send(changed, authorization, lambda: None)
        self.gateway.send(request, authorization, lambda: None)
        with self.assertRaisesRegex(DomainError, "SIGNED_REQUEST_REPLAY"):
            self.gateway.send(request, authorization, lambda: None)
        with self.assertRaisesRegex(DomainError, "SIGNING_REQUEST_ALREADY_ISSUED"):
            self.gateway.sign(request)

    def test_retirement_blocks_delayed_callback_and_vault_reads(self):
        request = self.request()
        authorization = self.gateway.sign(request)
        reads = self.bridge.reads
        self.gateway.retire()
        for action in (
            lambda: self.gateway.sign(request),
            lambda: self.gateway.send(request, authorization, lambda: None),
        ):
            with self.assertRaisesRegex(DomainError, "SIGNER_CONTEXT_RETIRED"):
                action()
        self.assertEqual(reads, self.bridge.reads)

    def test_old_drain_only_grant_and_locked_vault(self):
        self.authority.draining = True
        with self.assertRaisesRegex(DomainError, "AUTHORITY_FENCED"):
            self.gateway.sign(self.request())
        self.gateway.sign(self.request(drain_only=True))
        self.bridge.locked = True
        with self.assertRaisesRegex(DomainError, "KEYSTORE_UNAVAILABLE"):
            self.gateway.sign(self.request(drain_only=True, request_id="c" * 32))

    def test_caller_cannot_rebind_token_by_replacing_authorization_metadata(self):
        request = self.request(query=(("limit", "10"),))
        authorization = self.gateway.sign(request)
        changed = self.request(query=(("limit", "250"),))
        sent = []
        with self.assertRaisesRegex(DomainError, "SIGNED_REQUEST_MISMATCH"):
            self.gateway.send(changed, replace(authorization, request_digest=changed.digest),
                              lambda: sent.append(True))
        with self.assertRaisesRegex(DomainError, "SIGNED_REQUEST_MISMATCH"):
            self.gateway.send(request, replace(authorization, issued_at=NOW - timedelta(seconds=1)),
                              lambda: sent.append(True))
        self.assertEqual(sent, [])
        self.gateway.send(request, authorization, lambda: sent.append(True))
        self.assertEqual(sent, [True])

    def test_failed_invocation_consumes_token_without_replay(self):
        request = self.request()
        authorization = self.gateway.sign(request)
        def invoke():
            raise TimeoutError("synthetic ambiguous send")
        with self.assertRaises(TimeoutError):
            self.gateway.send(request, authorization, invoke)
        with self.assertRaisesRegex(DomainError, "SIGNED_REQUEST_REPLAY"):
            self.gateway.send(request, authorization, lambda: self.fail("must not retry"))

    def test_http_invocation_serializes_with_authority_fence_and_retirement(self):
        from coinfly.venues.coinbase.http_reader import BoundHttpsReader
        from tests.venues.test_http_boundary import Connection, Response

        entered, release, fencing, retired = Event(), Event(), Event(), Event()
        class PausedConnection(Connection):
            def request(self, method, target, *, headers):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test send was not released")

            def getresponse(self):
                return Response()

        def fence():
            fencing.set()
            with self.authority.lock:
                self.authority.allowed = False
                self.gateway.retire()
            retired.set()

        with patch("coinfly.venues.coinbase.http_reader.HTTPSConnection", PausedConnection):
            reader = BoundHttpsReader(endpoint_profile("coinbase-advanced-spot"), signer=self.gateway)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(reader.get, "/accounts")
                try:
                    self.assertTrue(entered.wait(3))
                    transition = pool.submit(fence)
                    self.assertTrue(fencing.wait(3))
                    self.assertFalse(retired.wait(.05))
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=3)["accounts"], [])
                transition.result(timeout=3)
            with self.assertRaisesRegex(DomainError, "AUTHORITY_FENCED"):
                reader.get("/accounts")

    def test_sandbox_reader_cannot_inherit_a_live_signer(self):
        from coinfly.venues.coinbase.http_reader import BoundHttpsReader

        reads = self.bridge.reads
        with self.assertRaisesRegex(DomainError, "HTTP_SIGNER_REALM_MISMATCH"):
            BoundHttpsReader(endpoint_profile("coinbase-static-sandbox"), signer=self.gateway)
        self.assertEqual(self.bridge.reads, reads)

    def test_linux_has_no_file_or_environment_fallback(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict("os.environ", {"COINBASE_API_KEY": "secret-canary"}),
        ):
            with self.assertRaisesRegex(DomainError, "NATIVE_KEYSTORE_UNSUPPORTED_PLATFORM"):
                open_native_keystore()

    def test_unsigned_native_release_cannot_load_a_binary(self):
        with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"):
            with self.assertRaisesRegex(DomainError, "NATIVE_SIGNING_UNCERTIFIED"):
                open_native_keystore()

    def test_wrong_curve_is_rejected_without_key_echo(self):
        generated = ec.generate_private_key(ec.SECP384R1())
        pem = generated.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        with self.assertRaisesRegex(DomainError, "ES256_KEY_INVALID") as caught:
            ES256SecretVerifier().verify(
                self.metadata,
                bytearray(
                    json.dumps({"key_name": "organizations/test/apiKeys/test", "private_key": pem}).encode()
                ),
            )
        self.assertNotIn(pem, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
