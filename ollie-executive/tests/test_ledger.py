import sqlite3

import pytest


def test_stable_caller_ids_and_generated_ids(ledger, conn):
    assert ledger.add_goal("Goal", "Outcome", entity_id="goal_reliable") == "goal_reliable"
    generated = ledger.add_goal("Second", "Another outcome")
    assert generated.startswith("goal_") and len(generated) == 37
    assert conn.execute("SELECT id FROM goals ORDER BY id").fetchall()


def test_commitment_requires_success_contract(ledger):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_commitment("Do it", "ollie", "founder", "start", "")


def test_terminal_commitment_requires_linked_evidence(ledger, conn):
    commitment = ledger.add_commitment("Ship", "ollie", "founder", "test", "deployed and healthy")
    with pytest.raises(ValueError, match="requires evidence"):
        ledger.set_commitment_status(commitment, "verified")
    evidence = ledger.add_evidence("test", "health check passed", commitment_id=commitment)
    ledger.set_commitment_status(commitment, "verified", evidence_id=evidence)
    row = conn.execute("SELECT status,verification_evidence_id,closed_at FROM commitments WHERE id=?", (commitment,)).fetchone()
    assert tuple(row) == ("verified", evidence, "2026-07-10T12:00:00Z")


def test_database_rejects_evidence_from_another_commitment(ledger, conn):
    first = ledger.add_commitment("One", "ollie", "founder", "act", "proof")
    second = ledger.add_commitment("Two", "ollie", "founder", "act", "proof")
    evidence = ledger.add_evidence("test", "proof for two", commitment_id=second)
    with pytest.raises(sqlite3.IntegrityError, match="linked to that commitment"):
        ledger.set_commitment_status(first, "failed", evidence_id=evidence)


def test_evidence_must_have_subject(ledger):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_evidence("artifact", "nowhere")


def test_event_is_append_only_in_shape_and_requires_one_subject(ledger):
    goal = ledger.add_goal("Goal", "Outcome")
    event = ledger.add_event("founder_approved", "tushar", goal_id=goal, payload={"label": "valuable"})
    assert event.startswith("event_")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_event("bad", "ollie", goal_id=goal, work_item_id="missing")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("UPDATE events SET kind='rewritten' WHERE id=?", (event,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("DELETE FROM events WHERE id=?", (event,))


def test_run_lifecycle_updates_work_item(ledger, conn):
    work = ledger.add_work_item("Maintain", "maintenance")
    run = ledger.start_run(work, "worker-1")
    assert conn.execute("SELECT status FROM work_items WHERE id=?", (work,)).fetchone()[0] == "running"
    ledger.finish_run(run, "failed", "temporary failure")
    assert conn.execute("SELECT status FROM work_items WHERE id=?", (work,)).fetchone()[0] == "ready"


def test_verified_run_requires_evidence_and_start_is_single_use(ledger, conn):
    work = ledger.add_work_item("Ship", "maintenance")
    run = ledger.start_run(work, "worker-1")
    with pytest.raises(ValueError, match="not ready"):
        ledger.start_run(work, "worker-2")
    with pytest.raises(sqlite3.IntegrityError, match="requires linked evidence"):
        ledger.finish_run(run, "verified", "looks done")
    assert conn.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "running"
    ledger.add_evidence("test", "acceptance passed", run_id=run)
    ledger.finish_run(run, "verified", "acceptance passed")
    assert conn.execute("SELECT status FROM work_items WHERE id=?", (work,)).fetchone()[0] == "done"
