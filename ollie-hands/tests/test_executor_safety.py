"""P0 executor safety: uncertain actions never repeat and gates fail closed."""

import pathlib
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import executor as E  # noqa: E402
from ollie_hands import actscript as A  # noqa: E402
from ollie_hands import observe as O  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


def _step(*, postcondition=None):
    return SimpleNamespace(
        id="s1",
        kind="uia",
        args={"op": "invoke", "name": "Submit"},
        timeout=30,
        preconditions=[],
        postcondition=postcondition,
        on_fail="escalate",
        checkpoint=False,
        decision=SimpleNamespace(consent="confirm"),
    )


def _rec():
    return {"abort": threading.Event()}


def test_failed_postcondition_reobserves_but_never_redispatches(monkeypatch):
    dispatches = []
    checks = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    monkeypatch.setattr(E.Cond, "check",
                        lambda *args, **kwargs: checks.append(args) or (False, "not found"))
    monkeypatch.setattr(E.time, "sleep", lambda _: None)

    outcome = E._run_step(
        _step(postcondition={"type": "uia_exists", "name": "Done"}),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
    )

    assert len(dispatches) == 1
    assert len(checks) == 2
    assert outcome["status"] == "outcome_unknown"
    assert "action was not repeated" in outcome["detail"]


def test_closed_gate_immediately_before_dispatch_prevents_action(monkeypatch):
    dispatches = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})

    outcome = E._run_step(
        _step(), cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: False,
    )

    assert dispatches == []
    assert outcome == {"id": "s1", "status": "cancelled", "stage": "action",
                       "detail": "execution gate is closed"}


def test_gate_closing_after_dispatch_prevents_verification(monkeypatch):
    dispatches = []
    checks = []
    gate_results = iter((True, False))
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    monkeypatch.setattr(E.Cond, "check",
                        lambda *args, **kwargs: checks.append(args) or (True, "ok"))

    outcome = E._run_step(
        _step(postcondition={"type": "uia_exists", "name": "Done"}),
        cfg=object(), audit=AuditStub(), rec=_rec(),
        gate=lambda: next(gate_results),
    )

    assert len(dispatches) == 1
    assert checks == []
    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] == "postcondition"
    assert "no action was repeated" in outcome["detail"]


def test_gate_closing_before_recovery_prevents_second_observation(monkeypatch):
    dispatches = []
    checks = []
    gate_results = iter((True, True, False))
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    monkeypatch.setattr(E.Cond, "check",
                        lambda *args, **kwargs: checks.append(args) or (False, "not found"))

    outcome = E._run_step(
        _step(postcondition={"type": "uia_exists", "name": "Done"}),
        cfg=object(), audit=AuditStub(), rec=_rec(),
        gate=lambda: next(gate_results),
    )

    assert len(dispatches) == 1
    assert len(checks) == 1
    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] == "postcondition_recovery"


def test_task_abort_is_checked_at_dispatch_boundary(monkeypatch):
    dispatches = []
    rec = _rec()
    rec["abort"].set()
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})

    outcome = E._run_step(
        _step(), cfg=object(), audit=AuditStub(), rec=rec, gate=lambda: True,
    )

    assert dispatches == []
    assert outcome["status"] == "cancelled"
    assert outcome["detail"] == "task abort requested"


def test_gate_exception_fails_closed(monkeypatch):
    dispatches = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})

    def broken_gate():
        raise OSError("config unavailable")

    outcome = E._run_step(
        _step(), cfg=object(), audit=AuditStub(), rec=_rec(), gate=broken_gate,
    )

    assert dispatches == []
    assert outcome["status"] == "cancelled"
    assert "config unavailable" in outcome["detail"]


class ConsentStub:
    def confirm(self, *args, **kwargs):
        return True

    def notify(self, *args, **kwargs):
        pass


def test_run_preserves_outcome_unknown_and_stops_later_steps(monkeypatch):
    script = A.parse({"title": "two writes", "steps": [
        {"id": "first", "kind": "uia",
         "args": {"op": "invoke", "name": "Submit"},
         "postcondition": {"type": "uia_exists", "name": "Done"}},
        {"id": "second", "kind": "uia",
         "args": {"op": "invoke", "name": "Next"},
         "postcondition": {"type": "uia_exists", "name": "Finished"}},
    ]})
    dispatches = []
    gate_results = iter((True, False))
    monkeypatch.setattr(O, "last_input_tick", lambda: 0)
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    monkeypatch.setattr(E.Cond, "check",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("verification must not run after gate closes")))

    summary = E.run(script, cfg=object(), audit=AuditStub(),
                    consent=ConsentStub(), gate=lambda: next(gate_results))

    assert summary["status"] == "outcome_unknown"
    assert summary["step"] == 1
    assert len(summary["results"]) == 1
    assert summary["results"][0]["status"] == "outcome_unknown"
    assert len(dispatches) == 1


def test_run_reports_clean_abort_when_gate_closes_before_dispatch(monkeypatch):
    script = A.parse({"title": "one write", "steps": [
        {"id": "first", "kind": "uia",
         "args": {"op": "invoke", "name": "Submit"},
         "postcondition": {"type": "uia_exists", "name": "Done"}},
    ]})
    dispatches = []
    monkeypatch.setattr(O, "last_input_tick", lambda: 0)
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})

    summary = E.run(script, cfg=object(), audit=AuditStub(),
                    consent=ConsentStub(), gate=lambda: False)

    assert summary["status"] == "aborted"
    assert summary["results"][0]["status"] == "cancelled"
    assert dispatches == []


# ------------------------------------------------------- P0-A: denied plan ---
# consent.confirm returns (approved, ref). A non-empty tuple is always truthy,
# so the executor must normalize the decision at the boundary: any shape that
# is not an explicit approval fails closed and dispatches NOTHING.


class RecordingConsent:
    def __init__(self, result):
        self.result = result
        self.confirm_calls = []
        self.notifications = []

    def confirm(self, *args, **kwargs):
        self.confirm_calls.append((args, kwargs))
        return self.result

    def notify(self, *args, **kwargs):
        self.notifications.append((args, kwargs))


def _confirm_script():
    return A.parse({"title": "owner-gated write", "steps": [
        {"id": "first", "kind": "uia",
         "args": {"op": "invoke", "name": "Submit"},
         "postcondition": {"type": "uia_exists", "name": "Done"}},
    ]})


def _run_with_consent(monkeypatch, consent_result):
    dispatches = []
    monkeypatch.setattr(O, "last_input_tick", lambda: 0)
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    monkeypatch.setattr(E.Cond, "check", lambda *args, **kwargs: (True, "ok"))
    consent = RecordingConsent(consent_result)
    summary = E.run(_confirm_script(), cfg=object(), audit=AuditStub(),
                    consent=consent, gate=lambda: True)
    return summary, dispatches, consent


def test_denied_plan_tuple_dispatches_nothing(monkeypatch):
    summary, dispatches, consent = _run_with_consent(monkeypatch, (False, "H-denied"))
    assert summary["status"] == "denied"
    assert dispatches == []
    assert len(consent.confirm_calls) == 1


def test_denied_plan_bare_false_dispatches_nothing(monkeypatch):
    summary, dispatches, _ = _run_with_consent(monkeypatch, False)
    assert summary["status"] == "denied"
    assert dispatches == []


def test_timed_out_plan_dispatches_nothing(monkeypatch):
    # A timeout reaches the executor as an unapproved decision; it must be
    # indistinguishable from an explicit denial.
    summary, dispatches, _ = _run_with_consent(monkeypatch, (False, "H-timed-out"))
    assert summary["status"] == "denied"
    assert dispatches == []


def test_delivery_failure_dispatches_nothing(monkeypatch):
    # If the owner never even saw the prompt, the decision is unapproved.
    summary, dispatches, _ = _run_with_consent(monkeypatch, (False, ""))
    assert summary["status"] == "denied"
    assert dispatches == []


@pytest.mark.parametrize("malformed", [
    None,
    "yes",
    "",
    0,
    1,
    (),
    (1, "H-x"),
    ("true", "H-x"),
    ["H-x"],
    {"approved": True},
])
def test_malformed_consent_results_fail_closed(monkeypatch, malformed):
    summary, dispatches, _ = _run_with_consent(monkeypatch, malformed)
    assert summary["status"] == "denied"
    assert dispatches == []


def test_approved_plan_tuple_dispatches_once(monkeypatch):
    summary, dispatches, _ = _run_with_consent(monkeypatch, (True, "H-approved"))
    assert summary["status"] == "ok"
    assert len(dispatches) == 1
