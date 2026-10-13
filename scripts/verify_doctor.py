"""CI check for source-release diagnostics; never changes runtime certification."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run_storage_probe() -> int:
    """Frozen release probe: verify fresh storage without operator state."""
    from coinfly.paths import ApplicationPaths, StorageInitializationError

    with tempfile.TemporaryDirectory(prefix="coinfly-doctor-") as directory:
        application = ApplicationPaths.for_home(Path(directory))
        try:
            application.prepare()
        except StorageInitializationError as error:
            from coinfly.operations.qualification import report_storage_failure

            return report_storage_failure(error).exit_code
        from coinfly.operations.qualification import inspect_release

        report = inspect_release(application=application)
        print(json.dumps(report.public_dict()))
        return report.exit_code


def main() -> None:
    result = subprocess.run([sys.executable, "-m", "coinfly", "doctor"],
                            capture_output=True, text=True, check=False, timeout=30)
    report = json.loads(result.stdout)
    expected = {"SIGNED_DATASET_DISTRIBUTION", "NATIVE_RELEASE_QUALIFICATION",
                "PRODUCTION_TELEMETRY_QUALIFICATION", "NATIVE_SIGNING_UNCERTIFIED",
                "REGION_UNVERIFIED", "LIVE_GATEWAY_UNCERTIFIED"}
    if (result.returncode != 2 or report.get("exit_code") != 2
            or report.get("production_ready") is not False
            or report.get("local_research_ready") is not True
            or not expected.issubset(report.get("blocking_codes", []))):
        raise SystemExit("Source-release doctor contract failed; inspect coinfly doctor")
    print("Doctor verified: local research dependencies ready; production blocked; exit 2")


if __name__ == "__main__":
    main()
