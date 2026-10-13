"""Bounded single-writer SQLite transaction domain; never run network I/O here."""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import localcontext
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread, get_ident
from typing import Any, Callable, TypeVar, cast

from coinfly.domain.context import DomainError
from coinfly.domain.decimal_math import financial_context

T = TypeVar("T")
MINIMUM_SQLITE = (3, 51, 3)
_path_guard = Lock()
_active_paths: set[Path] = set()
logger = logging.getLogger(__name__)


def database_locked(error: BaseException) -> bool:
    """Use SQLite result codes, including extended BUSY/LOCKED codes."""
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 0xFF
    ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@dataclass(frozen=True)
class _Job:
    operation: Callable[[sqlite3.Connection], Any]
    future: Future[Any]


class Writer:
    """One connection and one thread, with a transaction for each queued callable.

    A callable may compose repository ``*_tx`` functions to commit several domain
    changes atomically. It must not commit, close the connection, return a cursor,
    wait for other actors, or perform network I/O. Exceptions roll back the entire
    operation. A Future resolves only after FULL commit; publication belongs after
    result delivery and risk invalidation. This is a local store, not a host lease.
    """

    def __init__(self, path: str | Path, *, capacity: int = 512,
                 startup_timeout: float = 10.0,
                 startup_progress: Callable[[str], None] | None = None) -> None:
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
            raise DomainError("SQLITE_VERSION_UNSUPPORTED", "SQLite >=3.51.3 is required")
        if capacity < 1:
            raise ValueError("positive writer queue capacity required")
        if not 0 < startup_timeout <= 60:
            raise ValueError("startup timeout must be between zero and 60 seconds")
        if str(path) == ":memory:":
            raise DomainError("DURABLE_PATH_REQUIRED")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._queue: Queue[_Job | None] = Queue(maxsize=capacity)
        self._lock = Lock()
        self._closed = False
        self._thread_id: int | None = None
        self._ready: Future[None] = Future()
        self._startup_cancelled = Event()
        self._startup_deadline = time.monotonic() + startup_timeout
        self._startup_progress = startup_progress
        self._thread = Thread(target=self._run, name="coinfly-sqlite-writer", daemon=False)
        with _path_guard:
            if self.path in _active_paths:
                raise DomainError("WRITER_PATH_ALREADY_OWNED")
            _active_paths.add(self.path)
        try:
            self._thread.start()
        except BaseException:
            with _path_guard:
                _active_paths.discard(self.path)
            raise
        try:
            self._ready.result(timeout=startup_timeout)
        except TimeoutError:
            self._startup_cancelled.set()
            self._closed = True
            self._queue.put_nowait(None)  # Also handles readiness racing the deadline.
            raise DomainError("DATABASE_INITIALIZATION_TIMEOUT") from None
        except BaseException:
            # An interrupted wait must not join a healthy writer thread that has
            # already become ready and parked on the queue: cancel the startup
            # and send the shutdown sentinel exactly like the timeout branch.
            self._closed = True
            self._startup_cancelled.set()
            self._queue.put_nowait(None)
            self._thread.join()
            raise

    def _initialize(self, connection: sqlite3.Connection) -> None:
        schema = Path(__file__).with_name("schema.sql")
        from hashlib import sha256

        ddl = schema.read_text(encoding="utf-8")
        digest = sha256(ddl.encode("utf-8")).hexdigest()

        def verify_format() -> None:
            # Reading sqlite_master takes a shared lock, so this check runs inside
            # the lock-retry loop below: a ledger held by another process is
            # retried until the startup deadline, never reported as a failure.
            # The marker is inserted in the SAME bootstrap transaction as the DDL.
            # A pre-refactor ledger is preserved, never imported or silently reset.
            marker = connection.execute("SELECT 1 FROM sqlite_master WHERE name='cf_application_format'").fetchone()
            if marker is None:
                existing = connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
                if existing is not None:
                    raise DomainError("FRESH_INSTALL_REQUIRED")
            else:
                row = connection.execute("SELECT application_version,schema_sha256 FROM cf_application_format WHERE singleton=1").fetchone()
                if row is None or tuple(row) != ("1.0.0", digest):
                    raise DomainError("SCHEMA_CONTRACT_MISMATCH")
                from coinfly.storage.component_schema import require_schema_objects, script_statements
                require_schema_objects(connection, script_statements(ddl))

        marker_sql = (
            "CREATE TABLE IF NOT EXISTS cf_application_format (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "application_version TEXT NOT NULL,schema_sha256 TEXT NOT NULL);\n"
            f"INSERT OR IGNORE INTO cf_application_format VALUES(1,'1.0.0','{digest}');\n"
        )
        statements = "BEGIN IMMEDIATE;\n" + marker_sql + ddl + "\nCOMMIT;"
        connection.set_progress_handler(
            lambda: int(self._startup_cancelled.is_set() or time.monotonic() >= self._startup_deadline), 1000)
        try:
            while not self._startup_cancelled.is_set():
                remaining = self._startup_deadline - time.monotonic()
                if remaining <= 0:
                    break
                connection.execute(f"PRAGMA busy_timeout={max(1, min(100, int(remaining * 1000)))}")
                try:
                    verify_format()
                    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if str(mode).lower() != "wal":
                        raise DomainError("SQLITE_WAL_REQUIRED")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.executescript(statements)
                    self.path.chmod(0o600)
                    return
                except sqlite3.OperationalError as error:
                    connection.rollback()
                    if not database_locked(error):
                        if time.monotonic() >= self._startup_deadline or self._startup_cancelled.is_set():
                            break
                        raise
                    logger.warning("Database locked by another process - Retrying...")
                    if self._startup_progress is not None:
                        self._startup_progress("DATABASE_LOCKED_RETRYING")
                    self._startup_cancelled.wait(min(0.2, max(0, self._startup_deadline - time.monotonic())))
            raise DomainError("DATABASE_INITIALIZATION_TIMEOUT")
        finally:
            connection.set_progress_handler(None, 0)

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        self._thread_id = get_ident()
        try:
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            self._initialize(connection)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA wal_autocheckpoint=1000")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise DomainError("SQLITE_FULL_REQUIRED")
            if self._startup_cancelled.is_set():
                raise DomainError("DATABASE_INITIALIZATION_TIMEOUT")
            self._ready.set_result(None)
            while True:
                job = self._queue.get()
                try:
                    if job is None:
                        break
                    if not job.future.set_running_or_notify_cancel():
                        continue
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        # A caller's localcontext does not follow a queued job.
                        # Each transaction also discards context mutations left
                        # by a preceding callback on this long-lived thread.
                        with localcontext(financial_context(512)):
                            value = job.operation(connection)
                        if isinstance(value, (sqlite3.Connection, sqlite3.Cursor)):
                            raise DomainError("WRITER_HANDLE_ESCAPE")
                        if not connection.in_transaction:
                            raise DomainError("WRITER_TRANSACTION_ESCAPED")
                        connection.commit()
                    except BaseException as error:
                        connection.rollback()
                        job.future.set_exception(error)
                    else:
                        job.future.set_result(value)
                finally:
                    self._queue.task_done()
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)
            else:
                # Never leave accepted jobs hanging after a terminal writer error.
                with self._lock:
                    self._closed = True
                while not self._queue.empty():
                    job = self._queue.get_nowait()
                    if job is not None and not job.future.done():
                        job.future.set_exception(DomainError("WRITER_FAILED"))
                    self._queue.task_done()
        finally:
            if connection is not None:
                connection.close()
            with _path_guard:
                _active_paths.discard(self.path)

    def submit(self, operation: Callable[[sqlite3.Connection], T]) -> Future[T]:
        if get_ident() == self._thread_id:
            raise DomainError("WRITER_REENTRANT_CALL")
        future: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise DomainError("WRITER_CLOSED")
            try:
                self._queue.put_nowait(_Job(operation, cast("Future[Any]", future)))
            except Full as error:
                raise DomainError("WRITER_BACKPRESSURE") from error
        return future

    def call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return self.submit(operation).result()

    def close(self) -> None:
        if get_ident() == self._thread_id:
            raise DomainError("WRITER_REENTRANT_CLOSE")
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join()

    def __enter__(self) -> Writer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
