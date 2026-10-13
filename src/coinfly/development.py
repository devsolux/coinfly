"""Source-checkout desktop composition with isolated local paper state."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coinfly.bootstrap.development import DevelopmentBootstrapManager
from coinfly.bootstrap.manifest import BootstrapError
from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError, Environment, RunProfile
from coinfly.ipc.local import LocalRPCClient, default_socket_path
from coinfly.ipc.protocol import RPCError
from coinfly.paths import (
    ApplicationPaths,
    StorageInitializationError,
    initialize_application_storage,
    private_directory,
)
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.startup import WorkerStartup
from coinfly.worker.supervisor import WorkerSupervisor


def checkout_root() -> Path:
    if getattr(sys, "frozen", False):
        from coinfly.bootstrap.development_distribution import is_development_distribution

        if not is_development_distribution():
            raise DomainError("DEVELOPMENT_DISTRIBUTION_REQUIRED")
        # Desktop and worker share Contents/MacOS, independent of the launch cwd.
        return Path(sys.executable).resolve().parent
    root = Path(__file__).resolve().parents[2]
    if not (root / "dev.sh").is_file() or not (root / "pyproject.toml").is_file():
        raise DomainError("DEVELOPMENT_CHECKOUT_REQUIRED")
    return root


def development_root(checkout: Path) -> Path:
    paths = ApplicationPaths.for_current_user()
    paths.prepare()
    identity = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16]
    private_directory(paths.storage / "development")
    root = paths.storage / "development" / identity
    private_directory(root)
    return root


def development_socket(checkout: Path) -> Path:
    identity = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16]
    if sys.platform == "win32":
        import os

        from coinfly.ipc.windows_native import pipe_path, process_sid
        return Path(pipe_path(process_sid(os.getpid()), checkout=identity))
    return default_socket_path().with_name(f"development-{identity}.sock")


# How long a launcher waits for a stale worker to retire (automation stopped,
# paper book reconciled, ledger closed) before leaving it running and reporting.
STALE_WORKER_RETIREMENT_SECONDS = 45.0


def probe_worker(socket_path: Path, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """Handshake with the worker on ``socket_path``; None when no worker answers.

    The result carries ``build_match`` (the worker runs the same sources as this
    process) and the worker's build identity. A worker that answers but refuses
    the handshake (another protocol/application) is reported as a mismatch.
    """
    from coinfly.observability.diagnostics import build_identity

    if not socket_path.exists():
        return None
    try:
        with LocalRPCClient(socket_path, timeout=timeout, identity=dict(build_identity())) as client:
            handshake = dict(client.handshake or {})
    except RPCError as error:
        if error.code in ("PROTOCOL_VERSION_MISMATCH", "APPLICATION_MISMATCH"):
            return {"build_match": False, "refused": error.code, "build": {}}
        return None if error.code == "RESPONSE_UNAVAILABLE" else {"build_match": False, "refused": error.code, "build": {}}
    except (OSError, DomainError, RuntimeError):
        return None
    handshake.setdefault("build_match", False)
    return handshake


def _wait_until_absent(socket_path: Path, timeout: float) -> bool:
    """Wait until no worker answers; then give the leaving process a moment to remove its endpoint."""
    deadline = time.monotonic() + timeout
    while probe_worker(socket_path, timeout=1.0) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    # The listener closes before the endpoint file is removed; a successor that
    # binds in between would race the old process's cleanup. A stale file left
    # by a crashed process is replaced by the successor's own stale check.
    settle = time.monotonic() + 3.0
    while socket_path.exists() and time.monotonic() < settle:
        time.sleep(0.1)
    return True


def retire_worker(socket_path: Path, *, force: bool = False, only_if_stale: bool = True,
                  timeout: float = STALE_WORKER_RETIREMENT_SECONDS) -> str:
    """Ask the running development worker to retire; returns what happened.

    ``absent``   — nothing is running;
    ``current``  — it runs this checkout's sources (left alone when ``only_if_stale``);
    ``retired``  — it acknowledged and its endpoint is gone;
    ``waiting``  — it is retiring but still manages paper positions or orders
                   (graceful retirement drains them first; ``force`` stops it now).

    A worker keeps running after the desktop closes (it owns the paper book), so
    after the checkout changes the next desktop would otherwise attach to a
    worker running the previous code: every command is then refused with
    WORKER_BUILD_MISMATCH and ``dev.sh worker`` cannot bind the endpoint.
    """
    handshake = probe_worker(socket_path)
    if handshake is None:
        return "absent"
    if only_if_stale and handshake.get("build_match"):
        return "current"
    try:
        # worker.shutdown is handshake-exempt: a stale worker accepts it from a newer client.
        with LocalRPCClient(socket_path, timeout=5.0) as client:
            client.call("worker.shutdown", {"force": True} if force else {})
    except (OSError, DomainError, RPCError, RuntimeError):
        pass  # it may already be closing; the endpoint check below decides
    return "retired" if _wait_until_absent(socket_path, timeout) else "waiting"


class DevelopmentWorker(WorkerSupervisor):
    """Reject non-paper contexts at the worker boundary, independently of the UI."""

    def _validate_candidate(self, config: EngineConfig) -> None:
        if config.environment != Environment.SANDBOX or config.run_profile != RunProfile.PAPER:
            raise DomainError("DEVELOPMENT_PAPER_ONLY")
        super()._validate_candidate(config)


def create_worker(checkout: Path, **kwargs: Any) -> DevelopmentWorker:
    root = development_root(checkout)
    application = ApplicationPaths.for_current_user()
    return DevelopmentWorker(paths=WorkerPaths(root, application),
                             socket_path=development_socket(checkout),
                             bootstrap=DevelopmentBootstrapManager(application=application), **kwargs)


def run_worker(checkout: Path) -> int:
    from coinfly.observability.diagnostics import configure_worker_log

    socket_path = development_socket(checkout)
    outcome = retire_worker(socket_path)
    if outcome == "current":
        print("coinfly development: a worker for this checkout is already running with the current sources.", flush=True)
        return 0
    if outcome == "waiting":
        print("coinfly development: the running worker was started from older sources and is retiring; it still "
              "manages paper positions or orders. Wait for its drain, or stop it now with ./dev.sh stop-worker --force.",
              file=sys.stderr, flush=True)
        return 2
    if outcome == "retired":
        print("coinfly development: retired a worker that was running older sources.", flush=True)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    # Bounded, redacted diagnostics: stages, stable codes and exception chains.
    configure_worker_log(ApplicationPaths.for_current_user().logs, "development")
    startup = WorkerStartup(socket_path=socket_path,
                            factory=lambda **kwargs: create_worker(checkout, **kwargs))
    try:
        startup.start()
        while not stopped.is_set() and not startup.shutdown_requested.is_set():
            stopped.wait(0.5)
        return 0
    finally:
        startup.close()


def run_desktop(checkout: Path) -> int:
    from coinfly.ui.app import run_app

    socket_path = development_socket(checkout)

    def ensure_worker() -> None:
        # A worker from before the last source change is retired first (it would
        # refuse every command with WORKER_BUILD_MISMATCH); one that still
        # drains paper positions is left running and shown as stale.
        outcome = retire_worker(socket_path)
        if outcome == "current":
            return
        if outcome == "waiting":
            print("Local development: the running worker uses older sources and is still draining paper positions; "
                  "stop it with ./dev.sh stop-worker --force to replace it now.", file=sys.stderr, flush=True)
            return
        if getattr(sys, "frozen", False):
            from coinfly.cli import _worker_command

            command = _worker_command()
        else:
            command = [sys.executable, "-I", "-m", "coinfly.development", "worker"]
        subprocess.Popen(command,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)

    from coinfly.cli import desktop_credential_services

    # Same OS-keychain setup store and read-only Coinbase verifier as the signed
    # desktop. The development worker still rejects every non-paper context.
    store, verifier = desktop_credential_services()
    print("Local development: source-lock verification; isolated paper worker; no publisher certification.", flush=True)
    return run_app(socket_path=socket_path, worker_launcher=ensure_worker, credential_store=store,
                   connection_verifier=verifier)


async def prepare_dataset() -> int:
    paths = ApplicationPaths.for_current_user()
    paths.prepare()
    manager = DevelopmentBootstrapManager(application=paths)
    updates = manager.subscribe()

    async def report() -> None:
        previous, last = None, 0.0
        while True:
            progress = await updates.get()
            stage = (progress.state, progress.asset)
            if stage != previous or time.monotonic() - last >= 2:
                print(f"{progress.state}: {progress.asset or 'MaleCNS'} "
                      f"{progress.bytes_completed}/{progress.bytes_total}", flush=True)
                previous, last = stage, time.monotonic()

    reporter = asyncio.create_task(report())
    try:
        result = await manager.check()
        print(f"{result.state}: {result.reason or result.verification}", flush=True)
        return 0 if result.ready else 2
    finally:
        manager.unsubscribe(updates)
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)


def stop_worker(checkout: Path, *, force: bool) -> int:
    """``dev.sh stop-worker``: retire this checkout's development worker, whatever its sources."""
    socket_path = development_socket(checkout)
    outcome = retire_worker(socket_path, force=force, only_if_stale=False)
    messages = {
        "absent": "No development worker is running for this checkout.",
        "retired": "The development worker stopped." if not force else "The development worker was stopped (forced); "
                   "the next start recovers its ledger, fences and open obligations.",
        "waiting": "The worker is retiring gracefully: automation is stopped and it exits once its paper positions and "
                   "orders are closed. Use ./dev.sh stop-worker --force to stop it now.",
    }
    print(f"coinfly development: {messages[outcome]}", flush=True)
    return 0 if outcome in ("absent", "retired") else 2


def main(argv: Sequence[str] | None = None) -> int:
    from coinfly.observability.diagnostics import build_identity
    build_identity()  # Capture before any worker/UI threads or later source changes.
    parser = argparse.ArgumentParser(description="Isolated Coinfly source development")
    parser.add_argument("command", choices=("desktop", "worker", "prepare", "stop-worker"))
    parser.add_argument("--force", action="store_true", help="stop-worker: stop now even while paper positions are open")
    arguments = parser.parse_args(argv)
    if arguments.force and arguments.command != "stop-worker":
        parser.error("--force applies to stop-worker only")
    try:
        root = checkout_root()
        initialize_application_storage()
        if arguments.command == "desktop":
            return run_desktop(root)
        if arguments.command == "worker":
            return run_worker(root)
        if arguments.command == "stop-worker":
            return stop_worker(root, force=arguments.force)
        return asyncio.run(prepare_dataset())
    except StorageInitializationError as error:
        from coinfly.operations.qualification import report_storage_failure

        return report_storage_failure(error).exit_code
    except (DomainError, RPCError, BootstrapError) as error:
        print(f"coinfly development: {error.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
