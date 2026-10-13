"""Strict JSON-RPC subset with bounded framing, context epochs and deadlines.

No request is retried by the transport. Durable mutation receipts belong to the
worker, and a lost reply must be resolved using the original request ID.
"""

from __future__ import annotations

import json
import math
import re
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# The IPC contract version: frame shape, methods and their parameters. A desktop
# and a worker with different versions refuse each other at the handshake.
PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 262_144
MAX_DEPTH = 24
MAX_ITEMS = 20_000
MAX_DEADLINE_MS = 30_000
METHODS = frozenset({
    "state.snapshot", "state.subscribe", "state.unsubscribe", "operation.get", "operation.retry",
    "context.preview", "context.apply", "engine.arm", "history.open", "history.page",
    "history.close", "slot.create", "slot.start", "slot.edit", "slot.stop", "slot.delete",
    "safety.pause_entries", "safety.stop", "alarm.acknowledge",
    "bootstrap.status", "bootstrap.pause", "bootstrap.retry", "bootstrap.cancel",
    "bootstrap.network_settings", "credentials.enroll", "credentials.status",
    "worker.shutdown", "worker.hello", "automation.configure", "automation.enable", "automation.resume_entries",
    "automation.pause_entries", "automation.stop", "automation.status",
    "live.prerequisites", "live.record_mandate", "live.revoke_mandate",
})
READ_METHODS = frozenset({
    "worker.hello", "state.snapshot", "state.subscribe", "state.unsubscribe", "operation.get",
    "context.preview", "history.open", "history.page", "history.close",
    "bootstrap.status", "credentials.status", "automation.status", "live.prerequisites",
})
OWNER_METHODS = frozenset({
    "bootstrap.pause", "bootstrap.retry", "bootstrap.cancel", "bootstrap.network_settings",
    "alarm.acknowledge", "worker.shutdown", "live.record_mandate", "live.revoke_mandate",
})
_METHOD = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class RPCError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def freeze(value: Any) -> Any:
    """Copy incoming DTOs; a reader can never mutate the render snapshot."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    return value


def _validate_tree(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_ITEMS]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise RPCError("MESSAGE_LIMIT")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or len(key) > 160 for key in value):
            raise RPCError("INVALID_OBJECT_KEY")
        for item in value.values():
            _validate_tree(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _validate_tree(item, depth + 1, budget)
    elif isinstance(value, float) and not math.isfinite(value):
        raise RPCError("NONFINITE_NUMBER")
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**63 - 1:
        raise RPCError("INTEGER_LIMIT")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise RPCError("INVALID_JSON_VALUE")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RPCError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode_json(payload: bytes) -> dict[str, Any]:
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise RPCError("FRAME_LIMIT")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RPCError("INVALID_JSON") from exc
    _validate_tree(value)
    if not isinstance(value, dict):
        raise RPCError("OBJECT_REQUIRED")
    return value


def encode_frame(value: Mapping[str, Any]) -> bytes:
    plain = thaw(value)
    _validate_tree(plain)
    try:
        payload = json.dumps(plain, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise RPCError("INVALID_JSON_VALUE") from exc
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise RPCError("FRAME_LIMIT")
    return struct.pack("!I", len(payload)) + payload


class FrameDecoder:
    """Incremental decoder. Oversized claims are rejected before body allocation."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    @property
    def pending(self) -> bool:
        return bool(self._buffer) or self._expected is not None

    def feed(self, data: bytes) -> tuple[dict[str, Any], ...]:
        if len(data) > MAX_FRAME_BYTES + 4:
            raise RPCError("FRAME_LIMIT")
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < 4:
                    break
                self._expected = struct.unpack("!I", self._buffer[:4])[0]
                del self._buffer[:4]
                if not 0 < self._expected <= MAX_FRAME_BYTES:
                    raise RPCError("FRAME_LIMIT")
            if len(self._buffer) < self._expected:
                break
            payload = bytes(self._buffer[:self._expected])
            del self._buffer[:self._expected]
            self._expected = None
            messages.append(decode_json(payload))
        if len(self._buffer) > MAX_FRAME_BYTES:
            raise RPCError("FRAME_LIMIT")
        return tuple(messages)


@dataclass(frozen=True, slots=True)
class RPCRequest:
    request_id: str
    method: str
    params: Mapping[str, Any]
    epoch: int
    deadline_ms: int

    @classmethod
    def parse(cls, data: Mapping[str, Any], *, now_ms: int | None = None) -> "RPCRequest":
        if set(data) != {"jsonrpc", "id", "method", "params", "epoch", "deadline_ms"}:
            raise RPCError("INVALID_REQUEST_FIELDS")
        if data["jsonrpc"] != "2.0":
            raise RPCError("INVALID_RPC_VERSION")
        request_id, method = data["id"], data["method"]
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise RPCError("INVALID_REQUEST_ID")
        if not isinstance(method, str) or not _METHOD.fullmatch(method) or method not in METHODS:
            raise RPCError("METHOD_NOT_ALLOWED")
        params, epoch, deadline = data["params"], data["epoch"], data["deadline_ms"]
        if not isinstance(params, Mapping):
            raise RPCError("OBJECT_PARAMS_REQUIRED")
        if type(epoch) is not int or not 0 <= epoch < 2**63:
            raise RPCError("INVALID_EPOCH")
        if type(deadline) is not int:
            raise RPCError("INVALID_DEADLINE")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if deadline <= now:
            raise RPCError("DEADLINE_EXPIRED")
        if deadline > now + MAX_DEADLINE_MS:
            raise RPCError("DEADLINE_TOO_LONG")
        return cls(request_id, method, freeze(params), epoch, deadline)

    def as_dict(self) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": self.request_id, "method": self.method,
                "params": thaw(self.params), "epoch": self.epoch, "deadline_ms": self.deadline_ms}


def result_response(request_id: str, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": thaw(result)}


def error_response(request_id: str | None, code: str) -> dict[str, Any]:
    # Exception text can contain credentials. Only stable codes leave the worker.
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": code}}
