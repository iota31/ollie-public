"""Independent transport-loss / outcome_unknown safety tests for the executor.

The executor's central safety property is that any action whose outcome we
cannot verify must NEVER be repeated: an irreversible side effect may already
have happened, and a "retry" could double-fire it. The browser transport can
die between dispatch and verification (Playwright/Camoufox teardown, RDP drop,
process kill), and the postcondition check may then raise before we ever see
the world state.

These tests assert the executor's behavior at that boundary:

  1) A browser dispatch whose transport dies before the postcondition runs
     must surface as outcome_unknown, not as a redispatch and not as ok.
  2) The dispatch must be called EXACTLY ONCE — no retry, no second dispatch.
  3) The postcondition check must NOT be invoked after transport loss (we
     have no evidence to verify against).
  4) The audit trail must record the outcome_unknown event with the step
     identity, so the brain has a structured signal to escalate.
  5) The script-level run() must propagate outcome_unknown into the summary
     and STOP the remaining steps — a multi-step script with an unknown
     first step must not silently run the second.
  6) The legacy policy envelope {"scope": ..., "commit": ...} remains the
     ONLY accepted shape: callers cannot smuggle a malformed envelope or a
     bare string to downgrade the consent tier.

These tests are independent of test_executor_safety.py: they target the
browser transport-loss path (Playwright/Camoufox), while test_executor_safety
covers the broader gate/abort surface. Both files must pass.
"""

from __future__ import annotations

import pathlib
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import actscript as A  # noqa: E402
from ollie_hands import engine as Eng  # noqa: E402
from ollie_hands import executor as E  # noqa: E402
from ollie_hands import observe as O  # noqa: E402
from ollie_hands import policy as P  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


class ConsentStub:
    def confirm(self, *args, **kwargs):
        return True

    def notify(self, *args, **kwargs):
        pass


def _rec():
    return {"abort": threading.Event()}


# ---------------------------------------------------------------------------
# Helpers: legacy policy envelope
# ---------------------------------------------------------------------------
#
# The retained (legacy) typed consequence envelope has exactly two keys:
#
#     {"scope": "local" | "external" | "identity",
#      "commit": bool}
#
# Anything else — a malformed dict, a bare string, a dict missing scope —
# is invalid and must fail closed (T3 CONFIRM). The kept tests below pin that
# contract using the LOCAL envelope only.

LOCAL = {"scope": "local", "commit": False}


def _browser_step(*, postcondition=None, effect=None, commit=False,
                  target_text="Click me"):
    args = {
        "op": "click",
        "selector": "#submit",
        "timeout": 5,
    }
    if effect is not None:
        args["effect"] = effect
    if commit:
        args["commit"] = True
    return SimpleNamespace(
        id="b1",
        kind="browser",
        args=args,
        timeout=5,
        preconditions=[],
        postcondition=postcondition,
        on_fail="escalate",
        checkpoint=False,
        decision=SimpleNamespace(consent=P.CONFIRM),
        target_text=target_text,
    )


# ---------------------------------------------------------------------------
# 1) Browser transport dies mid-flight -> outcome_unknown, no retry
# ---------------------------------------------------------------------------


def test_browser_transport_death_before_postcondition_is_outcome_unknown(monkeypatch):
    """A browser dispatch that raises a transport-closed exception BEFORE the
    postcondition check runs must be surfaced as outcome_unknown — the action
    may have completed; we must not pretend the world is in a known state."""
    dispatches = []

    def fake_dispatch(kind, p, *, cfg=None):
        dispatches.append((kind, p))
        raise RuntimeError(
            "Connection closed while reading from the driver")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError(
                                "verification must not run after transport loss")))

    outcome = E._run_step(
        _browser_step(postcondition={"type": "web_property",
                                     "selector": "#done", "nonempty": True}),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
    )

    # Exactly ONE dispatch attempt — no retry on transport death.
    assert len(dispatches) == 1
    # The step is reported as outcome_unknown, with the step id preserved.
    assert outcome["status"] == "outcome_unknown"
    assert outcome["id"] == "b1"
    assert outcome["stage"] == "action"


def test_browser_transport_death_does_not_invoke_postcondition(monkeypatch):
    """The postcondition check must NEVER run after the dispatch's transport
    has died. Verifying against a world we never observed would be theatre."""
    checks = []

    def fake_dispatch(kind, p, *, cfg=None):
        raise RuntimeError("Target page, context or browser has been closed")

    def fake_check(*args, **kwargs):
        checks.append(args)
        return (True, "ok")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check", fake_check)

    outcome = E._run_step(
        _browser_step(postcondition={"type": "web_text",
                                     "selector": "body", "contains": "Done"}),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
    )

    assert checks == []
    assert outcome["status"] == "outcome_unknown"


def test_browser_transport_death_audits_outcome_unknown(monkeypatch):
    """An outcome_unknown event must be emitted so the brain can escalate.
    The audit args must carry the step id and the transport-loss detail."""
    audit = AuditStub()

    def fake_dispatch(kind, p, *, cfg=None):
        raise RuntimeError("Browser closed")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no verification after transport loss")))

    E._run_step(
        _browser_step(postcondition={"type": "web_url", "url": "https://x"}),
        cfg=object(), audit=audit, rec=_rec(), gate=lambda: True,
    )

    unknowns = [e for e in audit.events if e[1].get("status") == "outcome_unknown"]
    assert len(unknowns) == 1
    ev = unknowns[0]
    assert ev[0] == "step"
    assert ev[1].get("args", {}).get("id") == "b1"
    assert "browser closed" in ev[1].get("detail", "").lower()


def test_browser_transport_death_is_never_silently_retried(monkeypatch):
    """A transport-closed exception must NOT be caught and retried inside the
    executor. We must observe exactly one dispatch attempt and let the failure
    surface as outcome_unknown."""
    dispatches = []

    def fake_dispatch(kind, p, *, cfg=None):
        dispatches.append(kind)
        raise RuntimeError("Connection closed")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no verification after transport loss")))

    E._run_step(
        _browser_step(postcondition={"type": "web_property",
                                     "selector": "#x", "nonempty": True}),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
    )

    assert dispatches == ["browser"]


# ---------------------------------------------------------------------------
# 2) Script-level propagation: outcome_unknown stops later steps
# ---------------------------------------------------------------------------


def test_script_outcome_unknown_stops_later_steps(monkeypatch):
    """A two-step browser script whose first step's outcome is unknown must
    not run its second step. The summary must report outcome_unknown, with
    exactly one result recorded."""
    script = A.parse({"title": "submit then confirm", "steps": [
        {"id": "submit", "kind": "browser",
         "args": {"op": "click", "selector": "#submit"},
         "postcondition": {"type": "web_property",
                           "selector": "#done", "nonempty": True}},
        {"id": "confirm", "kind": "browser",
         "args": {"op": "click", "selector": "#ok"},
         "postcondition": {"type": "web_property",
                           "selector": "#confirmed", "nonempty": True}},
    ]})

    dispatches = []

    def fake_dispatch(kind, p, *, cfg=None):
        dispatches.append(p.get("selector"))
        # First step's transport dies; second step must NEVER be attempted.
        if p.get("selector") == "#submit":
            raise RuntimeError("Connection closed while reading from the driver")
        raise AssertionError("second step ran after outcome_unknown on first")

    monkeypatch.setattr(O, "last_input_tick", lambda: 0)
    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)

    summary = E.run(script, cfg=object(), audit=AuditStub(),
                    consent=ConsentStub(), gate=lambda: True)

    assert summary["status"] == "outcome_unknown"
    assert dispatches == ["#submit"]
    assert len(summary["results"]) == 1
    assert summary["results"][0]["id"] == "submit"
    assert summary["results"][0]["status"] == "outcome_unknown"


# ---------------------------------------------------------------------------
# 3) Legacy policy envelope shape contract
# ---------------------------------------------------------------------------
#
# The retained envelope is exactly {"scope": ..., "commit": ...}. Invalid
# shapes must fail closed.


def test_browser_click_with_invalid_effect_envelope_confirms():
    """An envelope missing 'scope', a string envelope, or a malformed dict
    must NOT silently downgrade a click to a narrated/auto tier."""
    # bare string (rejected by the envelope validator)
    d = P.classify_browser("click", effect="local")
    assert d.consent == P.CONFIRM
    # malformed envelope — dict missing the retained keys
    d2 = P.classify_browser("click", effect={"unexpected": 1})
    assert d2.consent == P.CONFIRM
    # missing scope key
    d3 = P.classify_browser("click", effect={"commit": False})
    assert d3.consent == P.CONFIRM


def test_browser_click_with_legacy_local_envelope_remains_confirm():
    """A click is a side-effect — even with the legacy local envelope, it
    remains CONFIRM. The envelope declares an intent; it never overrides the
    fail-closed rule for commit-prone browser verbs."""
    d = P.classify_browser("click", effect=LOCAL, target_text="Continue")
    assert d.consent == P.CONFIRM


# ---------------------------------------------------------------------------
# 4) Postcondition transport loss semantics — independent of browser.py
# ---------------------------------------------------------------------------


def test_postcondition_transport_loss_does_not_invoke_recovery_check(monkeypatch):
    """If the postcondition check ITSELF triggers a transport-loss exception
    (rather than returning False), the recovery re-observe must NOT run.
    An unknown outcome from a check exception is identical to an unknown
    outcome from a False return: do not retry the action, do not re-check."""
    dispatches = []
    check_calls = []

    def fake_dispatch(kind, p, *, cfg=None):
        dispatches.append(p)
        return {"ok": True}

    def fake_check(*args, **kwargs):
        check_calls.append((args, kwargs))
        raise RuntimeError("Connection closed while reading from the driver")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check", fake_check)
    monkeypatch.setattr(E.time, "sleep", lambda _: None)

    outcome = E._run_step(
        _browser_step(postcondition={"type": "web_property",
                                     "selector": "#done", "nonempty": True}),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
    )

    # The recovery re-check would be a second call; it must not happen.
    assert len(check_calls) == 1
    # The action dispatched exactly once.
    assert len(dispatches) == 1
    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] == "postcondition"


def test_gate_closing_during_postcondition_recovers_to_unknown(monkeypatch):
    """If the gate closes DURING postcondition recovery, the executor must
    still report outcome_unknown (the action was already dispatched, so a
    normal cancellation would obscure the truth)."""
    dispatches = []
    checks = []
    gate_results = iter((True, True, False))  # open, open (post), closed (recovery)

    def fake_dispatch(kind, p, *, cfg=None):
        dispatches.append(p)
        return {"ok": True}

    def fake_check(*args, **kwargs):
        checks.append(args)
        return (False, "not yet")

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)
    monkeypatch.setattr(E.Cond, "check", fake_check)
    monkeypatch.setattr(E.time, "sleep", lambda _: None)

    outcome = E._run_step(
        _browser_step(postcondition={"type": "web_property",
                                     "selector": "#done", "nonempty": True}),
        cfg=object(), audit=AuditStub(), rec=_rec(),
        gate=lambda: next(gate_results),
    )

    assert len(dispatches) == 1
    assert len(checks) == 1
    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] in ("postcondition_recovery", "postcondition")


if __name__ == "__main__":
    # Standalone runner — pytest is the canonical entry point.
    import subprocess
    import sys as _sys
    raise SystemExit(subprocess.call(
        [_sys.executable, "-m", "pytest", "-xvs", __file__]))
