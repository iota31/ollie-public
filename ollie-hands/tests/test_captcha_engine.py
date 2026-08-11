"""Engine-level captcha path: policy gate, secret key resolution, audit masking.

Pure logic; no network and no Windows deps.
"""

import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import engine as E
from ollie_hands import policy as P
from ollie_hands import config as Cfg


class FakeAudit:
    def __init__(self):
        self.events = []
    def event(self, *a, **k):
        self.events.append((a, k))


class FakeConsent:
    def __init__(self, *, auto=True):
        self.auto = auto
        self.notified = []
        self.confirmed = []
    def confirm(self, preview):
        self.confirmed.append(preview)
        return self.auto
    def notify(self, msg):
        self.notified.append(msg)


def _cfg_with_key(key: str = "test-key-123"):
    cfg = Cfg.Config()
    # Tests exercise the captcha path itself, so the engine must be enabled;
    # a fresh Config() is inert (enabled=False) and the kill-switch check
    # correctly masks every other outcome. The kill switch must stay first:
    # a disabled engine does nothing at all, including captcha solves.
    cfg.enabled = True
    cfg.nocaptcha_api_key = key
    return cfg


def test_captcha_preview_masks_key():
    p = {"task": {"type": "ReCaptchaV2TaskProxyless", "websiteURL": "https://ex", "websiteKey": "K"}}
    prev = E._preview("captcha", p)
    assert "key" not in prev.lower()
    assert "ReCaptchaV2TaskProxyless" in prev or "captcha" in prev.lower()


def test_captcha_classify_commit_vs_default():
    d1 = E._classify("captcha", {"task": {"type": "X"}})
    assert d1.consent == P.NOTIFY
    d2 = E._classify("captcha", {"task": {"type": "X"}, "commit": True})
    assert d2.consent == P.CONFIRM


def test_captcha_blocked_without_key(monkeypatch):
    cfg = _cfg_with_key("")
    audit = FakeAudit()
    cons = FakeConsent(auto=True)

    out = E.act_step("captcha", {"task": {"type": "X"}}, cfg=cfg, audit=audit, consent=cons)
    assert out["status"] == "blocked"
    assert "not configured" in (out.get("error") or "").lower()


def test_captcha_routes_to_captcha_module_and_masks_result():
    cfg = _cfg_with_key("abc")
    audit = FakeAudit()
    cons = FakeConsent(auto=True)

    fake_result = {"status": "solved", "solution": {"gRecaptchaResponse": "TOK"}}

    def fake_solve(task, *, client_key, timeout):
        assert client_key == "abc", f"expected host key 'abc', got {client_key!r}"
        assert task.get("type") == "ReCaptchaV2TaskProxyless"
        return fake_result

    # Patch the name as bound inside the engine module to guarantee interception.
    with __import__('unittest.mock').mock.patch("ollie_hands.engine.Cap.solve", fake_solve):
        out = E.act_step("captcha", {"task": {"type": "ReCaptchaV2TaskProxyless", "websiteURL": "u", "websiteKey": "k"}},
                         cfg=cfg, audit=audit, consent=cons)
    assert out["status"] == "ok"
    # The token must be returned (caller needs it to submit/inject).
    assert out.get("result", {}).get("solution", {}).get("gRecaptchaResponse") == "TOK"
    # Audit must never contain the host API key.
    s = json.dumps(audit.events, default=str)
    assert "abc" not in s


if __name__ == "__main__":
    test_captcha_preview_masks_key()
    test_captcha_classify_commit_vs_default()
    # The last two need pytest for monkeypatch; run via pytest to include them.
    print("captcha engine (pure) tests passed (run with pytest for the monkeypatch cases)")