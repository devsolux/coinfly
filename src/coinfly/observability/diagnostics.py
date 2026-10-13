"""Redacted failure diagnostics: exception chains, build identity and a bounded log.

Nothing here may carry credentials, filesystem paths, exception messages or
request payloads. A redacted chain keeps only exception class names, stable
``code`` attributes, module names of import failures and ``errno`` symbols.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any

MAX_CHAIN_LINKS = 8
LOG_FILE_BYTES = 1_048_576
LOG_FILE_BACKUPS = 3
_fingerprint_lock = Lock()
_fingerprint: str | None = None
_configured_logs: set[Path] = set()
_process_start_monotonic = time.monotonic()


@dataclass(frozen=True, slots=True)
class ExceptionLink:
    """One exception in a ``__cause__``/``__context__`` chain, redacted."""

    type: str
    code: str | None = None
    module: str | None = None
    errno: str | None = None
    relation: str = "raised"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type, "relation": self.relation}
        if self.code is not None:
            result["code"] = self.code
        if self.module is not None:
            result["module"] = self.module
        if self.errno is not None:
            result["errno"] = self.errno
        return result


def stable_code(error: BaseException) -> str | None:
    """The stable nonsensitive code an exception carries, if any."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and 1 <= len(code) <= 80 and code.replace("_", "").isalnum():
        return code
    return None


def _module_name(error: BaseException) -> str | None:
    if isinstance(error, ImportError):
        name = getattr(error, "name", None)
        if isinstance(name, str) and 0 < len(name) <= 160 and all(part.isidentifier() for part in name.split(".")):
            return name
    return None


def _errno_symbol(error: BaseException) -> str | None:
    if isinstance(error, OSError) and isinstance(error.errno, int):
        return errno.errorcode.get(error.errno, f"E{error.errno}")
    return None


def redacted_chain(error: BaseException) -> tuple[ExceptionLink, ...]:
    """Walk ``__cause__`` then ``__context__`` without ever reading ``str(error)``."""
    links: list[ExceptionLink] = []
    seen: set[int] = set()
    current: BaseException | None = error
    relation = "raised"
    while current is not None and len(links) < MAX_CHAIN_LINKS and id(current) not in seen:
        seen.add(id(current))
        kind = type(current)
        links.append(ExceptionLink(
            type=f"{kind.__module__}.{kind.__qualname__}" if kind.__module__ != "builtins" else kind.__qualname__,
            code=stable_code(current), module=_module_name(current), errno=_errno_symbol(current), relation=relation,
        ))
        if current.__cause__ is not None:
            current, relation = current.__cause__, "caused_by"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, relation = current.__context__, "during"
        else:
            break
    return tuple(links)


def code_origin(error: BaseException) -> str | None:
    """Innermost traceback frame inside the ``coinfly`` package, as ``path:line function``.

    Code locations only — never locals, arguments or exception text — so a
    defect such as an unexpected validation error can be located from the
    worker log or the status snapshot without exposing any payload.
    """
    frame_location: str | None = None
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        normalized = code.co_filename.replace("\\", "/")
        marker = normalized.rfind("/coinfly/")
        if marker >= 0:
            frame_location = f"{normalized[marker + 1:]}:{traceback.tb_lineno} {code.co_name}"
        traceback = traceback.tb_next
    return frame_location


def chain_summary(chain: tuple[ExceptionLink, ...]) -> str:
    """A compact single-line rendering for logs and desktop diagnostics."""
    parts = []
    for link in chain:
        text = link.type.rsplit(".", 1)[-1]
        if link.code:
            text += f"[{link.code}]"
        elif link.module:
            text += f"[{link.module}]"
        elif link.errno:
            text += f"[{link.errno}]"
        parts.append(text)
    return " <- ".join(parts)


def _package_root() -> Path:
    import coinfly

    return Path(coinfly.__file__).resolve().parent


def source_fingerprint() -> str:
    """SHA-256 over the Python sources of this ``coinfly`` package tree, computed once.

    Desktop and worker compare fingerprints to detect a worker process that kept
    running after the checkout or the application bundle was replaced. Frozen
    builds collect ``pyz+py`` sources, so the same walk applies there. The value
    is cached for the process lifetime: it identifies the code this process
    started with, not whatever is on disk later.
    """
    global _fingerprint
    with _fingerprint_lock:
        if _fingerprint is not None:
            return _fingerprint
        digest = hashlib.sha256()
        root = _package_root()
        files = sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        _fingerprint = digest.hexdigest()
        return _fingerprint


def runtime_identity() -> dict[str, Any]:
    """Nonsensitive process identity for the desktop's stale-worker diagnostics."""
    return {
        "source_fingerprint": source_fingerprint(),
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "frozen": bool(getattr(sys, "frozen", False)),
        "pid": os.getpid(),
        "started_at_monotonic": round(_process_start_monotonic, 3),
    }


@lru_cache(maxsize=1)
def build_identity() -> Mapping[str, Any]:
    """Immutable identity of the code this process started with, captured once at startup.

    Application id, version, protocol version and the source fingerprint;
    the desktop and the worker exchange it in the ``worker.hello`` handshake so
    a mismatch is refused per connection instead of failing lazily later.
    """
    from datetime import datetime, timezone
    from types import MappingProxyType

    from coinfly import __application_id__, __version__
    from coinfly.ipc.protocol import PROTOCOL_VERSION

    return MappingProxyType({
        "application_id": __application_id__, "version": __version__, "protocol_version": PROTOCOL_VERSION,
        "source_fingerprint": source_fingerprint(), "python": ".".join(str(part) for part in sys.version_info[:3]),
        "frozen": bool(getattr(sys, "frozen", False)), "captured_at": datetime.now(timezone.utc).isoformat(),
    })


class _RedactingFormatter(logging.Formatter):
    """Never format exception text: records carry only redacted chain summaries."""

    def formatException(self, exc_info: Any) -> str:  # noqa: N802 - logging API name
        error = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
        if isinstance(error, BaseException):
            origin = code_origin(error)
            return "exception-chain: " + chain_summary(redacted_chain(error)) + (f" at {origin}" if origin else "")
        return "exception-chain: <unavailable>"

    def format(self, record: logging.LogRecord) -> str:
        record.exc_text = None  # Force our redacted formatter on every render.
        return super().format(record)


def configure_worker_log(directory: Path, profile: str) -> Path | None:
    """Attach one bounded rotating log file for the worker process (idempotent).

    The file lives under the application's ``logs/`` folder and holds lifecycle
    stages, stable codes and redacted exception chains. It is created only by
    worker entry points, never by tests or the desktop.
    """
    if not profile.replace("-", "").isalnum():
        raise ValueError("log profile must be alphanumeric")
    path = directory / f"worker-{profile}.log"
    with _fingerprint_lock:
        if path in _configured_logs:
            return path
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
        except OSError:
            return None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("coinfly")
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        _configured_logs.add(path)
        return path


def configured_log_names() -> tuple[str, ...]:
    """File names (never directories) of the log files this process writes."""
    with _fingerprint_lock:
        return tuple(sorted(path.name for path in _configured_logs))


def failure_record(error: BaseException, *, stage: str, recorded_at: Any) -> dict[str, Any]:
    """A JSON-safe redacted failure record for status snapshots."""
    chain = redacted_chain(error)
    record = {"stage": stage, "code": stable_code(error) or "OPERATION_FAILED",
              "type": chain[0].type if chain else type(error).__name__, "summary": chain_summary(chain),
              "chain": [link.as_dict() for link in chain],
              "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else str(recorded_at)}
    origin = code_origin(error)
    if origin is not None:
        record["origin"] = origin
    return record
