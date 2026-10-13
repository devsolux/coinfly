"""Explicit Windows/Linux paper-development packaging, never live certification.

Run with the locked environment including the `release` dependency group.
macOS continues to use the reviewed signing/notarization builder.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("signed", "unsigned"), default="signed")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if sys.platform == "darwin":
        command = "build" if args.mode == "signed" else "build-unsigned"
        return subprocess.run(["/bin/bash", str(ROOT / "dev.sh"), command,
                               *(["--preflight"] if args.preflight else [])], cwd=ROOT, check=False).returncode
    if args.mode != "signed" or args.preflight:
        parser.error("--mode unsigned and --preflight apply only to macOS production builds")
    architecture = {"x86_64": "amd64"}.get(platform.machine().lower(), platform.machine().lower())
    if sys.platform not in {"win32", "linux"} or architecture != "amd64":
        print("build_desktop: native Windows/Linux AMD64 required", file=sys.stderr)
        return 2
    if importlib.util.find_spec("PyInstaller") is None:
        print("build_desktop: sync the locked release group before building", file=sys.stderr)
        return 2
    from coinfly.bootstrap.development_distribution import development_documents
    from coinfly.research.assets import KERNEL_SOURCE_SHA256
    from coinfly.research.kernel_registry import KernelRegistry

    generated = ROOT / "build" / "desktop-development"
    generated.mkdir(parents=True, exist_ok=True)
    kernels = generated / "kernels"
    build = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/build_kernel.py"),
                            "--kernels-directory", str(kernels)], cwd=ROOT, check=False)
    if build.returncode:
        return build.returncode
    registry = KernelRegistry(kernels)
    if registry.verified() is None:
        print("build_desktop: explicit local kernel verification failed", file=sys.stderr)
        return 2
    descriptor = generated / "publisher"
    descriptor.mkdir(exist_ok=True)
    for name, document in development_documents().items():
        (descriptor / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    environment = dict(os.environ)
    # Build-only paths are not runtime financial or trust-root overrides.
    environment["COINFLY_STAGED_PUBLISHER"] = str(descriptor)
    environment["COINFLY_DEVELOPMENT_KERNEL"] = str(kernels / KERNEL_SOURCE_SHA256[:16])
    return subprocess.run([sys.executable, "-I", "-m", "PyInstaller", "--noconfirm", "--clean",
                           str(ROOT / "packaging/desktop/development.spec")], cwd=ROOT,
                          env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
