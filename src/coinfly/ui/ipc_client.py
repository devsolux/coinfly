"""Nonblocking desktop commands and reactive subscriptions on dedicated I/O threads."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.local import LocalRPCClient, default_socket_path
from coinfly.ipc.protocol import RPCError, freeze
from coinfly.ipc.reactive_cache import ReactiveCache, ResnapshotRequired

from .export_history import export_frozen_report
from .render_scheduler import RenderQueue


@dataclass(frozen=True, slots=True)
class _Command:
    method: str
    params: Mapping[str, Any]
    epoch: int
    request_id: str


@dataclass(frozen=True, slots=True)
class _Export:
    snapshot_id: str
    destination: Path
    epoch: int
    request_id: str


class DesktopClient:
    def __init__(self, render_queue: RenderQueue, socket_path: str | Path | None = None, *,
                 peer_verifier: Callable[[PeerIdentity], bool] | None = None,
                 worker_launcher: Callable[[], None] | None = None) -> None:
        self.render_queue, self.path = render_queue, Path(socket_path) if socket_path is not None else default_socket_path()
        self.peer_verifier = peer_verifier
        self._worker_launcher = worker_launcher
        self.cache = ReactiveCache()
        self._commands: queue.PriorityQueue[tuple[int, int, _Command]] = queue.PriorityQueue(maxsize=64)
        self._command_sequence = count()
        self._stop = threading.Event()
        self._resnapshot = threading.Event()
        self._relaunch = threading.Event()
        self._connections: list[LocalRPCClient] = []
        self._observer_connection: LocalRPCClient | None = None
        self._connection_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._export_thread: threading.Thread | None = None
        self._export_lock = threading.Lock()

    @property
    def supports_sensitive_enrollment(self) -> bool:
        return self.peer_verifier is not None

    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._observe, "coinfly-ui-reader"), (self._execute, "coinfly-ui-commands")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def submit(self, method: str, params: Mapping[str, Any] | None = None, *,
               epoch: int, request_id: str | None = None) -> str:
        identifier = request_id or uuid.uuid4().hex
        if method == "credentials.enroll" and not self.supports_sensitive_enrollment:
            raise RPCError("SIGNED_WORKER_REQUIRED")
        safety = method in {"safety.pause_entries", "safety.stop", "slot.stop", "slot.delete", "operation.retry"}
        if not safety and self._commands.qsize() >= 56:
            raise RPCError("COMMAND_QUEUE_FULL")
        try:
            priority = 0 if safety else (20 if method.startswith("history.") else 10)
            self._commands.put_nowait((priority, next(self._command_sequence), _Command(method, freeze(params or {}), epoch, identifier)))
        except queue.Full as exc:
            raise RPCError("COMMAND_QUEUE_FULL") from exc
        return identifier

    def export_history(self, snapshot_id: str, destination: Path, *, epoch: int) -> str:
        request_id = uuid.uuid4().hex
        with self._export_lock:
            if self._export_thread is not None and self._export_thread.is_alive():
                raise RPCError("EXPORT_ALREADY_RUNNING")
            task = _Export(snapshot_id, destination, epoch, request_id)
            self._export_thread = threading.Thread(target=self._export, args=(task,), name="coinfly-ui-export", daemon=True)
            self._export_thread.start()
        return request_id

    def _export(self, task: _Export) -> None:
        client = self._new_connection()
        try:
            def fetch(cursor: str | None, direction: str) -> Mapping[str, Any]:
                if self._stop.is_set():
                    raise RPCError("EXPORT_CANCELLED")
                result = client.call("history.page", {"snapshot_id": task.snapshot_id, "cursor": cursor,
                                                       "direction": direction}, epoch=task.epoch)
                if not isinstance(result, Mapping):
                    raise RPCError("INVALID_HISTORY_PAGE")
                return result
            count = export_frozen_report(task.snapshot_id, task.destination, fetch)
            self.render_queue.put("response", {"method": "history.export", "request_id": task.request_id,
                                               "result": {"state": "EXPORTED", "records": count}})
        except Exception as exc:
            self.render_queue.put("response", {"method": "history.export", "request_id": task.request_id,
                                               "error": getattr(exc, "code", "EXPORT_FAILED")})
        finally:
            self._release(client)

    def restart_worker(self, *, epoch: int) -> str:
        """Ask the connected worker to stop; relaunch it when the connection drops."""
        self._relaunch.set()
        return self.submit("worker.shutdown", {}, epoch=epoch)

    def request_resnapshot(self) -> None:
        self._resnapshot.set()
        with self._connection_lock:
            observer = self._observer_connection
        if observer is not None:
            observer.close()

    def _new_connection(self) -> LocalRPCClient:
        from coinfly.observability.diagnostics import build_identity

        # Every desktop connection binds itself to the worker's protocol and
        # build identity first (worker.hello); a different protocol or
        # application is refused, a different build is reported.
        client = LocalRPCClient(self.path, timeout=5.0, peer_verifier=self.peer_verifier, identity=dict(build_identity()))
        with self._connection_lock:
            self._connections.append(client)
        return client

    def _release(self, client: LocalRPCClient) -> None:
        client.close()
        with self._connection_lock:
            if client in self._connections:
                self._connections.remove(client)

    def _execute(self) -> None:
        while not self._stop.is_set():
            try:
                _, _, command = self._commands.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                return
            client = self._new_connection()
            try:
                result = client.call(command.method, command.params, request_id=command.request_id, epoch=command.epoch)
                self.render_queue.put("response", {"method": command.method, "request_id": command.request_id,
                                                   "result": result})
            except Exception as exc:
                # Do not leak exception messages containing sensitive command values.
                code = getattr(exc, "code", "WORKER_UNAVAILABLE")
                self.render_queue.put("response", {"method": command.method, "request_id": command.request_id,
                                                   "error": code})
            finally:
                self._release(client)

    def _observe(self) -> None:
        started = time.monotonic()
        if self._worker_launcher is not None:
            try:
                self._worker_launcher()
            except Exception:
                self.render_queue.put("connection", {"connected": False, "code": "WORKER_START_UNAVAILABLE"})
        while not self._stop.is_set():
            self._resnapshot.clear()
            client = self._new_connection()
            with self._connection_lock:
                self._observer_connection = client
            try:
                initial = client.call("state.subscribe", epoch=0)
                snapshot = initial.get("snapshot", initial) if isinstance(initial, dict) else None
                if not isinstance(snapshot, Mapping) or "sequence" not in snapshot:
                    raise RPCError("SUBSCRIPTION_SNAPSHOT_REQUIRED")
                self.render_queue.put("snapshot", self.cache.apply_snapshot(snapshot))
                self.render_queue.put("connection", {"connected": True})
                # The ten-second terminal diagnostic is for a worker that never
                # appeared; a later reconnect blip restarts that budget.
                started = time.monotonic()
                for message in client.notifications(self._stop):
                    if self._resnapshot.is_set():
                        break
                    method, params = message["method"], message["params"]
                    if method == "state.snapshot":
                        try:
                            snapshot = self.cache.apply_snapshot(params)
                        except RPCError as exc:
                            if exc.code == "STALE_SNAPSHOT":
                                continue
                            raise
                        self.render_queue.put("snapshot", snapshot)
                    elif method == "state.delta":
                        self.render_queue.put("snapshot", self.cache.apply_delta(params))
                    elif method == "state.resnapshot":
                        raise ResnapshotRequired()
                    elif method == "alarm.raised":
                        if not self.render_queue.put("alarm", params):
                            raise ResnapshotRequired()
                    elif method == "history.changed":
                        self.render_queue.put("history_changed", params)
                    elif method == "bootstrap.progress":
                        self.render_queue.put("bootstrap_progress", params)
            except Exception as exc:
                self.render_queue.put("connection", {"connected": False,
                                                       "timed_out": time.monotonic() - started >= 10,
                                                       "code": getattr(exc, "code", "WORKER_UNAVAILABLE")})
            finally:
                self._release(client)
                with self._connection_lock:
                    if self._observer_connection is client:
                        self._observer_connection = None
            if self._relaunch.is_set() and self._worker_launcher is not None and not self._stop.is_set():
                # The retired worker's socket disappears with its process; give it
                # a moment, then launch a replacement exactly like at startup.
                self._stop.wait(1.0)
                self._relaunch.clear()
                started = time.monotonic()
                try:
                    self._worker_launcher()
                except Exception:
                    self.render_queue.put("connection", {"connected": False, "code": "WORKER_START_UNAVAILABLE"})
            self._stop.wait(1.0)

    def close(self) -> None:
        """Signal I/O shutdown; Tk callbacks never join or wait for threads."""
        self._stop.set()
        with self._connection_lock:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()
