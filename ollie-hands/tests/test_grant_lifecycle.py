"""executor.run() grant lifecycle: approve-before-issue, deny issues nothing.

The scope-consent branch in executor.run was written directly on the box and
had no repo coverage. Its security-relevant claims are:

  * an authorization plan WITHOUT a grant_id must get owner approval of the
    SCOPE SUMMARY before any grant exists — even when the step tiers are only
    NOTIFY, which would otherwise never prompt;
  * a denial (or timeout) issues NOTHING;
  * reusing a grant_id re-validates against the store and never re-prompts;
  * a denial expressed as a (False, ref) tuple is not read as approval (P0-A).
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import actscript as A  # noqa: E402
from ollie_hands import executor as E  # noqa: E402
from ollie_hands import grants as G  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def statuses(self):
        return [kw.get("status") for _, kw in self.events]


class ConsentSpy:
    """Records prompts and returns a canned (approved, ref) tuple."""

    def __init__(self, decision):
        self.decision = decision
        self.prompts = []
        self.notifications = []

    def confirm(self, preview, script_hash=""):
        self.prompts.append((preview, script_hash))
        return (self.decision, "H-ref123")

    def notify(self, message):
        self.notifications.append(message)

    def task_started(self, title, steps):
        self.notifications.append(f"started:{title}")
        return 1

    def task_finished(self, *a, **k):
        self.notifications.append("finished")


AUTH = {"family": "read the news", "resources": ["https://example.com"],
        "effects": ["navigation"], "ttl_seconds": 600}


def _script(authorization=None, grant_id=None):
    auth = dict(authorization) if authorization else None
    if auth is not None and grant_id:
        auth["grant_id"] = grant_id
    return A.parse({
        "title": "t",
        "steps": [{"id": "s1", "kind": "browser",
                   "args": {"op": "goto", "url": "https://example.com/"}}],
        "authorization": auth,
    })


def _host_stubs(monkeypatch):
    """The step loop reads Windows-only host state; stub it for a POSIX runner."""
    from ollie_hands import observe as obs
    from ollie_hands import pixels as L3

    monkeypatch.setattr(obs, "last_input_tick", lambda: 0)
    monkeypatch.setattr(L3, "last_injected_tick", lambda: 0)


def _run(script, consent, store, monkeypatch, audit=None):
    _host_stubs(monkeypatch)
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: {"url": "https://example.com/"})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/")
    return E.run(script, cfg=object(), audit=audit or AuditStub(),
                 consent=consent, grant_store=store)


def test_scope_is_approved_before_any_grant_is_issued(monkeypatch):
    store = G.GrantStore(AuditStub())
    consent = ConsentSpy(True)
    script = _script(AUTH)
    # A bare goto plan is only NOTIFY-tier, so without the scope gate it would
    # never prompt at all.
    assert script.consent.consent != "confirm"

    summary = _run(script, consent, store, monkeypatch)

    assert len(consent.prompts) == 1, "owner must be asked exactly once"
    preview, digest = consent.prompts[0]
    assert "https://example.com" in preview          # the origins being granted
    assert "single-use" in preview                   # the commit allowance
    assert digest == script.hash                     # bound to this exact plan
    assert summary["grant_id"] in store._grants


def test_a_denied_scope_issues_nothing(monkeypatch):
    store = G.GrantStore(AuditStub())
    consent = ConsentSpy(False)
    audit = AuditStub()

    summary = _run(_script(AUTH), consent, store, monkeypatch, audit=audit)

    assert summary["status"] == "denied"
    assert "grant_id" not in summary
    assert store._grants == {}, "a denial must not leave a usable grant behind"
    assert "denied" in audit.statuses()


def test_a_tuple_denial_is_not_read_as_approval(monkeypatch):
    """P0-A: (False, 'H-ref') is a non-empty tuple and therefore truthy."""
    store = G.GrantStore(AuditStub())

    class TupleDenial(ConsentSpy):
        def confirm(self, preview, script_hash=""):
            self.prompts.append((preview, script_hash))
            return (False, "H-ref123")

    consent = TupleDenial(False)
    summary = _run(_script(AUTH), consent, store, monkeypatch)

    assert summary["status"] == "denied"
    assert store._grants == {}


def test_an_unrecognised_consent_shape_fails_closed(monkeypatch):
    store = G.GrantStore(AuditStub())

    class WeirdConsent(ConsentSpy):
        def confirm(self, preview, script_hash=""):
            self.prompts.append((preview, script_hash))
            return "yes please"

    summary = _run(_script(AUTH), WeirdConsent(True), store, monkeypatch)
    assert summary["status"] == "denied"
    assert store._grants == {}


def test_reusing_a_valid_grant_does_not_re_prompt(monkeypatch):
    store = G.GrantStore(AuditStub())
    scope = G.Scope.parse(AUTH)
    grant = store.issue(scope)

    consent = ConsentSpy(True)
    summary = _run(_script(AUTH, grant_id=grant.id), consent, store, monkeypatch)

    assert consent.prompts == [], "an approved scope is not re-approved"
    assert summary["status"] == "ok"


def test_reusing_an_unknown_grant_is_rejected_without_running(monkeypatch):
    store = G.GrantStore(AuditStub())
    consent = ConsentSpy(True)
    audit = AuditStub()

    dispatched = []
    _host_stubs(monkeypatch)
    monkeypatch.setattr(E.Eng, "_dispatch",
                        lambda *a, **k: dispatched.append(a) or {"url": "https://example.com/"})
    monkeypatch.setattr(E, "_live_browser_url", lambda: "https://example.com/")

    summary = E.run(_script(AUTH, grant_id="no-such-grant"), cfg=object(),
                    audit=audit, consent=consent, grant_store=store)

    assert summary["status"] == "grant_rejected"
    assert dispatched == []
    assert consent.prompts == []
    assert "grant_rejected" in audit.statuses()


def test_reusing_an_expired_grant_is_rejected(monkeypatch):
    clock = [1000.0]
    store = G.GrantStore(AuditStub(), clock=lambda: clock[0])
    grant = store.issue(G.Scope.parse({**AUTH, "ttl_seconds": 30}))
    clock[0] += 31

    summary = E.run(_script({**AUTH, "ttl_seconds": 30}, grant_id=grant.id),
                    cfg=object(), audit=AuditStub(), consent=ConsentSpy(True),
                    grant_store=store)

    assert summary["status"] == "grant_rejected"


def test_plans_without_an_authorization_are_unaffected(monkeypatch):
    """The grant machinery must not change the tiering of ordinary plans."""
    store = G.GrantStore(AuditStub())
    consent = ConsentSpy(True)
    summary = _run(_script(None), consent, store, monkeypatch)

    assert summary["status"] == "ok"
    assert consent.prompts == []
    assert store._grants == {}
