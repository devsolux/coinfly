"""Fresh-install 1.0.0 schema contracts. No upgrade, import or downgrade path.

Each component installs its final DDL inside the caller's transaction and pins
that DDL's digest. Reopening exactly this schema is idempotent. A different or
unversioned existing schema is rejected without altering or deleting user data.
Scientific model/study versions are separate and are not database migrations.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from functools import lru_cache
from hashlib import sha256

from coinfly.domain.context import DomainError

SCHEMA_CONTRACTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS cf_schema_contracts (component TEXT PRIMARY KEY, "
    "schema_sha256 TEXT NOT NULL, application_version TEXT NOT NULL)"
)


def schema_digest(statements: Sequence[str]) -> str:
    return sha256("\n".join(statements).encode("utf-8")).hexdigest()


def install_component_schema(connection: sqlite3.Connection, component: str, statements: Sequence[str]) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,80}", component) or not statements:
        raise DomainError("SCHEMA_CONTRACT_INVALID")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='cf_schema_versions'").fetchone():
        raise DomainError("FRESH_INSTALL_REQUIRED")
    connection.execute(SCHEMA_CONTRACTS_TABLE)
    digest = schema_digest(statements)
    row = connection.execute("SELECT schema_sha256,application_version FROM cf_schema_contracts WHERE component=?", (component,)).fetchone()
    if row is not None:
        if tuple(row) != (digest, "1.0.0"):
            raise DomainError("SCHEMA_CONTRACT_MISMATCH")
        require_schema_objects(connection, tuple(statements))
        return digest
    for sql in statements:
        match = re.match(r"CREATE TABLE(?: IF NOT EXISTS)? ([A-Za-z_][A-Za-z0-9_]*)", sql)
        if match and connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (match[1],)).fetchone():
            raise DomainError("UNVERSIONED_SCHEMA_REJECTED")
    for sql in statements:
        connection.execute(sql)
    connection.execute("INSERT INTO cf_schema_contracts VALUES(?,?,?)", (component, digest, "1.0.0"))
    return digest


def require_columns(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
        raise DomainError("SCHEMA_CONTRACT_INVALID")
    present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if not set(columns).issubset(present):
        raise DomainError("SCHEMA_COLUMNS_MISSING")


@lru_cache(maxsize=32)
def _schema_objects(statements: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    reference = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            reference.execute(statement)
        return tuple(reference.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
        ))
    finally:
        reference.close()


def require_schema_objects(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """A matching marker alone cannot conceal removed columns or indexes."""
    for kind, name, sql in _schema_objects(statements):
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
        if row is None or row[0] != sql:
            raise DomainError("SCHEMA_STRUCTURE_MISMATCH")


@lru_cache(maxsize=4)
def script_statements(script: str) -> tuple[str, ...]:
    """Split complete SQL statements, retaining trigger bodies and comments."""
    pending = ""
    result: list[str] = []
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            result.append(pending)
            pending = ""
    if pending.strip():
        # A trailing comment is harmless; a statement without a terminator is
        # not a complete pinned installation contract.
        if any(line.strip() and not line.lstrip().startswith("--") for line in pending.splitlines()):
            raise DomainError("SCHEMA_CONTRACT_INVALID")
    return tuple(result)
