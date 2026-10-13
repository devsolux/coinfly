"""OS-only storage for desktop Coinbase setup; never a signing authorization.

These unbound setup credentials are deliberately separate from NativeKeystore's
certified, portfolio-bound execution references. They never cross unsigned IPC.

Status vocabulary (four distinct states, none implies the next):
``CREDENTIAL_FORMAT_VALID`` (syntax only) → ``SECURELY_STORED`` (local vault
round trip plus ES256/P-256 parse) → ``COINBASE_AUTH_VERIFIED`` (an explicit,
user-initiated read-only request succeeded; see ``venues.coinbase.connection_test``)
→ live enrollment, which still requires the signed-worker, native, regional and
gateway gates and is never derived from this module.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import Protocol, cast

from coinfly import __application_id__
from coinfly.config.credential_input import credential_input_error, normalize_secret
from coinfly.domain.context import DomainError
from coinfly.security.coinbase_jwt import load_es256_private_key, validate_key_name

SERVICE = f"{__application_id__}.coinbase.setup.LIVE"
ACCOUNT = "credential-pair-v1"


class OSKeyring(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def get_password(self, service: str, username: str) -> str | None: ...


def native_keyring() -> OSKeyring:
    # Explicit backends bypass PYTHON_KEYRING_BACKEND, entry points, config files,
    # null stores and third-party plaintext fallback backends.
    module, name = {
        "darwin": ("keyring.backends.macOS", "Keyring"),
        "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
        "linux": ("keyring.backends.SecretService", "Keyring"),
    }.get(sys.platform, ("", ""))
    if not module:
        raise DomainError("OS_KEYRING_UNAVAILABLE")
    try:
        backend = getattr(importlib.import_module(module), name)()
        if sys.platform == "darwin":
            backend.keychain = None  # Ignore KEYCHAIN_PATH / KEYRING_PROPERTY_KEYCHAIN.
        elif sys.platform == "win32":
            backend.persist = "local machine"
        elif backend.priority <= 0:
            raise DomainError("OS_KEYRING_UNAVAILABLE")
        return cast(OSKeyring, backend)
    except Exception:
        raise DomainError("OS_KEYRING_UNAVAILABLE") from None


class DesktopCredentialStore:
    @staticmethod
    def _validate_pair(key: str, secret: str) -> tuple[str, str]:
        code = credential_input_error(key, secret)
        if code is not None:
            raise DomainError(code)
        secret = normalize_secret(secret)
        # Precise, redacted classification: malformed PEM, non-EC, Ed25519 and
        # wrong-curve keys each get their own code and never reach the vault.
        load_es256_private_key(secret)
        return validate_key_name(key), secret

    def save(self, key: str, secret: str) -> None:
        key, secret = self._validate_pair(key, secret)
        # One item replaces the pair atomically; no half-updated key/secret pair.
        payload = json.dumps({"key_name": key, "private_key": secret}, separators=(",", ":"))
        try:
            backend = native_keyring()
            backend.set_password(SERVICE, ACCOUNT, payload)
            if backend.get_password(SERVICE, ACCOUNT) != payload:
                raise DomainError("KEYRING_VERIFICATION_FAILED")
        except DomainError:
            raise
        except Exception:
            # OS errors can include credential values. Never surface their text.
            raise DomainError("OS_KEYRING_LOCKED_OR_DENIED") from None
        finally:
            payload = secret = ""

    def read_setup_pair(self) -> tuple[str, str]:
        """Return the validated stored pair for an explicit, user-initiated use.

        Raises the same redacted codes ``status()`` reports. Callers must not
        persist, log or transmit the returned material anywhere but the signer.
        """
        try:
            payload = native_keyring().get_password(SERVICE, ACCOUNT)
        except DomainError:
            raise
        except Exception:
            raise DomainError("OS_KEYRING_LOCKED_OR_DENIED") from None
        if payload is None:
            # Distinct from the form's CREDENTIALS_MISSING: the vault holds no pair.
            raise DomainError("CREDENTIALS_NOT_STORED")
        try:
            values = json.loads(payload)
        except ValueError:
            raise DomainError("CREDENTIALS_INVALID") from None
        if (not isinstance(values, dict) or set(values) != {"key_name", "private_key"}
                or not all(isinstance(value, str) for value in values.values())):
            raise DomainError("CREDENTIALS_INVALID")
        return self._validate_pair(values["key_name"], values["private_key"])

    def status(self) -> str:
        try:
            self.read_setup_pair()
            return "SECURELY_STORED"
        except DomainError as error:
            return error.code
        except Exception:
            return "OS_KEYRING_LOCKED_OR_DENIED"
