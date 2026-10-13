"""Serve diagnostic IPC while the database/control actors initialize off-thread.

The deadline fences the result; it does not pretend Python can kill a thread.
A late constructor is disposed, and Retry cannot overlap its cleanup.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from coinfly.config.models import default_config
from coinfly.domain.context import DomainError
from coinfly.ipc.auth import OwnerOnlySandboxPolicy, PeerIdentity
from coinfly.ipc.local import LocalRPCServer, default_socket_path
from coinfly.ipc.protocol import PROTOCOL_VERSION, RPCError, RPCRequest
from coinfly.observability.diagnostics import build_identity, runtime_identity
from coinfly.worker.supervisor import WorkerSupervisor

logger = logging.getLogger(__name__)

# A replaced worker releases the authority lock only after it has closed its
# ledger and actors; its successor waits for that handover instead of staying
# BLOCKED on AUTHORITY_ALREADY_OWNED until someone presses Retry.
AUTHORITY_HANDOVER_SECONDS = 90.0
AUTHORITY_HANDOVER_INTERVAL = 1.0


class WorkerStartup:
    def __init__(self, *, socket_path: Path | None = None, timeout: float = 10,
                 factory: Callable[..., WorkerSupervisor] = WorkerSupervisor) -> None:
        if not 0 < timeout <= 60:
            raise ValueError("positive startup budget of at most 60 seconds required")
        self._factory, self._timeout = factory, timeout
        self.shutdown_requested = threading.Event()
        self._lock = threading.RLock()
        self._worker: WorkerSupervisor | None = None
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._closed = False
        self._expired = False
        self._sequence = 0
        self._instance = str(uuid4())
        self._stage, self._reason, self._retries = "CHECKING", "DATABASE_INITIALIZING", 0
        self._handover_deadline: float | None = None
        self._handover_timer: threading.Timer | None = None
        self._server = LocalRPCServer(socket_path or default_socket_path(), self.handle,
            authorization=OwnerOnlySandboxPolicy(realm_resolver=self._realm_for), cleanup_stale=True)

    def _realm_for(self, request: RPCRequest) -> str:
        with self._lock:
            worker = self._worker
        return worker._realm_for(request) if worker is not None else "SANDBOX"

    def start(self) -> WorkerStartup:
        self._server.start()
        try:
            self.retry()
        except BaseException:
            self._server.close()
            raise
        return self

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"worker_instance_id": self._instance, "sequence": self._sequence,
                    "context_epoch": 0, "config_revision": 0, "engine_state": "UNAVAILABLE",
                    "effective_config": None, "proposed_config": default_config().model_dump(mode="json"),
                    "admissions_fenced": True, "armed": False, "worker_initializing": True,
                    "bootstrap": {"stage": self._stage, "reason": self._reason, "ready": False,
                                  "component": "database", "retries": self._retries},
                    "release_gates": WorkerSupervisor._release_checks(), "slots": [], "alarms": [],
                    "balance": {"coverage": "UNKNOWN_UNIVERSE", "equity": None}, "live_available": False,
                    # Identity of the code this process runs, so the desktop can
                    # tell (and retire) a stale worker even before it is ready.
                    "build": dict(build_identity()), "runtime": runtime_identity()}

    def _hello(self, request: RPCRequest) -> dict[str, Any]:
        """The protocol/build handshake is answered while initializing, as the ready worker would.

        Without it every desktop connection (which always sends ``worker.hello``
        first) was refused with DATABASE_NOT_READY, so a slow or blocked start
        showed as an unreachable worker instead of its diagnostic snapshot.
        """
        params = request.params
        protocol = params.get("protocol_version")
        if type(protocol) is not int or protocol != PROTOCOL_VERSION:
            raise DomainError("PROTOCOL_VERSION_MISMATCH")
        build = build_identity()
        if params.get("application_id") != build["application_id"]:
            raise DomainError("APPLICATION_MISMATCH")
        fingerprint = params.get("source_fingerprint")
        return {"protocol_version": PROTOCOL_VERSION, "build": dict(build), "worker_instance_id": self._instance,
                "build_match": isinstance(fingerprint, str) and fingerprint == build["source_fingerprint"],
                "initializing": True}

    def _publish(self) -> None:
        self._sequence += 1
        self._server.publish_notification("state.snapshot", self.snapshot())

    def _progress(self, code: str) -> None:
        with self._lock:
            if self._expired or self._closed:
                return
            self._stage, self._reason = "RETRYING", code
            self._retries += 1
            self._publish()

    def _expire(self) -> None:
        with self._lock:
            if self._worker is not None or self._closed:
                return
            self._expired = True
            self._stage, self._reason = "BLOCKED", "DATABASE_INITIALIZATION_TIMEOUT"
            logger.warning("Database initialization timed out; execution remains unavailable")
            self._publish()

    def retry(self, *, handover: bool = False) -> None:
        with self._lock:
            if self._closed:
                raise DomainError("WORKER_CLOSED")
            if self._worker is not None or self._thread is not None and self._thread.is_alive():
                raise DomainError("BOOTSTRAP_BUSY")
            if not handover:
                self._handover_deadline = None
            self._expired = False
            self._stage, self._reason, self._retries = "CHECKING", "DATABASE_INITIALIZING", 0
            self._publish()
            self._thread = threading.Thread(target=self._initialize, name="coinfly-database-bootstrap", daemon=False)
            self._timer = threading.Timer(self._timeout, self._expire)
            self._timer.daemon = True
            self._timer.start()
            self._thread.start()

    def _initialize(self) -> None:
        worker: WorkerSupervisor | None = None
        deadline = time.monotonic() + self._timeout
        try:
            # One budget: the visible watchdog and the SQLite lock/schema deadline
            # expire together, so a late constructor cannot hold the authority
            # lock for a second, hidden ten-second window.
            worker = self._factory(startup_progress=self._progress, startup_timeout=self._timeout,
                                   shutdown_request=self.shutdown_requested.set)
            with self._lock:
                if self._closed or self._expired or time.monotonic() >= deadline:
                    if not self._closed and not self._expired:
                        self._expire()
                    return
                worker.start(server=self._server)
                if time.monotonic() >= deadline:
                    self._expire()
                    return
                self._worker = worker
                worker = None  # Ownership transfers only after successful startup.
            self._server.publish_notification("state.resnapshot", {})
        except Exception as error:
            with self._lock:
                if not self._closed and not self._expired:
                    code = error.code if isinstance(error, (DomainError, RPCError)) else "DATABASE_INITIALIZATION_FAILED"
                    if code == "AUTHORITY_ALREADY_OWNED" and self._schedule_handover_retry():
                        self._stage, self._reason = "RETRYING", "AUTHORITY_HANDOVER_PENDING"
                        self._publish()
                        return
                    self._stage = "BLOCKED"
                    self._reason = code
                    logger.warning("Worker initialization blocked: %s", self._reason)
                    self._publish()
        finally:
            if self._timer is not None:
                self._timer.cancel()
            if worker is not None:
                # This service retains the diagnostic endpoint on failure.
                worker._server = None
                worker.close()

    def _schedule_handover_retry(self) -> bool:
        """Retry while a predecessor still holds the authority, within a bounded window (lock held)."""
        now = time.monotonic()
        if self._handover_deadline is None:
            self._handover_deadline = now + AUTHORITY_HANDOVER_SECONDS
            logger.warning("Authority still held by a previous worker; waiting up to %.0fs for its handover",
                           AUTHORITY_HANDOVER_SECONDS)
        if now >= self._handover_deadline:
            return False

        def again() -> None:
            try:
                self.retry(handover=True)
            except DomainError:
                pass  # closed meanwhile, or an operator retry is already running
        timer = threading.Timer(AUTHORITY_HANDOVER_INTERVAL, again)
        timer.daemon = True
        self._handover_timer = timer
        timer.start()
        return True

    def handle(self, request: RPCRequest, peer: PeerIdentity) -> Any:
        with self._lock:
            worker = self._worker
        if worker is not None:
            return worker.handle(request, peer)
        if request.method == "worker.hello":
            return self._hello(request)
        if request.method in {"state.snapshot", "state.subscribe"}:
            return self.snapshot()
        if request.method == "bootstrap.status":
            return self.snapshot()["bootstrap"]
        if request.method == "state.unsubscribe":
            return {"subscribed": False}
        if request.method == "bootstrap.retry":
            self.retry()
            return {"accepted": True}
        if request.method == "worker.shutdown":
            # A worker that never finished constructing can still be retired.
            self.shutdown_requested.set()
            return {"accepted": True, "worker_instance_id": self._instance}
        raise DomainError("DATABASE_NOT_READY")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
            if self._handover_timer is not None:
                self._handover_timer.cancel()
            worker = self._worker
        self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=15)
        if worker is not None:
            worker.close()
