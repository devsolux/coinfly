"""One production macOS pipeline; dev.sh resolves the operator signing defaults."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
DEPLOYMENT_TARGET = "26.0"
XCODE_VERSION = "27.0"
CLANG_VERSION = "21.0.0"
CLANG_BUILD = "clang-2100.3.34.2"
SDK_VERSION = "27.0"
SDK_BUILD = "26A425"
MODES = ("signed", "unsigned")


def initialize_development_distribution(directory: Path) -> Path:
    """Publish a complete descriptor pair atomically; never overwrite existing inputs."""
    from coinfly.bootstrap.development_distribution import development_documents, is_development_distribution

    directory = directory.expanduser().absolute()
    if directory.is_symlink():
        raise RuntimeError("Development metadata directory must not be a symlink.")
    if directory.exists() and any(directory.iterdir()):
        if not is_development_distribution(directory):
            raise RuntimeError("Existing metadata is not a development distribution; it was not changed.")
        return directory
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".development-metadata-", dir=directory.parent) as temporary:
        candidate = Path(temporary) / "publisher"
        candidate.mkdir()
        for name, document in development_documents().items():
            (candidate / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        is_development_distribution(candidate)
        try:
            # Replaces an empty mkdir -p target, or installs a previously absent one.
            # A nonempty directory cannot be replaced (including concurrent builds).
            os.rename(candidate, directory)
        except OSError:
            if not is_development_distribution(directory):
                raise
    return directory


def stage_distribution(source: Path | None, stage: Path, *, require_publisher: bool = True) -> Path:
    """Seal two validated public files; explicit invalid inputs never trigger fallback."""
    from coinfly.bootstrap.development_distribution import is_development_distribution
    from coinfly.bootstrap.distribution import MANIFEST_FILENAME, ROOT_FILENAME, load_distribution
    from coinfly.bootstrap.manifest import MAX_METADATA_BYTES
    from coinfly.paths import ApplicationPaths

    if source is None:
        if require_publisher:
            source = ApplicationPaths.for_current_user().root
        else:
            source = initialize_development_distribution(stage / "development-distribution")
    source = source.expanduser().resolve()
    if not all((source / name).is_file() for name in (ROOT_FILENAME, MANIFEST_FILENAME)):
        raise RuntimeError(f"Publisher metadata required: reviewed devsolux trust-root.json and manifest.json "
                           f"must exist in {source}. Use --dataset-distribution DIR to select another directory.")
    destination = stage / "publisher"
    destination.mkdir()
    for name in (ROOT_FILENAME, MANIFEST_FILENAME):
        with (source / name).open("rb") as stream:
            payload = stream.read(MAX_METADATA_BYTES + 1)
        if len(payload) > MAX_METADATA_BYTES:
            raise RuntimeError("Publisher metadata exceeds the size limit.")
        (destination / name).write_bytes(payload)
    if is_development_distribution(destination):
        if require_publisher:
            raise RuntimeError("Production release cannot use development metadata.")
        print("Development build: LOCAL_SOURCE_LOCKS verification; isolated SANDBOX/PAPER only.", flush=True)
    else:
        load_distribution(destination)  # Validate the exact staged bytes before signing.
    return destination


def check_bundled_distribution(app: Path, staged: Path) -> None:
    """Verify actual Resources bytes before the final signing/notarization steps."""
    from coinfly.bootstrap.distribution import MANIFEST_FILENAME, ROOT_FILENAME

    resources = app / "Contents/Resources"
    directory = resources / "coinfly/bootstrap/publisher"
    for name in (ROOT_FILENAME, MANIFEST_FILENAME):
        target = directory / name
        if (not target.resolve().is_relative_to(resources.resolve())
                or target.read_bytes() != (staged / name).read_bytes()):
            raise RuntimeError(f"Bundled metadata differs from validated build input: {name}.")


def run(*args: str | Path, capture: bool = False, **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run([str(arg) for arg in args], check=True, text=True, capture_output=capture, **kwargs)


def validate_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Invalid production distribution mode: {mode}")


def toolchain_preflight() -> dict[str, str]:
    """Inspect selected Xcode, never change xcode-select or substitute a compiler."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build requires native macOS arm64.")
    translated = subprocess.run(["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
                                text=True, capture_output=True, check=False, timeout=15)
    if translated.returncode != 0 or translated.stdout.strip() != "0":
        raise RuntimeError("Cannot establish native execution (Rosetta translation check failed).")
    developer = Path(os.environ.get("DEVELOPER_DIR") or run(
        "/usr/bin/xcode-select", "-p", capture=True, timeout=15).stdout.strip()).resolve()
    if developer.name != "Developer" or developer.parent.name != "Contents" or not (developer / "usr/bin/xcodebuild").is_file():
        raise RuntimeError("DEVELOPER_DIR must select a full Xcode.app/Contents/Developer installation.")
    environment = {**os.environ, "DEVELOPER_DIR": str(developer)}

    def xcrun(*args: str) -> str:
        return run("/usr/bin/xcrun", *args, capture=True, env=environment, timeout=30).stdout.strip()

    xcode = run(developer / "usr/bin/xcodebuild", "-version", capture=True, env=environment, timeout=30).stdout
    version = re.search(r"(?m)^Xcode (\S+)$", xcode)
    build = re.search(r"(?m)^Build version (\S+)$", xcode)
    if version is None or version[1] != XCODE_VERSION or build is None:
        raise RuntimeError(f"Required Xcode {XCODE_VERSION}; observed {xcode.strip()!r}.")
    tools = {name: xcrun("--find", name) for name in ("clang", "clang++", "lipo", "vtool", "otool")}
    sdk_path = Path(xcrun("--sdk", "macosx", "--show-sdk-path")).resolve()
    for path in (*tools.values(), str(sdk_path)):
        if not Path(path).is_absolute() or not Path(path).resolve().is_relative_to(developer):
            raise RuntimeError(f"Mixed toolchain: {path} is outside selected Xcode.")
    compiler = run(tools["clang"], "--version", capture=True, env=environment, timeout=30).stdout.splitlines()[0]
    if compiler != f"Apple clang version {CLANG_VERSION} ({CLANG_BUILD})":
        raise RuntimeError(f"Required Apple clang {CLANG_VERSION} ({CLANG_BUILD}); observed {compiler!r}.")
    sdk_version = xcrun("--sdk", "macosx", "--show-sdk-version")
    sdk_build = xcrun("--sdk", "macosx", "--show-sdk-build-version")
    sdk_metadata = plistlib.loads((sdk_path / "System/Library/CoreServices/SystemVersion.plist").read_bytes())
    if (sdk_version != SDK_VERSION or sdk_build != SDK_BUILD
            or sdk_metadata.get("ProductBuildVersion") != SDK_BUILD):
        raise RuntimeError(f"Required SDK {SDK_VERSION}/{SDK_BUILD}; observed {sdk_version}/{sdk_build}, "
                           f"metadata build {sdk_metadata.get('ProductBuildVersion')!r}.")
    host_os = run("/usr/bin/sw_vers", "-productVersion", capture=True, timeout=15).stdout.strip()
    if tuple(int(part) for part in host_os.split(".")) < (26, 6):
        raise RuntimeError("Xcode 27.0 requires build-host macOS >=26.6; application target remains 26.0.")
    for name in ("cmake", "uv", "ditto", "codesign"):
        if shutil.which(name) is None:
            raise RuntimeError(f"Missing release tool: {name}.")
    if sys.version_info < (3, 11) or sqlite3.sqlite_version_info < (3, 51, 3):
        raise RuntimeError(f"Native Python >=3.11 with linked SQLite >=3.51.3 required; observed "
                           f"{platform.python_version()} / {sqlite3.sqlite_version}.")
    if sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise RuntimeError("Selected interpreter belongs to a foreign environment; use this checkout's launcher.")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        raise RuntimeError("At least 10 GiB free disk space is required for the isolated release workspace.")
    return {"developer_dir": str(developer), "xcode_version": version[1], "xcode_build": build[1],
            "compiler": compiler, "sdk_version": sdk_version, "sdk_build": sdk_build,
            "sdk_path": str(sdk_path), "deployment_target": DEPLOYMENT_TARGET,
            "minimum_supported_os": DEPLOYMENT_TARGET, "architecture": "arm64",
            "host_os": host_os, "host_build": run("/usr/bin/sw_vers", "-buildVersion", capture=True).stdout.strip(),
            "hardware": run("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string", capture=True).stdout.strip(),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, **tools}


def signing_preflight(identity: str, profile: str) -> str:
    if not identity or identity == "-" or not profile:
        raise RuntimeError("A Developer ID Application identity and notarytool Keychain profile are required.")
    for tool in ("notarytool", "stapler"):
        run("/usr/bin/xcrun", "--find", tool, capture=True, timeout=30)
    identities = run("/usr/bin/security", "find-identity", "-v", "-p", "codesigning", capture=True, timeout=30).stdout
    matches = []
    for digest, name, team in re.findall(
        r'([0-9A-Fa-f]{40}) "(Developer ID Application: [^"\n]+ \(([A-Z0-9]{10})\))"', identities
    ):
        if identity.upper() == digest.upper() or identity == name:
            matches.append(team)
    if len(matches) != 1:
        raise RuntimeError("Signing identity must match exactly one valid Developer ID Application certificate. "
                           "Unlock the signing Keychain or override MACOS_SIGN_IDENTITY.")
    # Check the profile before spending time compiling; never print Keychain contents.
    try:
        # Sign a disposable Mach-O to prove private-key use, not only enumeration.
        with tempfile.TemporaryDirectory(prefix="coinfly-signing-preflight-") as temporary:
            probe = Path(temporary) / "probe"
            shutil.copyfile("/usr/bin/true", probe)
            run("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp=none", probe,
                capture=True, timeout=60)
            run("/usr/bin/codesign", "--verify", "--strict", probe, capture=True, timeout=30)
        result = run("/usr/bin/xcrun", "notarytool", "history", "--keychain-profile", profile,
                     "--output-format", "json", capture=True, timeout=60)
        document = json.loads(result.stdout)
        if not isinstance(document, dict) or not isinstance(document.get("history"), list):
            raise ValueError("Malformed notarytool history")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError("Resolved MACOS_SIGN_IDENTITY/private key or MACOS_NOTARY_PROFILE is unusable. "
                           "Check that exact identity and stored profile; no fallback was attempted.") from error
    return matches[0]


def preflight(identity: str, profile: str, *, mode: str = "signed") -> str:
    validate_mode(mode)
    return signing_preflight(identity, profile) if mode == "signed" else ""


def gui_preflight() -> None:
    import tkinter

    try:
        root = tkinter.Tk()
        try:
            root.withdraw()
            root.update()
        finally:
            root.destroy()
    except tkinter.TclError as error:
        raise RuntimeError("A native Tk window requires a logged-in desktop session; release UI checks cannot skip.") from error


def check_bundle(app: Path) -> None:
    """Reject incompatible payloads; never raise the declared minimum OS."""
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    if info.get("LSBackgroundOnly"):
        raise RuntimeError("Desktop app must not be marked LSBackgroundOnly.")
    if info.get("LSMinimumSystemVersion") != DEPLOYMENT_TARGET:
        raise RuntimeError(f"Application deployment target must remain {DEPLOYMENT_TARGET}.")
    minimum = (26, 0, 0)
    for path in sorted(app.rglob("*")):
        if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
            raise RuntimeError(f"Bundle symlink escapes the application: {path}")
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic not in MACHO_MAGIC:
            continue
        arch = run("/usr/bin/xcrun", "lipo", "-archs", path, capture=True, timeout=30).stdout.strip()
        if arch != "arm64":
            raise RuntimeError(f"Release binary is not thin arm64: {path} ({arch}).")
        build = run("/usr/bin/xcrun", "vtool", "-show-build", path, capture=True, timeout=30).stdout
        found = False
        for command in re.split(r"(?m)^Load command \d+\s*$", build):
            # LC_BUILD_VERSION also lists linker/tool versions: those are NOT
            # deployment targets. Only the legacy load command uses 'version'.
            field = "version" if "cmd LC_VERSION_MIN_MACOSX" in command else "minos"
            match = re.search(rf"(?m)^\s*{field} (\d+(?:\.\d+)+)\s*$", command)
            if match:
                found = True
                observed = tuple(int(part) for part in match[1].split("."))
                observed += (0,) * (3 - len(observed))
                if observed > minimum:
                    raise RuntimeError(f"Binary requires macOS {match[1]}, above {DEPLOYMENT_TARGET}: {path}")
                if path.name in {"libcoinfly_neural.dylib", "libcoinfly_vault.dylib"}:
                    if observed != minimum or not re.search(r"(?m)^\s*sdk 27\.0(?:\.0)?\s*$", command):
                        raise RuntimeError(f"Project binary has wrong deployment target/SDK: {path}")
        if not found:
            raise RuntimeError(f"No macOS deployment command: {path}")


def sign_bundle(app: Path, identity: str) -> None:
    # PyInstaller signs dependencies first; give the independent worker its own
    # identity, then reseal the app without --deep overwriting that identity.
    entitlements = ROOT / "packaging/macos/entitlements.plist"
    for target, identifier in (
        (app / "Contents/MacOS/coinfly-worker", "com.devsolux.coinfly.worker"),
        (app, "com.devsolux.coinfly"),
    ):
        run("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime",
            "--entitlements", entitlements, "--identifier", identifier, target, timeout=120)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app, timeout=60)


def finalize_unsigned(app: Path) -> None:
    """Reseal PyInstaller's mechanical ARM64 ad-hoc signatures, without credentials."""
    for target, identifier in ((app / "Contents/MacOS/coinfly-worker", "com.devsolux.coinfly.worker"),
                               (app, "com.devsolux.coinfly")):
        run("/usr/bin/codesign", "--force", "--sign", "-", "--identifier", identifier, target, timeout=60)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app, timeout=60)


def smoke_bundle(app: Path, *, development: bool = False, python: Path | None = None,
                 source: Path | None = None) -> dict:
    for name in ("coinfly", "coinfly-worker"):
        run(app / "Contents/MacOS" / name, "--version", timeout=60)
    # The package check bootstraps an isolated home, never the builder's ledger.
    result = subprocess.run([str(app / "Contents/MacOS/coinfly"), "--smoke-doctor"],
                            text=True, capture_output=True, timeout=60)
    if result.returncode not in (0, 2):
        raise RuntimeError(f"Bundled doctor failed: {result.stderr or result.stdout}")
    report = json.loads(result.stdout)
    if not report.get("desktop_dependencies_ready") or not report.get("local_research_ready"):
        raise RuntimeError("Bundled runtime failed desktop/research dependency checks: "
                           + ", ".join(report.get("blocking_codes", [])))
    required = "DEVELOPMENT_DATASET_DISTRIBUTION" if development else "SIGNED_DATASET_DISTRIBUTION"
    distribution = next((check for check in report.get("checks", []) if check["code"] == required), {})
    if not distribution.get("satisfied"):
        raise RuntimeError("Bundled publisher metadata is missing or invalid.")
    if development and (report.get("production_ready") is not False or any(
        check["code"] == "SIGNED_DATASET_DISTRIBUTION" and check.get("satisfied")
        for check in report.get("checks", [])
    )):
        raise RuntimeError("Development bundle incorrectly claims production/publisher qualification.")
    # Package metadata and headless imports do not prove that a window can open.
    # The diagnostic uses an isolated socket and never launches an operator worker.
    run(python or sys.executable, "-I", (source or ROOT) / "scripts/verify_ui_startup.py", app, timeout=70)
    return report


def require_production_acceptance(report: dict) -> None:
    """Do not promote the disconnected UI smoke to full production acceptance.

    These existing product gates include unimplemented evidence providers. Their
    absence is a release blocker, not permission to issue a weaker distribution.
    Account-specific host fencing and regional enrollment are separate gates.
    """
    required = {"NATIVE_RELEASE_QUALIFICATION", "PRODUCTION_TELEMETRY_QUALIFICATION",
                "STRATEGY_EVALUATION", "NATIVE_SIGNING_UNCERTIFIED", "LIVE_GATEWAY_UNCERTIFIED"}
    setup = {"REGION_UNVERIFIED", "EXTERNAL_HOST_FENCING"}
    checks = report.get("checks", [])
    passed = {check["code"] for check in checks if check.get("satisfied") is True}
    blockers = (required - passed) | {
        check["code"] for check in checks if check.get("satisfied") is not True and check["code"] not in setup
    }
    if blockers:
        raise RuntimeError("Production acceptance unavailable/failed: " + ", ".join(sorted(blockers))
                           + ". Smoke tests cannot replace full-graph, frozen worker or target-host qualification.")


def notarize(app: Path, stage: Path, profile: str) -> None:
    submission = stage / "notarization.zip"
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, submission)
    try:
        result = subprocess.run(
            ["xcrun", "notarytool", "submit", str(submission), "--keychain-profile", profile,
             "--wait", "--timeout", "15m", "--output-format", "json"], text=True, capture_output=True, timeout=960,
        )
    except subprocess.TimeoutExpired as error:
        (stage / "notarization-result.json").write_text(
            json.dumps({"status": "Unknown", "reason": "SUBMISSION_TIMEOUT", "automatic_retry": False}) + "\n")
        raise RuntimeError(f"Notarization outcome unknown after timeout. Reconcile the submission; see {stage}.") from error
    (stage / "notarization-result.json").write_text(result.stdout, encoding="utf-8")
    (stage / "notarization-stderr.log").write_text(result.stderr, encoding="utf-8")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Notarization returned no valid result. See {stage}.") from error
    if not isinstance(report, dict):
        raise RuntimeError(f"Notarization returned no valid result. See {stage}.")
    if result.returncode != 0 or report.get("status") != "Accepted":
        submission_id = report.get("id")
        if isinstance(submission_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", submission_id):
            # Preserve Apple's rejection details; a failed log fetch must not
            # replace the original notarization failure.
            subprocess.run(["xcrun", "notarytool", "log", submission_id, "--keychain-profile", profile,
                            str(stage / "notarization-log.json")], check=False, timeout=60)
        raise RuntimeError(f"Notarization was not Accepted ({report.get('status', 'unknown')}). See {stage}.")
    run("xcrun", "stapler", "staple", app, timeout=120)
    run("xcrun", "stapler", "validate", app, timeout=60)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app, timeout=60)
    run("spctl", "--assess", "--type", "execute", "--verbose=2", app, timeout=60)


def release_environment(stage: Path, toolchain: dict[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("UV_", "PYTHON", "CMAKE_", "PYINSTALLER_", "DYLD_"))
                   and key not in {"VIRTUAL_ENV", "CONDA_PREFIX", "CC", "CXX", "SDKROOT"}}
    environment.update({"UV_PROJECT_ENVIRONMENT": str(stage / "environment"), "UV_PYTHON_DOWNLOADS": "never",
                        "UV_NO_CONFIG": "1", "PYTHONNOUSERSITE": "1", "PYTHONPATH": "",
                        "DEVELOPER_DIR": toolchain["developer_dir"], "SDKROOT": toolchain["sdk_path"],
                        "MACOSX_DEPLOYMENT_TARGET": DEPLOYMENT_TARGET,
                        "CC": toolchain["clang"], "CXX": toolchain["clang++"]})
    return environment


def snapshot_source(destination: Path) -> str:
    """Copy only build inputs, including uncommitted changes; never operator data."""
    excluded = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    inputs = [ROOT / name for name in ("src", "tests", "scripts", "packaging", "native", "assets", "docs",
                                      ".github", "dev.sh", "dev.py", "dev.ps1", "pyproject.toml", "uv.lock",
                                      "README.md", "LICENSE", "AGENTS.md")]
    if any(path.is_symlink() for path in inputs):
        raise RuntimeError("Source snapshot cannot follow symlinks in build input roots")
    digest = hashlib.sha256()
    paths = [path for entry in inputs for path in ([entry] if entry.is_file() else sorted(entry.rglob("*")))]
    for path in sorted(paths):
        relative = path.relative_to(ROOT)
        if any(part in excluded or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError(f"Source snapshot cannot follow symlinks: {relative}")
        if not path.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        target.write_bytes(payload)
        shutil.copymode(path, target)
        digest.update(relative.as_posix().encode() + b"\0" + hashlib.sha256(payload).digest())
    return digest.hexdigest()


def quality_gates(source: Path, python: Path, environment: dict[str, str], evidence: Path) -> None:
    """Common gates for both modes; first failure aborts without publication."""
    evidence.mkdir()
    commands = (
        ("shell", ["/bin/bash", "-n", str(source / "dev.sh")]),
        ("ruff", [str(python), "-I", "-m", "ruff", "check", "src", "tests", "scripts", "dev.py"]),
        ("mypy", [str(python), "-I", "-m", "mypy", "src/coinfly"]),
        ("pytest", [str(python), "-I", "-m", "pytest", "-q", "--native-ui",
                    f"--junitxml={evidence / 'pytest.xml'}"]),
        ("dependencies", [str(python), "-I", "-m", "pip_audit", "--local", "--skip-editable"]),
    )
    for name, command in commands:
        print(f"Production gate: {name}", flush=True)
        with (evidence / f"{name}.log").open("w") as log:
            run(*command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=3600)


def check_mode(app: Path, expected_mode: str) -> None:
    validate_mode(expected_mode)
    document = json.loads((app / "Contents/Resources/coinfly-build.json").read_bytes())
    if document.get("build_profile") != "production" or document.get("distribution_mode") != expected_mode:
        raise RuntimeError("Artifact mode mismatch; production/signed expectations cannot be downgraded.")


def verify_archive(archive: Path, publisher: Path, *, mode: str, python: Path, source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="coinfly-relocated-") as temporary:
        target = Path(temporary)
        run("/usr/bin/ditto", "-x", "-k", archive, target, timeout=180)
        app = target / "coinfly.app"
        check_mode(app, mode)
        check_bundled_distribution(app, publisher)
        check_bundle(app)
        require_production_acceptance(smoke_bundle(app, python=python, source=source))
        if mode == "signed":
            run("/usr/bin/codesign", "--verify", "--deep", "--strict", app, timeout=60)
            run("/usr/bin/xcrun", "stapler", "validate", app, timeout=60)
            run("/usr/sbin/spctl", "--assess", "--type", "execute", app, timeout=60)


def build(identity: str, profile: str, team: str, *, dataset_distribution: Path | None = None,
          mode: str = "signed", toolchain: dict[str, str] | None = None) -> Path:
    validate_mode(mode)
    if mode == "unsigned":
        identity, profile, team = "", "", ""
    toolchain = toolchain or toolchain_preflight()

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    info = plistlib.loads((ROOT / "packaging/macos/Info.plist").read_bytes())
    version = metadata["project"]["version"]
    if (metadata["project"]["name"] != "coinfly"
            or info["CFBundleIdentifier"] != metadata["tool"]["coinfly"]["application-id"]
            or info["CFBundleVersion"] != version or info["CFBundleShortVersionString"] != version
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)):
        raise RuntimeError("Release identity mismatch.")
    for directory in (ROOT / "build", ROOT / "dist"):
        if directory.is_symlink():
            raise RuntimeError(f"Release output directory must not be a symlink: {directory}.")
        directory.mkdir(exist_ok=True)
    # A fresh workspace avoids stale bundles and retains diagnostics on failure.
    stage = Path(tempfile.mkdtemp(prefix=f"macos-{mode}-", dir=ROOT / "build"))
    print(f"Release workspace: {stage}", flush=True)
    publisher = stage_distribution(dataset_distribution, stage)
    source = stage / "source"
    source_digest = snapshot_source(source)
    environment = release_environment(stage, toolchain)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    run(uv, "lock", "--check", "--project", source, "--python", sys._base_executable,
        "--no-python-downloads", env=environment, timeout=180)
    run(uv, "sync", "--project", source, "--locked", "--python", sys._base_executable,
        "--no-python-downloads", "--no-editable", "--extra", "desktop", "--extra", "research",
        "--extra", "training", "--group", "dev", "--group", "release", env=environment, timeout=900)
    python = stage / "environment/bin/python"
    quality_gates(source, python, environment, stage / "evidence")
    native = stage / "native"
    run("cmake", "-S", source / "native", "-B", native, "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={DEPLOYMENT_TARGET}",
        f"-DCMAKE_OSX_SYSROOT={toolchain['sdk_path']}", f"-DCMAKE_CXX_COMPILER={toolchain['clang++']}",
        f"-DCMAKE_OBJCXX_COMPILER={toolchain['clang++']}", f"-DCOINFLY_TEAM_ID={team}", env=environment, timeout=180)
    run("cmake", "--build", native, "--config", "Release", "--parallel", env=environment, timeout=600)
    environment = {**environment, "COINFLY_NATIVE_BUILD": str(native), "COINFLY_BUILD_MODE": mode,
                   "COINFLY_STAGED_PUBLISHER": str(publisher),
                   "PYINSTALLER_CONFIG_DIR": str(stage / "pyinstaller-cache")}
    run(python, "-I", "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", stage / "bundle", "--workpath", stage / "pyinstaller",
        source / "packaging/macos/coinfly.spec", env=environment, cwd=source, timeout=1800)
    app = stage / "bundle/coinfly.app"
    check_bundled_distribution(app, publisher)
    check_bundle(app)
    manifest = {"build_profile": "production", "distribution_mode": mode,
                "publisher": metadata["tool"]["coinfly"]["publisher"],
                "code_signature": "Developer ID" if mode == "signed" else "ad-hoc",
                "source_sha256": source_digest, "toolchain": toolchain,
                "notarization": "pending" if mode == "signed" else "NOT_APPLICABLE_UNSIGNED",
                "stapling": "pending" if mode == "signed" else "NOT_APPLICABLE_UNSIGNED"}
    (app / "Contents/Resources/coinfly-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if mode == "signed":
        sign_bundle(app, identity)
    else:
        finalize_unsigned(app)
    check_mode(app, mode)
    require_production_acceptance(smoke_bundle(app, python=python, source=source))
    if mode == "signed":
        notarize(app, stage, profile)
        manifest.update(notarization="Accepted", stapling="validated")
    # ZIP files cannot be stapled. Archive the app again AFTER stapling, and only
    # replace a previous release after every verification has succeeded.
    filename = f"coinfly-{version}-macos-arm64-{mode}.zip"
    publication = stage / "publication"
    publication.mkdir()
    archive = publication / filename
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive)
    verify_archive(archive, publisher, mode=mode, python=python, source=source)
    with archive.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {filename}\n", encoding="utf-8")
    manifest["archive_sha256"] = checksum
    (publication / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copytree(stage / "evidence", publication / "evidence")
    directory = ROOT / "dist"
    for name in (version, "macos-arm64", mode):
        directory /= name
        if directory.is_symlink():
            raise RuntimeError(f"Release directory must not be a symlink: {directory}")
        directory.mkdir(exist_ok=True)
    output_directory = directory / stage.name
    os.rename(publication, output_directory)
    output = output_directory / filename
    print(f"Production {mode} release: {output}\nSHA-256: {checksum}", flush=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, help="Internal explicit production mode from dev.sh")
    parser.add_argument("--preflight", action="store_true", help="Check release tools, identity and notary profile")
    parser.add_argument("--initialize-development-distribution", type=Path,
                        help="Create/validate local paper metadata and exit without signing or notarizing")
    parser.add_argument("--require-publisher", action="store_true",
                        help="Production/CI mode: reject missing or development metadata")
    parser.add_argument("--dataset-distribution", type=Path,
                        help="Reviewed devsolux trust root and signed dataset manifest directory "
                             "(default: ~/.devsolux/coinfly)")
    args = parser.parse_args()
    try:
        if args.initialize_development_distribution is not None:
            if args.require_publisher or args.preflight or args.mode:
                raise RuntimeError("Metadata initialization cannot be combined with release options.")
            directory = initialize_development_distribution(args.initialize_development_distribution)
            print(f"Development metadata: {directory}", flush=True)
            return 0
        if args.mode is None:
            # Direct legacy entry points resolve defaults in exactly one place.
            distribution_args = (["--dataset-distribution", str(args.dataset_distribution.expanduser().resolve())]
                                 if args.dataset_distribution is not None else [])
            return subprocess.run(["/bin/bash", str(ROOT / "dev.sh"), "build",
                                   *(["--preflight"] if args.preflight else []), *distribution_args],
                                  check=False).returncode
        toolchain = toolchain_preflight()
        print(json.dumps({"production_toolchain": toolchain}, sort_keys=True), flush=True)
        os.environ["DEVELOPER_DIR"] = toolchain["developer_dir"]
        identity, profile = (os.environ.get("MACOS_SIGN_IDENTITY", ""), os.environ.get("MACOS_NOTARY_PROFILE", "")) \
            if args.mode == "signed" else ("", "")
        team = preflight(identity, profile, mode=args.mode)
        for variable in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
            if os.environ.get(variable) and Path(os.environ[variable]).resolve() != (ROOT / ".venv").resolve():
                raise RuntimeError(f"Foreign {variable}: unset it before building; release environments are isolated.")
        with tempfile.TemporaryDirectory(prefix="coinfly-release-preflight-") as temporary:
            stage_distribution(args.dataset_distribution, Path(temporary))
        gui_preflight()
        if args.preflight:
            print(f"macOS production {args.mode} prerequisites passed; no build or acceptance was performed.")
        else:
            build(identity, profile, team, dataset_distribution=args.dataset_distribution,
                  mode=args.mode, toolchain=toolchain)
    except (RuntimeError, OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        print(f"coinfly build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
