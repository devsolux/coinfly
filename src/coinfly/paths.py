"""Native per-user application storage; no environment or cwd overrides."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from coinfly.domain.context import DomainError


class StorageInitializationError(DomainError):
    """Stable, redacted storage diagnostics, also used by ``coinfly doctor``."""

    @property
    def detail(self) -> str:
        return {
            "STORAGE_PERMISSION_DENIED": "Cannot write Coinfly storage. Check the account's permissions and ACLs on ~/.devsolux/coinfly.",
            "STORAGE_PATH_CONFLICT": "A file occupies a required Coinfly directory. Move the conflicting file aside and retry.",
            "STORAGE_IO_FAILURE": "Coinfly storage is unavailable. Check free space, disk health and read-only mounts, then retry.",
            "STORAGE_DIRECTORY_MISSING": "Coinfly storage is not initialized. Start Coinfly to create ~/.devsolux/coinfly and its folders.",
            "NATIVE_HOME_UNAVAILABLE": "The account home directory could not be resolved or accessed. Check the OS account profile.",
            "UNSAFE_RUNTIME_PATH": "Coinfly storage contains a symbolic link or junction. Use real directories under the account home.",
            "RUNTIME_PATH_NOT_PRIVATE": "Coinfly storage must be owned by this account with owner-only permissions (0700 on POSIX).",
        }[self.code]

    @classmethod
    def from_os_error(cls, error: OSError) -> StorageInitializationError:
        if isinstance(error, PermissionError):
            return cls("STORAGE_PERMISSION_DENIED")
        if isinstance(error, (FileExistsError, NotADirectoryError)):
            return cls("STORAGE_PATH_CONFLICT")
        return cls("STORAGE_IO_FAILURE")


def native_user_home() -> Path:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(32768)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        function = shell.SHGetFolderPathW
        function.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
        function.restype = ctypes.c_long
        # CSIDL_PROFILE uses the native profile, ignoring HOME/USERPROFILE.
        if function(None, 0x0028, None, 0, buffer) != 0 or not buffer.value:
            raise DomainError("NATIVE_HOME_UNAVAILABLE")
        return Path(buffer.value)
    else:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        # Includes Windows junctions on Python 3.11 (before Path.is_junction).
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def private_directory(path: Path) -> None:
    if is_link(path):
        raise DomainError("UNSAFE_RUNTIME_PATH")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_directory(path)


def _check_private_directory(path: Path) -> None:
    if is_link(path):
        raise DomainError("UNSAFE_RUNTIME_PATH")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageInitializationError("STORAGE_PATH_CONFLICT")
    if sys.platform != "win32" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
        raise DomainError("RUNTIME_PATH_NOT_PRIVATE")


@dataclass(frozen=True)
class ApplicationPaths:
    root: Path

    @classmethod
    def for_home(cls, home: Path) -> ApplicationPaths:
        try:
            return cls(home.expanduser().resolve() / ".devsolux" / "coinfly")
        except OSError as error:
            raise StorageInitializationError.from_os_error(error) from None
        except RuntimeError:
            raise StorageInitializationError("NATIVE_HOME_UNAVAILABLE") from None

    @classmethod
    def for_current_user(cls) -> ApplicationPaths:
        try:
            return cls.for_home(native_user_home())
        except DomainError as error:
            raise StorageInitializationError(error.code) from None
        except (OSError, KeyError):
            raise StorageInitializationError("NATIVE_HOME_UNAVAILABLE") from None

    @property
    def storage(self) -> Path:
        return self.root / "storage"

    @property
    def ledger(self) -> Path:
        """Reserved ledger assets; the existing SQLite database stays in storage."""
        return self.root / "ledger"

    @property
    def datasets(self) -> Path:
        return self.root / "datasets"

    @property
    def signed_datasets(self) -> Path:
        return self.datasets / "malecns" / "signed"

    @property
    def source_datasets(self) -> Path:
        return self.datasets / "malecns" / "source-locks"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def checkpoints(self) -> Path:
        return self.models / "checkpoints"

    @property
    def kernels(self) -> Path:
        return self.models / "kernels"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def authority_locks(self) -> Path:
        """Signed-worker authority ownership locks; prepared and inspected like every folder."""
        return self.root / "authority-locks"

    @property
    def directories(self) -> tuple[Path, ...]:
        # Parent-first validation prevents an existing managed link/junction
        # from redirecting mkdir or a write probe outside the storage perimeter.
        return (self.root.parent, self.root, self.storage, self.ledger, self.datasets, self.datasets / "malecns",
                self.signed_datasets, self.source_datasets, self.models, self.checkpoints,
                self.kernels, self.logs, self.cache, self.runs, self.snapshots, self.authority_locks)

    def prepare(self) -> None:
        """Idempotent startup barrier: return only once every folder is writable.

        Each mkdir is race-tolerant; the entire tree is not a filesystem
        transaction. A failure leaves existing data intact for the next retry.
        """
        try:
            if not self.root.parent.parent.is_dir():
                raise StorageInitializationError("NATIVE_HOME_UNAVAILABLE")
            for path in self.directories:
                private_directory(path)
                # mkdir(exist_ok=True) alone succeeds on an unwritable folder.
                # Probe real ACL/mount permissions before starting any threads.
                with tempfile.TemporaryFile(prefix=".coinfly-probe-", dir=path) as probe:
                    probe.write(b"\0")
                    probe.flush()
        except OSError as error:
            raise StorageInitializationError.from_os_error(error) from None
        except DomainError as error:
            raise StorageInitializationError(error.code) from None

    def inspect(self) -> None:
        """Read-only doctor preflight; startup additionally probes actual writes."""
        try:
            for path in self.directories:
                _check_private_directory(path)
                if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                    raise StorageInitializationError("STORAGE_PERMISSION_DENIED")
        except FileNotFoundError:
            raise StorageInitializationError("STORAGE_DIRECTORY_MISSING") from None
        except OSError as error:
            raise StorageInitializationError.from_os_error(error) from None
        except DomainError as error:
            raise StorageInitializationError(error.code) from None


def initialize_application_storage() -> ApplicationPaths:
    """Call synchronously at entry points before database, UI or worker threads."""
    paths = ApplicationPaths.for_current_user()
    paths.prepare()
    return paths
