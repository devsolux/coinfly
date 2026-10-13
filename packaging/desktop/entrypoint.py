"""Separate frozen desktop/worker executables sharing one bundled runtime."""

import multiprocessing
import sys
from pathlib import Path

from coinfly import __application_id__, __version__
from coinfly.cli import main as desktop_main
from coinfly.cli import worker_main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if Path(sys.executable).stem == "coinfly-worker":
        if sys.argv[1:] == ["--version"]:
            print(f"coinfly-worker {__version__} ({__application_id__}.worker)")
            raise SystemExit(0)
        if sys.argv[1:]:
            raise SystemExit("coinfly-worker takes no arguments (except --version)")
        raise SystemExit(worker_main())
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-desktop":
        from verify_ui_startup import run_desktop_probe

        raise SystemExit(run_desktop_probe(Path(sys.argv[2])))
    if sys.argv[1:] == ["--smoke-doctor"]:
        from verify_doctor import run_storage_probe

        raise SystemExit(run_storage_probe())
    raise SystemExit(desktop_main())
