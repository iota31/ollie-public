"""P0: owner approval authority is isolated, bound, and single-use."""

import asyncio
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands.consent import Consent  # noqa: E402
from ollie_hands.auth import BearerMiddleware  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


def make_consent(*, attempts=12, window=60, timeout=1):
    cfg = SimpleNamespace(
        telegram_bot_token="", owner_chat_id="", confirm_timeout=timeout,
        approval_rate_limit_attempts=attempts,
        approval_rate_limit_window=window,
    )
    c = Consent(cfg, AuditStub())
    c._send = lambda _text: True
    return c


def begin(c, digest):
    existing = set(c._pending)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(c.confirm("do the thing", digest)),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not (set(c._pending) - existing) and time.monotonic() < deadline:
        time.sleep(0.005)
    new_codes = set(c._pending) - existing
    assert new_codes
    return thread, result, new_codes.pop()


def test_resolution_requires_matching_digest_and_is_single_use():
    c = make_consent()
    thread, result, code = begin(c, "a" * 16)
    assert c.resolve(code, True, "", "relay")["error_code"] == "digest_required"
    assert c.resolve(code, True, "b" * 16, "relay")["error_code"] == "digest_mismatch"
    assert c.resolve(code, True, "a" * 16, "relay")["ok"] is True
    thread.join(1)
    # confirm()'s contract is the (approved, ref) tuple; callers normalize it
    # to a bare decision at the boundary via normalize_consent_result.
    assert result == [(True, code)]
    assert c.resolve(code, True, "a" * 16, "relay")["error_code"] == "unknown_or_expired"


def test_rate_limit_applies_per_relay_client():
    c = make_consent(attempts=2)
    assert c.resolve("nope", True, "a" * 16, "relay-a")["error_code"] == "unknown_or_expired"
    assert c.resolve("nope", True, "a" * 16, "relay-a")["error_code"] == "unknown_or_expired"
    assert c.resolve("nope", True, "a" * 16, "relay-a")["error_code"] == "rate_limited"
    # A separate network principal has an independent bucket.
    assert c.resolve("nope", True, "a" * 16, "relay-b")["error_code"] == "unknown_or_expired"


def test_concurrent_prompts_are_isolated_by_challenge_and_digest():
    c = make_consent()
    t1, r1, code1 = begin(c, "1" * 16)
    t2, r2, code2 = begin(c, "2" * 16)
    assert code1 != code2
    assert len(code1) >= 20 and len(code2) >= 20
    assert c.resolve(code1, True, "2" * 16, "relay")["error_code"] == "digest_mismatch"
    assert c.resolve(code1, True, "1" * 16, "relay")["ok"] is True
    assert c.resolve(code2, False, "2" * 16, "relay")["ok"] is True
    t1.join(1)
    t2.join(1)
    assert r1 == [(True, code1)]
    assert r2 == [(False, code2)]


def test_concurrent_replay_has_exactly_one_winner():
    c = make_consent()
    waiter, decision, code = begin(c, "f" * 16)
    barrier = threading.Barrier(3)
    outcomes = []

    def resolve_once():
        barrier.wait()
        outcomes.append(c.resolve(code, True, "f" * 16, "relay"))

    racers = [threading.Thread(target=resolve_once) for _ in range(2)]
    for racer in racers:
        racer.start()
    barrier.wait()
    for racer in racers:
        racer.join(1)
    waiter.join(1)
    assert sum(outcome["ok"] for outcome in outcomes) == 1
    assert sorted(outcome.get("error_code", "ok") for outcome in outcomes) == [
        "ok", "unknown_or_expired"
    ]
    assert decision == [(True, code)]


def test_task_narration_edits_one_message_to_terminal_state(monkeypatch):
    c = make_consent()
    sent = []
    edited = []
    monkeypatch.setattr(c, "_send", lambda text: sent.append(text) or 42)
    monkeypatch.setattr(c, "_edit",
                        lambda message_id, text: edited.append((message_id, text)) or True)

    message_id = c.task_started("diagnose Reddit", 3)
    c.task_finished(message_id, "diagnose Reddit", status="escalated",
                    step=2, total=3, detail="driver closed")

    assert len(sent) == 1
    assert "running: diagnose Reddit · 0/3" in sent[0]
    assert edited == [(42, "🤖 Ollie (hands): stopped: diagnose Reddit · 2/3"
                            " · driver closed")]


def test_task_narration_sends_terminal_fallback_when_edit_fails(monkeypatch):
    c = make_consent()
    sent = []
    monkeypatch.setattr(c, "_send", lambda text: sent.append(text) or 42)
    monkeypatch.setattr(c, "_edit", lambda *_args: False)

    message_id = c.task_started("probe", 2)
    c.task_finished(message_id, "probe", status="ok", step=2, total=2)

    assert len(sent) == 2
    assert "done: probe · 2/2" in sent[1]


async def call_auth(path, token):
    called = []
    messages = []

    async def inner(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerMiddleware(inner, "mcp-secret", "approval-secret")
    scope = {
        "type": "http", "path": path,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1234),
    }
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return called, messages[0]["status"]


def test_mcp_bearer_is_denied_on_consent_route():
    called, status = asyncio.run(call_auth("/consent", "mcp-secret"))
    assert called == []
    assert status == 401
    called, status = asyncio.run(call_auth("/consent", "approval-secret"))
    assert called == ["/consent"]
    assert status == 204
    # Conversely, approval authority does not grant MCP authority.
    called, status = asyncio.run(call_auth("/mcp", "approval-secret"))
    assert called == []
    assert status == 401
