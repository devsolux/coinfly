"""Explicit, user-initiated, read-only Coinbase authentication check.

Sends ONE authenticated ``GET /api/v3/brokerage/accounts?limit=1`` (a documented
harmless read requiring only the ``view`` permission) with a request-bound ES256
JWT built from the desktop setup credentials. The response body is discarded
after a shape check; balances, account identifiers, the key and the token are
never logged, stored or returned.

A verified result means exactly: "Coinbase accepted this credential for this
read operation at this time." It does not establish trading permission, live
enrollment, regional eligibility, native signing or gateway certification, and
it cannot arm or unlock live execution. No order is previewed, placed, cancelled
or simulated. One bounded attempt per click; no automatic retry.
"""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPSConnection
from typing import Any, Protocol
from urllib.parse import urlsplit

from coinfly.domain.context import DomainError
from coinfly.security.coinbase_jwt import RequestJwt, build_request_jwt, load_es256_private_key, new_nonce
from coinfly.security.credential_realms import CredentialPurpose
from coinfly.venues import decimal_json
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile
from coinfly.venues.coinbase.http_reader import pinned_tls_context

VERIFICATION_PROFILE_ID = "coinbase-advanced-spot"
VERIFICATION_PATH = "/accounts"
VERIFICATION_QUERY = "limit=1"
VERIFICATION_OPERATION = "GET /api/v3/brokerage/accounts"
CLOCK_SKEW_SUSPECT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
CODES = frozenset({
    "COINBASE_AUTH_VERIFIED", "COINBASE_AUTH_REJECTED", "COINBASE_AUTH_FORBIDDEN", "COINBASE_RATE_LIMITED",
    "COINBASE_SERVER_ERROR", "COINBASE_REQUEST_REJECTED", "COINBASE_RESPONSE_MALFORMED",
    "COINBASE_CLOCK_SKEW_SUSPECTED", "HTTP_REDIRECT_FORBIDDEN", "NETWORK_DNS_FAILURE", "NETWORK_TLS_FAILURE",
    "NETWORK_TIMEOUT", "NETWORK_UNREACHABLE", "CONNECTION_TEST_CANCELLED", "COINBASE_AUTH_UNKNOWN_FAILURE",
    "CREDENTIALS_NOT_STORED", "CREDENTIALS_MISSING", "CREDENTIALS_INVALID", "COINBASE_KEY_FORMAT_INVALID",
    "COINBASE_SECRET_FORMAT_INVALID",
    "COINBASE_KEY_NOT_EC", "COINBASE_KEY_CURVE_UNSUPPORTED", "COINBASE_KEY_ED25519_UNSUPPORTED",
    "OS_KEYRING_UNAVAILABLE", "OS_KEYRING_LOCKED_OR_DENIED", "KEYRING_VERIFICATION_FAILED",
})


class SetupCredentialReader(Protocol):
    def read_setup_pair(self) -> tuple[str, str]: ...


@dataclass(frozen=True, slots=True)
class HttpsRequest:
    """Everything the transport needs; ``bearer`` is the only secret and is not printed."""

    host: str
    target: str
    headers: Mapping[str, str]
    timeout: float

    def __repr__(self) -> str:
        return f"HttpsRequest(host={self.host!r}, target={self.target!r}, headers=<redacted>)"


@dataclass(frozen=True, slots=True)
class HttpsResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[HttpsRequest, threading.Event], HttpsResponse]


@dataclass(frozen=True, slots=True)
class ConnectionVerdict:
    """Redacted outcome safe for UI events, logs and IPC-free rendering."""

    code: str
    http_status: int | None
    checked_at: datetime
    operation: str = VERIFICATION_OPERATION
    clock_skew_seconds: float | None = None

    @property
    def verified(self) -> bool:
        return self.code == "COINBASE_AUTH_VERIFIED"

    def public_dict(self) -> dict[str, Any]:
        return {"code": self.code, "http_status": self.http_status, "checked_at": self.checked_at.isoformat(),
                "operation": self.operation, "clock_skew_seconds": self.clock_skew_seconds,
                "verified": self.verified, "authorizes_live_execution": False}


def _classify_transport_error(error: BaseException) -> str:
    if isinstance(error, socket.gaierror):
        return "NETWORK_DNS_FAILURE"
    if isinstance(error, (ssl.SSLCertVerificationError, ssl.SSLError)):
        return "NETWORK_TLS_FAILURE"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "NETWORK_TIMEOUT"
    if isinstance(error, (ConnectionError, OSError)):
        return "NETWORK_UNREACHABLE"
    return "COINBASE_AUTH_UNKNOWN_FAILURE"


def https_transport(request: HttpsRequest, cancel: threading.Event) -> HttpsResponse:
    """Fresh connection, pinned CA, no proxy/netrc/cookies/redirect following."""
    connection = HTTPSConnection(request.host, timeout=request.timeout, context=pinned_tls_context())
    try:
        if cancel.is_set():
            raise DomainError("CONNECTION_TEST_CANCELLED")
        connection.request("GET", request.target, headers=dict(request.headers))
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        return HttpsResponse(response.status, {k.lower(): v for k, v in response.getheaders()}, body)
    finally:
        connection.close()


class CoinbaseConnectionTester:
    """Injected into the desktop as a narrow verifier; owns no worker or ledger state."""

    def __init__(self, credentials: SetupCredentialReader, *, transport: Transport = https_transport,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 nonce_source: Callable[[], str] = new_nonce, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not 0 < timeout <= 30:
            raise DomainError("HTTP_BUDGET_INVALID")
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._nonce_source = nonce_source
        self._timeout = timeout
        self._endpoint = endpoint_profile(VERIFICATION_PROFILE_ID)

    def verify_connection(self, cancel: threading.Event | None = None) -> ConnectionVerdict:
        cancel = cancel or threading.Event()
        checked_at = self._clock()
        try:
            return self._verify(cancel, checked_at)
        except DomainError as error:
            code = error.code if error.code in CODES else "COINBASE_AUTH_UNKNOWN_FAILURE"
            return ConnectionVerdict(code, None, checked_at)
        except BaseException as error:  # Never let library text carry material to the UI.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return ConnectionVerdict(_classify_transport_error(error), None, checked_at)

    def _verify(self, cancel: threading.Event, checked_at: datetime) -> ConnectionVerdict:
        key_name, secret = self._credentials.read_setup_pair()
        try:
            private_key = load_es256_private_key(secret)
        finally:
            secret = ""
        address = self._endpoint.validate("GET", VERIFICATION_PATH, purpose=CredentialPurpose.OBSERVATION)
        parsed = urlsplit(address)
        if parsed.hostname is None:
            raise DomainError("ENDPOINT_PROFILE_INVALID")
        if cancel.is_set():
            raise DomainError("CONNECTION_TEST_CANCELLED")
        token: RequestJwt = build_request_jwt(key_name=key_name, private_key=private_key, method="GET",
                                              host=parsed.hostname, path=parsed.path, now=checked_at,
                                              nonce=self._nonce_source())
        headers = {"Accept": "application/json", "User-Agent": "coinfly/1.0.0",
                   "Authorization": "Bearer " + token.token}
        request = HttpsRequest(parsed.hostname, f"{parsed.path}?{VERIFICATION_QUERY}", headers, self._timeout)
        try:
            response = self._transport(request, cancel)
        except DomainError:
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise DomainError(_classify_transport_error(error)) from None
        finally:
            headers.clear()
            del token
        if cancel.is_set():
            raise DomainError("CONNECTION_TEST_CANCELLED")
        return self._classify(response, checked_at)

    def _classify(self, response: HttpsResponse, checked_at: datetime) -> ConnectionVerdict:
        status = response.status
        skew = _clock_skew(response.headers.get("date"), checked_at)
        if status == 200:
            if len(response.body) > MAX_RESPONSE_BYTES:
                return ConnectionVerdict("COINBASE_RESPONSE_MALFORMED", status, checked_at, clock_skew_seconds=skew)
            try:
                data = decimal_json.loads(response.body)
            except DomainError:
                return ConnectionVerdict("COINBASE_RESPONSE_MALFORMED", status, checked_at, clock_skew_seconds=skew)
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                return ConnectionVerdict("COINBASE_RESPONSE_MALFORMED", status, checked_at, clock_skew_seconds=skew)
            # Discard account contents: only authentication status is needed.
            return ConnectionVerdict("COINBASE_AUTH_VERIFIED", status, checked_at, clock_skew_seconds=skew)
        if 300 <= status < 400:
            return ConnectionVerdict("HTTP_REDIRECT_FORBIDDEN", status, checked_at, clock_skew_seconds=skew)
        if status == 401:
            if skew is not None and abs(skew) > CLOCK_SKEW_SUSPECT_SECONDS:
                return ConnectionVerdict("COINBASE_CLOCK_SKEW_SUSPECTED", status, checked_at, clock_skew_seconds=skew)
            return ConnectionVerdict("COINBASE_AUTH_REJECTED", status, checked_at, clock_skew_seconds=skew)
        if status == 403:
            return ConnectionVerdict("COINBASE_AUTH_FORBIDDEN", status, checked_at, clock_skew_seconds=skew)
        if status == 429:
            return ConnectionVerdict("COINBASE_RATE_LIMITED", status, checked_at, clock_skew_seconds=skew)
        if status >= 500:
            return ConnectionVerdict("COINBASE_SERVER_ERROR", status, checked_at, clock_skew_seconds=skew)
        return ConnectionVerdict("COINBASE_REQUEST_REJECTED", status, checked_at, clock_skew_seconds=skew)


def _clock_skew(date_header: str | None, local: datetime) -> float | None:
    """Server minus local clock in seconds, from the RFC 9110 Date header when present."""
    if not date_header:
        return None
    try:
        remote = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return None
    if remote.tzinfo is None:
        remote = remote.replace(tzinfo=timezone.utc)
    return round((remote - local).total_seconds(), 3)
