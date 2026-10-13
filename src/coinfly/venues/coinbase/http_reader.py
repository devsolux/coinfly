"""GET-only HTTPS owner: explicit CA bundle, no proxies/netrc/redirects/replay."""

import asyncio
import socket
import ssl
import threading
from http.client import HTTPSConnection
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import certifi

from coinfly.domain.context import ContextKey, DomainError
from coinfly.network.context_transports import ContextTransports
from coinfly.security.signing_gateway import SigningGateway
from coinfly.venues import decimal_json
from coinfly.venues.coinbase.endpoint_profiles import EndpointProfile, require_registered


def pinned_tls_context() -> ssl.SSLContext:
    """TLS 1.2+ client context trusting only the pinned ``certifi`` bundle.

    Do not call create_default_context/load_default_certs: they may consult
    ambient SSL_CERT_FILE/SSL_CERT_DIR. The pinned package is the CA source.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=certifi.where())
    return context


# Documented query parameters per read route (2026-09-21). Values are strings;
# a route not listed accepts no query at all.
_QUERY_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "/accounts": frozenset({"limit", "cursor"}),
    "/orders/historical/batch": frozenset({"limit", "cursor", "product_ids", "order_status", "start_date", "end_date", "order_ids"}),
    "/orders/historical/fills": frozenset({"limit", "cursor", "product_ids", "order_ids", "start_sequence_timestamp",
                                           "end_sequence_timestamp"}),
}


class BoundHttpsReader:
    def __init__(
        self,
        endpoint: EndpointProfile,
        *,
        signer: SigningGateway | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        context: ContextKey | None = None,
        transports: ContextTransports | None = None,
    ) -> None:
        require_registered(endpoint)
        if endpoint.authenticated != (signer is not None):
            raise DomainError("HTTP_SIGNER_REALM_MISMATCH")
        if signer is not None:
            signer.require_endpoint(endpoint)
        if not 0 < timeout <= 30 or not 1024 <= max_response_bytes <= 8 * 1024 * 1024:
            raise DomainError("HTTP_BUDGET_INVALID")
        self._endpoint = endpoint
        self._signer = signer
        self._timeout = timeout
        self._max_bytes = max_response_bytes
        self._lifecycle_lock = threading.RLock()
        self._retired = False
        self._connections: set[HTTPSConnection] = set()
        self._tls = pinned_tls_context()
        if transports is not None:
            if context is None or context.environment != endpoint.environment or context.product_track != endpoint.product:
                raise DomainError("HTTP_CONTEXT_REALM_MISMATCH")
            if signer is not None:
                signer.require_context(context)
            transports.register(context, self)

    def _require_active(self) -> None:
        if self._retired:
            raise DomainError("NETWORK_CONTEXT_RETIRED")

    def retire(self) -> None:
        with self._lifecycle_lock:
            self._retired = True
            connections = tuple(self._connections)
        # Revoke tokens as well as sockets. No reader lock is held while taking
        # the signer's lock (send takes authority -> signer -> reader).
        failed = False
        if self._signer is not None:
            try:
                self._signer.retire()
            except Exception:
                failed = True
        for connection in connections:
            try:
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass  # A disconnected socket is already unable to send.
                connection.close()
            except Exception:
                failed = True
        if failed:
            raise DomainError("NETWORK_RETIREMENT_UNPROVEN")

    def get(self, path: str, *, query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        with self._lifecycle_lock:
            self._require_active()
        params = dict(query or {})
        allowed_query = _QUERY_ALLOWLIST.get(path, frozenset())
        if not set(params).issubset(allowed_query):
            raise DomainError("HTTP_QUERY_FORBIDDEN")
        if any(not isinstance(value, str) or len(value) > 4096 or not value for value in params.values()):
            raise DomainError("HTTP_QUERY_INVALID")
        if "limit" in params and (not params["limit"].isdigit() or not 1 <= int(params["limit"]) <= 250):
            raise DomainError("HTTP_QUERY_INVALID")
        request = self._signer.make_read_request(path=path, query=params) if self._signer else None
        purpose = request.purpose if request else None
        address = self._endpoint.validate("GET", path, purpose=purpose)
        parsed = urlsplit(address)
        if parsed.hostname is None:
            raise DomainError("ENDPOINT_PROFILE_INVALID")
        target = parsed.path + ("?" + urlencode(sorted(params.items())) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "coinfly/1.0.0"}
        authorization = self._signer.sign(request) if self._signer and request else None
        if authorization:
            headers["Authorization"] = "Bearer " + authorization.token
        # http.client has no proxy, netrc, cookie/session or redirect machinery.
        # A fresh connection per request cannot cross an environment handover.
        connection = HTTPSConnection(parsed.hostname, timeout=self._timeout, context=self._tls)
        try:
            def send() -> None:
                with self._lifecycle_lock:
                    self._require_active()
                    self._connections.add(connection)
                    connection.request("GET", target, headers=headers)

            if self._signer and request and authorization:
                self._signer.send(
                    request, authorization, send
                )
            else:
                send()
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise DomainError("HTTP_REDIRECT_FORBIDDEN")
            if response.status != 200:
                raise DomainError("HTTP_READ_FAILED")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise DomainError("HTTP_ENCODING_UNSUPPORTED")
            body = response.read(self._max_bytes + 1)
            if len(body) > self._max_bytes:
                raise DomainError("HTTP_RESPONSE_TOO_LARGE")
            data = decimal_json.loads(body)  # exact numbers: never a binary float
            if not isinstance(data, dict):
                raise DomainError("HTTP_RESPONSE_INVALID")
            with self._lifecycle_lock:
                self._require_active()  # Discard a late response from a retired realm.
                return data
        except DomainError:
            raise
        except Exception:
            raise DomainError("HTTP_READ_FAILED") from None
        finally:
            connection.close()
            with self._lifecycle_lock:
                self._connections.discard(connection)
            headers.clear()

    def read_key_permissions(self) -> Mapping[str, Any]:
        return self.get("/key_permissions")

    async def read_accounts_page(self, *, cursor: str | None, limit: int) -> Mapping[str, Any]:
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        return await asyncio.to_thread(self.get, "/accounts", query=params)


class BoundHttpsMutator(BoundHttpsReader):
    """Adds at-most-once POST initiation to the GET-only reader.

    A mutation is initiated once inside the signer's send boundary and never
    retried: any failure after initiation is reported as
    ``HTTP_WRITE_OUTCOME_UNKNOWN`` so the caller must reconcile from venue
    evidence instead of guessing. The body digest is bound *locally* — the
    signing request names the digest of the body this process will send, so a
    different body cannot ride on the signature — while the broker verifies its
    own JWT claims (method, host, path, expiry, nonce); the JWT carries no body
    hash, so the digest is worker-side evidence, not venue-side integrity. Only an EXECUTION credential on an
    authenticated endpoint that lists the POST route can construct one.
    """

    def __init__(self, endpoint: EndpointProfile, *, signer: SigningGateway, timeout: float = 10.0,
                 max_response_bytes: int = 2 * 1024 * 1024, context: ContextKey | None = None,
                 transports: ContextTransports | None = None) -> None:
        if not endpoint.authenticated or not any(method == "POST" for method, _ in endpoint.routes):
            raise DomainError("ENDPOINT_ROUTE_FORBIDDEN")
        super().__init__(endpoint, signer=signer, timeout=timeout, max_response_bytes=max_response_bytes,
                         context=context, transports=transports)

    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lifecycle_lock:
            self._require_active()
        if self._signer is None:
            raise DomainError("HTTP_SIGNER_REALM_MISMATCH")
        # Canonical, compact, sorted; a float in a request body is refused
        # (JSON_FLOAT_FORBIDDEN) and Decimals are written as documented strings.
        encoded = decimal_json.dumps(body, decimals="string").encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise DomainError("HTTP_BODY_TOO_LARGE")
        from hashlib import sha256

        request = self._signer.make_write_request(path=path, body_sha256=sha256(encoded).hexdigest())
        address = self._endpoint.validate("POST", path, purpose=request.purpose)
        parsed = urlsplit(address)
        if parsed.hostname is None:
            raise DomainError("ENDPOINT_PROFILE_INVALID")
        authorization = self._signer.sign(request)
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "coinfly/1.0.0",
                   "Authorization": "Bearer " + authorization.token}
        connection = HTTPSConnection(parsed.hostname, timeout=self._timeout, context=self._tls)
        initiated = False
        try:
            def send() -> None:
                nonlocal initiated
                with self._lifecycle_lock:
                    self._require_active()
                    self._connections.add(connection)
                    initiated = True
                    connection.request("POST", parsed.path, body=encoded, headers=headers)

            self._signer.send(request, authorization, send)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise DomainError("HTTP_REDIRECT_FORBIDDEN")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise DomainError("HTTP_ENCODING_UNSUPPORTED")
            raw = response.read(self._max_bytes + 1)
            if len(raw) > self._max_bytes:
                raise DomainError("HTTP_RESPONSE_TOO_LARGE")
            data = decimal_json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise DomainError("HTTP_RESPONSE_INVALID")
            with self._lifecycle_lock:
                self._require_active()
            return {"status": response.status, "body": data}
        except DomainError as error:
            if initiated and error.code not in ("HTTP_REDIRECT_FORBIDDEN",):
                raise DomainError("HTTP_WRITE_OUTCOME_UNKNOWN") from None
            raise
        except Exception:
            if initiated:
                raise DomainError("HTTP_WRITE_OUTCOME_UNKNOWN") from None
            raise DomainError("HTTP_WRITE_FAILED") from None
        finally:
            connection.close()
            with self._lifecycle_lock:
                self._connections.discard(connection)
            headers.clear()
