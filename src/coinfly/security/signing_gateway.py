"""Worker-owned ES256 request signing; no secret/session crosses to agents/UI."""

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Callable, Mapping, Protocol, TypeVar

from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import Field, SecretStr

from coinfly.domain.context import ContextKey, DomainError, FrozenModel
from coinfly.security.coinbase_jwt import (
    JWT_LIFETIME_SECONDS,
    build_request_jwt,
    load_es256_private_key,
    validate_key_name,
)
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose, CredentialReference
from coinfly.security.keystore import NativeKeystore
from coinfly.venues.coinbase.capability_handshake import PermissionEvidence
from coinfly.venues.coinbase.endpoint_profiles import EndpointProfile, require_registered

T = TypeVar("T")

MAX_PENDING_REQUESTS = 256
MAX_TRACKED_REQUESTS = 100000
_TOMBSTONE_RETENTION = timedelta(seconds=JWT_LIFETIME_SECONDS)


class _SecretPayload(FrozenModel):
    key_name: SecretStr
    private_key: SecretStr


def _load_key(secret: bytearray) -> tuple[str, ec.EllipticCurvePrivateKey]:
    """Certified-path loader: one canonical parser, one opaque failure code."""
    try:
        payload = _SecretPayload.model_validate_json(secret)
        key_name = payload.key_name.get_secret_value()
        if key_name != key_name.strip() or any(ord(char) < 33 or ord(char) > 126 for char in key_name):
            raise ValueError
        return validate_key_name(key_name), load_es256_private_key(payload.private_key.get_secret_value())
    except Exception:
        raise DomainError("ES256_KEY_INVALID") from None


class ES256SecretVerifier:
    """Local key-material validation only; remote permissions are a separate gate."""

    def verify(self, metadata: CredentialMetadata, secret: bytearray) -> None:
        if metadata.purpose not in (CredentialPurpose.EXECUTION, CredentialPurpose.OBSERVATION):
            raise DomainError("COINBASE_CREDENTIAL_PURPOSE_FORBIDDEN")
        _load_key(secret)


class SigningRequest(FrozenModel):
    context: ContextKey
    endpoint_profile_id: str
    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    body_sha256: str = Field(default=sha256(b"").hexdigest(), pattern=r"^[a-f0-9]{64}$")
    purpose: CredentialPurpose
    credential_version: int = Field(ge=1)
    profile_version: int = Field(ge=1)
    drain_only: bool = False
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class SigningAuthority(Protocol):
    """Trusted worker validates current evidence/lease/lifecycle, not model data."""

    def require_authorized(self, request: SigningRequest, *, now: datetime) -> None: ...

    def run_at_send_boundary(self, request: SigningRequest, invoke: Callable[[], T]) -> T:
        """Hold the authority's reentrant transition lock through invocation.

        The authority lock always precedes the signer's lock. Implementations
        must not defer ``invoke`` to a different thread or release the lock first.
        """
        ...


@dataclass(frozen=True, slots=True)
class SignedAuthorization:
    request_digest: str
    issued_at: datetime
    token: str = field(repr=False)


class SigningGateway:
    def __init__(
        self,
        *,
        context: ContextKey,
        endpoint: EndpointProfile,
        credential: CredentialReference,
        profile_version: int,
        permissions: PermissionEvidence,
        keystore: NativeKeystore,
        authority: SigningAuthority,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        require_registered(endpoint)
        self._context = context
        self._endpoint = endpoint
        self._credential = credential
        self._profile_version = profile_version
        self._permissions = permissions
        self._keystore = keystore
        self._authority = authority
        self._clock = clock
        self._retired = False
        self._lock = threading.RLock()
        self._issued: dict[str, tuple[str, datetime, str, str]] = {}
        # Spent request ids. A read id only has to outlive the token window (a
        # token older than that is refused anyway), so its tombstone carries a
        # retain-until time and is pruned once it passes. A mutation id is never
        # pruned (None): an order request is not silently signed twice.
        self._consumed: dict[str, datetime | None] = {}

    def require_endpoint(self, endpoint: EndpointProfile) -> None:
        require_registered(endpoint)
        if endpoint != self._endpoint:
            raise DomainError("HTTP_SIGNER_ENDPOINT_MISMATCH")

    def require_context(self, context: ContextKey) -> None:
        if context != self._context:
            raise DomainError("HTTP_SIGNER_CONTEXT_MISMATCH")

    def _validate(self, request: SigningRequest) -> datetime:
        if self._retired or request.context != self._context:
            raise DomainError("SIGNER_CONTEXT_RETIRED_OR_MISMATCH")
        if (
            request.endpoint_profile_id != self._endpoint.profile_id
            or request.profile_version != self._profile_version
        ):
            raise DomainError("SIGNER_PROFILE_MISMATCH")
        if (
            self._endpoint.environment != self._context.environment
            or self._endpoint.product != self._context.product_track
        ):
            raise DomainError("SIGNER_ENDPOINT_REALM_MISMATCH")
        self._endpoint.validate(request.method, request.path, purpose=request.purpose)
        self._credential.metadata.require_match(
            request.context,
            portfolio_id=self._credential.metadata.portfolio_id,
            purpose=request.purpose,
            version=request.credential_version,
        )
        now = self._clock()
        self._permissions.require_current(self._credential.metadata, now=now)
        self._authority.require_authorized(request, now=now)
        return now

    def sign(self, request: SigningRequest) -> SignedAuthorization:
        return self._authority.run_at_send_boundary(request, lambda: self._sign(request))

    def _sign(self, request: SigningRequest) -> SignedAuthorization:
        with self._lock:
            now = self._validate(request)
            # Abandoned tokens expire without occupying the pending window
            # forever. Keep their IDs consumed: a read only for the token window,
            # a mutation permanently, so an order request is never silently reissued.
            for identifier, (_, issued_at, _, method) in tuple(self._issued.items()):
                if (now - issued_at).total_seconds() >= JWT_LIFETIME_SECONDS:
                    self._issued.pop(identifier)
                    self._consumed[identifier] = now + _TOMBSTONE_RETENTION if method == "GET" else None
            # Bound the replay ledger under sustained read traffic: a tombstone
            # past its token window protects nothing (the token is stale).
            for identifier, retain_until in tuple(self._consumed.items()):
                if retain_until is not None and now >= retain_until:
                    del self._consumed[identifier]
            if request.request_id in self._issued or request.request_id in self._consumed:
                raise DomainError("SIGNING_REQUEST_ALREADY_ISSUED")
            if len(self._issued) >= MAX_PENDING_REQUESTS or len(self._issued) + len(self._consumed) >= MAX_TRACKED_REQUESTS:
                raise DomainError("SIGNING_CAPACITY_EXCEEDED")
            with self._keystore.borrow(
                self._credential,
                request.context,
                portfolio_id=self._credential.metadata.portfolio_id,
                purpose=request.purpose,
                version=request.credential_version,
            ) as secret:
                name, key = _load_key(secret)
                # Per Coinbase's request JWT contract (canonical builder). Query
                # and body remain bound in the local request digest and are
                # rechecked at the send boundary.
                hostname = self._endpoint.origin.removeprefix("https://") if self._endpoint.origin else ""
                token = build_request_jwt(key_name=name, private_key=key, method=request.method, host=hostname,
                                          path=self._endpoint.prefix + request.path, now=now).token
            self._issued[request.request_id] = (request.digest, now, sha256(token.encode()).hexdigest(), request.method)
            return SignedAuthorization(request.digest, now, token)

    def send(self, request: SigningRequest, authorization: SignedAuthorization, invoke: Callable[[], T]) -> T:
        """Validate, consume and initiate I/O in one authority/retirement boundary.

        The callback initiates the request synchronously, with no deferred send
        or retry. Response waiting belongs outside this boundary. Consumption is
        permanent even when invocation raises with an unknown network outcome.
        """
        return self._authority.run_at_send_boundary(
            request, lambda: self._send(request, authorization, invoke)
        )

    def _send(self, request: SigningRequest, authorization: SignedAuthorization, invoke: Callable[[], T]) -> T:
        with self._lock:
            now = self._validate(request)
            # A token minted before an OS-vault lock must not authorize a later
            # invocation. Recheck native access without exposing or retaining it.
            with self._keystore.borrow(
                self._credential,
                request.context,
                portfolio_id=self._credential.metadata.portfolio_id,
                purpose=request.purpose,
                version=request.credential_version,
            ):
                pass
            issued = self._issued.get(request.request_id)
            if (
                authorization.request_digest != request.digest
                or (issued is not None and (issued[0] != request.digest or issued[1] != authorization.issued_at))
                or not 0 <= (now - authorization.issued_at).total_seconds() < JWT_LIFETIME_SECONDS
            ):
                raise DomainError("SIGNED_REQUEST_MISMATCH_OR_STALE")
            expected = self._issued.pop(request.request_id, None)
            if expected is None:
                # Unissued forgeries must not allocate unbounded tombstones.
                raise DomainError("SIGNED_REQUEST_REPLAY_OR_FORGERY")
            self._consumed[request.request_id] = None if request.method != "GET" else now + _TOMBSTONE_RETENTION
            if not secrets.compare_digest(
                expected[2], sha256(authorization.token.encode()).hexdigest()
            ):
                raise DomainError("SIGNED_REQUEST_REPLAY_OR_FORGERY")
            return invoke()

    def retire(self) -> None:
        with self._lock:
            self._retired = True
            self._issued.clear()
            self._consumed.clear()
            self._keystore.retire_context(self._context)

    def make_write_request(self, *, path: str, body_sha256: str) -> SigningRequest:
        """A mutation (POST) whose signing request is bound locally to the exact body digest; EXECUTION purpose only.

        The digest binds this process to the body it signs for; the broker's JWT
        claims (method, host, path, expiry) are what the venue verifies.
        """
        if self._credential.metadata.purpose != CredentialPurpose.EXECUTION:
            raise DomainError("OBSERVER_MUTATION_FORBIDDEN")
        return SigningRequest(
            context=self._context,
            endpoint_profile_id=self._endpoint.profile_id,
            method="POST",
            path=path,
            query=(),
            body_sha256=body_sha256,
            purpose=CredentialPurpose.EXECUTION,
            credential_version=self._credential.metadata.version,
            profile_version=self._profile_version,
            request_id=secrets.token_hex(16),
        )

    def make_read_request(self, *, path: str, query: Mapping[str, str]) -> SigningRequest:
        return SigningRequest(
            context=self._context,
            endpoint_profile_id=self._endpoint.profile_id,
            method="GET",
            path=path,
            query=tuple(sorted(query.items())),
            purpose=self._credential.metadata.purpose,
            credential_version=self._credential.metadata.version,
            profile_version=self._profile_version,
            request_id=secrets.token_hex(16),
        )
