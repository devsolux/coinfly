"""Explicit local build of the MaleCNS native propagator for paper research.

Targets: macOS ARM64, Windows AMD64, Linux AMD64.

Usage (explicit build, never desktop startup):

    .venv/bin/python -I scripts/build_kernel.py [--sign-identity ID]

The script verifies that ``src/coinfly/research/neural/kernel.cpp`` still has the
pinned SHA-256, compiles it with CMake into a temporary build directory, copies
the library to ``~/.devsolux/coinfly/models/kernels/<source-sha-prefix>/`` and
writes the development attestation beside it. Optionally the library is signed
with an explicit code-signing identity (default: ad-hoc). The result is a
LOCAL_DEVELOPMENT_KERNEL capability: it lets the preserved controller run in the
local paper worker and satisfies no release gate.

It never runs at application startup and never touches release trust anchors.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coinfly.paths import ApplicationPaths  # noqa: E402
from coinfly.research.assets import KERNEL_SOURCE, KERNEL_SOURCE_SHA256  # noqa: E402
from coinfly.research.kernel_registry import (  # noqa: E402
    DEVELOPMENT_IDENTITY_PREFIX,
    library_name,
    write_development_attestation,
)


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=True, text=True, capture_output=capture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the local development neural kernel")
    parser.add_argument("--sign-identity", default=None, help="codesign identity (default: ad-hoc signature)")
    parser.add_argument("--kernels-directory", default=None, help="override the kernels folder (tests only)")
    arguments = parser.parse_args(argv)
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    if (sys.platform, machine) not in {("darwin", "arm64"), ("win32", "amd64"), ("linux", "amd64")}:
        print("build_kernel: supported targets are macOS ARM64, Windows AMD64 and Linux AMD64.", file=sys.stderr)
        return 2
    if arguments.sign_identity and sys.platform != "darwin":
        parser.error("--sign-identity is a macOS codesign option")
    source_hash = hashlib.sha256(KERNEL_SOURCE.read_bytes()).hexdigest()
    if source_hash != KERNEL_SOURCE_SHA256:
        print("build_kernel: kernel.cpp does not match the pinned reviewed hash; refusing to build.", file=sys.stderr)
        return 2
    for tool in ("cmake",):
        if shutil.which(tool) is None:
            print(f"build_kernel: missing tool {tool}. Install CMake and the platform C++17 toolchain.", file=sys.stderr)
            return 2
    kernels = Path(arguments.kernels_directory) if arguments.kernels_directory else ApplicationPaths.for_current_user().kernels
    target_directory = kernels / KERNEL_SOURCE_SHA256[:16]
    target_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coinfly-kernel-build-") as temporary:
        build = Path(temporary) / "build"
        platform_args = ["-DCMAKE_OSX_ARCHITECTURES=arm64"] if sys.platform == "darwin" else (["-A", "x64"] if sys.platform == "win32" else [])
        run("cmake", "-S", str(ROOT / "native"), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release", *platform_args)
        run("cmake", "--build", str(build), "--target", "coinfly_neural", "--config", "Release")
        produced = next(build.rglob(library_name()), None)
        if produced is None:
            print("build_kernel: the build produced no library.", file=sys.stderr)
            return 1
        destination = target_directory / library_name()
        shutil.copy2(produced, destination)
    os.chmod(destination, 0o600)
    identity = arguments.sign_identity
    if sys.platform == "darwin" and shutil.which("codesign") is not None:
        run("codesign", "--force", "--sign", identity or "-", str(destination))
    compiler_tool = next((tool for tool in ("clang++", "g++") if shutil.which(tool)), None)
    compiler = run(compiler_tool, "--version", capture=True).stdout.splitlines()[0] if compiler_tool else "CMake-selected C++17 toolchain"
    signed_identity = DEVELOPMENT_IDENTITY_PREFIX + (identity or ("adhoc" if sys.platform == "darwin" else "unsigned-local"))
    attestation = write_development_attestation(destination, compiler=compiler, signed_identity=signed_identity)
    print(f"Local development kernel written: {destination.name} (capability LOCAL_DEVELOPMENT_KERNEL)")
    print(f"Attestation: {attestation.name}; source {KERNEL_SOURCE_SHA256[:16]}…; no release gate is satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
