"""Regression test for tuple-shaped inline consent decisions in single actions."""

from types import SimpleNamespace

from ollie_hands import engine
from ollie_hands import policy


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class ConsentStub:
    def confirm(self, preview, script_hash=""):
        assert preview
        assert script_hash
        return True, "H-AbC123"

    def notify(self, message):
        raise AssertionError(f"unexpected notification: {message}")


def test_single_action_accepts_approved_inline_consent_tuple(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_classify",
        lambda kind, params: policy.Decision("T3", policy.CONFIRM, "owner approval"),
    )
    dispatched = []
    monkeypatch.setattr(
        engine,
        "_dispatch",
        lambda kind, params, cfg=None: dispatched.append((kind, params)) or {"done": True},
    )

    result = engine.act_step(
        "browser",
        {"op": "fill", "selector": "#username", "value": "ollie"},
        cfg=SimpleNamespace(nocaptcha_api_key=""),
        audit=AuditStub(),
        consent=ConsentStub(),
    )

    assert result["status"] == "ok"
    assert dispatched == [("browser", {"op": "fill", "selector": "#username", "value": "ollie"})]


def test_single_action_rejects_denied_inline_consent_tuple(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_classify",
        lambda kind, params: policy.Decision("T3", policy.CONFIRM, "owner approval"),
    )
    dispatched = []
    monkeypatch.setattr(
        engine,
        "_dispatch",
        lambda kind, params, cfg=None: dispatched.append((kind, params)) or {"done": True},
    )

    class DeniedConsentStub(ConsentStub):
        def confirm(self, preview, script_hash=""):
            assert preview
            assert script_hash
            return False, "H-AbC123"

    result = engine.act_step(
        "browser",
        {"op": "fill", "selector": "#username", "value": "ollie"},
        cfg=SimpleNamespace(nocaptcha_api_key=""),
        audit=AuditStub(),
        consent=DeniedConsentStub(),
    )

    assert result["status"] == "denied"
    assert dispatched == []
