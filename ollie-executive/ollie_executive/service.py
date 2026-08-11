"""Domain operations for the executive ledger."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .selector import Selection, select_next
from .db import transaction


TERMINAL_COMMITMENT_STATUSES = {"verified", "failed", "cancelled"}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,127}$")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def valid_id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError("IDs must be 2-128 characters and contain only letters, digits, _ . : -")
    return value


class ExecutiveLedger:
    def __init__(self, conn: sqlite3.Connection, *, clock=utc_now):
        self.conn = conn
        self.clock = clock

    def add_goal(self, title: str, outcome: str, *, priority: int = 50, entity_id: str | None = None) -> str:
        entity_id = valid_id(entity_id or new_id("goal"))
        now = self.clock()
        self.conn.execute(
            "INSERT INTO goals(id,title,outcome,priority,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (entity_id, title, outcome, priority, now, now),
        )
        return entity_id

    def add_commitment(
        self, title: str, owner: str, source: str, next_action: str, success_criteria: str,
        *, goal_id: str | None = None, due_at: str | None = None,
        followup_at: str | None = None, entity_id: str | None = None,
    ) -> str:
        entity_id = valid_id(entity_id or new_id("commitment"))
        now = self.clock()
        self.conn.execute(
            """INSERT INTO commitments(
                id,goal_id,title,owner,source,next_action,due_at,followup_at,
                success_criteria,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, goal_id, title, owner, source, next_action, due_at,
             followup_at, success_criteria, now, now),
        )
        return entity_id

    def set_commitment_status(
        self, commitment_id: str, status: str, *, evidence_id: str | None = None,
    ) -> None:
        now = self.clock()
        terminal = status in TERMINAL_COMMITMENT_STATUSES
        if terminal and not evidence_id:
            raise ValueError("terminal commitment status requires evidence")
        if not terminal and evidence_id:
            raise ValueError("evidence closes a commitment only with a terminal status")
        cursor = self.conn.execute(
            """UPDATE commitments
               SET status=?, verification_evidence_id=?, closed_at=?, updated_at=?
               WHERE id=?""",
            (status, evidence_id, now if terminal else None, now, commitment_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(commitment_id)

    def add_work_item(
        self, title: str, work_class: str, *, goal_id: str | None = None,
        commitment_id: str | None = None, expected_value: int = 50,
        urgency: int = 50, confidence: int = 50, effort: int = 50,
        risk: int = 0, dependencies_ready: bool = True,
        not_before: str | None = None, entity_id: str | None = None,
    ) -> str:
        entity_id = valid_id(entity_id or new_id("work"))
        now = self.clock()
        self.conn.execute(
            """INSERT INTO work_items(
                id,goal_id,commitment_id,title,work_class,expected_value,urgency,
                confidence,effort,risk,dependencies_ready,not_before,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, goal_id, commitment_id, title, work_class, expected_value,
             urgency, confidence, effort, risk, int(dependencies_ready), not_before, now, now),
        )
        return entity_id

    def start_run(self, work_item_id: str, worker: str, *, entity_id: str | None = None) -> str:
        entity_id = valid_id(entity_id or new_id("run"))
        now = self.clock()
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE work_items SET status='running', updated_at=? "
                "WHERE id=? AND status='ready'",
                (now, work_item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"work item {work_item_id} is missing or not ready")
            self.conn.execute(
                "INSERT INTO runs(id,work_item_id,worker,started_at) VALUES (?,?,?,?)",
                (entity_id, work_item_id, worker, now),
            )
        return entity_id

    def finish_run(self, run_id: str, status: str, summary: str) -> None:
        if status == "running":
            raise ValueError("finish status cannot be running")
        now = self.clock()
        with transaction(self.conn):
            row = self.conn.execute("SELECT work_item_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            self.conn.execute(
                "UPDATE runs SET status=?, summary=?, ended_at=? WHERE id=?",
                (status, summary, now, run_id),
            )
            work_status = {"verified": "done", "blocked": "blocked", "failed": "ready", "cancelled": "cancelled"}[status]
            self.conn.execute(
                "UPDATE work_items SET status=?, updated_at=? WHERE id=?",
                (work_status, now, row["work_item_id"]),
            )

    def add_evidence(
        self, kind: str, summary: str, *, commitment_id: str | None = None,
        work_item_id: str | None = None, run_id: str | None = None,
        uri: str | None = None, sha256: str | None = None,
        entity_id: str | None = None,
    ) -> str:
        entity_id = valid_id(entity_id or new_id("evidence"))
        self.conn.execute(
            """INSERT INTO evidence(
                id,commitment_id,work_item_id,run_id,kind,uri,summary,sha256,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (entity_id, commitment_id, work_item_id, run_id, kind, uri, summary, sha256, self.clock()),
        )
        return entity_id

    def add_event(
        self, kind: str, actor: str, *, goal_id: str | None = None,
        commitment_id: str | None = None, work_item_id: str | None = None,
        run_id: str | None = None, value: int | None = None,
        payload: dict[str, Any] | None = None, entity_id: str | None = None,
    ) -> str:
        entity_id = valid_id(entity_id or new_id("event"))
        self.conn.execute(
            """INSERT INTO events(
                id,goal_id,commitment_id,work_item_id,run_id,kind,value,payload_json,actor,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, goal_id, commitment_id, work_item_id, run_id, kind, value,
             json.dumps(payload or {}, sort_keys=True, separators=(",", ":")), actor, self.clock()),
        )
        return entity_id

    def select(self, now: str | None = None) -> Selection | None:
        return select_next(self.conn, now or self.clock())
