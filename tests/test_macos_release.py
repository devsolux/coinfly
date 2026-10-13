"""Release failures never become successful/stapled distribution artifacts."""

import importlib.util
import json
import plistlib
import runpy
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.bootstrap.test_packaged_distribution import write_distribution

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder():
    spec = importlib.util.spec_from_file_location("build_macos", ROOT / "scripts/build_macos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("status", "exit_code"), [("Invalid", 0), ("In Progress", 0), ("Accepted", 1)])
def test_notarization_must_explicitly_succeed_before_stapling(builder, tmp_path, monkeypatch, status, exit_code):
    calls = []
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, exit_code, json.dumps({"status": status}), ""))
    with pytest.raises(RuntimeError, match="not Accepted"):
        builder.notarize(tmp_path / "coinfly.app", tmp_path, "fixture-profile")
    assert len(calls) == 1 and calls[0][0] == "ditto"
    assert json.loads((tmp_path / "notarization-result.json").read_text())["status"] == status


def test_notarization_requires_parseable_result(builder, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "not JSON", "fixture failure"))
    with pytest.raises(RuntimeError, match="no valid result"):
        builder.notarize(tmp_path / "coinfly.app", tmp_path, "fixture-profile")


def test_accepted_app_is_stapled_and_verified(builder, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, '{"status":"Accepted"}', ""))
    builder.notarize(tmp_path / "coinfly.app", tmp_path, "fixture-profile")
    assert [call[:3] for call in calls[1:]] == [
        ("xcrun", "stapler", "staple"), ("xcrun", "stapler", "validate"),
        ("codesign", "--verify", "--deep"), ("spctl", "--assess", "--type"),
    ]


def test_smoke_rejects_desktop_crash_even_when_doctor_passes(builder, tmp_path, monkeypatch):
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if len(args) > 2 and args[2] == ROOT / "scripts/verify_ui_startup.py":
            assert kwargs["timeout"] == 70
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(builder, "run", command)
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 2, json.dumps({"desktop_dependencies_ready": True, "local_research_ready": True,
                           "checks": [{"code": "SIGNED_DATASET_DISTRIBUTION", "satisfied": True}]}), ""))
    with pytest.raises(subprocess.CalledProcessError):
        builder.smoke_bundle(tmp_path / "coinfly.app")
    assert [call[-1] for call in calls[:2]] == ["--version", "--version"]
    assert calls[-1][-1] == tmp_path / "coinfly.app"


def test_release_stages_only_validated_public_metadata(builder, tmp_path):
    source = write_distribution(tmp_path / "reviewed")
    (source / "private.pem").write_text("PRIVATE_CANARY")
    stage = tmp_path / "stage"
    stage.mkdir()
    destination = builder.stage_distribution(source, stage)
    assert {path.name for path in destination.iterdir()} == {"trust-root.json", "manifest.json"}


def test_default_distribution_uses_native_application_root(builder, tmp_path, monkeypatch):
    source = write_distribution(tmp_path / "home ü/.devsolux/coinfly")
    (source / "private.pem").write_text("PRIVATE_CANARY")
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path / "home ü")
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    monkeypatch.chdir(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    destination = builder.stage_distribution(None, stage)
    assert {path.name for path in destination.iterdir()} == {"trust-root.json", "manifest.json"}
    for path in destination.iterdir():
        assert path.read_bytes() == (source / path.name).read_bytes()


def test_direct_builder_forwards_explicit_distribution_as_argument(builder, tmp_path, monkeypatch):
    source = tmp_path / "reviewed ü metadata"
    monkeypatch.setattr(builder.sys, "argv", ["build_macos.py", "--preflight", "--dataset-distribution", str(source)])
    calls = []
    monkeypatch.setattr(builder.subprocess, "run", lambda args, **kwargs:
                        (calls.append(args), subprocess.CompletedProcess(args, 73))[1])
    assert builder.main() == 73
    assert calls == [["/bin/bash", str(ROOT / "dev.sh"), "build", "--preflight", "--dataset-distribution", str(source)]]


def test_missing_metadata_creates_valid_development_pair(builder, tmp_path):
    from coinfly.bootstrap.development_distribution import is_development_distribution
    from coinfly.bootstrap.distribution import load_distribution
    from coinfly.bootstrap.manifest import BootstrapError

    destination = builder.stage_distribution(None, tmp_path, require_publisher=False)
    assert is_development_distribution(destination)
    assert json.loads((destination / "manifest.json").read_bytes())["is_development_build"] is True
    with pytest.raises(BootstrapError, match="DEVELOPMENT_DISTRIBUTION_ONLY"):
        load_distribution(destination)


def test_production_mode_rejects_missing_and_development_metadata(builder, tmp_path, monkeypatch):
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path / "home")
    with pytest.raises(RuntimeError, match="Publisher metadata required"):
        builder.stage_distribution(None, tmp_path, require_publisher=True)
    source = builder.initialize_development_distribution(tmp_path / "development")
    with pytest.raises(RuntimeError, match="cannot use development"):
        builder.stage_distribution(source, tmp_path, require_publisher=True)


@pytest.mark.parametrize("failure", ["missing", "incomplete", "tampered"])
def test_explicit_invalid_publisher_never_falls_back(builder, tmp_path, failure):
    source = tmp_path / "reviewed"
    if failure != "missing":
        write_distribution(source)
        manifest = source / "manifest.json"
        if failure == "incomplete":
            manifest.unlink()
        else:
            document = json.loads(manifest.read_bytes())
            document["signed"]["version"] += 1
            manifest.write_text(json.dumps(document))
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises((OSError, RuntimeError)):
        builder.stage_distribution(source, stage)
    assert not (stage / "development-distribution").exists()


def test_development_initialization_is_idempotent_atomic_and_preserves_existing(builder, tmp_path):
    directory = tmp_path / "local metadata"
    directory.mkdir()  # dev.sh's mkdir -p is supported.
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(builder.initialize_development_distribution, [directory] * 8)) == [directory] * 8
    original = {path.name: path.read_bytes() for path in directory.iterdir()}
    builder.initialize_development_distribution(directory)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == original
    (directory / "manifest.json").write_text("corrupted")
    with pytest.raises(RuntimeError):
        builder.initialize_development_distribution(directory)
    assert (directory / "manifest.json").read_text() == "corrupted"
    reviewed = write_distribution(tmp_path / "reviewed")
    original = (reviewed / "trust-root.json").read_bytes()
    with pytest.raises(RuntimeError, match="not a development distribution"):
        builder.initialize_development_distribution(reviewed)
    assert (reviewed / "trust-root.json").read_bytes() == original


@pytest.mark.parametrize("failure", ["missing", "changed", "external-link"])
def test_bundle_resources_must_match_staged_metadata(builder, tmp_path, failure):
    staged = builder.initialize_development_distribution(tmp_path / "staged")
    app = tmp_path / "coinfly.app"
    resources = app / "Contents/Resources/coinfly/bootstrap/publisher"
    resources.mkdir(parents=True)
    for path in staged.iterdir():
        (resources / path.name).write_bytes(path.read_bytes())
    builder.check_bundled_distribution(app, staged)
    manifest = resources / "manifest.json"
    manifest.unlink()
    if failure == "changed":
        manifest.write_text("{}")
    elif failure == "external-link":
        manifest.symlink_to(staged / "manifest.json")
    with pytest.raises((OSError, RuntimeError)):
        builder.check_bundled_distribution(app, staged)


def test_development_smoke_requires_own_metadata_without_production_claims(builder, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: None)
    report = {"desktop_dependencies_ready": True, "local_research_ready": True, "production_ready": False,
              "checks": [{"code": "DEVELOPMENT_DATASET_DISTRIBUTION", "satisfied": True},
                         {"code": "SIGNED_DATASET_DISTRIBUTION", "satisfied": False}]}
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 2, json.dumps(report), ""))
    builder.smoke_bundle(tmp_path / "coinfly.app", development=True)
    with pytest.raises(RuntimeError, match="metadata"):
        builder.smoke_bundle(tmp_path / "coinfly.app")
    report["checks"][1]["satisfied"] = True
    with pytest.raises(RuntimeError, match="incorrectly claims"):
        builder.smoke_bundle(tmp_path / "coinfly.app", development=True)


def test_bundled_smoke_rejects_missing_publisher_even_with_desktop_dependencies(builder, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 2, json.dumps({"desktop_dependencies_ready": True, "local_research_ready": True,
                           "checks": [{"code": "SIGNED_DATASET_DISTRIBUTION", "satisfied": False}]}), ""))
    with pytest.raises(RuntimeError, match="publisher metadata"):
        builder.smoke_bundle(tmp_path / "coinfly.app")


def test_release_doctor_initializes_only_isolated_storage(monkeypatch, capsys):
    from coinfly.paths import ApplicationPaths

    roots = []
    prepare = ApplicationPaths.prepare

    def isolated(application):
        assert application.root.parent.parent.name.startswith("coinfly-doctor-")
        roots.append(application.root)
        prepare(application)

    def forbidden():
        pytest.fail("Release probe inspected operator storage")

    monkeypatch.setattr(ApplicationPaths, "prepare", isolated)
    monkeypatch.setattr(ApplicationPaths, "for_current_user", forbidden)
    probe = runpy.run_path(str(ROOT / "scripts/verify_doctor.py"))["run_storage_probe"]
    assert probe() == 2  # Source fixtures still lack external production evidence.
    report = json.loads(capsys.readouterr().out)
    assert next(check for check in report["checks"] if check["code"] == "APPLICATION_STORAGE")["satisfied"]
    assert roots and all(not root.exists() for root in roots)


def test_background_only_desktop_is_rejected(builder, tmp_path):
    app = tmp_path / "coinfly.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"LSBackgroundOnly": True}))
    with pytest.raises(RuntimeError, match="LSBackgroundOnly"):
        builder.check_bundle(app)


def test_preflight_rejects_ad_hoc_and_unavailable_identity(builder, monkeypatch):
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(builder.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(RuntimeError, match="Developer ID"):
        builder.preflight("-", "fixture-profile")
    with pytest.raises(RuntimeError, match="exactly one"):
        builder.preflight("unavailable", "fixture-profile")


def test_runtime_minimum_and_architecture_are_verified(builder, tmp_path, monkeypatch):
    app = tmp_path / "coinfly.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    metadata = contents / "Info.plist"
    metadata.write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "26.0"}))
    (contents / "binary").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
    arch = "arm64"

    def command(*args, **kwargs):
        build = "Load command 10\n      cmd LC_BUILD_VERSION\n    minos 15.0\n  version 27037.1\n"
        return subprocess.CompletedProcess(args, 0, arch if args[1] == "lipo" else build, "")

    monkeypatch.setattr(builder, "run", command)
    builder.check_bundle(app)
    assert plistlib.loads(metadata.read_bytes())["LSMinimumSystemVersion"] == "26.0"
    arch = "x86_64 arm64"
    with pytest.raises(RuntimeError, match="not thin arm64"):
        builder.check_bundle(app)


def test_notary_timeout_is_unknown_not_accepted_or_retried(builder, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "run", lambda *args, **kwargs: calls.append(args))

    def timeout(args, **kwargs):
        assert kwargs["timeout"] == 960
        assert args[args.index("--timeout") + 1] == "15m"
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(builder.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        builder.notarize(tmp_path / "coinfly.app", tmp_path, "fixture-profile")
    assert len(calls) == 1
    report = json.loads((tmp_path / "notarization-result.json").read_text())
    assert report["status"] == "Unknown" and report["automatic_retry"] is False
