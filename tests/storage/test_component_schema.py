"""Fresh 1.0.0 component contract tests, using the SQLite SQL layer only."""
import sqlite3

import pytest

from coinfly.domain.context import DomainError
from coinfly.storage.component_schema import install_component_schema, require_columns

DDL = ("CREATE TABLE IF NOT EXISTS sample (id TEXT PRIMARY KEY,value TEXT NOT NULL)",)

@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()

def test_fresh_install_and_reopen(db):
    with db:
        digest = install_component_schema(db, "sample", DDL)
        db.execute("INSERT INTO sample VALUES('id','retained')")
    with db:
        assert install_component_schema(db, "sample", DDL) == digest
    assert db.execute("SELECT value FROM sample").fetchone()[0] == "retained"
    require_columns(db, "sample", ("id", "value"))

def test_modified_contract_is_not_migrated(db):
    with db:
        install_component_schema(db, "sample", DDL)
    with pytest.raises(DomainError, match="SCHEMA_CONTRACT_MISMATCH"), db:
        install_component_schema(db, "sample", ("CREATE TABLE sample (id TEXT,note TEXT)",))
    assert [r[1] for r in db.execute("PRAGMA table_info(sample)")] == ["id", "value"]

def test_unversioned_and_old_versioned_tables_are_preserved(db):
    db.execute("CREATE TABLE sample (id TEXT)")
    with pytest.raises(DomainError, match="UNVERSIONED_SCHEMA_REJECTED"), db:
        install_component_schema(db, "sample", DDL)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='sample'").fetchone()
    db.execute("CREATE TABLE cf_schema_versions (component TEXT)")
    with pytest.raises(DomainError, match="FRESH_INSTALL_REQUIRED"), db:
        install_component_schema(db, "other", ("CREATE TABLE other (id TEXT)",))

def test_component_installation_rolls_back(db):
    db.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError):
        with db:
            install_component_schema(db, "sample", DDL + ("INVALID SQL",))
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='sample'").fetchone()

def test_bad_identifiers_are_not_executed(db):
    with pytest.raises(DomainError, match="SCHEMA_CONTRACT_INVALID"):
        require_columns(db, "sample); DROP TABLE sample;--", ("id",))
