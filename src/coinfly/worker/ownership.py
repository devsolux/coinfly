"""OS-held ownership keyed only by the stable, attested account authority.

This protects a single host. An external host-fencing certificate is additionally
required for live takeover; a local lock does not make a distributed lease.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path
from types import TracebackType
from typing import Self

from coinfly.domain.context import DomainError
from coinfly.paths import StorageInitializationError, is_link
from coinfly.worker.paths import WorkerPaths


class AuthorityOwnership:
    def __init__(self, authority_id: str, *, lock_directory: Path | None = None) -> None:
        if not authority_id or len(authority_id) > 160:
            raise DomainError("INVALID_AUTHORITY")
        self.authority_id = authority_id
        directory = lock_directory if lock_directory is not None else WorkerPaths.for_current_user().locks
        self.path = directory / (hashlib.sha256(authority_id.encode()).hexdigest() + ".lock")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self.held:
            raise DomainError("OWNERSHIP_ALREADY_HELD")
        parent = self.path.parent
        if is_link(parent) or is_link(self.path):
            raise DomainError("UNSAFE_OWNERSHIP_PATH")
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = parent.stat()
        if sys.platform != "win32" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise DomainError("OWNERSHIP_PATH_NOT_PRIVATE")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            # A denied, read-only or occupied lock path is a storage diagnostic,
            # never a database failure and never a raw OS message with the path.
            raise StorageInitializationError.from_os_error(exc) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DomainError("UNSAFE_OWNERSHIP_FILE")
            if sys.platform == "win32":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise DomainError("OWNERSHIP_FILE_NOT_PRIVATE")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DomainError("AUTHORITY_ALREADY_OWNED") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def assert_held(self) -> None:
        if not self.held:
            raise DomainError("AUTHORITY_NOT_OWNED")

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        # Never unlink a lock inode: other processes may already have opened it.
        os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
