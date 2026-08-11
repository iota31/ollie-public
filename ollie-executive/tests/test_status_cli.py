import json
import subprocess
import sys
from pathlib import Path

from ollie_executive.status import render_status


def test_markdown_status_is_generated_from_canonical_state(ledger, conn):
    goal = ledger.add_goal("Reliable | autonomy", "Verified outcomes", priority=90, entity_id="goal_reliable")
    commitment = ledger.add_commitment("Ship ledger", "ollie", "founder", "implement", "tests pass", goal_id=goal, entity_id="commitment_ship")
    ledger.add_work_item("Implement ledger", "founder_commitment", commitment_id=commitment, goal_id=goal, entity_id="work_ledger")
    text = render_status(conn, "2026-07-10T12:00:00Z")
    assert "Reliable \\| autonomy" in text
    assert "commitment_ship" in text
    assert "work_ledger" in text
    assert "Next selected work" in text


def test_cli_end_to_end(tmp_path):
    root = Path(__file__).parents[1]
    db = tmp_path / "cli.db"
    command = [sys.executable, "-m", "ollie_executive", "--db", str(db)]
    init = subprocess.run(command + ["init"], cwd=root, text=True, capture_output=True)
    assert init.returncode == 0, init.stderr
    add = subprocess.run(command + ["goal-add", "--id", "goal_cli", "--title", "CLI", "--outcome", "works"], cwd=root, text=True, capture_output=True)
    assert json.loads(add.stdout) == {"id": "goal_cli"}
    status = subprocess.run(command + ["status", "--now", "2026-07-10T12:00:00Z"], cwd=root, text=True, capture_output=True)
    assert status.returncode == 0, status.stderr
    assert "goal_cli" in status.stdout

