import sqlite3

import pytest

from ollie_executive.db import Migration, connect, migrate


def test_file_database_uses_wal_and_foreign_keys(tmp_path):
    conn = connect(tmp_path / "ledger.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_is_idempotent(conn):
    migrate(conn)
    rows = conn.execute("SELECT version,name FROM schema_migrations").fetchall()
    assert [tuple(row) for row in rows] == [(1, "initial executive ledger")]


def test_failed_migration_rolls_back_all_its_steps(tmp_path):
    conn = connect(tmp_path / "rollback.db")
    bad = Migration(99, "fail atomically", (
        "CREATE TABLE should_disappear(id INTEGER PRIMARY KEY)",
        "INSERT INTO table_that_does_not_exist VALUES (1)",
    ))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, (bad,))
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='should_disappear'").fetchone() is None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=99").fetchone() is None


def test_migration_name_drift_is_rejected(conn):
    with pytest.raises(RuntimeError, match="name does not match"):
        migrate(conn, (Migration(1, "renamed", ()),))


def test_foreign_keys_reject_orphans(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO work_items(id,goal_id,title,work_class,created_at,updated_at) VALUES ('work_x','missing','x','goal_work','t','t')")
