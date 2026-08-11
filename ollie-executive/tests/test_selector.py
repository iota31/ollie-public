def add_commitment(ledger, name="Founder promise"):
    return ledger.add_commitment(name, "ollie", "founder", "do next", "verified artifact")


def test_class_precedence_beats_any_score(ledger):
    ledger.add_work_item("Amazing exploration", "exploration", expected_value=100, urgency=100, confidence=100, effort=0, risk=0, entity_id="work_explore")
    commitment = add_commitment(ledger)
    ledger.add_work_item("Small founder request", "founder_commitment", commitment_id=commitment, expected_value=0, urgency=0, confidence=0, effort=100, risk=100, entity_id="work_founder")
    assert ledger.select().id == "work_founder"


def test_founder_and_followup_share_class_then_use_score(ledger):
    c1 = add_commitment(ledger, "One")
    c2 = add_commitment(ledger, "Two")
    ledger.add_work_item("Founder", "founder_commitment", commitment_id=c1, expected_value=10, entity_id="work_founder")
    ledger.add_work_item("Follow up", "followup", commitment_id=c2, expected_value=90, entity_id="work_followup")
    assert ledger.select().id == "work_followup"


def test_within_class_score_and_explanation(ledger):
    ledger.add_work_item("Risky", "maintenance", expected_value=100, risk=100, entity_id="work_risky")
    ledger.add_work_item("Safe", "maintenance", expected_value=80, risk=0, entity_id="work_safe")
    choice = ledger.select()
    assert choice.id == "work_safe"
    assert "3*value" in choice.explanation


def test_ineligible_work_is_ignored(ledger, conn):
    ledger.add_work_item("Dependencies", "blocker", dependencies_ready=False)
    ledger.add_work_item("Future", "blocker", not_before="2026-07-11T00:00:00Z")
    ready = ledger.add_work_item("Ready", "goal_work", goal_id=ledger.add_goal("G", "O"))
    assert ledger.select("2026-07-10T12:00:00Z").id == ready
    conn.execute("UPDATE work_items SET status='blocked' WHERE id=?", (ready,))
    assert ledger.select("2026-07-10T12:00:00Z") is None


def test_ties_are_broken_by_stable_id(ledger):
    ledger.add_work_item("B", "maintenance", entity_id="work_b")
    ledger.add_work_item("A", "maintenance", entity_id="work_a")
    assert ledger.select().id == "work_a"

