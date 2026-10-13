"""Read-only release evidence inspection; no switch can manufacture certification."""

import importlib.metadata
import json
import platform
import sqlite3
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from coinfly import __application_id__, __project_name__, __version__
from coinfly.paths import ApplicationPaths, StorageInitializationError
from coinfly.security.release_gates import live_release_gates


@dataclass(frozen=True)
class QualificationCheck:
    code: str
    satisfied: bool
    detail: str
    scope: Literal["research", "desktop", "production"] = "production"
    failure_kind: Literal["environment", "system"] = "environment"


@dataclass(frozen=True)
class QualificationReport:
    project: str
    version: str
    identifier: str
    host: str
    checks: tuple[QualificationCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.satisfied for check in self.checks)

    def _scope_ready(self, scopes: set[str]) -> bool:
        checks = tuple(check for check in self.checks if check.scope in scopes)
        return bool(checks) and all(check.satisfied for check in checks)

    @property
    def local_research_ready(self) -> bool:
        """Deterministic core/tests only; never MaleCNS or execution authorization."""
        return self._scope_ready({"research"})

    @property
    def exit_code(self) -> int:
        if any(not check.satisfied and check.failure_kind == "system" for check in self.checks):
            return 1
        return 0 if self.ready else 2

    def public_dict(self) -> dict[str, object]:
        return {**asdict(self), "production_ready": self.ready,
                "local_research_ready": self.local_research_ready,
                "desktop_dependencies_ready": self._scope_ready({"research", "desktop"}),
                "blocking_codes": [check.code for check in self.checks if not check.satisfied],
                "system_error_codes": [check.code for check in self.checks
                                       if not check.satisfied and check.failure_kind == "system"],
                "environment_blocking_codes": [check.code for check in self.checks
                                               if not check.satisfied and check.failure_kind == "environment"],
                "status": {0: "ready", 1: "system_error", 2: "setup_required"}[self.exit_code],
                "exit_code": self.exit_code,
                "research_boundary": "Local synthetic research requires no production credentials. "
                    "Normal activation still requires verified MaleCNS assets; this report grants no execution authority."}


def _package_check(package: str, expected: str, code: str,
                   scope: Literal["research", "desktop"]) -> QualificationCheck:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return QualificationCheck(code, False, f"{package}: missing; requires {expected}", scope)
    except (OSError, ValueError):
        return QualificationCheck(code, False, f"{package}: metadata unreadable", scope)
    return QualificationCheck(code, version == expected, f"{package}: {version}; requires {expected}", scope)


def _tk_check() -> QualificationCheck:
    try:
        # Import the compiled binding without opening a GUI or requiring DISPLAY.
        binding = __import__("_tkinter")
    except (ImportError, OSError):
        return QualificationCheck("TK_RUNTIME", False, "Native Tcl/Tk binding unavailable", "desktop")
    return QualificationCheck("TK_RUNTIME", True, f"Tcl/Tk {binding.TK_VERSION}", "desktop")


def _storage_check(application: ApplicationPaths | None = None) -> QualificationCheck:
    try:
        (application or ApplicationPaths.for_current_user()).inspect()
    except StorageInitializationError as error:
        return QualificationCheck(error.code, False, error.detail, "desktop")
    return QualificationCheck("APPLICATION_STORAGE", True,
                              "Private application directories exist and access checks pass", "desktop")


def _publisher_check() -> QualificationCheck:
    from coinfly.bootstrap.distribution import load_distribution
    from coinfly.bootstrap.manifest import BootstrapError

    try:
        load_distribution()
    except BootstrapError as error:
        return QualificationCheck("SIGNED_DATASET_DISTRIBUTION", False, error.code)
    return QualificationCheck("SIGNED_DATASET_DISTRIBUTION", True,
                              "Packaged publisher root and signed manifest verified; data activation is separate")


def _development_checks() -> tuple[QualificationCheck, ...]:
    from coinfly.bootstrap.development_distribution import is_development_distribution
    from coinfly.bootstrap.manifest import BootstrapError

    try:
        development = is_development_distribution()
    except BootstrapError:
        return ()  # The publisher check retains the precise metadata failure code.
    if not development:
        return ()
    return (QualificationCheck("DEVELOPMENT_DATASET_DISTRIBUTION", True,
                               "Local source-lock descriptor verified; SANDBOX/PAPER only; data activation is separate",
                               "desktop"),)


def report_storage_failure(error: StorageInitializationError) -> QualificationReport:
    """Report even when file logging cannot start, without leaking OS exception text."""
    report = QualificationReport(__project_name__, __version__, __application_id__, sys.platform, (
        QualificationCheck(error.code, False, error.detail, "desktop"),
    ))
    print(json.dumps(report.public_dict(), indent=2), file=sys.stderr)
    return report


def _core_checks() -> tuple[QualificationCheck, ...]:
    """Local invariants only: no operator database, credentials or network."""
    from pydantic import ValidationError

    from coinfly.accounting.journal import JournalEntry
    from coinfly.domain.decimal_math import financial_context

    amount = "0.123456789012345678"
    exact = JournalEntry.model_validate_json(
        '{"account":"doctor","asset":"USD","amount":"' + amount + '"}'
    ).amount == Decimal(amount)
    try:
        JournalEntry.model_validate_json('{"account":"doctor","asset":"USD","amount":' + amount + '}')
    except ValidationError:
        pass
    else:
        exact = False
    exact = exact and financial_context().multiply(Decimal(amount), Decimal(10)) == Decimal("1.234567890123456780")
    schema_ok = False
    try:
        schema = Path(__file__).parents[1] / "storage" / "schema.sql"
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(schema.read_text(encoding="utf-8"))
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schema_ok = {"ex_intents", "ex_fills", "ex_entries", "ex_reservations"} <= tables
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        pass
    return (
        QualificationCheck("EXACT_ACCOUNTING_CONTRACT", exact, "Exact monetary wire/arithmetic invariants",
                           "research", "system"),
        QualificationCheck("PACKAGED_LEDGER_SCHEMA", schema_ok, "Packaged ledger initializes in isolated memory",
                           "research", "system"),
    )


def inspect_release(*, application: ApplicationPaths | None = None) -> QualificationReport:
    try:
        return _inspect_release(application=application)
    except Exception:
        # An unexpected implementation failure must not masquerade as an
        # expected missing prerequisite. Exception text may contain private data.
        return QualificationReport(__project_name__, __version__, __application_id__, sys.platform, (
            QualificationCheck("DOCTOR_INTERNAL_ERROR", False, "Unexpected diagnostic failure",
                               "research", "system"),
        ))


def _inspect_release(*, application: ApplicationPaths | None = None) -> QualificationReport:
    system, machine = platform.system(), platform.machine().lower()
    native = (system == "Darwin" and machine in {"arm64", "aarch64"}
              or system in {"Windows", "Linux"} and machine in {"amd64", "x86_64"})
    checks = [
        QualificationCheck("PYTHON_RUNTIME", sys.version_info >= (3, 11), platform.python_version(), "research"),
        QualificationCheck("SQLITE_RUNTIME", sqlite3.sqlite_version_info >= (3, 51, 3),
                           sqlite3.sqlite_version, "research"),
        _package_check("pydantic", "2.13.5", "PYDANTIC_PIN", "research"),
        _package_check("cryptography", "50.0.1", "CRYPTOGRAPHY_PIN", "research"),
        _package_check("certifi", "2026.7.22", "CERTIFI_PIN", "research"),
        _package_check("numpy", "2.4.6", "NUMPY_PIN", "research"),
        _package_check("pandas", "3.0.5", "PANDAS_PIN", "research"),
        _package_check("pyarrow", "25.0.1", "PYARROW_PIN", "research"),
        _package_check("Pillow", "12.3.0", "PILLOW_PIN", "research"),
        _package_check("customtkinter", "6.0.0", "CUSTOMTKINTER_PIN", "desktop"),
        _package_check("keyring", "25.7.0", "KEYRING_PIN", "desktop"),
        _tk_check(),
        _storage_check(application),
        QualificationCheck("NATIVE_TARGET", native, f"{system}/{machine}"),
        _publisher_check(),
        QualificationCheck("NATIVE_RELEASE_QUALIFICATION", False,
                           "Signed/notarized target packages, vault/IPC integration, DPI and performance evidence required"),
        QualificationCheck("PRODUCTION_TELEMETRY_QUALIFICATION", False,
                           "Qualified production latency, loss, crash and reconciliation telemetry evidence required"),
        QualificationCheck("EXTERNAL_HOST_FENCING", False,
                           "Live takeover needs evidence that other execution hosts cannot mutate this authority"),
        QualificationCheck("STRATEGY_EVALUATION", False,
                           "Chronological replay/shadow evaluation and authorized staged canaries required"),
    ]
    checks.extend(QualificationCheck(gate.code, gate.satisfied, gate.evidence_required)
                  for gate in live_release_gates())
    checks.extend(_development_checks())
    # Do not label an unavailable prerequisite as a broken invariant in code
    # that cannot be meaningfully inspected on that runtime.
    if all(check.satisfied for check in checks if check.code in {"PYDANTIC_PIN", "SQLITE_RUNTIME"}):
        checks.extend(_core_checks())
    return QualificationReport(__project_name__, __version__, __application_id__,
                               f"{system}/{machine}", tuple(checks))
