"""Single-flight vault I/O; no Tk calls, secret-bearing events or queued saves.

Two injected, narrow interfaces keep signing and vault internals out of the UI:
``CredentialStore`` (save/status in the OS keyring) and ``ConnectionVerifier``
(an explicit, read-only Coinbase authentication check). Storage status and
remote authentication status are separate render events and never merge.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Protocol

from coinfly.domain.context import DomainError
from coinfly.ui.render_scheduler import RenderQueue


class CredentialStore(Protocol):
    def save(self, key: str, secret: str) -> None: ...
    def status(self) -> str: ...


class ConnectionVerifier(Protocol):
    def verify_connection(self, cancel: threading.Event | None = None) -> Any:
        """Return an object with ``public_dict()`` holding only redacted fields."""
        ...


class CredentialWorker:
    def __init__(self, queue: RenderQueue, store: CredentialStore | None = None,
                 verifier: ConnectionVerifier | None = None) -> None:
        self._queue = queue
        self._store = store
        self._verifier = verifier
        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, key: str | None = None, secret: str | None = None) -> bool:
        return self._start(self._run, (key, secret))

    def submit_connection_test(self) -> bool:
        """Explicit user action only: never called merely because Settings opened."""
        return self._start(self._run_connection_test, ())

    def _start(self, target: Any, args: tuple[Any, ...]) -> bool:
        with self._lock:
            if self._busy or self._closed:
                return False
            self._busy = True
            self._cancel.clear()
        threading.Thread(target=target, args=args, name="coinfly-os-keyring", daemon=True).start()
        return True

    def _run(self, key: str | None, secret: str | None) -> None:
        try:
            if self._store is None:
                raise DomainError("OS_KEYRING_UNAVAILABLE")
            if key is not None and secret is not None:
                self._store.save(key, secret)
                code = "SECURELY_STORED"
            else:
                code = self._store.status()
        except DomainError as error:
            code = error.code
        except Exception:
            code = "OS_KEYRING_LOCKED_OR_DENIED"
        finally:
            key = secret = None
        self._finish("credentials", {"code": code})

    def _run_connection_test(self) -> None:
        payload: dict[str, Any]
        try:
            if self._verifier is None:
                raise DomainError("CONNECTION_TEST_UNAVAILABLE")
            verdict = self._verifier.verify_connection(self._cancel)
            payload = _redacted(verdict.public_dict())
        except DomainError as error:
            payload = {"code": error.code, "http_status": None, "verified": False, "authorizes_live_execution": False}
        except Exception:
            payload = {"code": "COINBASE_AUTH_UNKNOWN_FAILURE", "http_status": None, "verified": False,
                       "authorizes_live_execution": False}
        self._finish("connection_test", payload)

    def _finish(self, kind: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._busy = False
            if self._closed:
                return
            # On overflow the queue emits a visible resnapshot event; the app
            # derives its busy state from ``busy`` so controls never stay stuck.
            self._queue.put(kind, payload)

    def cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._cancel.set()


_ALLOWED_KEYS = frozenset({"code", "http_status", "checked_at", "operation", "clock_skew_seconds", "verified",
                           "authorizes_live_execution"})


def _redacted(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist scalar fields; a verifier can never smuggle tokens or accounts into a render event."""
    result: dict[str, Any] = {}
    for key in _ALLOWED_KEYS:
        value = payload.get(key)
        if value is None or isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= 80):
            result[key] = value
    result["authorizes_live_execution"] = False
    result.setdefault("code", "COINBASE_AUTH_UNKNOWN_FAILURE")
    result.setdefault("verified", result["code"] == "COINBASE_AUTH_VERIFIED")
    return result
