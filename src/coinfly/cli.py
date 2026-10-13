"""Desktop launcher and nonsensitive diagnostics; no financial CLI overrides."""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coinfly import __application_id__, __version__
from coinfly.bootstrap.manifest import BootstrapError
from coinfly.desktop_errors import DesktopDependencyError
from coinfly.domain.context import DomainError
from coinfly.paths import StorageInitializationError, initialize_application_storage


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        worker = Path(sys.executable).parent / ("coinfly-worker.exe" if sys.platform == "win32" else "coinfly-worker")
        if not worker.is_file():
            raise DomainError("PACKAGED_WORKER_MISSING")
        return [str(worker)]
    return [sys.executable, "-I", "-m", "coinfly.worker"]


def _launch_desktop() -> int:
    if getattr(sys, "frozen", False):
        from coinfly.bootstrap.development_distribution import is_development_distribution

        if is_development_distribution():
            from coinfly.development import main as development_main

            return development_main(["desktop"])
    return _launch_native_desktop()


def desktop_credential_services() -> tuple[Any, Any]:
    """Compose the OS-keychain setup store and the read-only Coinbase verifier.

    Both the signed-release desktop and the local development desktop inject
    exactly this pair; the UI imports neither implementation. Storing a pair or
    verifying authentication never touches the worker, never creates a signing
    capability and never satisfies a live gate, so the paper-only development
    worker keeps rejecting non-paper contexts regardless of what is stored.
    """
    from coinfly.security.desktop_credentials import DesktopCredentialStore
    from coinfly.venues.coinbase.connection_test import CoinbaseConnectionTester

    store = DesktopCredentialStore()
    return store, CoinbaseConnectionTester(store)


def _launch_native_desktop() -> int:
    from coinfly.ipc.local import LocalRPCClient, default_socket_path
    from coinfly.ui.app import load_toolkit, run_app

    load_toolkit()
    socket_path = default_socket_path()

    def ensure_worker() -> None:
        try:
            with LocalRPCClient(socket_path, timeout=1) as client:
                client.call("state.snapshot")
            return
        except (OSError, DomainError, RuntimeError):
            pass
        # No secrets, endpoints, funding, or mode flags are passed to the child.
        subprocess.Popen(_worker_command(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    store, verifier = desktop_credential_services()
    return run_app(socket_path=socket_path, worker_launcher=ensure_worker, credential_store=store,
                   connection_verifier=verifier)


def main(argv: Sequence[str] | None = None) -> int:
    from coinfly.observability.diagnostics import build_identity
    build_identity()  # Capture before any worker/UI threads or later source changes.
    parser = argparse.ArgumentParser(prog="coinfly", description="Coinfly desktop and release diagnostics")
    parser.add_argument("--version", action="version", version=f"coinfly {__version__} ({__application_id__})")
    parser.add_argument("command", nargs="?", choices=("desktop", "doctor"), default="desktop")
    arguments = parser.parse_args(argv)
    if arguments.command == "doctor":
        from coinfly.operations.qualification import inspect_release

        report = inspect_release()
        print(json.dumps(report.public_dict(), indent=2))
        return report.exit_code
    try:
        initialize_application_storage()
        return _launch_desktop()
    except StorageInitializationError as error:
        from coinfly.operations.qualification import report_storage_failure

        return report_storage_failure(error).exit_code
    except (ImportError, DomainError, BootstrapError, DesktopDependencyError) as exc:
        code = exc.code if isinstance(exc, (DomainError, BootstrapError)) else "DESKTOP_DEPENDENCY_MISSING"
        print(f"coinfly: {code}. Run coinfly doctor for readiness details.", file=sys.stderr)
        return 2


def worker_main() -> int:
    """Prepare storage before importing worker/database implementation modules."""
    from coinfly.observability.diagnostics import build_identity
    build_identity()
    try:
        initialize_application_storage()
        if getattr(sys, "frozen", False):
            from coinfly.bootstrap.development_distribution import is_development_distribution

            if is_development_distribution():
                from coinfly.development import main as development_main

                return development_main(["worker"])
    except StorageInitializationError as error:
        from coinfly.operations.qualification import report_storage_failure

        return report_storage_failure(error).exit_code
    except BootstrapError as error:
        print(f"coinfly worker: {error.code}", file=sys.stderr)
        return 2
    try:
        from coinfly.worker.supervisor import main as run_worker
    except ImportError:
        # Match the desktop entry point: a missing dependency is a clean exit 2,
        # never a traceback that prints installation paths.
        print("coinfly worker: DESKTOP_DEPENDENCY_MISSING. Run coinfly doctor for readiness details.", file=sys.stderr)
        return 2

    return run_worker()


if __name__ == "__main__":
    raise SystemExit(main())
