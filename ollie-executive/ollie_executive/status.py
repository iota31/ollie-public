"""Generated Markdown read model for humans; never owns canonical state."""

from __future__ import annotations

import sqlite3

from .selector import select_next


def _safe(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_status(conn: sqlite3.Connection, now: str) -> str:
    goals = conn.execute(
        "SELECT id,title,outcome,priority FROM goals WHERE status='active' ORDER BY priority DESC, created_at, id"
    ).fetchall()
    commitments = conn.execute(
        """SELECT id,title,owner,status,followup_at FROM commitments
           WHERE status NOT IN ('verified','failed','cancelled')
           ORDER BY CASE WHEN followup_at IS NULL THEN 1 ELSE 0 END, followup_at, created_at, id"""
    ).fetchall()
    work = conn.execute(
        """SELECT id,title,work_class,status FROM work_items
           WHERE status IN ('ready','running','blocked') ORDER BY created_at, id"""
    ).fetchall()
    selected = select_next(conn, now)
    lines = ["# Ollie Executive Status", "", f"Generated: `{now}`", ""]
    if selected:
        lines += ["## Next selected work", "", f"**{_safe(selected.title)}** (`{selected.id}`)", "", selected.explanation, ""]
    else:
        lines += ["## Next selected work", "", "No eligible work. This is a valid outcome.", ""]
    lines += ["## Active goals", "", "| ID | Priority | Goal | Outcome |", "|---|---:|---|---|"]
    lines += [f"| `{r['id']}` | {r['priority']} | {_safe(r['title'])} | {_safe(r['outcome'])} |" for r in goals]
    if not goals:
        lines.append("| — | — | No active goals | — |")
    lines += ["", "## Open commitments", "", "| ID | Status | Owner | Follow-up | Commitment |", "|---|---|---|---|---|"]
    lines += [f"| `{r['id']}` | {r['status']} | {_safe(r['owner'])} | {r['followup_at'] or '—'} | {_safe(r['title'])} |" for r in commitments]
    if not commitments:
        lines.append("| — | — | — | — | No open commitments |")
    lines += ["", "## Work queue", "", "| ID | Class | Status | Work |", "|---|---|---|---|"]
    lines += [f"| `{r['id']}` | {r['work_class']} | {r['status']} | {_safe(r['title'])} |" for r in work]
    if not work:
        lines.append("| — | — | — | No queued work |")
    return "\n".join(lines) + "\n"

