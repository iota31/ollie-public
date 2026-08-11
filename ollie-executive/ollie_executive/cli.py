"""Command-line interface for the shadow executive ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import connect, migrate
from .service import ExecutiveLedger
from .status import render_status


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ollie's shadow-safe executive ledger")
    p.add_argument("--db", default="executive.db", help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    goal = sub.add_parser("goal-add")
    goal.add_argument("--id"); goal.add_argument("--title", required=True); goal.add_argument("--outcome", required=True)
    goal.add_argument("--priority", type=int, default=50)
    commitment = sub.add_parser("commitment-add")
    commitment.add_argument("--id"); commitment.add_argument("--goal-id"); commitment.add_argument("--title", required=True)
    commitment.add_argument("--owner", required=True); commitment.add_argument("--source", choices=("founder", "ollie", "system"), required=True)
    commitment.add_argument("--next-action", required=True); commitment.add_argument("--success", required=True)
    commitment.add_argument("--due-at"); commitment.add_argument("--followup-at")
    transition = sub.add_parser("commitment-status")
    transition.add_argument("id"); transition.add_argument("status", choices=("accepted", "running", "blocked", "verified", "failed", "cancelled"))
    transition.add_argument("--evidence-id")
    work = sub.add_parser("work-add")
    work.add_argument("--id"); work.add_argument("--title", required=True)
    work.add_argument("--class", dest="work_class", required=True, choices=("founder_commitment", "followup", "blocker", "goal_work", "maintenance", "exploration"))
    work.add_argument("--goal-id"); work.add_argument("--commitment-id"); work.add_argument("--not-before")
    for name, default in (("expected-value", 50), ("urgency", 50), ("confidence", 50), ("effort", 50), ("risk", 0)):
        work.add_argument(f"--{name}", type=int, default=default)
    work.add_argument("--dependencies-blocked", action="store_true")
    run = sub.add_parser("run-start"); run.add_argument("work_item_id"); run.add_argument("--worker", required=True); run.add_argument("--id")
    finish = sub.add_parser("run-finish"); finish.add_argument("id"); finish.add_argument("status", choices=("blocked", "verified", "failed", "cancelled")); finish.add_argument("--summary", required=True)
    evidence = sub.add_parser("evidence-add")
    evidence.add_argument("--id"); evidence.add_argument("--kind", required=True, choices=("artifact", "observation", "test", "external", "decision", "failure"))
    evidence.add_argument("--summary", required=True); evidence.add_argument("--uri"); evidence.add_argument("--sha256")
    evidence.add_argument("--commitment-id"); evidence.add_argument("--work-item-id"); evidence.add_argument("--run-id")
    event = sub.add_parser("event-add")
    event.add_argument("--id"); event.add_argument("--kind", required=True); event.add_argument("--actor", required=True)
    event.add_argument("--goal-id"); event.add_argument("--commitment-id"); event.add_argument("--work-item-id"); event.add_argument("--run-id")
    event.add_argument("--value", type=int); event.add_argument("--payload", default="{}")
    select = sub.add_parser("select"); select.add_argument("--now")
    status = sub.add_parser("status"); status.add_argument("--now"); status.add_argument("--output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    conn = connect(args.db)
    migrate(conn)
    ledger = ExecutiveLedger(conn)
    result = None
    if args.command == "init":
        result = {"database": str(Path(args.db).expanduser()), "schema": 1}
    elif args.command == "goal-add":
        result = {"id": ledger.add_goal(args.title, args.outcome, priority=args.priority, entity_id=args.id)}
    elif args.command == "commitment-add":
        result = {"id": ledger.add_commitment(args.title, args.owner, args.source, args.next_action, args.success, goal_id=args.goal_id, due_at=args.due_at, followup_at=args.followup_at, entity_id=args.id)}
    elif args.command == "commitment-status":
        ledger.set_commitment_status(args.id, args.status, evidence_id=args.evidence_id); result = {"id": args.id, "status": args.status}
    elif args.command == "work-add":
        result = {"id": ledger.add_work_item(args.title, args.work_class, goal_id=args.goal_id, commitment_id=args.commitment_id, expected_value=args.expected_value, urgency=args.urgency, confidence=args.confidence, effort=args.effort, risk=args.risk, dependencies_ready=not args.dependencies_blocked, not_before=args.not_before, entity_id=args.id)}
    elif args.command == "run-start":
        result = {"id": ledger.start_run(args.work_item_id, args.worker, entity_id=args.id)}
    elif args.command == "run-finish":
        ledger.finish_run(args.id, args.status, args.summary); result = {"id": args.id, "status": args.status}
    elif args.command == "evidence-add":
        result = {"id": ledger.add_evidence(args.kind, args.summary, commitment_id=args.commitment_id, work_item_id=args.work_item_id, run_id=args.run_id, uri=args.uri, sha256=args.sha256, entity_id=args.id)}
    elif args.command == "event-add":
        result = {"id": ledger.add_event(args.kind, args.actor, goal_id=args.goal_id, commitment_id=args.commitment_id, work_item_id=args.work_item_id, run_id=args.run_id, value=args.value, payload=json.loads(args.payload), entity_id=args.id)}
    elif args.command == "select":
        selected = ledger.select(args.now); result = None if selected is None else selected.__dict__
    elif args.command == "status":
        markdown = render_status(conn, args.now or ledger.clock())
        if args.output:
            Path(args.output).write_text(markdown)
            result = {"output": args.output}
        else:
            print(markdown, end="")
            return 0
    print(json.dumps(result, sort_keys=True))
    return 0

