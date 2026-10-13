"""Portable storage layout, native profile resolution."""

import ctypes
import os
import stat
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coinfly.domain.context import DomainError
from coinfly.paths import ApplicationPaths, is_link, native_user_home
from coinfly.worker.paths import WorkerPaths


def test_all_managed_directories_stay_under_application_root(tmp_path, monkeypatch):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path)
    monkeypatch.setenv("HOME", "/untrusted")
    monkeypatch.setenv("USERPROFILE", "C:\\untrusted")
    paths = ApplicationPaths.for_current_user()
    paths.prepare()
    assert paths.root == tmp_path / ".devsolux/coinfly"
    for directory in (paths.storage, paths.datasets, paths.models, paths.checkpoints,
                      paths.kernels, paths.logs, paths.cache, paths.runs):
        assert directory.is_dir() and directory.is_relative_to(paths.root)
        if os.name != "nt":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    worker = WorkerPaths.for_current_user()
    assert worker.database == paths.storage / "state.sqlite3"
    assert worker.locks == paths.root / "authority-locks"


def test_windows_profile_api_ignores_environment_and_handles_failure(monkeypatch):
    def profile(_window, folder, _token, _flags, buffer):
        assert folder == 0x0028
        buffer.value = "C:\\Users\\Baris"
        return 0
    function = Mock(side_effect=profile)
    monkeypatch.setattr("coinfly.paths.sys.platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: SimpleNamespace(SHGetFolderPathW=function), raising=False)
    monkeypatch.setenv("USERPROFILE", "C:\\wrong")
    monkeypatch.setenv("HOME", "/wrong")
    assert str(native_user_home()) == "C:\\Users\\Baris"
    function.side_effect = lambda *_args: 1
    with pytest.raises(DomainError, match="NATIVE_HOME_UNAVAILABLE"):
        native_user_home()
    windows = ApplicationPaths(PureWindowsPath("C:/Users/Baris/.devsolux/coinfly"))
    assert windows.source_datasets == PureWindowsPath("C:/Users/Baris/.devsolux/coinfly/datasets/malecns/source-locks")
    assert windows.checkpoints == PureWindowsPath("C:/Users/Baris/.devsolux/coinfly/models/checkpoints")


def test_managed_symlink_or_junction_is_rejected(tmp_path, monkeypatch):
    paths = ApplicationPaths.for_home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    paths.root.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(DomainError, match="UNSAFE_RUNTIME_PATH"):
        paths.prepare()
    assert not list(elsewhere.iterdir())
    probe = Mock(spec=Path)
    probe.is_symlink.return_value = False
    probe.lstat.return_value = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert is_link(probe)

