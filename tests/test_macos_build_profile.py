"""Exact toolchain/deployment contract; all native commands are isolated fixtures."""

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_macos_release import builder as builder


@pytest.fixture
def selected_toolchain(builder, tmp_path, monkeypatch):
    developer = tmp_path / "Xcode Ü.app/Contents/Developer"
    (developer / "usr/bin").mkdir(parents=True)
    (developer / "usr/bin/xcodebuild").touch()
    sdk = developer / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk"
    metadata = sdk / "System/Library/CoreServices/SystemVersion.plist"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(plistlib.dumps({"ProductBuildVersion": "26A425"}))
    state = {"xcode": "Xcode 27.0\nBuild version 27A266a\n",
             "compiler": "Apple clang version 21.0.0 (clang-2100.3.34.2)\n",
             "sdk_version": "27.0", "sdk_build": "26A425", "translated": "0",
             "host": "26.6.2", "foreign_tool": False}
    monkeypatch.setenv("DEVELOPER_DIR", str(developer))
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(builder.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(builder.shutil, "which", lambda name: "/fixture/" + name)
    monkeypatch.setattr(builder.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 * 1024**3))
    monkeypatch.setattr(builder.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, state["translated"], ""))

    def command(*args, **kwargs):
        values = list(map(str, args))
        if values[0].endswith("xcodebuild"):
            output = state["xcode"]
        elif "--find" in values:
            output = str((Path("/foreign") if state["foreign_tool"] else developer / "usr/bin") / values[-1])
        elif values[-1] == "--show-sdk-path":
            output = str(sdk)
        elif values[-1] == "--show-sdk-version":
            output = state["sdk_version"]
        elif values[-1] == "--show-sdk-build-version":
            output = state["sdk_build"]
        elif values[-1] == "--version":
            output = state["compiler"]
        elif "-productVersion" in values:
            output = state["host"]
        elif "-buildVersion" in values:
            output = "25G83"
        else:
            output = "Apple M4"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(builder, "run", command)
    return state, developer, metadata


def test_sdk_27_and_deployment_26_are_distinct_valid_fields(builder, selected_toolchain):
    state, developer, _ = selected_toolchain
    result = builder.toolchain_preflight()
    assert result["xcode_version"] == result["sdk_version"] == "27.0"
    assert result["deployment_target"] == result["minimum_supported_os"] == "26.0"
    assert result["sdk_build"] == "26A425" and result["host_build"] == "25G83"
    assert result["xcode_build"] == "27A266a"
    assert result["developer_dir"] == str(developer)


@pytest.mark.parametrize(("field", "value", "error"), [
    ("xcode", "Xcode 26.6\nBuild version old", "Required Xcode"),
    ("xcode", "nonsense", "Required Xcode"),
    ("compiler", "Apple clang version 21.0.0 (clang-2100.1.1.101)", "Required Apple clang"),
    ("compiler", "clang version 21.0.0", "Required Apple clang"),
    ("sdk_version", "26.0", "Required SDK"), ("sdk_build", "wrong", "Required SDK"),
    ("translated", "1", "Rosetta"), ("translated", "", "Rosetta"),
    ("host", "26.0", "build-host macOS"), ("foreign_tool", True, "Mixed toolchain"),
])
def test_wrong_native_profile_fails_without_substitution(builder, selected_toolchain, field, value, error):
    state, _, _ = selected_toolchain
    state[field] = value
    with pytest.raises(RuntimeError, match=error):
        builder.toolchain_preflight()


def test_sdk_authoritative_metadata_must_match(builder, selected_toolchain):
    _, _, metadata = selected_toolchain
    metadata.write_bytes(plistlib.dumps({"ProductBuildVersion": "mismatch"}))
    with pytest.raises(RuntimeError, match="metadata build 'mismatch'"):
        builder.toolchain_preflight()


def test_standalone_clt_is_not_full_xcode(builder, selected_toolchain, monkeypatch):
    monkeypatch.setenv("DEVELOPER_DIR", "/Library/Developer/CommandLineTools")
    with pytest.raises(RuntimeError, match="full Xcode"):
        builder.toolchain_preflight()


@pytest.mark.parametrize("minimum", ["26.1", "27.0", "26.0.1"])
def test_dependency_minimum_above_26_is_rejected_not_rewritten(builder, tmp_path, monkeypatch, minimum):
    contents = tmp_path / "Contents"
    contents.mkdir()
    info = contents / "Info.plist"
    info.write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "26.0"}))
    (contents / "dependency.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "arm64" if "lipo" in args else f"cmd LC_BUILD_VERSION\n platform MACOS\n minos {minimum}\n sdk 27.0\n", ""))
    with pytest.raises(RuntimeError, match="above 26.0"):
        builder.check_bundle(tmp_path)
    assert plistlib.loads(info.read_bytes())["LSMinimumSystemVersion"] == "26.0"


@pytest.mark.parametrize(("minimum", "sdk", "valid"), [("26.0", "27.0", True), ("27.0", "27.0", False),
                                                        ("26.0", "26.0", False), ("13.0", "27.0", False)])
def test_project_native_binaries_enforce_exact_target_and_sdk(builder, tmp_path, monkeypatch, minimum, sdk, valid):
    contents = tmp_path / "Contents"
    contents.mkdir()
    (contents / "Info.plist").write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "26.0"}))
    (contents / "libcoinfly_neural.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "arm64" if "lipo" in args else f"cmd LC_BUILD_VERSION\n platform MACOS\n minos {minimum}\n sdk {sdk}\n", ""))
    if valid:
        builder.check_bundle(tmp_path)
    else:
        with pytest.raises(RuntimeError):
            builder.check_bundle(tmp_path)


def test_build_environment_ignores_foreign_paths_and_download_settings(builder, tmp_path, monkeypatch):
    for name in ("PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "VIRTUAL_ENV",
                 "CMAKE_TOOLCHAIN_FILE", "CMAKE_PREFIX_PATH", "DYLD_LIBRARY_PATH", "SDKROOT", "CC"):
        monkeypatch.setenv(name, "/foreign")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "automatic")
    toolchain = {"developer_dir": "/Xcode", "sdk_path": "/SDK27", "clang": "/clang", "clang++": "/clang++"}
    env = builder.release_environment(tmp_path, toolchain)
    assert "/foreign" not in env.values()
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "environment")
    assert env["MACOSX_DEPLOYMENT_TARGET"] == "26.0" and env["UV_PYTHON_DOWNLOADS"] == "never"


def test_source_digest_tracks_uncommitted_build_inputs_and_excludes_data(builder, tmp_path, monkeypatch):
    source = tmp_path / "checkout"
    (source / "src/coinfly").mkdir(parents=True)
    module = source / "src/coinfly/module.py"
    module.write_text("v=1")
    (source / "data").mkdir()
    (source / "data/private").write_text("never copy")
    monkeypatch.setattr(builder, "ROOT", source)
    first = builder.snapshot_source(tmp_path / "snapshot1")
    module.write_text("v=2")
    second = builder.snapshot_source(tmp_path / "snapshot2")
    assert first != second
    assert not (tmp_path / "snapshot1/data").exists()
    module.unlink()
    module.symlink_to(source / "data/private")
    with pytest.raises(RuntimeError, match="symlinks"):
        builder.snapshot_source(tmp_path / "snapshot3")
