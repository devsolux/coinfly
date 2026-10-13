"""Read-only Coinbase authentication check with a synthetic transport; no Internet, no orders."""

import json
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from coinfly.domain.context import DomainError
from coinfly.security.coinbase_jwt import decode_request_jwt
from coinfly.ui.credential_worker import CredentialWorker
from coinfly.ui.render_scheduler import RenderQueue
from coinfly.venues.coinbase import connection_test as module
from coinfly.venues.coinbase.connection_test import CoinbaseConnectionTester, HttpsResponse

KEY_NAME = "organizations/test-org/apiKeys/test-key"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
NONCE = "fedcba9876543210fedcba9876543210"
KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()


class Reader:
    def __init__(self, key=KEY_NAME, secret=PEM, error=None):
        self.key, self.secret, self.error = key, secret, error

    def read_setup_pair(self):
        if self.error:
            raise DomainError(self.error)
        return self.key, self.secret


class Transport:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, request, cancel):
        self.calls.append(request)
        self.headers = dict(request.headers)  # the tester clears its own copy afterwards
        if self.error is not None:
            raise self.error
        return self.response


def response(status, body=None, headers=None):
    payload = json.dumps(body).encode() if body is not None else b""
    return HttpsResponse(status, {"date": format_datetime(NOW, usegmt=True), **(headers or {})}, payload)


def make_tester(transport, reader=None, clock=lambda: NOW):
    return CoinbaseConnectionTester(reader or Reader(), transport=transport, clock=clock, nonce_source=lambda: NONCE)


def test_success_is_a_bound_read_only_get_and_discards_account_contents():
    transport = Transport(response(200, {"accounts": [{"uuid": "SECRET-ACCOUNT-ID", "available_balance": {"value": "9"}}],
                                        "has_next": False, "cursor": "", "size": 1}))
    verdict = make_tester(transport).verify_connection()
    assert verdict.code == "COINBASE_AUTH_VERIFIED" and verdict.verified and verdict.http_status == 200
    request = transport.calls[0]
    assert request.host == "api.coinbase.com" and request.target == "/api/v3/brokerage/accounts?limit=1"
    assert transport.headers["Accept"] == "application/json"
    assert request.headers == {}  # bearer material is dropped as soon as the transport returns
    token = transport.headers["Authorization"].removeprefix("Bearer ")
    header, claims = decode_request_jwt(token, KEY.public_key())
    assert header == {"alg": "ES256", "typ": "JWT", "kid": KEY_NAME, "nonce": NONCE}
    assert claims == {"sub": KEY_NAME, "iss": "cdp", "nbf": int(NOW.timestamp()), "exp": int(NOW.timestamp()) + 120,
                      "uri": "GET api.coinbase.com/api/v3/brokerage/accounts"}
    public = verdict.public_dict()
    assert public["authorizes_live_execution"] is False and public["operation"] == "GET /api/v3/brokerage/accounts"
    text = json.dumps(public) + repr(verdict) + repr(request)
    assert "SECRET-ACCOUNT-ID" not in text and token not in text and "BEGIN" not in text
    assert set(public) == {"code", "http_status", "checked_at", "operation", "clock_skew_seconds", "verified",
                           "authorizes_live_execution"}


@pytest.mark.parametrize("status,code", [
    (401, "COINBASE_AUTH_REJECTED"), (403, "COINBASE_AUTH_FORBIDDEN"), (429, "COINBASE_RATE_LIMITED"),
    (500, "COINBASE_SERVER_ERROR"), (503, "COINBASE_SERVER_ERROR"), (404, "COINBASE_REQUEST_REJECTED"),
    (302, "HTTP_REDIRECT_FORBIDDEN"),
])
def test_http_statuses_are_classified_without_body_contents(status, code):
    verdict = make_tester(Transport(response(status, {"error": "PRIVATE_DETAIL", "message": "x"}))).verify_connection()
    assert verdict.code == code and verdict.http_status == status and not verdict.verified
    assert "PRIVATE_DETAIL" not in json.dumps(verdict.public_dict())


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"accounts": "nope"}', b'{"other": []}'])
def test_malformed_success_bodies_are_not_verification(body):
    verdict = make_tester(Transport(HttpsResponse(200, {}, body))).verify_connection()
    assert verdict.code == "COINBASE_RESPONSE_MALFORMED" and verdict.http_status == 200 and not verdict.verified


def test_clock_skew_is_reported_and_suspected_only_with_a_rejection():
    skewed = {"date": format_datetime(NOW + timedelta(seconds=95), usegmt=True)}
    rejected = make_tester(Transport(HttpsResponse(401, skewed, b"{}"))).verify_connection()
    assert rejected.code == "COINBASE_CLOCK_SKEW_SUSPECTED" and rejected.clock_skew_seconds == 95.0
    accepted = make_tester(Transport(HttpsResponse(200, skewed, b'{"accounts": []}'))).verify_connection()
    assert accepted.code == "COINBASE_AUTH_VERIFIED" and accepted.clock_skew_seconds == 95.0
    small = make_tester(Transport(HttpsResponse(401, {"date": format_datetime(NOW + timedelta(seconds=5), usegmt=True)}, b""))).verify_connection()
    assert small.code == "COINBASE_AUTH_REJECTED"
    missing = make_tester(Transport(HttpsResponse(401, {"date": "garbage"}, b""))).verify_connection()
    assert missing.code == "COINBASE_AUTH_REJECTED" and missing.clock_skew_seconds is None


@pytest.mark.parametrize("error,code", [
    (socket.gaierror(8, "nodename nor servname provided"), "NETWORK_DNS_FAILURE"),
    (ssl.SSLCertVerificationError("certificate verify failed"), "NETWORK_TLS_FAILURE"),
    (ssl.SSLError("handshake"), "NETWORK_TLS_FAILURE"),
    (TimeoutError("timed out"), "NETWORK_TIMEOUT"),
    (socket.timeout("timed out"), "NETWORK_TIMEOUT"),
    (ConnectionRefusedError(), "NETWORK_UNREACHABLE"),
    (OSError(101, "Network is unreachable"), "NETWORK_UNREACHABLE"),
    (RuntimeError("library text with PRIVATE KEY fragment"), "COINBASE_AUTH_UNKNOWN_FAILURE"),
    (DomainError("CONNECTION_TEST_CANCELLED"), "CONNECTION_TEST_CANCELLED"),
])
def test_transport_failures_are_classified_and_never_retried(error, code):
    transport = Transport(error=error)
    verdict = make_tester(transport).verify_connection()
    assert verdict.code == code and verdict.http_status is None and len(transport.calls) == 1
    assert "PRIVATE KEY" not in json.dumps(verdict.public_dict())


@pytest.mark.parametrize("reader,code", [
    (Reader(error="CREDENTIALS_NOT_STORED"), "CREDENTIALS_NOT_STORED"),
    (Reader(error="CREDENTIALS_MISSING"), "CREDENTIALS_MISSING"),
    (Reader(error="OS_KEYRING_LOCKED_OR_DENIED"), "OS_KEYRING_LOCKED_OR_DENIED"),
    (Reader(error="OS_KEYRING_UNAVAILABLE"), "OS_KEYRING_UNAVAILABLE"),
    (Reader(error="CREDENTIALS_INVALID"), "CREDENTIALS_INVALID"),
    (Reader(key="https://attacker.invalid"), "COINBASE_KEY_FORMAT_INVALID"),
    (Reader(secret="-----BEGIN PRIVATE KEY-----\ncorrupt\n-----END PRIVATE KEY-----"), "COINBASE_SECRET_FORMAT_INVALID"),
    (Reader(secret=ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()), "COINBASE_KEY_ED25519_UNSUPPORTED"),
    (Reader(secret=ec.generate_private_key(ec.SECP384R1()).private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()), "COINBASE_KEY_CURVE_UNSUPPORTED"),
])
def test_local_credential_problems_fail_before_any_network_request(reader, code):
    transport = Transport(response(200, {"accounts": []}))
    verdict = make_tester(transport, reader).verify_connection()
    assert verdict.code == code and verdict.http_status is None and transport.calls == []


def test_cancellation_before_send_and_after_response_is_reported():
    cancel = threading.Event()
    cancel.set()
    transport = Transport(response(200, {"accounts": []}))
    assert make_tester(transport).verify_connection(cancel).code == "CONNECTION_TEST_CANCELLED"
    assert transport.calls == []

    class Late(Transport):
        def __call__(self, request, cancel):
            cancel.set()
            return super().__call__(request, cancel)

    assert make_tester(Late(response(200, {"accounts": []}))).verify_connection(threading.Event()).code == "CONNECTION_TEST_CANCELLED"


def test_verifier_uses_registered_live_read_endpoint_only(monkeypatch):
    with pytest.raises(DomainError, match="ENDPOINT_PROFILE_UNCERTIFIED"):
        monkeypatch.setattr(module, "VERIFICATION_PROFILE_ID", "coinbase-perpetual-fabricated")
        CoinbaseConnectionTester(Reader(), transport=Transport(response(200, {"accounts": []})))
    with pytest.raises(DomainError, match="HTTP_BUDGET_INVALID"):
        CoinbaseConnectionTester(Reader(), transport=Transport(), timeout=31)


def test_real_transport_is_blocked_by_the_test_network_boundary():
    # The default transport is the only path to the Internet; the suite-wide
    # socket guard converts any attempt into a classified failure, never a request.
    verdict = CoinbaseConnectionTester(Reader(), clock=lambda: NOW).verify_connection()
    assert verdict.code in {"NETWORK_DNS_FAILURE", "NETWORK_UNREACHABLE", "COINBASE_AUTH_UNKNOWN_FAILURE"}
    assert not verdict.verified


def test_ui_worker_reports_redacted_connection_status_separately_from_storage():
    queue = RenderQueue()
    events = []

    class Store:
        def save(self, key, secret):
            pass

        def status(self):
            return "SECURELY_STORED"

    release = threading.Event()

    class Blocking(Transport):
        def __call__(self, request, cancel):
            assert release.wait(2)
            return super().__call__(request, cancel)

    worker = CredentialWorker(queue, Store(), make_tester(Blocking(response(200, {"accounts": [{"uuid": "X"}]}))))
    try:
        assert worker.submit()
        for _ in range(200):
            events += queue.take()
            if events:
                break
            threading.Event().wait(0.01)
        assert events[-1].kind == "credentials" and events[-1].payload == {"code": "SECURELY_STORED"}
        assert worker.submit_connection_test()
        assert not worker.submit_connection_test() and not worker.submit()  # single flight
        assert worker.busy
        release.set()
        for _ in range(200):
            events += queue.take()
            if len(events) >= 2:
                break
            threading.Event().wait(0.01)
        payload = events[-1].payload
        assert events[-1].kind == "connection_test" and payload["code"] == "COINBASE_AUTH_VERIFIED"
        assert payload["verified"] is True and payload["authorizes_live_execution"] is False
        assert "X" not in json.dumps(dict(payload)) and "Bearer" not in repr(events)
        assert not worker.busy
    finally:
        worker.close()


def test_ui_worker_without_verifier_reports_unavailable():
    queue = RenderQueue()
    worker = CredentialWorker(queue, None, None)
    try:
        assert worker.submit_connection_test()
        events = ()
        for _ in range(200):
            events = queue.take()
            if events:
                break
            threading.Event().wait(0.01)
        assert events[0].payload["code"] == "CONNECTION_TEST_UNAVAILABLE" and events[0].payload["verified"] is False
    finally:
        worker.close()
