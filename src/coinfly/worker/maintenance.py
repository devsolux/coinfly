"""Single-flight reconciliation independent of strategy cadence and UI lifetime."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone


class MaintenanceLoop:
    def __init__(self, run: Callable[[datetime], object], failed: Callable[[str], None], *,
                 interval_seconds: float = 5.0, clock: Callable[[], datetime] | None = None) -> None:
        if not 0.05 <= interval_seconds <= 10:
            raise ValueError("bounded maintenance interval required")
        self._run, self._failed = run, failed
        self._interval, self._clock = interval_seconds, clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("retired maintenance cannot restart")
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._work, name="coinfly-account-maintenance", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._run(self._clock())
            except Exception as error:
                code = getattr(error, "code", "ACCOUNT_MAINTENANCE_FAILED")
                try:
                    self._failed(code if isinstance(code, str) else "ACCOUNT_MAINTENANCE_FAILED")
                except Exception:
                    # A failed status observer must not silently kill maintenance.
                    # Do not print exception text or credentials from callbacks.
                    logging.getLogger(__name__).error("ACCOUNT_MAINTENANCE_OBSERVER_FAILED")

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()
