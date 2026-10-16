"""RFC 6455 client: framing, masking rules, handshake validation, ping/pong, silence.

The server side is an in-process thread on an AF_UNIX socketpair; the test never
opens an Internet socket (the suite forbids it), so ``socket.create_connection``
is replaced for the duration of each connection.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
from unittest import TestCase, mock

from coinfly.domain.context import DomainError
from coinfly.network.websocket_client import (
    GUID,
    MAX_FRAME_BYTES,
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_TEXT,
    FrameReader,
    WebSocketClient,
    encode_frame,
)


class FramingTests(TestCase):
    def test_roundtrip_all_length_encodings(self) -> None:
        for size in (0, 1, 125, 126, 65535, 65536, 70000):
            payload = bytes(range(256)) * (size // 256 + 1)
            payload = payload[:size]
            reader = FrameReader(expect_masked=True)
            frames = reader.feed(encode_frame(payload, mask=True))
            self.assertEqual(len(frames), 1)
            self.assertEqual((frames[0].opcode, frames[0].payload, frames[0].fin), (OPCODE_TEXT, payload, True))

    def test_partial_delivery_and_fragmentation(self) -> None:
        reader = FrameReader(expect_masked=False)
        first = encode_frame(b"hel", mask=False, fin=False)
        second = encode_frame(b"lo", opcode=0x0, mask=False, fin=True)
        stream = first + second
        assembled = []
        for index in range(len(stream)):
            assembled += reader.feed(stream[index:index + 1])
        self.assertEqual([frame.payload for frame in assembled], [b"hello"])

    def test_masking_rules_and_bounds(self) -> None:
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_MASKING_VIOLATION"):
            FrameReader(expect_masked=True).feed(encode_frame(b"x", mask=False))
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_MASKING_VIOLATION"):
            FrameReader(expect_masked=False).feed(encode_frame(b"x", mask=True))
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_RESERVED_BITS"):
            FrameReader(expect_masked=False).feed(bytes([0xF1, 0x01, 0x41]))
        oversized = bytes([0x81, 0x7F]) + struct.pack("!Q", MAX_FRAME_BYTES + 1)
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_FRAME_TOO_LARGE"):
            FrameReader(expect_masked=False).feed(oversized)
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_PROTOCOL_ERROR"):
            FrameReader(expect_masked=False).feed(encode_frame(b"x", opcode=0x0, mask=False))


class FakeServer:
    """Minimal WebSocket server on one end of a socketpair."""

    def __init__(self, *, accept_correctly: bool = True, status: bytes = b"101 Switching Protocols") -> None:
        self.server, self.client_end = socket.socketpair(socket.AF_UNIX)
        self.accept_correctly = accept_correctly
        self.status = status
        self.received: list[bytes] = []
        self.reader = FrameReader(expect_masked=True)
        self.thread = threading.Thread(target=self.handshake, daemon=True)
        self.thread.start()

    def handshake(self) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.server.recv(4096)
            if not chunk:
                return
            request += chunk
        key = ""
        for line in request.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode() if self.accept_correctly else "bogus"
        self.server.sendall(b"HTTP/1.1 " + self.status + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                            + accept.encode() + b"\r\n\r\n")

    def send(self, payload: bytes, opcode: int = OPCODE_TEXT) -> None:
        self.server.sendall(encode_frame(payload, opcode=opcode, mask=False))

    def read_frames(self, timeout: float = 2.0) -> list:
        self.server.settimeout(timeout)
        return self.reader.feed(self.server.recv(65536))

    def close(self) -> None:
        self.server.close()


class ClientTests(TestCase):
    def connect(self, server: FakeServer, **kwargs: object) -> WebSocketClient:
        client = WebSocketClient("ws://example.invalid/ws", tls_context=None, **kwargs)  # type: ignore[arg-type]
        with mock.patch("coinfly.network.websocket_client.socket.create_connection", return_value=server.client_end):
            client.connect()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        return client

    def test_handshake_send_receive_ping_and_close(self) -> None:
        server = FakeServer()
        client = self.connect(server, timeout=2.0)
        client.send_text('{"type":"subscribe"}')
        frames = server.read_frames()
        self.assertEqual([(frame.opcode, frame.payload) for frame in frames], [(OPCODE_TEXT, b'{"type":"subscribe"}')])
        server.send(b"ping-me", opcode=OPCODE_PING)
        server.send(b'{"channel":"heartbeats"}')
        self.assertEqual(client.receive(), '{"channel":"heartbeats"}')
        pong = server.read_frames()
        self.assertEqual([(frame.opcode, frame.payload) for frame in pong], [(0xA, b"ping-me")])
        server.send(struct.pack("!H", 1000), opcode=OPCODE_CLOSE)
        self.assertIsNone(client.receive())
        self.assertTrue(client.closed)
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_NOT_CONNECTED"):
            client.send_text("late")

    def test_silence_is_detected_as_a_timeout(self) -> None:
        server = FakeServer()
        client = self.connect(server, timeout=0.2)
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_SILENT_TIMEOUT"):
            client.receive()

    def test_bad_accept_or_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_HANDSHAKE_FAILED"):
            self.connect(FakeServer(accept_correctly=False))
        with self.assertRaisesRegex(DomainError, "WEBSOCKET_HANDSHAKE_REJECTED"):
            self.connect(FakeServer(status=b"403 Forbidden"))

    def test_failed_handshake_closes_the_tls_wrapped_socket(self) -> None:
        class DetachingContext:
            """Like SSLContext.wrap_socket: the wrapper takes the descriptor and the raw socket is left detached."""

            wrapped: socket.socket | None = None

            def wrap_socket(self, raw: socket.socket, *, server_hostname: str) -> socket.socket:
                self.wrapped = socket.socket(fileno=raw.detach())
                return self.wrapped

        server = FakeServer(accept_correctly=False)
        self.addCleanup(server.close)
        context = DetachingContext()
        client = WebSocketClient("wss://example.invalid/ws", tls_context=context)  # type: ignore[arg-type]
        with mock.patch("coinfly.network.websocket_client.socket.create_connection", return_value=server.client_end), \
                self.assertRaisesRegex(DomainError, "WEBSOCKET_HANDSHAKE_FAILED"):
            client.connect()
        assert context.wrapped is not None
        self.assertEqual(context.wrapped.fileno(), -1)  # closed, not left for the garbage collector

    def test_url_validation(self) -> None:
        for bad in ("http://example.invalid", "wss://user:pw@example.invalid", "wss:///nohost"):
            with self.assertRaisesRegex(DomainError, "WEBSOCKET_URL_INVALID"):
                WebSocketClient(bad, tls_context=None)
