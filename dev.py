#!/usr/bin/env python3
"""Native desktop development entry point; no financial command-line overrides."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
TARGETS = {("darwin", "arm64"), ("win32", "amd64"), ("linux", "amd64")}


def target(os_name: str, machine: str) -> str:
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine.lower(), machine.lower())
    if (os_name, architecture) not in TARGETS:
        raise ValueError("Supported desktops: macOS ARM64, Windows AMD64, Linux AMD64")
    return f"{ {'darwin': 'macos', 'win32': 'windows', 'linux': 'linux'}[os_name]}-{architecture}"


def python_path() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), cwd=ROOT, check=False).returncode


def runtime_probe(interpreter: Path, *, desktop: bool) -> None:
    script = '''import json,platform,sqlite3,sys
result={"os":sys.platform,"machine":platform.machine(),"python":list(sys.version_info[:3]),
        "sqlite":list(sqlite3.sqlite_version_info),"sqlite_text":sqlite3.sqlite_version}
try:
 import tkinter
 result["tkinter"]=True
except ImportError:
 result["tkinter"]=False
print(json.dumps(result))'''
    result = subprocess.run([str(interpreter), "-I", "-c", script], cwd=ROOT, check=True,
                            capture_output=True, text=True)
    state = json.loads(result.stdout)
    target(state["os"], state["machine"])
    if tuple(state["python"]) < (3, 11):
        raise ValueError("Python >=3.11 is required")
    if tuple(state["sqlite"]) < (3, 51, 3):
        raise ValueError(f"Selected Python links SQLite {state['sqlite_text']}; >=3.51.3 is required. "
                         "Select a compatible native interpreter; the requirement is not bypassed.")
    if desktop and not state["tkinter"]:
        raise ValueError("The selected native Python must include Tk for the desktop")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coinfly 1.0.0 native desktop development")
    parser.add_argument("command", nargs="?", default="run", choices=(
        "setup", "run", "worker", "stop-worker", "prepare", "kernel", "test", "test-ui", "check", "doctor", "build",
        "build-unsigned"))
    parser.add_argument("--preflight", action="store_true", help="Check macOS production build prerequisites only")
    parser.add_argument("--python", type=Path, help="Native interpreter for setup (absolute path)")
    parser.add_argument("--offline", action="store_true", help="Use cached dependencies during setup")
    parser.add_argument("--force", action="store_true", help="stop-worker: stop now even while paper positions are open")
    args, extra = parser.parse_known_args(argv)
    try:
        target(sys.platform, platform.machine())
        if args.command not in {"test", "kernel"} and extra:
            parser.error("Unexpected arguments")
        if args.command != "setup" and (args.python is not None or args.offline):
            parser.error("--python and --offline apply only to setup")
        if args.preflight and args.command not in {"build", "build-unsigned"}:
            parser.error("--preflight applies only to macOS builds")
        if args.force and args.command != "stop-worker":
            parser.error("--force applies only to stop-worker")
        if args.command in {"build", "build-unsigned"} and sys.platform == "darwin":
            return run(["/bin/bash", str(ROOT / "dev.sh"), args.command,
                        *(["--preflight"] if args.preflight else [])])
        if args.command == "build-unsigned" or args.preflight:
            parser.error("build-unsigned and --preflight require macOS ARM64")
        if args.command == "setup":
            interpreter = args.python or Path(sys.executable)
            if not interpreter.is_absolute() or not interpreter.is_file():
                raise ValueError("--python must name an existing absolute interpreter path")
            runtime_probe(interpreter, desktop=True)
            uv = shutil.which("uv")
            if uv is None:
                raise ValueError("Install uv first; this launcher never downloads executable tools")
            command = [uv, "sync", "--locked", "--python", str(interpreter),
                       "--extra", "desktop", "--extra", "research", "--extra", "training"]
            return run(command + (["--offline"] if args.offline else []))
        python = python_path()
        if not python.is_file():
            raise ValueError("Project environment is missing. Run python dev.py setup first")
        runtime_probe(python, desktop=args.command in {"run", "test-ui", "check", "build"})
        prefix = [str(python), "-I"]
        if args.command in {"run", "worker", "prepare"}:
            return run(prefix + ["-m", "coinfly.development", {"run": "desktop"}.get(args.command, args.command)])
        if args.command == "stop-worker":
            return run(prefix + ["-m", "coinfly.development", "stop-worker", *(["--force"] if args.force else [])])
        if args.command == "kernel":
            return run(prefix + [str(ROOT / "scripts/build_kernel.py"), *extra])
        if args.command == "doctor":
            return run(prefix + ["-m", "coinfly.cli", "doctor"])
        if args.command == "test":
            return run(prefix + ["-m", "pytest", *extra])
        if args.command == "test-ui":
            return run(prefix + ["-m", "pytest", "-q", "tests/ui", "--native-ui"])
        if args.command == "build":
            return run(prefix + [str(ROOT / "scripts/build_desktop.py")])
        for command in (["-m", "ruff", "check", "src", "tests", "scripts", "packaging", "dev.py"],
                        ["-m", "mypy", "src/coinfly"],
                        ["-m", "pytest", "-q"]):
            result = run(prefix + command)
            if result:
                return result
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"coinfly dev: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
