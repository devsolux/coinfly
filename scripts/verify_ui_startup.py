"""Verify a frozen Coinfly desktop using a real window and an external watchdog."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run_desktop_probe(report_path: Path) -> int:
    """Called inside the frozen app; never start or attach to an operator worker."""
    from coinfly.ui.app import CoinflyApp, load_toolkit

    with tempfile.TemporaryDirectory(prefix="coinfly-ui-") as directory:
        app = CoinflyApp(load_toolkit(), socket_path=Path(directory) / "worker.sock")
        errors: list[BaseException] = []
        rendered = False
        completed = False
        ticks = 0

        def callback_failed(_kind, error, _traceback):
            errors.append(error)
            app.root.quit()

        def check_main_window() -> None:
            app.root.update_idletasks()
            if not app.root.winfo_viewable() or not app.shell.winfo_viewable():
                raise RuntimeError("Main desktop window is not visible.")
            if app.current_view != "settings" or not app.settings.frame.winfo_viewable():
                raise RuntimeError("Main desktop did not reach Settings.")
            if app._bootstrap is None or app._bootstrap.window.state() != "withdrawn":
                raise RuntimeError("Dataset setup still covers the main desktop.")

        def heartbeat() -> None:
            nonlocal ticks
            ticks += 1
            app.root.after(100, heartbeat)

        def finish() -> None:
            nonlocal completed
            check_main_window()
            if ticks < 3:
                raise RuntimeError("Desktop event loop did not advance.")
            completed = True
            app.root.quit()

        def check_window() -> None:
            nonlocal rendered
            for language in ("en", "de", "en"):
                app.set_language(language)
                for view in app.views:
                    app.show_view(view)
                    app.root.update_idletasks()
            # Use the normal startup-dialog action; never manufacture data READY.
            if app._bootstrap is None:
                raise RuntimeError("Dataset setup did not initialize.")
            app._bootstrap.open_settings()
            check_main_window()
            rendered = True
            heartbeat()
            app.root.after(3000, finish)

        # Tk normally prints callback errors and continues. They must fail CI.
        app.root.report_callback_exception = callback_failed
        try:
            app.root.after(500, check_window)
            app.run()
            if errors:
                raise RuntimeError("Bundled desktop callback failed.") from errors[0]
            if not rendered or not completed:
                raise RuntimeError("Desktop closed before verification completed.")
        finally:
            if not app._closing:
                app.close()
    # Write proof only after verification AND clean window shutdown succeeded.
    report_path.write_text(json.dumps({"main_window_ready": True, "event_loop_ticks": ticks}),
                           encoding="utf-8")
    return 0


def verify_bundle(target: Path, *, timeout: float = 60) -> None:
    executable = target.resolve()
    if executable.is_dir() and executable.suffix == ".app":
        executable /= "Contents/MacOS/coinfly"
    if not executable.is_file():
        raise RuntimeError(f"Packaged executable not found: {executable}")
    with tempfile.TemporaryDirectory(prefix="coinfly-startup-") as directory:
        report_path = Path(directory) / "result.json"
        try:
            result = subprocess.run(
                [str(executable), "--smoke-desktop", str(report_path)],
                cwd=directory, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            # subprocess.run kills and reaps the stalled child on timeout.
            raise RuntimeError(f"Desktop startup exceeded {timeout:g}s; process killed.") from error
        if result.returncode != 0:
            raise RuntimeError(f"Desktop exited {result.returncode}:\n{result.stderr or result.stdout}")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Desktop exited without a valid window verification report.") from error
        if (not isinstance(report, dict) or report.get("main_window_ready") is not True
                or type(report.get("event_loop_ticks")) is not int or report["event_loop_ticks"] < 3):
            raise RuntimeError("Desktop did not verify a visible, responsive main window.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Path to coinfly.app or the frozen desktop executable")
    args = parser.parse_args()
    try:
        verify_bundle(args.bundle)
    except (OSError, RuntimeError) as error:
        print(f"coinfly UI verification: {error}", file=sys.stderr)
        return 1
    print("Bundled desktop verified: visible main window, navigation, translations and 3s event loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
