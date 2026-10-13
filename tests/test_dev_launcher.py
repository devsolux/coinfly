"""Exercise destructive cleanup only in disposable project fixtures."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Bash development launcher")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    shutil.copy2(ROOT / "dev.sh", root / "dev.sh")
    for name in ("pyproject.toml", "uv.lock"):
        (root / name).write_text("fixture\n")
    return root


def launch(project, *args):
    return subprocess.run(["/bin/bash", str(project / "dev.sh"), *args], cwd=project.parent,
                          env={**os.environ, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)


def test_clean_preserves_data_and_tooling_without_a_runtime(project):
    removed = [
        ".venv/broken", "src/example/__pycache__/module.pyc", "src/example/module.pyo",
        "src/example.egg-info/PKG-INFO", ".eggs/a", ".tox/a", ".nox/a", "__pypackages__/a",
        ".pytest_cache/a", ".mypy_cache-extra/a", ".ruff_cache/a", ".hypothesis/a",
        ".ipynb_checkpoints/a", "src/.DS_Store", "build/a", "dist/a", "htmlcov/a",
        ".coverage", ".coverage.worker", "coverage.xml", "coverage.json",
    ]
    preserved = [
        "src/coinfly/storage/schema.sql", "src/example/build/source.py", ".env", ".env.local",
        "pyproject.toml", "uv.lock", ".git/cache.pyc", ".agents/.pytest_cache/state",
        ".codex/__pycache__/state.pyc", "storage/.venv/state", "data/snapshot.pyc", "datasets/.DS_Store",
        "models/__pycache__/state", "runs/.pytest_cache/state", "logs/.DS_Store", "cache/file.pyc",
    ]
    for name in removed + preserved:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    for _ in range(2):
        result = launch(project, "clean")
        assert result.returncode == 0, result.stderr
        assert all(not (project / name).exists() for name in removed)
        assert all((project / name).read_text() == "fixture\n" for name in preserved)


def test_clean_unlinks_generated_symlinks_without_following_them(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.pyc"
    sentinel.write_text("keep\n")
    for name in (".venv", "build", "dist", "__pycache__", "storage", "linked-source"):
        (project / name).symlink_to(outside, target_is_directory=True)
    result = launch(project, "clean")
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "keep\n"
    for name in (".venv", "build", "dist", "__pycache__"):
        assert not (project / name).is_symlink()
    for name in ("storage", "linked-source"):
        assert (project / name).is_symlink()


@pytest.mark.parametrize("command", ["clean", "build", "build-unsigned"])
def test_rejects_extra_arguments_before_any_changes(project, command):
    marker = project / "build/keep"
    marker.parent.mkdir()
    marker.touch()
    result = launch(project, command, "unexpected")
    assert result.returncode == 1
    assert marker.exists()


def test_help_describes_environment_defaults_without_credentials(project):
    result = launch(project, "help")
    assert result.returncode == 0
    assert "351A9115F928304981211FEB5866E94B6C760AD1" in result.stdout
    assert "macos-notary" in result.stdout
    assert "build-unsigned" in result.stdout
    assert "No signing flags are required" in result.stdout


SIGNING_CASES = [
    (None, None), (None, "profile"), ("identity", None), ("", ""),
    ("", "profile"), ("identity", ""),
    ("351A9115F928304981211FEB5866E94B6C760AD1", "macos-notary"),
    ("Developer ID Application: Bärış Aydın (ABCDE12345)", "notary Prüfprofil 雪"),
    ("unavailable certificate", "missing profile"), ("  identity  ", "  profile  "),
]


@pytest.fixture
def build_probe(project, tmp_path):
    # The actual full shell entry point/parser runs, but cannot reach real tools.
    binaries = tmp_path / "tools"
    binaries.mkdir()
    uname = binaries / "uname"
    uname.write_text('#!/bin/bash\nif [[ "$1" == -s ]]; then echo Darwin; else echo arm64; fi\n')
    uname.chmod(0o755)
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    log = tmp_path / "calls.jsonl"
    python.write_text(f'''#!{sys.executable}
import json, os, sys
with open({str(log)!r}, 'a') as output:
    output.write(json.dumps({{"args": sys.argv[1:], "identity": os.environ.get("MACOS_SIGN_IDENTITY"),
        "profile": os.environ.get("MACOS_NOTARY_PROFILE")}}) + '\\n')
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
''')
    python.chmod(0o755)

    def invoke(command, arguments=(), identity=None, profile=None, exit_code=0):
        env = {key: value for key, value in os.environ.items()
               if key not in {"MACOS_SIGN_IDENTITY", "MACOS_NOTARY_PROFILE"}}
        env.update(PATH=str(binaries) + ":/usr/bin:/bin", PROBE_EXIT=str(exit_code))
        for key, value in (("MACOS_SIGN_IDENTITY", identity), ("MACOS_NOTARY_PROFILE", profile)):
            if value is not None:
                env[key] = value
        result = subprocess.run(["/bin/bash", str(project / "dev.sh"), command, *arguments],
                                cwd=tmp_path, env=env, text=True, capture_output=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return invoke


@pytest.mark.parametrize(("identity", "profile"), SIGNING_CASES)
@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize(("command", "mode"), [("build", "signed"), ("build-unsigned", "unsigned")])
def test_actual_shell_resolves_defaults_and_forwards_exact_values(project, build_probe, identity, profile, preflight, command, mode):
    arguments = ["--preflight"] if preflight else []
    result, calls = build_probe(command, arguments, identity, profile)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    call = calls[0]
    assert call["args"] == ["-I", str(project / "scripts/build_macos.py"), "--mode", mode, *arguments]
    assert call["identity"] == (identity or "351A9115F928304981211FEB5866E94B6C760AD1")
    assert call["profile"] == (profile or "macos-notary")
    assert not (project / "build").exists()  # No automatic metadata or environment sync.


@pytest.mark.parametrize("command", ["build", "build-unsigned"])
@pytest.mark.parametrize("preflight_first", [False, True])
def test_build_forwards_explicit_dataset_directory(project, build_probe, command, preflight_first):
    arguments = ["--dataset-distribution", "/reviewed metadata ü"]
    arguments = ["--preflight", *arguments] if preflight_first else [*arguments, "--preflight"]
    result, calls = build_probe(command, arguments)
    assert result.returncode == 0, result.stderr
    mode = "signed" if command == "build" else "unsigned"
    assert calls[0]["args"] == ["-I", str(project / "scripts/build_macos.py"), "--mode", mode, *arguments]


@pytest.mark.parametrize("command", ["build", "build-unsigned"])
@pytest.mark.parametrize("exit_code", [1, 2, 73, 143])
def test_build_child_failure_is_preserved(build_probe, command, exit_code):
    result, calls = build_probe(command, exit_code=exit_code)
    assert result.returncode == exit_code
    assert len(calls) == 1


@pytest.mark.parametrize("arguments", [("--identity", "x"), ("--notary-profile", "x"), ("--skip-tests",),
                                        ("--no-notary",)])
@pytest.mark.parametrize("command", ["build", "build-unsigned"])
def test_unsupported_build_options_fail_before_interpreter(build_probe, command, arguments):
    result, calls = build_probe(command, arguments)
    assert result.returncode != 0 and not calls
    assert "accepts only --preflight" in result.stderr


@pytest.mark.parametrize("arguments", [
    ("--preflight", "--preflight"), ("--dataset-distribution",), ("--dataset-distribution", ""),
    ("--dataset-distribution", "--preflight"),
    ("--dataset-distribution", "/one", "--dataset-distribution", "/two"),
])
@pytest.mark.parametrize("command", ["build", "build-unsigned"])
def test_invalid_build_options_fail_before_interpreter(build_probe, command, arguments):
    result, calls = build_probe(command, arguments)
    assert result.returncode != 0 and not calls
