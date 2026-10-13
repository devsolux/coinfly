"""Short owner-restricted Unix sockets; no remote or plaintext-secret fallback."""

from __future__ import annotations

import errno
import os
import queue
import socket
import stat
import struct
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .auth import AuthorizationPolicy, OwnerOnlySandboxPolicy, PeerIdentity, current_uid
from .protocol import (
    MAX_FRAME_BYTES,
    FrameDecoder,
    RPCError,
    RPCRequest,
    decode_json,
    encode_frame,
    error_response,
    result_response,
)

MAX_SOCKET_PATH_BYTES = 100


class ByteStream(Protocol):
    def recv(self, size: int) -> bytes: ...
    def sendall(self, payload: bytes) -> None: ...
    def settimeout(self, timeout: float) -> None: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class RPCHandler(Protocol):
    def __call__(self, request: RPCRequest, peer: PeerIdentity) -> Any: ...


def default_socket_path() -> Path:
    if os.name == "nt":
        raise RPCError("UNIX_IPC_UNAVAILABLE")
    # Deliberately short on macOS, independent from environment financial config.
    return Path("/tmp") / f"coinfly-{current_uid()}" / "worker.sock"


def _unix_socket() -> socket.socket:
    if os.name == "nt":
        raise RPCError("UNIX_IPC_UNAVAILABLE")
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise RPCError("UNIX_IPC_UNAVAILABLE")
    return socket.socket(family, socket.SOCK_STREAM)


def _check_directory(path: Path, *, create: bool) -> None:
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES or not path.is_absolute():
        raise RPCError("UNSAFE_SOCKET_PATH")
    directory = path.parent
    if create:
        try:
            directory.mkdir(mode=0o700, parents=False)
        except FileExistsError:
            pass
    try:
        item = directory.lstat()
    except OSError as exc:
        raise RPCError("IPC_DIRECTORY_UNAVAILABLE") from exc
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != current_uid() or item.st_mode & 0o077:
        raise RPCError("IPC_DIRECTORY_NOT_PRIVATE")


def peer_identity(connection: socket.socket) -> PeerIdentity:
    platform_name = sys.platform
    if platform_name.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return PeerIdentity(uid, gid, pid)
    if platform_name == "darwin":
        # CPython does not guarantee a getpeereid socket method. Bind the
        # documented OS API from the fixed system library; no user dylib search.
        # Apple: developer.apple.com/library/archive/documentation/System/
        # Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html
        import ctypes
        native = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        native.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        native.getpeereid.restype = ctypes.c_int
        darwin_uid, darwin_gid = ctypes.c_uint(), ctypes.c_uint()
        if native.getpeereid(connection.fileno(), ctypes.byref(darwin_uid), ctypes.byref(darwin_gid)) != 0:
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        darwin_pid: int | None = None
        try:
            darwin_pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]  # SOL_LOCAL / LOCAL_PEERPID
        except OSError:
            pass
        return PeerIdentity(darwin_uid.value, darwin_gid.value, darwin_pid)
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is not None:
        uid, gid = getpeereid()
        return PeerIdentity(uid, gid, None)
    raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")


def _recv_exact(connection: ByteStream, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        part = connection.recv(min(length - len(result), 65536))
        if not part:
            raise EOFError("IPC connection closed")
        result.extend(part)
    return bytes(result)


def receive_frame(connection: ByteStream) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise RPCError("FRAME_LIMIT")
    return decode_json(_recv_exact(connection, size))


class _Connection:
    def __init__(self, server: "UnixRPCServer", connection: ByteStream, peer: PeerIdentity) -> None:
        self.server, self.socket, self.peer = server, connection, peer
        self.outgoing: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.subscribed = False
        self.reader = threading.Thread(target=self._read, name="coinfly-ipc-peer", daemon=True)
        self.sender = threading.Thread(target=self._send, name="coinfly-ipc-send", daemon=True)

    def start(self) -> None:
        self.sender.start()
        self.reader.start()

    def emit(self, message: Mapping[str, Any]) -> None:
        try:
            self.outgoing.put_nowait(encode_frame(message))
        except (queue.Full, RPCError):
            # Disconnecting a slow consumer forces an authoritative resnapshot.
            # Never silently lose an alarm or command receipt.
            self.close()

    def _send(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    payload = self.outgoing.get(timeout=0.25)
                except queue.Empty:
                    continue
                if payload is None:
                    return
                self.socket.sendall(payload)
        except OSError:
            pass
        finally:
            self.close()

    def _read(self) -> None:
        decoder = FrameDecoder()
        partial_since: float | None = None
        try:
            while not self.closed.is_set():
                try:
                    packet = self.socket.recv(65536)
                except socket.timeout:
                    if decoder.pending:
                        raise RPCError("INCOMPLETE_FRAME_TIMEOUT") from None
                    continue
                if not packet:
                    return
                if partial_since is not None and time.monotonic() - partial_since > 5.0:
                    raise RPCError("INCOMPLETE_FRAME_TIMEOUT")
                for data in decoder.feed(packet):
                    request_id = data.get("id") if isinstance(data.get("id"), str) else None
                    was_subscribed = self.subscribed
                    try:
                        request = RPCRequest.parse(data)
                        self.server.authorization.authorize(self.peer, request)
                        if request.deadline_ms <= int(time.time() * 1000):
                            raise RPCError("DEADLINE_EXPIRED")
                        # Subscribe before snapshot capture. A racing commit may
                        # arrive before the response; the client buffers it.
                        if request.method == "state.subscribe":
                            self.subscribed = True
                        result = self.server.handler(request, self.peer)
                        self.emit(result_response(request.request_id, result))
                        if request.method == "state.unsubscribe":
                            self.subscribed = False
                    except Exception as exc:
                        self.subscribed = was_subscribed
                        code = getattr(exc, "code", "INTERNAL_ERROR")
                        self.emit(error_response(request_id, code if isinstance(code, str) else "INTERNAL_ERROR"))
                partial_since = (partial_since or time.monotonic()) if decoder.pending else None
        except (OSError, EOFError, RPCError):
            pass
        finally:
            self.close()
            self.server._remove(self)

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class UnixRPCServer:
    """Threaded server. Handler runs outside the Tk and worker event-loop threads.

    start() binds synchronously and starts bounded local I/O threads. The handler
    must submit worker mutations to the worker's serialized command owner.
    publish_notification() only queues bounded messages and never waits on peers.
    """

    def __init__(self, path: str | Path, handler: RPCHandler, *,
                 authorization: AuthorizationPolicy | None = None,
                 max_connections: int = 16, cleanup_stale: bool = False) -> None:
        if not 1 <= max_connections <= 64:
            raise ValueError("Connection limit must be between 1 and 64")
        self.path, self.handler = Path(path), handler
        self.authorization = authorization or OwnerOnlySandboxPolicy()
        self.max_connections, self.cleanup_stale = max_connections, cleanup_stale
        self._connections: set[_Connection] = set()
        self._connection_counter = 0
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._listener: Any = None
        self._thread: threading.Thread | None = None
        self._inode: tuple[int, int] | None = None

    def start(self) -> "UnixRPCServer":
        if self._listener is not None:
            raise RPCError("SERVER_ALREADY_STARTED")
        _check_directory(self.path, create=True)
        if self.path.exists() or self.path.is_symlink():
            self._remove_stale()
        listener = _unix_socket()
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            item = self.path.lstat()
            self._inode = (item.st_dev, item.st_ino)
            listener.listen(self.max_connections)
            listener.settimeout(0.25)
            self._listener = listener
            self._thread = threading.Thread(target=self._accept, name="coinfly-ipc-listen", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            listener.close()
            raise

    def _remove_stale(self) -> None:
        try:
            item = self.path.lstat()
        except FileNotFoundError:
            return  # The stale endpoint disappeared between exists() and lstat(): nothing to clean.
        if not self.cleanup_stale or not stat.S_ISSOCK(item.st_mode) or item.st_uid != current_uid():
            raise RPCError("IPC_ENDPOINT_EXISTS")
        probe = _unix_socket()
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                raise RPCError("IPC_ENDPOINT_EXISTS") from exc
        else:
            raise RPCError("IPC_ENDPOINT_ACTIVE")
        finally:
            probe.close()
        try:
            current = self.path.lstat()
            if (item.st_dev, item.st_ino) != (current.st_dev, current.st_ino):
                raise RPCError("IPC_ENDPOINT_CHANGED")
            self.path.unlink()
        except FileNotFoundError:
            return

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                peer = peer_identity(connection)
                if peer.uid != current_uid():
                    raise RPCError("PEER_OWNER_MISMATCH")
                connection.settimeout(5.0)
                with self._lock:
                    if len(self._connections) >= self.max_connections:
                        raise RPCError("CONNECTION_LIMIT")
                    self._connection_counter += 1
                    peer = replace(peer, connection_id=self._connection_counter)
                    client = _Connection(self, connection, peer)
                    self._connections.add(client)
                try:
                    client.start()
                except BaseException:
                    # A thread that never started must not occupy a connection slot.
                    self._remove(client)
                    raise
            except (RPCError, OSError, RuntimeError):
                connection.close()

    def _remove(self, connection: _Connection) -> None:
        with self._lock:
            self._connections.discard(connection)

    def publish_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method not in {"state.snapshot", "state.delta", "state.resnapshot", "alarm.raised", "history.changed", "bootstrap.progress"}:
            raise RPCError("NOTIFICATION_NOT_ALLOWED")
        message = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        with self._lock:
            clients = tuple(self._connections)
        for client in clients:
            if client.subscribed:
                client.emit(message)

    def close(self) -> None:
        self._stopped.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            clients = tuple(self._connections)
        for client in clients:
            client.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        try:
            item = self.path.lstat()
            if self._inode == (item.st_dev, item.st_ino) and not self._endpoint_answers():
                self.path.unlink()
        except FileNotFoundError:
            pass

    def _endpoint_answers(self) -> bool:
        """Whether a live listener answers on the path after this server's own listener closed.

        A successor may have removed this server's stale endpoint and bound a new
        one while this server was shutting down; a filesystem can reuse the inode
        number, so the inode check alone could delete the successor's endpoint.
        """
        probe = _unix_socket()
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.path))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def __enter__(self) -> "UnixRPCServer":
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.close()


class UnixRPCClient:
    """Synchronous client for an I/O thread only. No automatic command replay."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0,
                 peer_verifier: Callable[[PeerIdentity], bool] | None = None,
                 identity: Mapping[str, Any] | None = None) -> None:
        self.path, self.timeout = Path(path), timeout
        self.peer_verifier = peer_verifier
        # Client identity for the ``worker.hello`` handshake (protocol version,
        # application id, version, source fingerprint); None skips the handshake
        # (tools that only read, or tests).
        self.identity = dict(identity) if identity is not None else None
        self.handshake: dict[str, Any] | None = None
        self._socket: ByteStream | None = None
        self._lock = threading.Lock()
        self._pending_notifications: deque[dict[str, Any]] = deque()

    def connect(self) -> None:
        if self._socket is not None:
            return
        _check_directory(self.path, create=False)
        item = self.path.lstat()
        if not stat.S_ISSOCK(item.st_mode) or item.st_uid != current_uid() or item.st_mode & 0o077:
            raise RPCError("IPC_ENDPOINT_NOT_PRIVATE")
        connection = _unix_socket()
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.path))
            peer = peer_identity(connection)
            if peer.uid != current_uid():
                raise RPCError("PEER_OWNER_MISMATCH")
            if self.peer_verifier is not None and not self.peer_verifier(peer):
                raise RPCError("WORKER_SIGNATURE_UNVERIFIED")
            self._socket = connection
        except BaseException:
            connection.close()
            raise
        if self.identity is not None:
            # Bind this connection to the protocol and build identity before any command.
            try:
                self.handshake = self._exchange(self._request("worker.hello", self.identity, epoch=0))
            except BaseException:
                self.close()
                raise

    def _request(self, method: str, params: Mapping[str, Any] | None, *, request_id: str | None = None, epoch: int = 0,
                 deadline_ms: int | None = None) -> RPCRequest:
        return RPCRequest.parse({"jsonrpc": "2.0", "id": request_id or uuid.uuid4().hex,
                                 "method": method, "params": dict(params or {}), "epoch": epoch,
                                 "deadline_ms": deadline_ms or int(time.time() * 1000) + int(self.timeout * 1000)})

    def _exchange(self, request: RPCRequest) -> Any:
        assert self._socket is not None
        try:
            self._socket.sendall(encode_frame(request.as_dict()))
            while True:
                if int(time.time() * 1000) >= request.deadline_ms:
                    raise RPCError("RESPONSE_UNAVAILABLE")
                response = receive_frame(self._socket)
                if "id" in response:
                    break
                self._validate_notification(response)
                if len(self._pending_notifications) >= 64:
                    raise RPCError("SUBSCRIPTION_OVERFLOW")
                self._pending_notifications.append(response)
            if response.get("jsonrpc") != "2.0" or response.get("id") != request.request_id:
                raise RPCError("RESPONSE_ID_MISMATCH")
            if "error" in response:
                error = response["error"]
                raise RPCError(error.get("code", "RPC_FAILED") if isinstance(error, dict) else "RPC_FAILED")
            if set(response) != {"jsonrpc", "id", "result"}:
                raise RPCError("INVALID_RESPONSE")
            return response["result"]
        except (OSError, EOFError):
            self.close()
            raise RPCError("RESPONSE_UNAVAILABLE") from None

    def call(self, method: str, params: Mapping[str, Any] | None = None, *,
             request_id: str | None = None, epoch: int = 0,
             deadline_ms: int | None = None) -> Any:
        request = self._request(method, params, request_id=request_id, epoch=epoch, deadline_ms=deadline_ms)
        with self._lock:
            self.connect()
            return self._exchange(request)

    def notifications(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        """After state.subscribe, consume one connection exclusively here."""
        assert self._socket is not None
        decoder = FrameDecoder()
        while self._pending_notifications:
            yield self._pending_notifications.popleft()
        self._socket.settimeout(0.5)
        partial_since: float | None = None
        while not stop.is_set():
            try:
                packet = self._socket.recv(65536)
            except socket.timeout:
                if partial_since is not None and time.monotonic() - partial_since > 5.0:
                    raise RPCError("INCOMPLETE_FRAME_TIMEOUT") from None
                continue
            if not packet:
                raise EOFError("IPC subscription closed")
            if partial_since is not None and time.monotonic() - partial_since > 5.0:
                raise RPCError("INCOMPLETE_FRAME_TIMEOUT")
            for message in decoder.feed(packet):
                self._validate_notification(message)
                yield message
            partial_since = (partial_since or time.monotonic()) if decoder.pending else None

    @staticmethod
    def _validate_notification(message: Mapping[str, Any]) -> None:
        if (set(message) != {"jsonrpc", "method", "params"} or message["jsonrpc"] != "2.0"
                or message["method"] not in {"state.snapshot", "state.delta", "state.resnapshot", "alarm.raised", "history.changed", "bootstrap.progress"}
                or not isinstance(message["params"], dict)):
            raise RPCError("INVALID_NOTIFICATION")

    def close(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def __enter__(self) -> "UnixRPCClient":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
