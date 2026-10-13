import json
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from coinfly.domain.context import DomainError
from coinfly.security import desktop_credentials as credentials
from coinfly.ui.credential_worker import CredentialWorker
from coinfly.ui.render_scheduler import RenderQueue

KEY = "organizations/test-org/apiKeys/test-key"


def secret(key=None):
    return (key or ec.generate_private_key(ec.SECP256R1())).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


class MemoryKeyring:
    """Test-only synthetic vault; never configured by production."""
    def __init__(self):
        self.items = {}

    def set_password(self, service, account, password):
        self.items[service, account] = password

    def get_password(self, service, account):
        return self.items.get((service, account))


def test_pair_is_validated_and_replaced_as_one_keychain_item(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(credentials, "native_keyring", lambda: backend)
    store = credentials.DesktopCredentialStore()
    assert store.status() == "CREDENTIALS_NOT_STORED"  # vault empty; distinct from the form's CREDENTIALS_MISSING
    first = secret()
    store.save(KEY, first.replace("\n", "\\n"))
    assert store.status() == "SECURELY_STORED"
    assert len(backend.items) == 1
    assert set(backend.items) == {("com.devsolux.coinfly.coinbase.setup.LIVE", "credential-pair-v1")}
    pair = json.loads(next(iter(backend.items.values())))
    assert pair == {"key_name": KEY, "private_key": first.strip()}
    second = secret()
    store.save(KEY, second)
    assert len(backend.items) == 1
    assert first.strip() not in next(iter(backend.items.values()))


@pytest.mark.parametrize("key,code", [
    (ec.generate_private_key(ec.SECP384R1()), "COINBASE_KEY_CURVE_UNSUPPORTED"),
    (ed25519.Ed25519PrivateKey.generate(), "COINBASE_KEY_ED25519_UNSUPPORTED"),
    (rsa.generate_private_key(65537, 2048), "COINBASE_KEY_NOT_EC"),
])
def test_unsupported_keys_never_reach_vault(key, code, monkeypatch):
    def forbidden():
        pytest.fail("Invalid credential reached vault")
    monkeypatch.setattr(credentials, "native_keyring", forbidden)
    with pytest.raises(DomainError, match=code):
        credentials.DesktopCredentialStore().save(KEY, secret(key))


@pytest.mark.parametrize("material,code", [
    (secret(ec.generate_private_key(ec.SECP384R1())), "COINBASE_KEY_CURVE_UNSUPPORTED"),
    (secret(ed25519.Ed25519PrivateKey.generate()), "COINBASE_KEY_ED25519_UNSUPPORTED"),
    ("-----BEGIN PRIVATE KEY-----\ncorrupt\n-----END PRIVATE KEY-----", "COINBASE_SECRET_FORMAT_INVALID"),
])
def test_check_stored_credentials_cryptographically_validates_existing_pair(monkeypatch, material, code):
    backend = MemoryKeyring()
    backend.set_password(credentials.SERVICE, credentials.ACCOUNT,
                         json.dumps({"key_name": KEY, "private_key": material}))
    monkeypatch.setattr(credentials, "native_keyring", lambda: backend)
    store = credentials.DesktopCredentialStore()
    assert store.status() == code
    with pytest.raises(DomainError, match=code):
        store.read_setup_pair()


def test_read_setup_pair_returns_validated_material_only_for_explicit_use(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(credentials, "native_keyring", lambda: backend)
    store = credentials.DesktopCredentialStore()
    with pytest.raises(DomainError, match="CREDENTIALS_NOT_STORED"):
        store.read_setup_pair()
    material = secret()
    store.save(f"  {KEY}  ", material.replace("\n", "\\n"))
    assert store.read_setup_pair() == (KEY, material.strip())
    backend.set_password(credentials.SERVICE, credentials.ACCOUNT, "{not json")
    assert store.status() == "CREDENTIALS_INVALID"
    backend.set_password(credentials.SERVICE, credentials.ACCOUNT, json.dumps({"key_name": KEY, "extra": 1}))
    assert store.status() == "CREDENTIALS_INVALID"


def test_invalid_format_and_private_os_error_are_redacted(monkeypatch):
    assert credentials.credential_input_error("https://attacker.invalid", secret()) == "COINBASE_KEY_FORMAT_INVALID"
    assert credentials.credential_input_error(KEY, "x" * 5000) == "COINBASE_SECRET_FORMAT_INVALID"
    class Denied(MemoryKeyring):
        def set_password(self, *_):
            raise RuntimeError("SECRET_CANARY")
    monkeypatch.setattr(credentials, "native_keyring", Denied)
    with pytest.raises(DomainError, match="OS_KEYRING_LOCKED_OR_DENIED") as caught:
        credentials.DesktopCredentialStore().save(KEY, secret())
    assert "SECRET_CANARY" not in str(caught.value)


def test_keychain_wait_does_not_block_ui_or_accept_overlapping_save():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    queue = RenderQueue()
    main_thread = threading.get_ident()
    class WaitingStore:
        def save(self, key, material):
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(2)
            finished.set()
        def status(self):
            return "CREDENTIALS_MISSING"
    worker = CredentialWorker(queue, WaitingStore())
    try:
        assert worker.submit("PRIVATE_KEY_CANARY", "PRIVATE_SECRET_CANARY")
        assert entered.wait(1)
        assert not worker.submit("replacement", "replacement")
        assert not queue.take()
        release.set()
        assert finished.wait(1)
        for _ in range(100):
            events = queue.take()
            if events:
                break
            threading.Event().wait(0.01)
        assert events[0].payload == {"code": "SECURELY_STORED"}
        assert "CANARY" not in repr(events)
    finally:
        release.set()
        worker.close()


def test_os_backend_selection_cannot_use_plaintext_environment(monkeypatch):
    imported = []
    class Backend:
        keychain = "poisoned"
    class Module:
        Keyring = Backend
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    def import_module(name):
        imported.append(name)
        return Module
    monkeypatch.setattr(credentials.importlib, "import_module", import_module)
    assert credentials.native_keyring().keychain is None
    assert imported == ["keyring.backends.macOS"]
