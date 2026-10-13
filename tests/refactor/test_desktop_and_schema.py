"""Portable installation contracts; native Windows tests run only on Windows."""
import importlib.util
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from coinfly.domain.context import DomainError
from coinfly.ipc.protocol import RPCError
from coinfly.ipc.windows_native import pipe_path, principal_id, validate_path
from coinfly.observability.diagnostics import build_identity, runtime_identity
from coinfly.storage.component_schema import (
    install_component_schema,
    require_schema_objects,
    script_statements,
)
from coinfly.storage.writer import MINIMUM_SQLITE, Writer

ROOT = Path(__file__).parents[2]


def launcher():
    spec = importlib.util.spec_from_file_location("coinfly_development_launcher", ROOT / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("os_name,machine,name", [
    ("darwin","arm64","macos-arm64"), ("win32","AMD64","windows-amd64"),
    ("linux","x86_64","linux-amd64"), ("linux","amd64","linux-amd64"),
])
def test_portable_launcher_has_exact_native_target_contract(os_name, machine, name):
    assert launcher().target(os_name,machine) == name


@pytest.mark.parametrize("os_name,machine", [("darwin","x86_64"),("win32","arm64"),("linux","aarch64")])
def test_unsupported_interpreter_architecture_never_uses_emulated_fallback(os_name,machine):
    with pytest.raises(ValueError):
        launcher().target(os_name,machine)


def test_runtime_sqlite_minimum_remains_enforced_without_file_mutation(tmp_path):
    if sqlite3.sqlite_version_info >= MINIMUM_SQLITE:
        pytest.skip("below-minimum runtime test; native suite tests supported Writer")
    path = tmp_path / "not-created.sqlite3"
    with pytest.raises(DomainError) as caught:
        Writer(path)
    assert caught.value.code == "SQLITE_VERSION_UNSUPPORTED"
    assert not path.exists()


def test_component_marker_cannot_hide_removed_column():
    with sqlite3.connect(":memory:") as db:
        ddl = ("CREATE TABLE IF NOT EXISTS sample (id TEXT PRIMARY KEY, value TEXT)",)
        install_component_schema(db,"sample",ddl)
        db.execute("ALTER TABLE sample DROP COLUMN value")
        with pytest.raises(DomainError,match="SCHEMA_STRUCTURE_MISMATCH"):
            install_component_schema(db,"sample",ddl)


def test_root_contract_includes_indexes_and_trigger_bodies():
    script = (ROOT / "src/coinfly/storage/schema.sql").read_text()
    statements = script_statements(script)
    db = sqlite3.connect(":memory:")
    try:
        for sql in statements:
            db.execute(sql)
        require_schema_objects(db, statements)
        db.execute("DROP INDEX ex_intents_context")
        with pytest.raises(DomainError,match="SCHEMA_STRUCTURE_MISMATCH"):
            require_schema_objects(db,statements)
    finally:
        db.close()


def test_build_and_process_identity_are_stable_not_recaptured_per_refresh():
    first = build_identity()
    assert first is build_identity()
    assert runtime_identity()["started_at_monotonic"] == runtime_identity()["started_at_monotonic"]
    with pytest.raises(TypeError):
        first["version"] = "2.0.0"


def test_windows_pipe_names_are_sid_bound_and_checkout_isolated():
    sid = "S-1-5-21-123-456-789-1001"
    path = pipe_path(sid)
    assert validate_path(path) == path
    assert pipe_path(sid, "a"*32) != path
    assert pipe_path(sid) != pipe_path("S-1-5-21-123-456-789-1002")
    assert principal_id(sid) == principal_id(sid)
    for bad in (r"\\remote\pipe\coinfly-x", "../../worker", path+"/.."):
        with pytest.raises(RPCError):
            validate_path(bad)


@pytest.mark.skipif(os.name != "nt", reason="actual Win32 kernel API requires Windows AMD64")
def test_native_windows_pipe_roundtrip_and_live_authority_remain_separate():
    from coinfly.ipc.transport_named_pipe import NamedPipeRPCClient, NamedPipeRPCServer
    from coinfly.ipc.windows_native import process_sid
    path = pipe_path(process_sid(os.getpid()), uuid4().hex)
    server = NamedPipeRPCServer(path, lambda request, peer: {"epoch": request.epoch}).start()
    try:
        for _ in range(3):
            with NamedPipeRPCClient(path) as client:
                assert client.call("state.snapshot", epoch=4) == {"epoch":4}
                with pytest.raises(RPCError,match="SIGNED_HELPER_REQUIRED"):
                    client.call("engine.arm",epoch=4)
    finally:
        server.close()
