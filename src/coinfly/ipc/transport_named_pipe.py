"""SID-restricted local Win32 transport using the same bounded RPC protocol."""
from __future__ import annotations

import os
import socket
import threading
from dataclasses import replace

from . import windows_native
from .protocol import RPCError
from .transport_unix import UnixRPCClient, UnixRPCServer, _Connection


def default_pipe_path() -> str:
    return windows_native.pipe_path(windows_native.process_sid(os.getpid()))


class NamedPipeRPCServer(UnixRPCServer):
    def start(self) -> "NamedPipeRPCServer":
        if self._listener is not None:
            raise RPCError("SERVER_ALREADY_STARTED")
        self._listener = windows_native.new_server_stream(str(self.path), first=True)
        self._thread = threading.Thread(target=self._accept, name="coinfly-pipe-listen", daemon=True)
        self._thread.start()
        return self

    def _accept(self) -> None:
        while not self._stopped.is_set():
            connection = self._listener
            if connection is None:
                return
            try:
                connection._io("ConnectNamedPipe")
            except socket.timeout:
                continue
            except OSError as error:
                if error.errno == 232 and not self._stopped.is_set():
                    # A peer can disconnect before ConnectNamedPipe completes.
                    # Reset this instance without releasing endpoint ownership.
                    try:
                        connection.reset_listener()
                    except OSError:
                        return
                    continue
                return
            try:
                # Maintain a listening instance before handing off the connected
                # stream, preventing a name-ownership gap between clients.
                self._listener = windows_native.new_server_stream(str(self.path), first=False)
                peer = connection.peer()
                connection.settimeout(5.0)
                with self._lock:
                    if len(self._connections) >= self.max_connections:
                        raise RPCError("CONNECTION_LIMIT")
                    self._connection_counter += 1
                    client = _Connection(self, connection, replace(peer, connection_id=self._connection_counter))
                    self._connections.add(client)
                try:
                    client.start()
                except BaseException:
                    self._remove(client)
                    raise
            except (RPCError, OSError, RuntimeError):
                connection.close()
                if self._listener is connection:
                    return

    def close(self) -> None:
        self._stopped.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            clients = tuple(self._connections)
        for client in clients:
            client.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        # No filesystem paths are removed for a kernel-owned pipe endpoint.


class NamedPipeRPCClient(UnixRPCClient):
    def connect(self) -> None:
        if self._socket is not None:
            return
        connection = windows_native.connect(str(self.path), self.timeout)
        try:
            peer = connection.peer()
            if self.peer_verifier is not None and not self.peer_verifier(peer):
                raise RPCError("WORKER_SIGNATURE_UNVERIFIED")
            self._socket = connection
            if self.identity is not None:
                self.handshake = self._exchange(self._request("worker.hello", self.identity, epoch=0))
        except BaseException:
            connection.close()
            self._socket = None
            raise
