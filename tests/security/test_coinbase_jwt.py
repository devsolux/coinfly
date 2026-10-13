"""Canonical Coinbase request-JWT contract, checked by local decoding with ephemeral keys."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from coinfly.config.credential_input import credential_input_error, normalize_secret
from coinfly.domain.context import DomainError
from coinfly.security import coinbase_jwt as jwt

KEY_NAME = "organizations/11111111-2222-3333-4444-555555555555/apiKeys/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
HOST, PATH = "api.coinbase.com", "/api/v3/brokerage/accounts"


def pem(key, fmt=serialization.PrivateFormat.PKCS8):
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode()


@pytest.fixture(scope="module")
def p256():
    return ec.generate_private_key(ec.SECP256R1())


def test_token_matches_the_documented_es256_request_contract(p256):
    minted = jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH,
                                   now=NOW, nonce="0123456789abcdef0123456789abcdef")
    header, claims = jwt.decode_request_jwt(minted.token, p256.public_key())
    assert header == {"alg": "ES256", "typ": "JWT", "kid": KEY_NAME, "nonce": "0123456789abcdef0123456789abcdef"}
    assert claims == {"sub": KEY_NAME, "iss": "cdp", "nbf": int(NOW.timestamp()),
                      "exp": int(NOW.timestamp()) + 120, "uri": f"GET {HOST}{PATH}"}
    assert claims["exp"] - claims["nbf"] == jwt.JWT_LIFETIME_SECONDS == 120
    # Compact JOSE serialization: three base64url segments, raw 64-byte r||s signature.
    segments = minted.token.split(".")
    assert len(segments) == 3 and all("=" not in part for part in segments)
    assert len(jwt._unb64(segments[2])) == 64
    assert minted.uri == f"GET {HOST}{PATH}" and minted.issued_at == NOW


def test_each_request_gets_a_distinct_nonce_and_signature(p256):
    tokens = [jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH, now=NOW)
              for _ in range(8)]
    nonces = {jwt.decode_request_jwt(t.token, p256.public_key())[0]["nonce"] for t in tokens}
    assert len(nonces) == 8 and all(len(n) == 32 and int(n, 16) >= 0 for n in nonces)
    assert len({t.token for t in tokens}) == 8


def test_uri_binds_method_host_and_path_and_rejects_query_or_host_smuggling(p256):
    minted = jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST,
                                   path="/api/v3/brokerage/key_permissions", now=NOW)
    assert jwt.decode_request_jwt(minted.token, p256.public_key())[1]["uri"] == "GET api.coinbase.com/api/v3/brokerage/key_permissions"
    for method, host, path in [("HEAD", HOST, PATH), ("GET", "https://api.coinbase.com", PATH),
                               ("GET", "api.coinbase.com/api", "/x"), ("GET", HOST, "/accounts?limit=1"),
                               ("GET", HOST, "accounts"), ("GET", "", PATH)]:
        with pytest.raises(DomainError):
            jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method=method, host=host, path=path, now=NOW)


def test_signature_is_verified_against_the_matching_public_key_only(p256):
    minted = jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH, now=NOW)
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(DomainError, match="JWT_INVALID"):
        jwt.decode_request_jwt(minted.token, other.public_key())
    head, body, signature = minted.token.split(".")
    tampered = jwt._b64(json.dumps({**json.loads(jwt._unb64(body)), "uri": "POST api.coinbase.com/api/v3/brokerage/orders"}).encode())
    with pytest.raises(DomainError, match="JWT_INVALID"):
        jwt.decode_request_jwt(f"{head}.{tampered}.{signature}", p256.public_key())


def test_escaped_newlines_and_both_pem_formats_survive_paste(p256):
    multiline = pem(p256)
    for material in (multiline, multiline.replace("\n", "\\n"), multiline.replace("\n", "\r\n"),
                     multiline.replace("\n", "\\r\\n"), pem(p256, serialization.PrivateFormat.TraditionalOpenSSL)):
        assert credential_input_error(KEY_NAME, material) is None
        assert normalize_secret(material) == jwt.normalize_pem(material)
        loaded = jwt.load_es256_private_key(material)
        assert loaded.private_numbers().private_value == p256.private_numbers().private_value


@pytest.mark.parametrize("material,code", [
    (pem(ec.generate_private_key(ec.SECP384R1())), "COINBASE_KEY_CURVE_UNSUPPORTED"),
    (pem(ec.generate_private_key(ec.SECP256K1())), "COINBASE_KEY_CURVE_UNSUPPORTED"),
    (pem(ed25519.Ed25519PrivateKey.generate()), "COINBASE_KEY_ED25519_UNSUPPORTED"),
    (pem(rsa.generate_private_key(65537, 2048)), "COINBASE_KEY_NOT_EC"),
    ("-----BEGIN PRIVATE KEY-----\ncorrupt\n-----END PRIVATE KEY-----", "COINBASE_SECRET_FORMAT_INVALID"),
    ("not a pem at all", "COINBASE_SECRET_FORMAT_INVALID"),
    ("", "COINBASE_SECRET_FORMAT_INVALID"),
])
def test_unsupported_material_is_classified_without_echoing_it(material, code):
    with pytest.raises(DomainError) as caught:
        jwt.load_es256_private_key(material)
    assert caught.value.code == code
    text = str(caught.value) + repr(caught.value) + repr(caught.value.__cause__)
    assert "BEGIN" not in text and "corrupt" not in text and "not a pem" not in text


@pytest.mark.parametrize("name", ["", "organizations//apiKeys/x", "organizations/a/apiKeys/", "apiKeys/x",
                                  "organizations/a b/apiKeys/c", "organizations/" + "a" * 101 + "/apiKeys/b",
                                  "https://attacker.invalid/organizations/a/apiKeys/b"])
def test_malformed_key_names_are_rejected(name, p256):
    with pytest.raises(DomainError, match="COINBASE_KEY_FORMAT_INVALID"):
        jwt.build_request_jwt(key_name=name, private_key=p256, method="GET", host=HOST, path=PATH, now=NOW)


def test_naive_clock_and_foreign_curve_objects_are_refused(p256):
    with pytest.raises(DomainError, match="CLOCK_NOT_AWARE"):
        jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH,
                              now=NOW.replace(tzinfo=None))
    with pytest.raises(DomainError, match="COINBASE_KEY_CURVE_UNSUPPORTED"):
        jwt.build_request_jwt(key_name=KEY_NAME, private_key=ec.generate_private_key(ec.SECP384R1()), method="GET",
                              host=HOST, path=PATH, now=NOW)
    with pytest.raises(DomainError, match="JWT_NONCE_INVALID"):
        jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH, now=NOW, nonce="short")


def test_minted_token_never_appears_in_repr_or_str(p256):
    minted = jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST, path=PATH, now=NOW)
    assert minted.token not in repr(minted) and minted.token not in str(minted)
    assert "<redacted>" in repr(minted)


def test_clock_drives_validity_window_deterministically(p256):
    later = NOW + timedelta(seconds=90)
    first = jwt.decode_request_jwt(jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST,
                                                         path=PATH, now=NOW).token, p256.public_key())[1]
    second = jwt.decode_request_jwt(jwt.build_request_jwt(key_name=KEY_NAME, private_key=p256, method="GET", host=HOST,
                                                          path=PATH, now=later).token, p256.public_key())[1]
    assert second["nbf"] - first["nbf"] == 90 and second["exp"] - first["exp"] == 90
    assert first["nbf"] < first["exp"] and second["nbf"] < second["exp"]
