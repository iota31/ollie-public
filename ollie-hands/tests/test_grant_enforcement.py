"""Executor-side enforcement of a scoped grant (Inv3 / Inv4a / Inv4b).

These guards were written directly on the box and had no repo test coverage.
They are the runtime half of the grant contract — parse-time validation alone
is worthless if the live page can drift out of scope between plan and click.

  Inv4a  before every non-goto browser step, the LIVE url must be an approved
         origin (exact match), else escalate WITHOUT dispatching.
  Inv4b  after a goto, the LANDED url must be an approved origin, else return
         outcome_unknown — the navigation already happened, so never re-dispatch.
  Inv3   the single-use commit allowance is reserved ATOMICALLY before the
         first commit dispatch, and the reservation stands whatever happens.
"""

import pathlib
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import executor as E  # noqa: E402
from ollie_hands import grants as G  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def statuses(self):
        return [kw.get("status") for _, kw in self.events]


def _rec():
    return {"abort": threading.Event()}


def _bstep(op, *, args=None, decision="notify", sid="s1"):
    a = {"op": op}
    a.update(args or {})
    return SimpleNamespace(
        id=sid, kind="browser", args=a, timeout=30, preconditions=[],
        postcondition=None, on_fail="escalate", checkpoint=False,
        decision=SimpleNamespace(consent=decision),
    )


def _ctx(store, resources=("https://example.com",), holder="task-a", gid="g1"):
    return E.GrantContext(store=store, grant_id=gid, holder=holder,
                          required_resources=set(resources))


class StoreStub:
    def __init__(self, raise_on_reserve=None):
        self.reservations = []
        self.raise_on_reserve = raise_on_reserve

    def reserve_commit(self, grant_id, holder):
        if self.raise_on_reserve:
            raise G.GrantError(self.raise_on_reserve)
        self.reservations.append((grant_id, holder))


# ------------------------------------------------- _enforce_live_resource ---

def test_enforce_live_resource_accepts_only_exact_origins():
    approved = {"https://example.com"}
    E._enforce_live_resource("https://example.com/deep/path?q=1", approved)  # ok


@pytest.mark.parametrize("url, reason", [
    ("https://example.com.attacker.test/x", "live_resource_out_of_scope"),
    ("https://evil.test", "live_resource_out_of_scope"),
    ("http://example.com", "live_resource_out_of_scope"),   # scheme is part of origin
    ("https://example.com:8443", "live_resource_out_of_scope"),  # port too
    ("https://sub.example.com", "live_resource_out_of_scope"),   # no subdomain wildcard
    (None, "live_url_unavailable"),
    ("", "live_url_unavailable"),
    # Garbage that still parses as a host is rejected by the membership test;
    # garbage that cannot be canonicalised at all is rejected earlier. Either
    # way the step never runs — that is the property under test.
    ("not a url at all", "live_resource_out_of_scope"),
    ("http://[", "live_url_unparseable"),
    ("https://", "live_url_unparseable"),
    ("ftp://example.com", "live_url_unparseable"),
])
def test_enforce_live_resource_fails_closed(url, reason):
    with pytest.raises(G.GrantError, match=reason):
        E._enforce_live_resource(url, {"https://example.com"})


def test_enforce_live_resource_is_a_no_op_without_required_resources():
    """Ungranted runs are governed by the ordinary consent tiers, not origins."""
    E._enforce_live_resource(None, set())


# ------------------------------------------------------------------ Inv4a ---

def test_inv4a_out_of_scope_live_url_blocks_dispatch(monkeypatch):
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://evil.test/login")

    audit = AuditStub()
    outcome = E._run_step(_bstep("click", args={"selector": "#next",
                                                "effect": {"category": "navigation"}}),
                          cfg=object(), audit=audit, rec=_rec(), gate=lambda: True,
                          grant_ctx=_ctx(StoreStub()))

    assert dispatched == []
    assert outcome["status"] == "escalate"
    assert outcome["stage"] == "live_resource"
    assert "live_resource_out_of_scope" in outcome["detail"]


def test_inv4a_unreadable_live_url_blocks_dispatch(monkeypatch):
    """If we cannot observe where the browser actually is, we do not act."""
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: None)

    outcome = E._run_step(_bstep("fill", args={"selector": "#q", "value": "x",
                                               "effect": {"category": "draft"}}),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True, grant_ctx=_ctx(StoreStub()))

    assert dispatched == []
    assert outcome["status"] == "escalate"
    assert "live_url_unavailable" in outcome["detail"]


def test_inv4a_allows_an_in_scope_live_url(monkeypatch):
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/cart")

    outcome = E._run_step(_bstep("fill", args={"selector": "#q", "value": "x",
                                               "effect": {"category": "draft"}}),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True, grant_ctx=_ctx(StoreStub()))

    assert len(dispatched) == 1
    assert outcome["status"] == "ok"


def test_inv4a_does_not_gate_goto(monkeypatch):
    """goto is how you legitimately arrive; its destination is checked at parse
    time and its LANDING is checked by Inv4b."""
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: {"url": "https://example.com/"})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://evil.test")

    outcome = E._run_step(_bstep("goto", args={"url": "https://example.com/"}),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True, grant_ctx=_ctx(StoreStub()))

    assert outcome["status"] == "ok"


# ------------------------------------------------------------------ Inv4b ---

def test_inv4b_out_of_scope_landing_is_outcome_unknown_not_a_retry(monkeypatch):
    dispatched = []

    def fake_dispatch(*a, **k):
        dispatched.append(a)
        return {"url": "https://evil.test/phish"}

    monkeypatch.setattr(E.Eng, "_dispatch", fake_dispatch)

    audit = AuditStub()
    outcome = E._run_step(_bstep("goto", args={"url": "https://example.com/"}),
                          cfg=object(), audit=audit, rec=_rec(), gate=lambda: True,
                          grant_ctx=_ctx(StoreStub()))

    # Dispatched exactly once and NEVER repeated.
    assert len(dispatched) == 1
    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] == "landed_resource"
    assert "not repeated" in outcome["detail"]
    assert "outcome_unknown" in audit.statuses()


def test_inv4b_falls_back_to_the_live_url_when_dispatch_reports_none(monkeypatch):
    monkeypatch.setattr(E.Eng, "_dispatch", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://evil.test/")

    outcome = E._run_step(_bstep("goto", args={"url": "https://example.com/"}),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True, grant_ctx=_ctx(StoreStub()))

    assert outcome["status"] == "outcome_unknown"
    assert outcome["stage"] == "landed_resource"


# ------------------------------------------------------------------- Inv3 ---

def test_inv3_commit_allowance_is_reserved_before_the_commit_dispatch(monkeypatch):
    order = []
    store = StoreStub()

    def fake_reserve(gid, holder):
        order.append("reserve")
        store.reservations.append((gid, holder))

    store.reserve_commit = fake_reserve
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: order.append("dispatch") or {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/")

    outcome = E._run_step(
        _bstep("click", args={"selector": "#pay",
                              "effect": {"category": "external_commit"}},
               decision="confirm"),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
        grant_ctx=_ctx(store))

    assert order == ["reserve", "dispatch"], "reservation must precede dispatch"
    assert outcome["status"] == "ok"
    assert store.reservations == [("g1", "task-a")]


def test_inv3_a_lost_reservation_prevents_the_dispatch_entirely(monkeypatch):
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/")

    outcome = E._run_step(
        _bstep("click", args={"selector": "#pay",
                              "effect": {"category": "external_commit"}},
               decision="confirm"),
        cfg=object(), audit=AuditStub(), rec=_rec(), gate=lambda: True,
        grant_ctx=_ctx(StoreStub(raise_on_reserve="commit_already_consumed")))

    assert dispatched == []
    assert outcome["status"] == "grant_rejected"
    assert outcome["stage"] == "commit_reserve"
    assert "commit_already_consumed" in outcome["detail"]


def test_non_commit_steps_do_not_burn_the_allowance(monkeypatch):
    store = StoreStub()
    monkeypatch.setattr(E.Eng, "_dispatch", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/")

    outcome = E._run_step(_bstep("fill", args={"selector": "#q", "value": "x",
                                               "effect": {"category": "draft"}}),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True, grant_ctx=_ctx(store))

    assert store.reservations == []
    assert outcome["status"] == "ok"


@pytest.mark.parametrize("category", ["external_commit", "identity_commit",
                                      "destructive"])
def test_every_commit_effect_category_reserves(category):
    ctx = _ctx(StoreStub())
    assert ctx.step_is_commit(_bstep("click", args={"effect": {"category": category}}))


@pytest.mark.parametrize("category", ["observe", "navigation", "draft",
                                      "progress", "session_preference"])
def test_reversible_effect_categories_do_not_reserve(category):
    ctx = _ctx(StoreStub())
    assert not ctx.step_is_commit(_bstep("click", args={"effect": {"category": category}}))


def test_an_invalid_effect_envelope_is_treated_as_commit_bearing():
    """Fail closed: an unparseable declaration must not slip past reservation."""
    ctx = _ctx(StoreStub())
    assert ctx.step_is_commit(_bstep("click", args={"effect": {"category": "bogus"}}))
    assert ctx.step_is_commit(_bstep("click", args={"effect": "not-a-dict"}))
    assert ctx.step_is_commit(
        _bstep("click", args={"effect": {"category": "draft", "scope": "local"}}))


def test_non_browser_steps_are_never_commit_bearing():
    ctx = _ctx(StoreStub())
    shell = SimpleNamespace(kind="shell", args={"command": "dir"})
    assert not ctx.step_is_commit(shell)


# ------------------------------------------------- runtime text escalation ---

def test_live_button_text_escalates_past_a_benign_declaration(monkeypatch):
    """A hijacked planner declaring `navigation` on a button that actually reads
    "Place order" must not launder the commit past the gate."""
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E.Eng.L2, "element_text", lambda sel: "Place order")

    outcome = E._run_step(_bstep("click", args={"selector": "#go",
                                                "effect": {"category": "navigation"}},
                                 decision="notify"),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True)

    assert dispatched == []
    assert outcome["status"] == "escalate"
    assert outcome["stage"] == "runtime"
    assert "runtime_effect_escalated" in outcome["detail"]


def test_runtime_escalation_does_not_re_gate_an_already_confirmed_step(monkeypatch):
    """The owner already approved a CONFIRM step; re-escalating would deadlock."""
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})
    monkeypatch.setattr(E.Eng.L2, "element_text", lambda sel: "Place order")

    outcome = E._run_step(_bstep("click", args={"selector": "#go"},
                                 decision="confirm"),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True)

    assert len(dispatched) == 1
    assert outcome["status"] == "ok"


def test_unreadable_element_text_still_fails_closed_on_undeclared_effect(monkeypatch):
    """element_text blowing up must not become an implicit approval."""
    dispatched = []
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"ok": True})

    def boom(sel):
        raise RuntimeError("dom gone")

    monkeypatch.setattr(E.Eng.L2, "element_text", boom)

    outcome = E._run_step(_bstep("click", args={"selector": "#go"},
                                 decision="notify"),
                          cfg=object(), audit=AuditStub(), rec=_rec(),
                          gate=lambda: True)

    assert dispatched == []
    assert outcome["status"] == "escalate"
    assert outcome["stage"] == "runtime"
