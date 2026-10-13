"""Development bundle routing is sealed, paper-only, and not publisher trust."""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from coinfly.bootstrap.development_distribution import development_documents, is_development_distribution
from coinfly.bootstrap.distribution import load_distribution
from coinfly.bootstrap.manifest import BootstrapError


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    directory = tmp_path / "bundle/coinfly/bootstrap/publisher"
    directory.mkdir(parents=True)
    for name, document in development_documents().items():
        (directory / name).write_text(json.dumps(document))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr("coinfly.paths.native_user_home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return directory


def test_development_metadata_qualifies_only_its_own_profile(packaged):
    from coinfly.operations.qualification import _development_checks, _publisher_check

    assert is_development_distribution()
    assert _development_checks()[0].satisfied
    assert not _publisher_check().satisfied
    assert _publisher_check().detail == "DEVELOPMENT_DISTRIBUTION_ONLY"
    with pytest.raises(BootstrapError, match="DEVELOPMENT_DISTRIBUTION_ONLY"):
        load_distribution()


@pytest.mark.parametrize("field,value", [
    ("is_development_build", False), ("is_development_build", 1), ("run_profile", "STANDARD"),
    ("environment", "LIVE"), ("root_sha256", "0" * 64), ("dataset_files", []),
    ("application_version", "0.0.0"), ("unexpected", True),
])
def test_modified_development_contract_is_rejected(packaged, field, value):
    path = packaged / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(BootstrapError, match="DEVELOPMENT_DISTRIBUTION_INVALID"):
        is_development_distribution()


def test_changed_locks_and_incomplete_pair_are_rejected(packaged):
    root = packaged / "trust-root.json"
    document = json.loads(root.read_bytes())
    document["source_locks_sha256"] = "0" * 64
    root.write_text(json.dumps(document))
    with pytest.raises(BootstrapError, match="DEVELOPMENT_DISTRIBUTION_INVALID"):
        is_development_distribution()
    (packaged / "manifest.json").unlink()
    with pytest.raises(BootstrapError, match="SIGNED_MANIFEST_MISSING"):
        is_development_distribution()


@pytest.mark.parametrize("entry", ["desktop", "worker"])
def test_both_frozen_entrypoints_select_development_composition(packaged, monkeypatch, entry):
    from coinfly import cli, development

    selected = Mock(return_value=0)
    monkeypatch.setattr(development, "main", selected)
    production = Mock(side_effect=AssertionError("Selected production composition"))
    monkeypatch.setattr(cli, "_launch_native_desktop", production)
    monkeypatch.setattr("coinfly.worker.supervisor.main", production)
    result = cli.main(["desktop"]) if entry == "desktop" else cli.worker_main()
    assert result == 0
    selected.assert_called_once_with([entry])
    production.assert_not_called()


def test_frozen_development_uses_bundled_worker_without_checkout(packaged, tmp_path, monkeypatch):
    from coinfly import development

    desktop = tmp_path / "coinfly.app/Contents/MacOS/coinfly"
    desktop.parent.mkdir(parents=True)
    worker = desktop.with_name("coinfly-worker")
    worker.touch()
    monkeypatch.setattr(sys, "executable", str(desktop))
    assert development.checkout_root() == desktop.parent
    monkeypatch.setattr(development, "LocalRPCClient", Mock(side_effect=OSError))
    spawn = Mock()
    monkeypatch.setattr(development.subprocess, "Popen", spawn)

    def run_app(**kwargs):
        kwargs["worker_launcher"]()
        assert kwargs["socket_path"] == development.development_socket(desktop.parent)
        # The development desktop injects the same keychain store and read-only
        # verifier as the signed desktop; without them credentials cannot be saved.
        assert callable(kwargs["credential_store"].save) and callable(kwargs["credential_store"].status)
        assert callable(kwargs["connection_verifier"].verify_connection)
        return 0

    monkeypatch.setattr("coinfly.ui.app.run_app", run_app)
    assert development.run_desktop(development.checkout_root()) == 0
    assert spawn.call_args.args[0] == [str(worker)]
    monkeypatch.setattr(sys, "executable", str(worker))
    assert development.checkout_root() == desktop.parent


def test_runtime_ignores_external_development_descriptor(packaged, tmp_path, monkeypatch):
    external = tmp_path / "home/.devsolux/coinfly"
    external.mkdir(parents=True)
    for path in packaged.iterdir():
        (external / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty-bundle"))
    assert not is_development_distribution()


def test_documented_templates_match_runtime_contract():
    templates = Path(__file__).resolve().parents[2] / "packaging/development"
    for name, document in development_documents().items():
        assert json.loads((templates / name).read_bytes()) == document
