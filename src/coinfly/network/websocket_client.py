"""Minimal, bounded RFC 6455 WebSocket client (text frames) over pinned TLS.

No third-party dependency exists for WebSockets in this project, so the client
implements exactly what the market feed needs: one connection, an HTTP/1.1
upgrade with a validated ``Sec-WebSocket-Accept``, masked client frames, server
frames up to a hard byte limit, ping/pong, close, and a read timeout that turns
a silent peer into a detectable disconnect. There is no compression, no
fragmentation of outgoing frames, no proxy, no redirect and no retry inside the
client: reconnection policy belongs to the feed that owns it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from coinfly.domain.context import DomainError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024
OPCODE_CONTINUATION, OPCODE_TEXT, OPCODE_BINARY, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def pinned_context() -> ssl.SSLContext:
    import certifi

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=certifi.where())
    return context


def encode_frame(payload: bytes, *, opcode: int = OPCODE_TEXT, mask: bool = True, fin: bool = True) -> bytes:
    """Encode one frame; clients MUST mask, servers MUST NOT (RFC 6455 §5.1)."""
    if len(payload) > MAX_FRAME_BYTES:
        raise DomainError("WEBSOCKET_FRAME_TOO_LARGE")
    header = bytearray([(0x80 if fin else 0) | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if mask:
        key = os.urandom(4)
        header += key
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + payload


@dataclass(frozen=True, slots=True)
class Frame:
    opcode: int
    payload: bytes
    fin: bool


class FrameReader:
    """Incremental frame decoder with a hard size bound; usable by client and test server."""

    def __init__(self, *, expect_masked: bool) -> None:
        self._buffer = bytearray()
        self._expect_masked = expect_masked
        self._fragments: list[bytes] = []
        self._fragment_opcode: int | None = None

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        if len(self._buffer) > MAX_FRAME_BYTES + 14:
            raise DomainError("WEBSOCKET_FRAME_TOO_LARGE")
        frames: list[Frame] = []
        while True:
            frame = self._next()
            if frame is None:
                return frames
            if frame.opcode in (OPCODE_TEXT, OPCODE_BINARY, OPCODE_CONTINUATION):
                assembled = self._assemble(frame)
                if assembled is not None:
                    frames.append(assembled)
            else:
                frames.append(frame)

    def _assemble(self, frame: Frame) -> Frame | None:
        if frame.opcode == OPCODE_CONTINUATION:
            if self._fragment_opcode is None:
                raise DomainError("WEBSOCKET_PROTOCOL_ERROR")
            self._fragments.append(frame.payload)
        else:
            if self._fragment_opcode is not None:
                raise DomainError("WEBSOCKET_PROTOCOL_ERROR")
            if frame.fin:
                return frame
            self._fragment_opcode = frame.opcode
            self._fragments = [frame.payload]
        if sum(len(part) for part in self._fragments) > MAX_FRAME_BYTES:
            raise DomainError("WEBSOCKET_FRAME_TOO_LARGE")
        if frame.fin:
            opcode = self._fragment_opcode or OPCODE_TEXT
            payload = b"".join(self._fragments)
            self._fragments, self._fragment_opcode = [], None
            return Frame(opcode, payload, True)
        return None

    def _next(self) -> Frame | None:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        if first & 0x70:
            raise DomainError("WEBSOCKET_RESERVED_BITS")
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        if masked != self._expect_masked:
            raise DomainError("WEBSOCKET_MASKING_VIOLATION")
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length = struct.unpack("!H", buffer[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length = struct.unpack("!Q", buffer[2:10])[0]
            offset = 10
        if length > MAX_FRAME_BYTES:
            raise DomainError("WEBSOCKET_FRAME_TOO_LARGE")
        if masked:
            if len(buffer) < offset + 4:
                return None
            key = bytes(buffer[offset:offset + 4])
            offset += 4
        else:
            key = b""
        if len(buffer) < offset + length:
            return None
        payload = bytes(buffer[offset:offset + length])
        del buffer[:offset + length]
        if masked:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        return Frame(opcode, payload, fin)


class WebSocketClient:
    """One connection; ``receive`` returns text messages, answers pings, detects silence."""

    def __init__(self, url: str, *, timeout: float = 15.0, tls_context: ssl.SSLContext | None = None,
                 connect_timeout: float = 10.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("wss", "ws") or not parsed.hostname or parsed.username or parsed.password:
            raise DomainError("WEBSOCKET_URL_INVALID")
        self._parsed = parsed
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._tls = tls_context if tls_context is not None else (pinned_context() if parsed.scheme == "wss" else None)
        self._socket: socket.socket | None = None
        self._reader = FrameReader(expect_masked=False)
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def connect(self) -> None:
        parsed = self._parsed
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw = socket.create_connection((parsed.hostname, port), timeout=self._connect_timeout)
        try:
            sock: socket.socket = self._tls.wrap_socket(raw, server_hostname=parsed.hostname) if self._tls is not None else raw
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
            request = (f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: coinfly/1.0.0\r\n\r\n")
            sock.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise DomainError("WEBSOCKET_HANDSHAKE_FAILED")
                response.extend(chunk)
                if len(response) > MAX_HANDSHAKE_BYTES:
                    raise DomainError("WEBSOCKET_HANDSHAKE_FAILED")
            head, _, rest = bytes(response).partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if not lines or " 101 " not in lines[0]:
                raise DomainError("WEBSOCKET_HANDSHAKE_REJECTED")
            headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expected or headers.get("upgrade", "").lower() != "websocket":
                raise DomainError("WEBSOCKET_HANDSHAKE_FAILED")
            sock.settimeout(self._timeout)
            self._socket = sock
            if rest:
                self._pending = self._reader.feed(rest)
            else:
                self._pending = []
        except BaseException:
            raw.close()
            raise

    _pending: list[Frame]

    def send_text(self, text: str) -> None:
        with self._lock:
            if self._socket is None or self._closed:
                raise DomainError("WEBSOCKET_NOT_CONNECTED")
            self._socket.sendall(encode_frame(text.encode("utf-8"), opcode=OPCODE_TEXT))

    def receive(self) -> str | None:
        """Next text message; None when the peer closed. Raises on silence past the timeout."""
        if self._socket is None:
            raise DomainError("WEBSOCKET_NOT_CONNECTED")
        while True:
            while self._pending:
                frame = self._pending.pop(0)
                if frame.opcode == OPCODE_TEXT:
                    try:
                        return frame.payload.decode("utf-8")
                    except UnicodeDecodeError:
                        raise DomainError("WEBSOCKET_INVALID_UTF8") from None
                if frame.opcode == OPCODE_PING:
                    with self._lock:
                        if not self._closed:
                            self._socket.sendall(encode_frame(frame.payload, opcode=OPCODE_PONG))
                elif frame.opcode == OPCODE_CLOSE:
                    self.close()
                    return None
                # binary and pong frames are ignored
            try:
                chunk = self._socket.recv(65536)
            except socket.timeout:
                raise DomainError("WEBSOCKET_SILENT_TIMEOUT") from None
            if not chunk:
                self.close()
                return None
            self._pending = self._reader.feed(chunk)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._socket is not None:
                try:
                    self._socket.sendall(encode_frame(struct.pack("!H", 1000), opcode=OPCODE_CLOSE))
                except OSError:
                    pass
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._socket.close()
