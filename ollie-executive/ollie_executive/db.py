"""SQLite connection and transactional schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


MigrationStep = str | Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    steps: Sequence[MigrationStep]


SCHEMA_V1 = (
    """
    CREATE TABLE goals (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        outcome TEXT NOT NULL CHECK (length(trim(outcome)) > 0),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'paused', 'achieved', 'abandoned')),
        priority INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE commitments (
        id TEXT PRIMARY KEY,
        goal_id TEXT REFERENCES goals(id) ON DELETE RESTRICT,
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        owner TEXT NOT NULL CHECK (length(trim(owner)) > 0),
        source TEXT NOT NULL CHECK (source IN ('founder', 'ollie', 'system')),
        status TEXT NOT NULL DEFAULT 'accepted'
            CHECK (status IN ('accepted', 'running', 'blocked', 'verified', 'failed', 'cancelled')),
        next_action TEXT NOT NULL CHECK (length(trim(next_action)) > 0),
        due_at TEXT,
        followup_at TEXT,
        success_criteria TEXT NOT NULL CHECK (length(trim(success_criteria)) > 0),
        verification_evidence_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        CHECK (
            (status IN ('verified', 'failed', 'cancelled') AND verification_evidence_id IS NOT NULL AND closed_at IS NOT NULL)
            OR
            (status NOT IN ('verified', 'failed', 'cancelled') AND verification_evidence_id IS NULL AND closed_at IS NULL)
        )
    ) STRICT
    """,
    """
    CREATE TABLE work_items (
        id TEXT PRIMARY KEY,
        goal_id TEXT REFERENCES goals(id) ON DELETE RESTRICT,
        commitment_id TEXT REFERENCES commitments(id) ON DELETE RESTRICT,
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        work_class TEXT NOT NULL CHECK (work_class IN (
            'founder_commitment', 'followup', 'blocker', 'goal_work', 'maintenance', 'exploration'
        )),
        status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('ready', 'running', 'blocked', 'done', 'cancelled')),
        expected_value INTEGER NOT NULL DEFAULT 50 CHECK (expected_value BETWEEN 0 AND 100),
        urgency INTEGER NOT NULL DEFAULT 50 CHECK (urgency BETWEEN 0 AND 100),
        confidence INTEGER NOT NULL DEFAULT 50 CHECK (confidence BETWEEN 0 AND 100),
        effort INTEGER NOT NULL DEFAULT 50 CHECK (effort BETWEEN 0 AND 100),
        risk INTEGER NOT NULL DEFAULT 0 CHECK (risk BETWEEN 0 AND 100),
        dependencies_ready INTEGER NOT NULL DEFAULT 1 CHECK (dependencies_ready IN (0, 1)),
        not_before TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (work_class NOT IN ('founder_commitment', 'followup') OR commitment_id IS NOT NULL),
        CHECK (work_class != 'goal_work' OR goal_id IS NOT NULL)
    ) STRICT
    """,
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
        worker TEXT NOT NULL CHECK (length(trim(worker)) > 0),
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'blocked', 'verified', 'failed', 'cancelled')),
        summary TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        CHECK ((status = 'running' AND ended_at IS NULL) OR (status != 'running' AND ended_at IS NOT NULL))
    ) STRICT
    """,
    """
    CREATE TABLE evidence (
        id TEXT PRIMARY KEY,
        commitment_id TEXT REFERENCES commitments(id) ON DELETE RESTRICT,
        work_item_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
        run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (kind IN ('artifact', 'observation', 'test', 'external', 'decision', 'failure')),
        uri TEXT,
        summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
        sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
        created_at TEXT NOT NULL,
        CHECK (commitment_id IS NOT NULL OR work_item_id IS NOT NULL OR run_id IS NOT NULL)
    ) STRICT
    """,
    """
    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        goal_id TEXT REFERENCES goals(id) ON DELETE RESTRICT,
        commitment_id TEXT REFERENCES commitments(id) ON DELETE RESTRICT,
        work_item_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
        run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
        value INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}',
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        created_at TEXT NOT NULL,
        CHECK (
            (goal_id IS NOT NULL) + (commitment_id IS NOT NULL) +
            (work_item_id IS NOT NULL) + (run_id IS NOT NULL) = 1
        ),
        CHECK (json_valid(payload_json))
    ) STRICT
    """,
    "ALTER TABLE commitments ADD CONSTRAINT_PLACEHOLDER TEXT",
)


def _finish_v1(conn: sqlite3.Connection) -> None:
    # SQLite cannot add a named FK with ALTER TABLE. The column-level FK is
    # supplied through a trigger so it can also prove evidence ownership.
    conn.execute("ALTER TABLE commitments DROP COLUMN CONSTRAINT_PLACEHOLDER")
    conn.execute(
        """
        CREATE TRIGGER commitments_terminal_evidence_insert
        BEFORE INSERT ON commitments
        WHEN NEW.status IN ('verified', 'failed', 'cancelled')
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM evidence e
                WHERE e.id = NEW.verification_evidence_id AND e.commitment_id = NEW.id
            ) THEN RAISE(ABORT, 'terminal commitment requires evidence linked to that commitment') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER commitments_terminal_evidence_update
        BEFORE UPDATE OF status, verification_evidence_id ON commitments
        WHEN NEW.status IN ('verified', 'failed', 'cancelled')
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM evidence e
                WHERE e.id = NEW.verification_evidence_id AND e.commitment_id = NEW.id
            ) THEN RAISE(ABORT, 'terminal commitment requires evidence linked to that commitment') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER runs_verified_evidence_update
        BEFORE UPDATE OF status ON runs
        WHEN NEW.status = 'verified'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM evidence e WHERE e.run_id = NEW.id
            ) THEN RAISE(ABORT, 'verified run requires linked evidence') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER events_append_only_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER events_append_only_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """
    )
    for statement in (
        "CREATE INDEX commitments_goal_status_idx ON commitments(goal_id, status)",
        "CREATE INDEX commitments_followup_idx ON commitments(status, followup_at)",
        "CREATE INDEX work_items_selection_idx ON work_items(status, dependencies_ready, work_class, not_before)",
        "CREATE INDEX work_items_goal_idx ON work_items(goal_id)",
        "CREATE INDEX work_items_commitment_idx ON work_items(commitment_id)",
        "CREATE INDEX runs_work_item_idx ON runs(work_item_id, started_at)",
        "CREATE INDEX evidence_commitment_idx ON evidence(commitment_id)",
        "CREATE INDEX evidence_work_item_idx ON evidence(work_item_id)",
        "CREATE INDEX evidence_run_idx ON evidence(run_id)",
        "CREATE INDEX events_goal_idx ON events(goal_id, created_at)",
        "CREATE INDEX events_commitment_idx ON events(commitment_id, created_at)",
        "CREATE INDEX events_work_item_idx ON events(work_item_id, created_at)",
        "CREATE INDEX events_run_idx ON events(run_id, created_at)",
    ):
        conn.execute(statement)


MIGRATIONS = (Migration(1, "initial executive ledger", (*SCHEMA_V1, _finish_v1)),)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection. Schema changes are explicit."""
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """An explicit transaction for autocommit-mode connections."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> None:
    """Apply each unapplied migration atomically and in version order."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ) STRICT
        """
    )
    applied = {row[0]: row[1] for row in conn.execute(
        "SELECT version, name FROM schema_migrations"
    )}
    for migration in sorted(migrations, key=lambda item: item.version):
        if migration.version in applied:
            if applied[migration.version] != migration.name:
                raise RuntimeError(f"migration {migration.version} name does not match applied schema")
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for step in migration.steps:
                step(conn) if callable(step) else conn.execute(step)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
