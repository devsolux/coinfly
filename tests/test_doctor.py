import builtins
import importlib.metadata
import json
from dataclasses import replace

import pytest

from coinfly.cli import main
from coinfly.operations.qualification import QualificationCheck, inspect_release


def test_doctor_distinguishes_local_research_from_production(capsys):
    assert main(["doctor"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["local_research_ready"]
    assert not report["production_ready"]
    assert report["exit_code"] == 2
    assert report["status"] == "setup_required"
    assert report["system_error_codes"] == []
    assert "PRODUCTION_TELEMETRY_QUALIFICATION" in report["blocking_codes"]
    assert "SIGNED_DATASET_DISTRIBUTION" in report["blocking_codes"]
    assert "NATIVE_SIGNING_UNCERTIFIED" in report["blocking_codes"]


@pytest.mark.parametrize("failure", ["missing", "wrong_pin", "unreadable"])
def test_doctor_reports_dependency_failures_without_crashing(monkeypatch, failure):
    original = importlib.metadata.version
    def version(package):
        if package != "numpy":
            return original(package)
        if failure == "missing":
            raise importlib.metadata.PackageNotFoundError(package)
        if failure == "unreadable":
            raise OSError("private filesystem details must not be disclosed")
        return "0.0.0"
    monkeypatch.setattr(importlib.metadata, "version", version)
    report = inspect_release()
    assert not report.local_research_ready
    assert report.exit_code == 2
    assert "private filesystem" not in json.dumps(report.public_dict())
    assert "NUMPY_PIN" in report.public_dict()["blocking_codes"]


def test_zero_exit_requires_all_checks_and_empty_evidence_is_not_ready(monkeypatch, capsys):
    # Pure diagnostic fixtures do not modify any runtime release trust anchors.
    report = replace(inspect_release(), checks=(QualificationCheck("SYNTHETIC", True, "unit fixture"),))
    monkeypatch.setattr("coinfly.operations.qualification.inspect_release", lambda: report)
    assert main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["production_ready"]
    assert replace(report, checks=()).exit_code == 2


def test_doctor_has_no_financial_or_verification_bypass_flags():
    for option in ("--live", "--synthetic", "--skip-verification", "--balance=100"):
        with pytest.raises(SystemExit) as result:
            main(["doctor", option])
        assert result.value.code == 2


def test_missing_tk_blocks_desktop_without_disabling_local_research(monkeypatch):
    original = builtins.__import__
    def import_module(name, *args, **kwargs):
        if name == "_tkinter":
            raise ImportError("synthetic unavailable native binding")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_module)
    report = inspect_release().public_dict()
    assert report["local_research_ready"]
    assert not report["desktop_dependencies_ready"]
    assert "TK_RUNTIME" in report["blocking_codes"]


def test_doctor_system_error_takes_precedence_over_missing_release_evidence(monkeypatch, capsys):
    report = inspect_release()
    broken = QualificationCheck("BROKEN_INVARIANT", False, "synthetic failure", "research", "system")
    monkeypatch.setattr("coinfly.operations.qualification.inspect_release",
                        lambda: replace(report, checks=(*report.checks, broken)))
    assert main(["doctor"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "system_error"
    assert data["system_error_codes"] == ["BROKEN_INVARIANT"]
    assert "SIGNED_DATASET_DISTRIBUTION" in data["environment_blocking_codes"]


def test_unexpected_diagnostic_bug_is_redacted_and_exits_one(monkeypatch, capsys):
    def broken():
        raise RuntimeError("PRIVATE_CANARY account/key details")
    monkeypatch.setattr("coinfly.operations.qualification._core_checks", broken)
    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE_CANARY" not in output
    assert json.loads(output)["system_error_codes"] == ["DOCTOR_INTERNAL_ERROR"]


def test_broken_packaged_schema_is_a_system_error(monkeypatch):
    from pathlib import Path

    original = Path.read_text
    def read_text(path, *args, **kwargs):
        # The fresh 1.0.0 format ships one packaged ledger script (storage/schema.sql).
        if path.name == "schema.sql" and path.parent.name == "storage":
            return "INVALID SQL;"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    report = inspect_release()
    assert report.exit_code == 1
    assert report.public_dict()["system_error_codes"] == ["PACKAGED_LEDGER_SCHEMA"]
