"""Production orchestration fixtures never consult Keychain, Apple or exchanges."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.bootstrap.test_packaged_distribution import write_distribution
from tests.test_dev_launcher import SIGNING_CASES
from tests.test_macos_release import ROOT
from tests.test_macos_release import builder as builder


def qualified_report():
    # Orchestration fixture only; cannot become a release qualification input.
    return {"checks": [{"code": code, "satisfied": True} for code in (
        "NATIVE_RELEASE_QUALIFICATION", "PRODUCTION_TELEMETRY_QUALIFICATION",
        "STRATEGY_EVALUATION", "NATIVE_SIGNING_UNCERTIFIED", "LIVE_GATEWAY_UNCERTIFIED") ]}


def profile():
    return {"developer_dir": "/fixture/Xcode.app/Contents/Developer", "sdk_path": "/fixture/SDK27.sdk",
            "clang": "/fixture/clang", "clang++": "/fixture/clang++"}


@pytest.mark.parametrize(("identity", "notary"), SIGNING_CASES)
def test_unsigned_preflight_does_not_consult_apple(builder, monkeypatch, identity, notary):
    def forbidden(*args, **kwargs):
        pytest.fail("unsigned mode reached signing tools")
    monkeypatch.setattr(builder, "signing_preflight", forbidden)
    assert builder.preflight(identity or "", notary or "", mode="unsigned") == ""


@pytest.mark.parametrize("mode", ["development", "", "SIGNed"])
def test_unrecognized_modes_cannot_select_a_fallback(builder, mode):
    with pytest.raises(ValueError, match="Invalid production"):
        builder.preflight("", "", mode=mode)


@pytest.mark.parametrize("mode", ["signed", "unsigned"])
def test_production_always_requires_publisher_metadata(builder, tmp_path, monkeypatch, mode):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path / "home")
    with pytest.raises(RuntimeError, match="Publisher metadata required"):
        builder.stage_distribution(None, tmp_path)
    assert not (tmp_path / "development-distribution").exists()


@pytest.mark.parametrize("failure", ["identity", "private-key", "profile", "malformed-profile"])
def test_unusable_signed_references_fail_without_building(builder, monkeypatch, failure):
    calls = []
    identity, notary = "Developer ID Application: Bärış (ABCDE12345)", "profile with spaces 雪"

    def command(*args, **kwargs):
        calls.append(args)
        if "find-identity" in args:
            identities = f'  1) {"A" * 40} "{identity}"' if failure != "identity" else ""
            return subprocess.CompletedProcess(args, 0, identities, "")
        if (failure == "private-key" and "--sign" in args) or (failure == "profile" and "history" in args):
            raise subprocess.CalledProcessError(1, args)
        if "history" in args:
            assert args[args.index("--keychain-profile") + 1] == notary
            return subprocess.CompletedProcess(args, 0, "{}" if failure == "malformed-profile" else '{"history":[]}', "")
        if "--sign" in args:
            assert args[args.index("--sign") + 1] == identity
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(builder, "run", command)
    monkeypatch.setattr(builder.shutil, "copyfile", lambda *args: None)
    with pytest.raises(RuntimeError, match="exactly one|unusable"):
        builder.preflight(identity, notary, mode="signed")
    assert all(not any(token in call for token in ("sync", "cmake", "PyInstaller", "submit")) for call in calls)


@pytest.fixture
def production_pipeline(builder, tmp_path, monkeypatch):
    root = tmp_path / "checkout ü spaces"
    (root / "packaging/macos").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "packaging/macos/Info.plist"):
        shutil.copyfile(ROOT / name, root / name)
    publisher = write_distribution(tmp_path / "publisher")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.shutil, "which", lambda name: "/fixture/" + name)
    calls, events = [], []
    state = {"failure": None}

    def event(name):
        events.append(name)
        if state["failure"] == name:
            raise RuntimeError("injected " + name)

    def command(*args, **kwargs):
        args = tuple(str(arg) for arg in args)
        calls.append((args, kwargs))
        if "lock" in args:
            event("lock")
        elif "sync" in args:
            event("sync")
        elif "ruff" in args:
            event("ruff")
        elif "mypy" in args:
            event("mypy")
        elif "pytest" in args:
            assert "--native-ui" in args
            event("pytest")
        elif "pip_audit" in args:
            event("dependencies")
        elif "-n" in args:
            event("shell")
        elif args[0] == "cmake":
            event("compile" if "--build" in args else "configure")
        elif "PyInstaller" in args:
            event("freeze")
            destination = Path(args[args.index("--distpath") + 1]) / "coinfly.app/Contents"
            destination.mkdir(parents=True)
            shutil.copytree(kwargs["env"]["COINFLY_STAGED_PUBLISHER"], destination / "Resources/coinfly/bootstrap/publisher")
            shutil.copyfile(root / "packaging/macos/Info.plist", destination / "Info.plist")
        elif args[0] == "ditto":
            event("archive")
            Path(args[-1]).write_bytes(b"fixture archive")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(builder, "run", command)
    monkeypatch.setattr(builder, "check_bundle", lambda *args: event("inspect"))
    monkeypatch.setattr(builder, "sign_bundle", lambda *args: event("sign"))
    monkeypatch.setattr(builder, "finalize_unsigned", lambda *args: event("adhoc"))
    monkeypatch.setattr(builder, "notarize", lambda *args: event("notarize"))
    monkeypatch.setattr(builder, "smoke_bundle", lambda *args, **kwargs: (event("acceptance"), qualified_report())[1])
    monkeypatch.setattr(builder, "verify_archive", lambda *args, **kwargs: event("verify_archive"))
    return root, publisher, calls, events, state


@pytest.mark.parametrize("mode", ["signed", "unsigned"])
def test_modes_share_production_gates_and_publish_a_complete_set(builder, production_pipeline, monkeypatch, mode):
    root, publisher, calls, events, _ = production_pipeline
    monkeypatch.setenv("MACOS_SIGN_IDENTITY", "signer with spaces 雪")
    monkeypatch.setenv("MACOS_NOTARY_PROFILE", "notary profile")
    output = builder.build("signer with spaces 雪", "notary profile", "ABCDE12345", mode=mode,
                           toolchain=profile(), dataset_distribution=publisher)
    assert set(events) >= {"lock", "sync", "shell", "ruff", "mypy", "pytest", "dependencies", "configure",
                           "compile", "freeze", "inspect", "acceptance", "archive", "verify_archive"}
    assert output.is_relative_to(root / "dist/1.0.0/macos-arm64" / mode)
    manifest = json.loads((output.parent / "manifest.json").read_text())
    assert manifest["distribution_mode"] == mode and manifest["build_profile"] == "production"
    assert manifest["publisher"] == "devsolux"
    assert output.with_suffix(".zip.sha256").read_text().startswith(manifest["archive_sha256"])
    assert (output.parent / "evidence/ruff.log").exists()
    sync = next(command for command, _ in calls if "sync" in command)
    assert "--locked" in sync and "--no-editable" in sync and "--no-python-downloads" in sync
    freeze_env = next(kwargs["env"] for command, kwargs in calls if "PyInstaller" in command)
    assert freeze_env["COINFLY_BUILD_MODE"] == mode
    assert freeze_env["MACOS_SIGN_IDENTITY"] == "signer with spaces 雪"
    assert "--frozen" not in sync
    configure = next(command for command, _ in calls if "-DCMAKE_BUILD_TYPE=Release" in command)
    assert "-DCMAKE_OSX_DEPLOYMENT_TARGET=26.0" in configure
    assert f"-DCOINFLY_TEAM_ID={'ABCDE12345' if mode == 'signed' else ''}" in configure
    if mode == "unsigned":
        assert "sign" not in events and "notarize" not in events
        assert manifest["code_signature"] == "ad-hoc"
        assert manifest["notarization"] == manifest["stapling"] == "NOT_APPLICABLE_UNSIGNED"
    else:
        assert events.index("sign") < events.index("acceptance") < events.index("notarize") < events.index("archive")
        assert manifest["notarization"] == "Accepted"


@pytest.mark.parametrize("mode", ["signed", "unsigned"])
@pytest.mark.parametrize("failure", ["lock", "sync", "shell", "ruff", "mypy", "pytest", "dependencies",
                                     "configure", "compile", "freeze", "inspect", "acceptance", "archive", "verify_archive"])
def test_every_common_failure_prevents_publication(builder, production_pipeline, mode, failure):
    root, publisher, _, _, state = production_pipeline
    previous = root / "dist/previous.zip"
    previous.parent.mkdir()
    previous.write_bytes(b"last complete release")
    state["failure"] = failure
    with pytest.raises(RuntimeError, match="injected"):
        builder.build("identity", "profile", "ABCDE12345", mode=mode, toolchain=profile(), dataset_distribution=publisher)
    assert list((root / "dist").iterdir()) == [previous]
    assert previous.read_bytes() == b"last complete release"


@pytest.mark.parametrize("failure", ["sign", "notarize"])
def test_signing_failure_never_publishes_unsigned(builder, production_pipeline, failure):
    root, publisher, _, events, state = production_pipeline
    state["failure"] = failure
    with pytest.raises(RuntimeError, match="injected"):
        builder.build("identity", "profile", "ABCDE12345", toolchain=profile(), dataset_distribution=publisher)
    assert "adhoc" not in events and not list((root / "dist").iterdir())


def test_incomplete_functional_acceptance_cannot_publish(builder, production_pipeline, monkeypatch):
    root, publisher, _, events, _ = production_pipeline
    monkeypatch.setattr(builder, "smoke_bundle", lambda *args, **kwargs: {"checks": []})
    with pytest.raises(RuntimeError, match="NATIVE_RELEASE_QUALIFICATION"):
        builder.build("", "", "", mode="unsigned", toolchain=profile(), dataset_distribution=publisher)
    assert "archive" not in events and not list((root / "dist").iterdir())


@pytest.mark.parametrize("failure", ["checksum", "rename"])
def test_publication_interruption_preserves_previous_set(builder, production_pipeline, monkeypatch, failure):
    root, publisher, _, _, _ = production_pipeline
    write = Path.write_text

    def write_text(path, *args, **kwargs):
        if failure == "checksum" and path.suffix == ".sha256":
            raise OSError("injected checksum failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    if failure == "rename":
        monkeypatch.setattr(builder.os, "rename", lambda *args: (_ for _ in ()).throw(OSError("injected rename failure")))
    with pytest.raises(OSError, match="injected"):
        builder.build("", "", "", mode="unsigned", toolchain=profile(), dataset_distribution=publisher)
    assert not list((root / "dist").rglob("*.zip"))


@pytest.mark.parametrize("mode", ["signed", "unsigned"])
def test_caller_expected_mode_rejects_artifact_downgrade(builder, tmp_path, mode):
    metadata = tmp_path / "Contents/Resources/coinfly-build.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"build_profile": "production", "distribution_mode": mode}))
    builder.check_mode(tmp_path, mode)
    with pytest.raises(RuntimeError, match="mode mismatch"):
        builder.check_mode(tmp_path, "unsigned" if mode == "signed" else "signed")


def test_production_spec_ignores_inherited_identity_in_unsigned_mode(monkeypatch, tmp_path):
    class StopSpec(Exception):
        pass
    captured = {}
    # Run actual spec code up to Analysis; no PyInstaller/signing executable used.
    hook = type("Hooks", (), {"collect_data_files": staticmethod(lambda *a: []),
                             "collect_submodules": staticmethod(lambda *a: []),
                             "copy_metadata": staticmethod(lambda *a, **k: [])})()
    import sys
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hook)
    monkeypatch.setenv("COINFLY_BUILD_MODE", "unsigned")
    monkeypatch.setenv("MACOS_SIGN_IDENTITY", "unavailable must not be used")
    monkeypatch.setenv("COINFLY_NATIVE_BUILD", str(tmp_path))
    monkeypatch.setenv("COINFLY_STAGED_PUBLISHER", str(tmp_path))

    def analysis(*args, **kwargs):
        captured.update(kwargs)
        raise StopSpec
    with pytest.raises(StopSpec):
        # Inspect all constructed globals through a trace-free exec namespace.
        namespace = {"SPECPATH": str(ROOT / "packaging/macos"), "Analysis": analysis}
        exec((ROOT / "packaging/macos/coinfly.spec").read_text(), namespace)
    assert namespace["identity"] is None


@pytest.mark.parametrize("name", ["dev.py", "scripts/build_desktop.py"])
@pytest.mark.parametrize("mode", ["signed", "unsigned"])
@pytest.mark.parametrize("preflight", [False, True])
def test_wrappers_delegate_once_to_shell_defaults(monkeypatch, name, mode, preflight):
    spec = importlib.util.spec_from_file_location("wrapper", ROOT / name)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    monkeypatch.setattr(wrapper.sys, "platform", "darwin")
    monkeypatch.setattr(wrapper.platform, "machine", lambda: "arm64")
    calls = []
    monkeypatch.setattr(wrapper.subprocess, "run", lambda args, **kwargs:
                        (calls.append(args), subprocess.CompletedProcess(args, 73))[1])
    command = "build" if mode == "signed" else "build-unsigned"
    args = [command] if name == "dev.py" else ["--mode", mode]
    args += ["--preflight"] if preflight else []
    if name == "dev.py":
        result = wrapper.main(args)
    else:
        monkeypatch.setattr(wrapper.sys, "argv", [name, *args])
        result = wrapper.main()
    assert result == 73
    assert calls == [["/bin/bash", str(ROOT / "dev.sh"), command, *(["--preflight"] if preflight else [])]]
