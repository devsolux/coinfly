"""Native account paths. Financial settings never come from process environment."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from coinfly.domain.context import DomainError
from coinfly.paths import ApplicationPaths, is_link
from coinfly.paths import native_user_home as native_user_home


@dataclass(frozen=True)
class WorkerPaths:
    root: Path
    application: ApplicationPaths | None = None
    lock_root: Path | None = None

    @classmethod
    def for_current_user(cls) -> "WorkerPaths":
        application = ApplicationPaths.for_current_user()
        return cls(application.storage, application, application.authority_locks)

    @property
    def database(self) -> Path:
        return self.root / "state.sqlite3"

    @property
    def locks(self) -> Path:
        return self.lock_root if self.lock_root is not None else self.root / "authority-locks"

    @property
    def socket(self) -> Path:
        from coinfly.ipc.local import default_socket_path

        return default_socket_path()

    def prepare(self) -> None:
        if self.application is not None:
            self.application.prepare()
        for path in (self.root, self.locks):
            if is_link(path):
                raise DomainError("UNSAFE_RUNTIME_PATH")
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            if sys.platform != "win32":
                metadata = path.stat()
                if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise DomainError("RUNTIME_PATH_NOT_PRIVATE")
