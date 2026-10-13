"""Canonical Coinbase App / Advanced Trade request JWT: one signer, no I/O.

Contract (Coinbase Developer Platform, API key authentication, reviewed
21 September 2026): ES256 over P-256 only (Ed25519 is not supported), header
``kid`` = API key name plus a random ``nonce``, claims ``sub`` = key name,
``iss`` = ``"cdp"``, ``nbf`` = now, ``exp`` = ``nbf`` + 120 seconds, and
``uri`` = ``"{METHOD} {host}{path}"``. Every distinct request needs a new token.

Key material never appears in exception text; callers receive stable codes.
"""

from __future__ import annotations

import json
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from coinfly.config.credential_input import (
    KEY_NAME_PATTERN,
    MAX_KEY_NAME_LENGTH,
    MAX_SECRET_LENGTH,
    PEM_PATTERN,
    normalize_secret,
)
from coinfly.domain.context import DomainError

JWT_ALGORITHM = "ES256"
JWT_ISSUER = "cdp"
JWT_LIFETIME_SECONDS = 120
NONCE_BYTES = 16

normalize_pem = normalize_secret


def validate_key_name(key_name: str) -> str:
    candidate = key_name.strip()
    if not candidate or len(candidate) > MAX_KEY_NAME_LENGTH or KEY_NAME_PATTERN.fullmatch(candidate) is None:
        raise DomainError("COINBASE_KEY_FORMAT_INVALID")
    return candidate


def load_es256_private_key(secret: str) -> ec.EllipticCurvePrivateKey:
    """Parse PEM material and classify why it cannot sign ES256 for Coinbase.

    The classification never includes the material itself. Parsing errors from
    ``cryptography`` are discarded because they can echo input fragments.
    """
    pem = normalize_pem(secret)
    if not pem or len(pem) > MAX_SECRET_LENGTH or PEM_PATTERN.fullmatch(pem) is None:
        raise DomainError("COINBASE_SECRET_FORMAT_INVALID")
    try:
        private = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception:
        raise DomainError("COINBASE_SECRET_FORMAT_INVALID") from None
    if isinstance(private, ec.EllipticCurvePrivateKey):
        if not isinstance(private.curve, ec.SECP256R1):
            raise DomainError("COINBASE_KEY_CURVE_UNSUPPORTED")
        return private
    if isinstance(private, ed25519.Ed25519PrivateKey):
        raise DomainError("COINBASE_KEY_ED25519_UNSUPPORTED")
    raise DomainError("COINBASE_KEY_NOT_EC")


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


def request_uri(method: str, host: str, path: str) -> str:
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        raise DomainError("ENDPOINT_ROUTE_FORBIDDEN")
    if not host or "/" in host or "://" in host or not path.startswith("/") or any(c in path for c in "?#\\"):
        raise DomainError("ENDPOINT_PROFILE_INVALID")
    return f"{method} {host}{path}"


def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


@dataclass(frozen=True, slots=True)
class RequestJwt:
    """A minted bearer token; ``repr``/``str`` never print the token."""

    token: str
    issued_at: datetime
    uri: str

    def __repr__(self) -> str:
        return f"RequestJwt(uri={self.uri!r}, issued_at={self.issued_at.isoformat()!r}, token='<redacted>')"

    __str__ = __repr__


def build_request_jwt(*, key_name: str, private_key: ec.EllipticCurvePrivateKey, method: str, host: str,
                      path: str, now: datetime, nonce: str | None = None) -> RequestJwt:
    """Mint one request-bound ES256 JWT; a distinct token for every request."""
    name = validate_key_name(key_name)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
        raise DomainError("COINBASE_KEY_CURVE_UNSUPPORTED")
    if now.tzinfo is None or now.utcoffset() is None:
        raise DomainError("CLOCK_NOT_AWARE")
    if nonce is None:
        nonce = new_nonce()
    if not re.fullmatch(r"[0-9a-f]{16,128}", nonce):
        raise DomainError("JWT_NONCE_INVALID")
    uri = request_uri(method, host, path)
    issued = int(now.timestamp())
    header = {"alg": JWT_ALGORITHM, "typ": "JWT", "kid": name, "nonce": nonce}
    claims = {"sub": name, "iss": JWT_ISSUER, "nbf": issued, "exp": issued + JWT_LIFETIME_SECONDS, "uri": uri}
    message = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64(
        json.dumps(claims, separators=(",", ":")).encode())
    signature = private_key.sign(message.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return RequestJwt(message + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big")), now, uri)


def decode_request_jwt(token: str, public_key: ec.EllipticCurvePublicKey) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Verify the ES256 signature locally and return (header, claims); tests and self-checks only."""
    try:
        header_text, claims_text, signature_text = token.split(".")
        raw = _unb64(signature_text)
        if len(raw) != 64:
            raise ValueError
        signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public_key.verify(signature, f"{header_text}.{claims_text}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        header, claims = json.loads(_unb64(header_text)), json.loads(_unb64(claims_text))
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        raise DomainError("JWT_INVALID") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise DomainError("JWT_INVALID")
    return header, claims
