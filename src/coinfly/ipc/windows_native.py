"""Win32 local byte streams. No TCP, pickle, remote clients, or default DACL.

Win32 APIs are bound lazily so importing the desktop on Unix never loads a
Windows library. Each operation owns a manual-reset event and OVERLAPPED
structure until completion, including cancellation. Native target tests are
required in addition to the portable contract tests.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import socket
import threading
from ctypes import wintypes as w
from functools import lru_cache
from typing import Any

from .protocol import RPCError

SID_PATTERN = re.compile(r"S-1-(?:[0-9]+-)*[0-9]+\Z")
PIPE_PATTERN = re.compile(r"coinfly-[a-f0-9]{24}(?:-[a-f0-9]{16,64})?\Z")
INVALID_HANDLE = ctypes.c_void_p(-1).value
IO_PENDING, PIPE_CONNECTED, BROKEN_PIPE = 997, 535, 109


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


@lru_cache(maxsize=1)
def api() -> Any:
    if os.name != "nt":
        raise RPCError("WINDOWS_IPC_REQUIRES_WINDOWS")
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    advapi = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    signatures = {
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateEventW": ([w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "CancelIoEx": ([w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL),
        "GetOverlappedResult": ([w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL),
        "ReadFile": ([w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)], w.BOOL),
        "WriteFile": ([w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)], w.BOOL),
        "CreateNamedPipeW": ([w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
                              ctypes.POINTER(SecurityAttributes)], w.HANDLE),
        "ConnectNamedPipe": ([w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL),
        "DisconnectNamedPipe": ([w.HANDLE], w.BOOL),
        "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        "WaitNamedPipeW": ([w.LPCWSTR, w.DWORD], w.BOOL),
        "GetNamedPipeClientProcessId": ([w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
        "GetNamedPipeServerProcessId": ([w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "LocalFree": ([w.HLOCAL], w.HLOCAL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(kernel, name)
        fn.argtypes = args
        fn.restype = result
    for name, args, result in (
        ("OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        ("GetTokenInformation", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        ("ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        ("ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), w.LPVOID], w.BOOL),
    ):
        fn = getattr(advapi, name)
        fn.argtypes = args
        fn.restype = result
    return kernel, advapi


def _error() -> OSError:
    return OSError(getattr(ctypes, "get_last_error")(), "Native local IPC operation failed")


def process_sid(pid: int) -> str:
    kernel, advapi = api()
    process = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
    token = w.HANDLE()
    try:
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        needed = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        if not 0 < needed.value <= 65536:
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0]
        text = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        try:
            sid = text.value or ""
            if not SID_PATTERN.fullmatch(sid):
                raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
            return sid
        finally:
            kernel.LocalFree(ctypes.cast(text, w.HLOCAL))
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def principal_id(sid: str) -> int:
    """Transport owner key, not a Windows RID or a substitute for a SID check."""
    if not SID_PATTERN.fullmatch(sid):
        raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
    return int.from_bytes(hashlib.sha256(sid.encode("ascii")).digest(), "big")


def pipe_path(sid: str, checkout: str | None = None) -> str:
    principal_id(sid)
    suffix = hashlib.sha256(sid.encode("ascii")).hexdigest()[:24]
    if checkout is not None:
        if not re.fullmatch(r"[a-f0-9]{16,64}", checkout):
            raise RPCError("UNSAFE_PIPE_PATH")
        suffix += "-" + checkout
    return "\\\\.\\pipe\\coinfly-" + suffix


def validate_path(path: str) -> str:
    prefix = "\\\\.\\pipe\\"
    if not path.startswith(prefix) or not PIPE_PATTERN.fullmatch(path[len(prefix):]):
        raise RPCError("UNSAFE_PIPE_PATH")
    return path


class PipeStream:
    """Socket-like bounded stream for the shared JSON RPC framing layer."""
    def __init__(self, handle: int, *, server: bool, timeout: float = 5.0) -> None:
        self.handle, self.server, self.timeout = handle, server, timeout
        self._closing = threading.Event()
        self._operations = 0
        self._lock = threading.Lock()
        self._closed_handle = False

    def settimeout(self, timeout: float) -> None:
        if not 0 < timeout <= 60:
            raise ValueError("Invalid IPC timeout")
        self.timeout = timeout

    def _io(self, name: str, buffer: Any = None, size: int = 0) -> int:
        kernel, _ = api()
        with self._lock:
            if self._closing.is_set():
                raise OSError("IPC connection closed")
            self._operations += 1
        event = kernel.CreateEventW(None, True, False, None)
        if not event:
            self._release_operation()
            raise _error()
        overlapped = Overlapped(hEvent=event)
        transferred = w.DWORD()
        try:
            if name == "ConnectNamedPipe":
                ok = kernel.ConnectNamedPipe(self.handle, ctypes.byref(overlapped))
            else:
                ok = getattr(kernel, name)(self.handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            error = getattr(ctypes, "get_last_error")() if not ok else 0
            if name == "ConnectNamedPipe" and error == PIPE_CONNECTED:
                return 0
            if not ok and error != IO_PENDING:
                if error in (BROKEN_PIPE, 232) and name == "ReadFile":
                    return 0
                raise _error()
            if not ok:
                wait = kernel.WaitForSingleObject(event, max(1, int(self.timeout * 1000)))
                if wait != 0:
                    # Keep the OVERLAPPED, event, and buffer alive until the OS
                    # confirms cancellation/completion; never free in-flight I/O.
                    kernel.CancelIoEx(self.handle, ctypes.byref(overlapped))
                    kernel.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(transferred), True)
                    if wait == 258:
                        raise socket.timeout("IPC operation timed out")
                    raise OSError("IPC wait failed")
            if not kernel.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
                if getattr(ctypes, "get_last_error")() in (BROKEN_PIPE, 232) and name == "ReadFile":
                    return 0
                raise _error()
            return int(transferred.value)
        finally:
            kernel.CloseHandle(event)
            self._release_operation()

    def _release_operation(self) -> None:
        with self._lock:
            self._operations -= 1
            if self._closing.is_set() and self._operations == 0 and not self._closed_handle:
                api()[0].CloseHandle(self.handle)
                self._closed_handle = True

    def reset_listener(self) -> None:
        if not self.server or self._closing.is_set():
            raise OSError("IPC listener unavailable")
        kernel, _ = api()
        if not kernel.DisconnectNamedPipe(self.handle) and getattr(ctypes, "get_last_error")() != 233:
            raise _error()

    def recv(self, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(min(size, 65536))
        count = self._io("ReadFile", buffer, len(buffer))
        return buffer.raw[:count]

    def sendall(self, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            part = payload[offset:offset + 65536]
            buffer = ctypes.create_string_buffer(part)
            sent = self._io("WriteFile", buffer, len(part))
            if sent <= 0:
                raise OSError("IPC connection closed")
            offset += sent

    def peer(self) -> Any:
        from .auth import PeerIdentity
        pid = w.ULONG()
        fn = api()[0].GetNamedPipeClientProcessId if self.server else api()[0].GetNamedPipeServerProcessId
        if not fn(self.handle, ctypes.byref(pid)):
            raise RPCError("NATIVE_PEER_AUTH_UNAVAILABLE")
        sid = process_sid(pid.value)
        if sid != process_sid(os.getpid()):
            raise RPCError("PEER_OWNER_MISMATCH")
        identity = principal_id(sid)
        return PeerIdentity(identity, identity, pid.value)

    def shutdown(self, how: int = 2) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closing.is_set():
                return
            self._closing.set()
            api()[0].CancelIoEx(self.handle, None)
            if self._operations == 0 and not self._closed_handle:
                api()[0].CloseHandle(self.handle)
                self._closed_handle = True


def new_server_stream(path: str, *, first: bool) -> PipeStream:
    kernel, advapi = api()
    validate_path(path)
    sid = process_sid(os.getpid())
    descriptor = w.LPVOID()
    # Protected DACL: the native account only. No Everyone/Anonymous defaults.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;GA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise RPCError("PIPE_SECURITY_DESCRIPTOR_FAILED")
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        flags = 0x3 | 0x40000000 | (0x80000 if first else 0)  # duplex, overlapped, first instance
        handle = kernel.CreateNamedPipeW(path, flags, 0x8, 255, 65536, 65536, 5000, ctypes.byref(attrs))
        # byte mode + PIPE_REJECT_REMOTE_CLIENTS; framing remains bounded JSON
        if handle in (None, INVALID_HANDLE):
            raise _error()
        return PipeStream(handle, server=True, timeout=0.25)
    finally:
        kernel.LocalFree(descriptor)


def connect(path: str, timeout: float) -> PipeStream:
    kernel, _ = api()
    validate_path(path)
    # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION prevents server impersonation.
    handle = kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000 | 0x100000 | 0x10000, None)
    if handle == INVALID_HANDLE:
        if getattr(ctypes, "get_last_error")() != 231 or not kernel.WaitNamedPipeW(path, int(timeout * 1000)):
            raise _error()
        handle = kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000 | 0x100000 | 0x10000, None)
    if handle in (None, INVALID_HANDLE):
        raise _error()
    stream = PipeStream(handle, server=False, timeout=timeout)
    try:
        stream.peer()  # Authenticate even before the higher-level hello.
        return stream
    except BaseException:
        stream.close()
        raise
