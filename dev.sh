#!/bin/bash
# coinfly 1.0.0 / com.devsolux.coinfly — local macOS Apple Silicon development.
# Compatible with the Bash 3.2 shipped by macOS. No financial CLI overrides.
set -euo pipefail

# Build mode selects signing; these references are ignored by unsigned builds.
export MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-351A9115F928304981211FEB5866E94B6C760AD1}"

export MACOS_NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-macos-notary}"

coinfly_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
coinfly_venv="$coinfly_root/.venv"
coinfly_python="$coinfly_venv/bin/python"

usage() {
    cat <<'HELP'
coinfly 1.0.0 — local development on macOS ARM64 (including M4)

Usage: ./dev.sh [command] [arguments]

  run                          Start the isolated paper development desktop (default).
  prepare                      Download, build and verify the full MaleCNS dataset.
  kernel [--sign-identity ID]  Build the local development neural kernel (CMake, pinned source).
  setup [--python PATH]         Create/sync .venv from uv.lock.
        [--offline]            Use only cached packages during setup.
  clean                        Remove project environments, caches, and builds.
  build [--preflight]           Signed, notarized and stapled production macOS ARM64 build.
  build-unsigned [--preflight]  Production build without Developer ID or notarization.
        [--dataset-distribution DIR]  Reviewed metadata (default: ~/.devsolux/coinfly).
  test [pytest arguments]      Run tests; extra arguments are passed to pytest.
  test-ui                      Run native desktop smoke tests (opens windows).
  check                        Check shell syntax, Ruff, mypy, and all tests.
  locale                       Test English/German labels and money formatting.
  tk                           Open Python's native Tk test window.
  doctor                       Show runtime and release readiness (may exit 2).
  worker                       Run the isolated development worker in the foreground.
  stop-worker [--force]        Retire this checkout's background development worker
                               (graceful: drains paper positions first; --force stops now).
  help                         Show this help.

First-time prerequisites (native Homebrew):
  /opt/homebrew/bin/brew install uv python@3.14 python-tk@3.14

build uses these environment defaults; nonempty overrides are forwarded unchanged:
  MACOS_SIGN_IDENTITY   351A9115F928304981211FEB5866E94B6C760AD1
  MACOS_NOTARY_PROFILE  macos-notary
No signing flags are required. The exact identity/private key and profile must exist.
build-unsigned never looks up Apple credentials, even when these variables are set.
Publisher: devsolux. Both modes read reviewed trust-root.json and manifest.json
from ~/.devsolux/coinfly by default; --dataset-distribution DIR overrides it.
Both modes require the production quality gates,
Xcode 27.0 / Apple clang 21.0.0 (clang-2100.3.34.2) / SDK 27.0 (26A425),
and deployment target macOS 26.0. DEVELOPER_DIR selects the full Xcode installation.
--preflight checks prerequisites only; it does not sync, compile or publish.
Production builds never generate development metadata. See packaging/README.md.

Setup accepts an absolute path to another native Python >=3.11 with Tk.
Run/prepare/test/test-ui/check/locale/tk/doctor/worker reuse .venv without a package sync;
setup runs automatically only when .venv is absent. No shell activation needed.
run/prepare/worker verify the pinned MaleCNS sources and full derived graph;
the first run downloads about 1.1 GB. Data stays under ~/.devsolux/coinfly/.
kernel compiles the pinned kernel.cpp once (Xcode CLT + CMake) into
~/.devsolux/coinfly/models/kernels/ with a local development attestation, so
the preserved MaleCNS controller can run in the paper worker. It is a local
research capability only and satisfies no release or live gate.
Datasets are shared; paper ledgers remain isolated per checkout under storage/.
This local paper profile does not claim publisher or native-kernel certification.
Use .venv/bin/coinfly for the separate publisher-signed desktop startup.
clean works without Python, uv, or a working .venv, on macOS or Linux.
It removes .venv, __pycache__, *.pyc/*.pyo, *.egg-info, .eggs, .tox,
.nox, __pypackages__, .pytest_cache, .mypy_cache*, .ruff_cache,
.hypothesis, .ipynb_checkpoints, .DS_Store, and root build/dist/coverage outputs.
It preserves source, .git, .agents, .codex, .env, lockfiles, application data,
and global caches (including data directories inside this checkout).
Stop Coinfly and its worker before cleaning; use ./dev.sh setup to rebuild.
Use the app's EN/DE selector to test the UI language interactively.
The development worker keeps running after the desktop closes (it owns the
paper book). run and worker replace a worker that was started from older
sources automatically; stop-worker retires it explicitly.
HELP
}

fail() { printf 'coinfly dev: %s\n' "$*" >&2; exit 1; }

clean_environment() {
    [[ "$coinfly_root" != / ]] || fail "Refusing to clean the filesystem root."
    cd -- "$coinfly_root"
    printf 'Cleaning generated development files in %s\n' "$coinfly_root"
    # -P keeps traversal inside real directories; rm receives no trailing slash
    # on symlinks, so it removes the link itself rather than its external target.
    # Prune generated directories before deleting them; preserve version control.
    /usr/bin/find -P . \
        \( -name .git -o -name .hg -o -name .svn -o -name .agents \
           -o -name .codex -o -name .env -o -name '.env.*' \
           -o -path ./data -o -path ./storage -o -path ./datasets \
           -o -path ./models -o -path ./runs -o -path ./logs -o -path ./cache \
           -o -path ./build -o -path ./dist -o -path ./htmlcov \) -prune -o \
        \( -name .venv -o -name __pycache__ -o -name '*.egg-info' \
           -o -name .eggs -o -name .tox -o -name .nox -o -name __pypackages__ \
           -o -name .pytest_cache -o -name '.mypy_cache*' -o -name .ruff_cache \
           -o -name .hypothesis -o -name .ipynb_checkpoints \) \
        -prune -exec /bin/rm -rf -- {} + -o \
        \( -type f -o -type l \) \
        \( -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) \
        -exec /bin/rm -f -- {} +
    # These names may be legitimate source directories deeper in the project.
    # Only the root build and coverage outputs belong to this cleanup scope.
    /bin/rm -rf -- "$coinfly_root/build" "$coinfly_root/dist" "$coinfly_root/htmlcov"
    /bin/rm -f -- "$coinfly_root/.coverage" "$coinfly_root"/.coverage.* \
        "$coinfly_root/coverage.xml" "$coinfly_root/coverage.json"
    printf 'Clean complete. Run ./dev.sh setup to recreate the development environment.\n'
}

require_host() {
    [[ "$(uname -s)" == "Darwin" ]] || fail "This launcher targets macOS ARM64. Use the README commands on this host."
    local translated
    translated="$(/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true)"
    [[ "$translated" != "1" ]] || fail "Rosetta shell detected. Open Terminal with 'Open using Rosetta' disabled, then retry."
    [[ "$(uname -m)" == "arm64" ]] || fail "A native arm64 shell is required. Check: uname -m"
}

runtime_check() {
    local interpreter="$1" level="$2"
    "$interpreter" -I - "$level" "$coinfly_venv" <<'PY'
import importlib.metadata
import platform
import sqlite3
import sys
from pathlib import Path

level, expected_venv = sys.argv[1:]

def fail(message):
    print(f"coinfly dev: {message}", file=sys.stderr)
    raise SystemExit(1)

if sys.platform != "darwin" or platform.machine().lower() != "arm64":
    fail("The selected Python is not native macOS arm64. Select an ARM64 Python with setup --python PATH.")
if sys.version_info < (3, 11):
    fail("Python 3.11 or newer is required.")
if sqlite3.sqlite_version_info < (3, 51, 3):
    fail(f"Python is linked to SQLite {sqlite3.sqlite_version}; >=3.51.3 is required. "
         "Upgrade the matching Homebrew Python/Tk installation and rerun setup. "
         "Updating only the sqlite3 command does not update Python's linked runtime.")
try:
    import tkinter
except ImportError:
    fail("Tk is missing from this Python. Install the matching python-tk formula, then rerun setup.")

print(f"Python {platform.python_version()} / {platform.machine()} / SQLite {sqlite3.sqlite_version} / Tk {tkinter.TkVersion}")
if level != "base":
    if Path(sys.prefix).resolve() != Path(expected_venv).resolve():
        fail("The interpreter does not belong to this project's .venv. Rerun setup.")
if level not in ("base", "environment"):
    try:
        import coinfly
        version = importlib.metadata.version("customtkinter")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        fail("Project dependencies are incomplete. Run ./dev.sh setup.")
    if (getattr(coinfly, "__project_name__", None), getattr(coinfly, "__version__", None),
        getattr(coinfly, "__application_id__", None)) != (
        "coinfly", "1.0.0", "com.devsolux.coinfly"
    ):
        fail("Installed Coinfly identity does not match this project. Run ./dev.sh setup.")
    if version != "6.0.0":
        fail(f"CustomTkinter {version} is installed; 6.0.0 is required. Run ./dev.sh setup.")
    expected_source = Path(expected_venv).parent / "src" / "coinfly" / "__init__.py"
    if Path(coinfly.__file__).resolve() != expected_source.resolve():
        fail("Coinfly imports from another installation. Run ./dev.sh setup to install this checkout.")
    print(f"coinfly {coinfly.__version__} / {coinfly.__application_id__} / CustomTkinter {version}")
    if level == "window":
        import customtkinter
        root = None
        try:
            root = customtkinter.CTk()
            root.withdraw()
            root.update_idletasks()
            print(f"Native window check passed / Tcl/Tk {root.tk.call('info', 'patchlevel')}")
        except tkinter.TclError:
            fail("A native Tk window could not open. Run from a logged-in macOS desktop session; use ./dev.sh tk to diagnose.")
        finally:
            if root is not None:
                root.destroy()
PY
}

setup_environment() {
    local selected_python="" uv_binary="" offline=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python)
                [[ $# -ge 2 && "$2" == /* ]] || fail "--python requires an absolute executable path."
                selected_python="$2"; shift 2 ;;
            --offline) offline=1; shift ;;
            *) fail "Unknown setup argument: $1. Run ./dev.sh help." ;;
        esac
    done
    if [[ -z "$selected_python" ]]; then
        for candidate in /opt/homebrew/opt/python@3.14/bin/python3.14 /opt/homebrew/bin/python3.14; do
            if [[ -x "$candidate" ]]; then selected_python="$candidate"; break; fi
        done
    fi
    [[ -n "$selected_python" && -x "$selected_python" ]] || fail "Native Python not found. Run: /opt/homebrew/bin/brew install uv python@3.14 python-tk@3.14"
    if [[ -x /opt/homebrew/bin/uv ]]; then
        uv_binary=/opt/homebrew/bin/uv
    else
        uv_binary="$(command -v uv || true)"
    fi
    [[ -n "$uv_binary" ]] || fail "uv is missing. Run: /opt/homebrew/bin/brew install uv python@3.14 python-tk@3.14"
    runtime_check "$selected_python" base
    if [[ -e "$coinfly_venv" || -L "$coinfly_venv" ]]; then
        [[ -x "$coinfly_python" && ! -L "$coinfly_venv" ]] || fail "Existing .venv is unusable or a symlink. Preserve it and select a fresh project directory; it was not deleted."
        runtime_check "$coinfly_python" environment
        local selected_base existing_base
        selected_base="$("$selected_python" -I -c 'import pathlib, sys; print(pathlib.Path(sys._base_executable).resolve())')"
        existing_base="$("$coinfly_python" -I -c 'import pathlib, sys; print(pathlib.Path(sys._base_executable).resolve())')"
        [[ "$selected_base" == "$existing_base" ]] || fail "Existing .venv uses a different Python. Keep it with setup --python $coinfly_python, or preserve/move .venv before choosing another interpreter."
    fi
    local sync_options=(sync --project "$coinfly_root" --locked --python "$selected_python" --no-python-downloads --extra desktop --extra research --extra training --group dev)
    if [[ "$offline" == 1 ]]; then sync_options+=(--offline); fi
    printf 'Synchronizing locked dependencies in %s\n' "$coinfly_venv"
    # The setup destination is always this checkout, even from an activated venv.
    # Only this subprocess receives the explicit development environment path.
    if ! UV_PROJECT_ENVIRONMENT="$coinfly_venv" "$uv_binary" "${sync_options[@]}"; then
        fail "Dependency setup failed; uv.lock was kept strict. Fix the reported network/package error and rerun setup."
    fi
    runtime_check "$coinfly_python" installed
    printf 'Setup complete. Run ./dev.sh run, ./dev.sh test, or ./dev.sh locale.\n'
}

ensure_environment() {
    if [[ ! -e "$coinfly_venv" && ! -L "$coinfly_venv" ]]; then setup_environment; fi
    [[ -x "$coinfly_python" && ! -L "$coinfly_venv" ]] || fail "A usable local .venv is required. Run ./dev.sh setup."
    runtime_check "$coinfly_python" installed
}

validate_build_arguments() {
    local preflight_seen=0 distribution_seen=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --preflight)
                [[ "$preflight_seen" == 0 ]] || fail "--preflight may be supplied only once."
                preflight_seen=1; shift ;;
            --dataset-distribution)
                [[ "$distribution_seen" == 0 ]] || fail "--dataset-distribution may be supplied only once."
                [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "--dataset-distribution requires a directory."
                distribution_seen=1; shift 2 ;;
            *) fail "Build accepts only --preflight and --dataset-distribution DIR. Run ./dev.sh help." ;;
        esac
    done
}

build_release() {
    local mode="$1"
    shift
    # Never bootstrap/sync the development environment on the release path.
    if [[ -L "$coinfly_venv" ]]; then fail "Release interpreter .venv must not be a symlink."; fi
    if [[ ! -x "$coinfly_python" ]]; then
        coinfly_python=/opt/homebrew/opt/python@3.14/bin/python3.14
    fi
    [[ -x "$coinfly_python" ]] || fail "Native Python missing; prepare a compatible interpreter with ./dev.sh setup."
    exec "$coinfly_python" -I "$coinfly_root/scripts/build_macos.py" --mode "$mode" "$@"
}

command_name="${1:-run}"
if [[ $# -gt 0 ]]; then shift; fi
case "$command_name" in
    help|-h|--help) usage; exit 0 ;;
    run|prepare|test-ui|check|locale|tk|doctor|worker|clean)
        [[ $# -eq 0 ]] || fail "$command_name takes no arguments. Run ./dev.sh help." ;;
    build|build-unsigned) validate_build_arguments "$@" ;;
    setup|test|kernel) ;;
    stop-worker)
        if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --force ) ]]; then
            fail "stop-worker accepts only --force. Run ./dev.sh help."
        fi ;;
    *) fail "Unknown command: $command_name. Run ./dev.sh help." ;;
esac
[[ -f "$coinfly_root/pyproject.toml" && -f "$coinfly_root/uv.lock" ]] || fail "Put dev.sh in the Coinfly project root beside pyproject.toml and uv.lock."
if [[ "$command_name" == clean ]]; then clean_environment; exit 0; fi
require_host
cd -- "$coinfly_root"
if [[ "$command_name" == setup ]]; then setup_environment "$@"; exit 0; fi
case "$command_name" in
    build) build_release signed "$@" ;;
    build-unsigned) build_release unsigned "$@" ;;
esac
ensure_environment

case "$command_name" in
    run)
        runtime_check "$coinfly_python" window
        exec "$coinfly_python" -I -m coinfly.development desktop ;;
    prepare) exec "$coinfly_python" -I -m coinfly.development prepare ;;
    kernel) exec "$coinfly_python" -I "$coinfly_root/scripts/build_kernel.py" "$@" ;;
    test) exec "$coinfly_python" -I -m pytest -q "$@" ;;
    test-ui)
        runtime_check "$coinfly_python" window
        exec "$coinfly_python" -I -m pytest -q tests/ui/test_native_desktop.py --native-ui ;;
    check)
        /bin/bash -n "$coinfly_root/dev.sh"
        "$coinfly_python" -I -m ruff check src tests scripts
        "$coinfly_python" -I -m mypy src/coinfly
        exec "$coinfly_python" -I -m pytest -q ;;
    locale)
        exec "$coinfly_python" -I -m pytest -q tests/ui/test_context_balance_bootstrap.py::ContextPresentationTests::test_locale_money_is_explicit_and_labels_complete ;;
    tk) exec "$coinfly_python" -I -m tkinter ;;
    doctor) exec "$coinfly_python" -I -m coinfly doctor ;;
    worker) exec "$coinfly_python" -I -m coinfly.development worker ;;
    stop-worker) exec "$coinfly_python" -I -m coinfly.development stop-worker "$@" ;;
esac
