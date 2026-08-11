import json
import threading
from types import SimpleNamespace

from ollie_hands import actscript, engine, executor, observe, policy
from ollie_hands.consent import Consent, consent_post_response
from ollie_hands.mode import BYPASS, NORMAL, Mode


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


class ConsentStub:
    def __init__(self, approved=True):
        self.approved = approved
        self.calls = 0

    def confirm(self, *_args, **_kwargs):
        self.calls += 1
        return self.approved

    def notify(self, *_args, **_kwargs):
        pass


def _cfg():
    return SimpleNamespace(nocaptcha_api_key="")


def test_direct_confirm_uses_normal_consent_and_bypass_skips_it(monkeypatch):
    monkeypatch.setattr(engine, "_classify",
                        lambda *_: policy.Decision("T3", policy.CONFIRM, "external"))
    dispatches = []
    monkeypatch.setattr(engine, "_dispatch",
                        lambda *args, **kwargs: dispatches.append(args) or {"ok": True})
    mode = Mode()
    consent = ConsentStub(approved=False)
    denied = engine.act_step("browser", {"op": "click"}, cfg=_cfg(),
                             audit=AuditStub(), consent=consent, mode=mode)
    assert denied["status"] == "denied"
    assert consent.calls == 1
    assert dispatches == []

    mode.set(BYPASS)
    result = engine.act_step("browser", {"op": "click"}, cfg=_cfg(),
                             audit=AuditStub(), consent=consent, mode=mode)
    assert result["status"] == "ok"
    assert consent.calls == 1
    assert len(dispatches) == 1


def test_blocked_direct_action_never_dispatches_in_bypass(monkeypatch):
    monkeypatch.setattr(engine, "_classify",
                        lambda *_: policy.Decision("T4", policy.BLOCKED, "security"))
    monkeypatch.setattr(engine, "_dispatch",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("blocked action dispatched")))
    mode = Mode()
    mode.set(BYPASS)
    result = engine.act_step("shell", {"command": "blocked"}, cfg=_cfg(),
                             audit=AuditStub(), consent=ConsentStub(), mode=mode)
    assert result["status"] == "blocked"


def test_confirm_plan_skips_only_in_bypass(monkeypatch):
    script = actscript.parse({"title": "commit", "steps": [{
        "id": "s1", "kind": "browser",
        "args": {"op": "click", "selector": "button", "commit": True},
        "postcondition": {"type": "web_url", "contains": "done"},
    }]})
    monkeypatch.setattr(observe, "last_input_tick", lambda: 0)
    monkeypatch.setattr(executor, "_run_step",
                        lambda *args, **kwargs: {"id": "s1", "status": "ok"})
    consent = ConsentStub(approved=False)
    mode = Mode()
    normal = executor.run(script, cfg=_cfg(), audit=AuditStub(), consent=consent,
                          mode=mode, gate=lambda: True)
    assert normal["status"] == "denied"
    mode.set(BYPASS)
    bypass = executor.run(script, cfg=_cfg(), audit=AuditStub(), consent=consent,
                          mode=mode, gate=lambda: True)
    assert bypass["status"] == "ok"
    assert consent.calls == 1


def test_atomic_approve_and_bypass_consumes_ref(monkeypatch):
    audit = AuditStub()
    cfg = SimpleNamespace(confirm_timeout=30, approval_rate_limit_window=60,
                          approval_rate_limit_attempts=12)
    consent = Consent(cfg, audit)
    pending = consent.begin_confirm("post", "digest")
    mode = Mode()
    # The challenge is digest-bound: resolution must present the exact digest,
    # matching the strict contract the live relay satisfies via the pending
    # inventory (and the owner via the typed "approve <ref> <digest>" reply).
    response = consent_post_response(
        {"ref": pending.ref, "approve": True, "enable_bypass": True,
         "script_hash": "digest"},
        consent, mode=mode,
    )
    assert response.status_code == 200
    assert json.loads(response.body)["mode"] == BYPASS
    assert pending.approved is True
    assert pending.event.is_set()
    assert mode.get() == BYPASS
    replay = consent_post_response(
        {"ref": pending.ref, "approve": True, "enable_bypass": True,
         "script_hash": "digest"},
        consent, mode=mode,
    )
    assert replay.status_code == 404


def test_failed_atomic_bypass_does_not_change_mode():
    audit = AuditStub()
    cfg = SimpleNamespace(confirm_timeout=30, approval_rate_limit_window=60,
                          approval_rate_limit_attempts=12)
    consent = Consent(cfg, audit)
    mode = Mode()
    response = consent_post_response(
        {"ref": "H-missing", "approve": True, "enable_bypass": True},
        consent, mode=mode,
    )
    assert response.status_code == 404
    assert mode.get() == NORMAL
