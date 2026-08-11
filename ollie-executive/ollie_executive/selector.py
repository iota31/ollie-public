"""Deterministic, explainable selection for shadow-mode executive work."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


CLASS_RANK = {
    "founder_commitment": 0,
    "followup": 0,
    "blocker": 1,
    "goal_work": 2,
    "maintenance": 3,
    "exploration": 4,
}


@dataclass(frozen=True)
class Selection:
    id: str
    title: str
    work_class: str
    class_rank: int
    score: int
    explanation: str


def within_class_score(row: sqlite3.Row) -> int:
    """Integer-only score: value/urgency dominate; cost and risk subtract."""
    return (
        3 * row["expected_value"]
        + 2 * row["urgency"]
        + row["confidence"]
        - row["effort"]
        - 2 * row["risk"]
    )


def select_next(conn: sqlite3.Connection, now: str) -> Selection | None:
    rows = conn.execute(
        """
        SELECT id, title, work_class, expected_value, urgency, confidence, effort, risk, created_at
        FROM work_items
        WHERE status = 'ready'
          AND dependencies_ready = 1
          AND (not_before IS NULL OR not_before <= ?)
        """,
        (now,),
    ).fetchall()
    if not rows:
        return None
    ranked = sorted(
        rows,
        key=lambda row: (
            CLASS_RANK[row["work_class"]],
            -within_class_score(row),
            row["created_at"],
            row["id"],
        ),
    )
    winner = ranked[0]
    score = within_class_score(winner)
    rank = CLASS_RANK[winner["work_class"]]
    return Selection(
        id=winner["id"],
        title=winner["title"],
        work_class=winner["work_class"],
        class_rank=rank,
        score=score,
        explanation=(
            f"class={winner['work_class']} rank={rank}; "
            f"score=3*value+2*urgency+confidence-effort-2*risk={score}"
        ),
    )

