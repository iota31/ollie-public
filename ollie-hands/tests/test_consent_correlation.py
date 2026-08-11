"""Focused tests for Telegram approval-lineage correlation logging.

These tests cover the audit events emitted at the Hands Telegram send
boundary (consent.deliver_pending):

  - keyboard_accepted: Telegram accepted the inline keyboard; we record the
    message_id, the H-ref, and the exact callback namespace/version/sample.
  - keyboard_rejected: Telegram returned a definitive markup rejection; we
    record the H-ref and lineage before falling back to plain text.
  - keyboard_send_failed: ambiguous send failure; we record the H-ref and
    lineage and DO NOT auto-approve.
  - plain_accepted: a plain-text fallback or overlong-ref send succeeded;
    we record the H-ref and message_id.

The tests never log a token, approval credential, action digest, or any
other secret, and they preserve fail-closed behavior (ambiguous send =
no auto-approval, pending stays intact).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ollie_hands.consent import Consent, _SendResult  # noqa: E402


class AuditStub:
    """Captures every audit.event call for assertion."""

    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


def make_consent(audit=None):
    if audit is None:
        audit = AuditStub()
    cfg = SimpleNamespace(
        telegram_bot_token="",
        owner_chat_id="",
        confirm_timeout=1,
        approval_rate_limit_attempts=12,
        approval_rate_limit_window=60,
    )
    return Consent(cfg, audit)


def _events_with(audit, status):
    """Filter audit events by status field."""
    return [e for e in audit.events if e[1].get("status") == status]


def _arg(ev, key):
    """Pull a single field out of the args dict from an event tuple."""
    return ev[1].get("args", {}).get(key)


# --- keyboard_accepted: Telegram accepted the keyboard ---------------------

def test_deliver_pending_emits_keyboard_accepted_with_message_id_and_lineage(monkeypatch):
    """When Telegram returns 200 with a message_id and the keyboard,
    deliver_pending must emit a keyboard_accepted audit event with the
    H-ref, Telegram message_id, callback namespace/version, and a sample
    of the approve/deny callback payloads (NOT the digest, NOT the token).
    """
    audit = AuditStub()
    c = make_consent(audit)

    def fake_swr(text, reply_markup=None):
        # Real-lineage keyboard path: success + concrete Telegram message_id.
        return _SendResult(success=True, message_id=424242)

    monkeypatch.setattr(c, "_send_with_result", fake_swr)

    pc = c.begin_confirm("do X", "digest_xxxxxxxx", ref_prefix="H-")
    ok = c.deliver_pending(pc.ref)
    assert ok is True
    # Pending remains intact for resolution.
    assert pc.ref in c._pending

    accepts = _events_with(audit, "keyboard_accepted")
    assert len(accepts) == 1, f"expected exactly one keyboard_accepted, got {accepts}"
    ev = accepts[0]
    # Tool name and detail contain no secrets.
    assert ev[0] == "confirm"
    detail = ev[1].get("detail", "")
    assert "digest_xxxxxxxx" not in detail, "detail must not contain action digest"
    # args carry the correlation fields.
    assert _arg(ev, "ref") == pc.ref
    assert _arg(ev, "message_id") == 424242
    assert _arg(ev, "cb_ns") == "ollie_approval"
    assert _arg(ev, "cb_ver") == "v1"
    a = _arg(ev, "cb_a")
    d = _arg(ev, "cb_d")
    assert a.startswith("ollie_approval:v1:a:") and a.endswith(pc.ref)
    assert d.startswith("ollie_approval:v1:d:") and d.endswith(pc.ref)
    # No secret values leak via the sample payloads.
    assert "digest_xxxxxxxx" not in a and "digest_xxxxxxxx" not in d


# --- keyboard_rejected: definitive markup rejection -> plain fallback ------

def test_deliver_pending_emits_keyboard_rejected_and_plain_accepted_on_fallback(monkeypatch):
    """Definitive markup rejection triggers fallback to plain text. Both
    states must emit correlation events with the H-ref and lineage, and
    plain_accepted must carry the plain-text message_id.
    """
    audit = AuditStub()
    c = make_consent(audit)

    def fake_swr(text, reply_markup=None):
        if reply_markup is not None:
            return _SendResult(success=False, definitive_rejection=True)
        return _SendResult(success=True, message_id=999)

    monkeypatch.setattr(c, "_send_with_result", fake_swr)
    monkeypatch.setattr(c, "_send", lambda text, reply_markup=None: 999)

    pc = c.begin_confirm("do Y", "digest_yyyyyyyy", ref_prefix="H-")
    ok = c.deliver_pending(pc.ref)
    assert ok is True
    assert pc.ref in c._pending

    rejected = _events_with(audit, "keyboard_rejected")
    assert len(rejected) == 1
    rev = rejected[0]
    assert _arg(rev, "ref") == pc.ref
    assert _arg(rev, "cb_ns") == "ollie_approval"
    assert _arg(rev, "cb_ver") == "v1"
    assert "digest_yyyyyyyy" not in rev[1].get("detail", "")

    plains = _events_with(audit, "plain_accepted")
    assert len(plains) == 1
    pev = plains[0]
    assert _arg(pev, "ref") == pc.ref
    assert _arg(pev, "message_id") == 999
    assert _arg(pev, "mode") == "plain_fallback"


# --- keyboard_send_failed: ambiguous failure -> no auto-approval ------------

def test_deliver_pending_emits_keyboard_send_failed_and_does_not_auto_approve(monkeypatch):
    """Ambiguous send failure must NOT trigger fallback or auto-approval.
    It must emit a correlation event with the H-ref and lineage.
    """
    audit = AuditStub()
    c = make_consent(audit)

    def fake_swr(text, reply_markup=None):
        return _SendResult(success=False, ambiguous_failure=True)

    monkeypatch.setattr(c, "_send_with_result", fake_swr)

    pc = c.begin_confirm("do Z", "digest_zzzzzzzz", ref_prefix="H-")
    ok = c.deliver_pending(pc.ref)
    assert ok is False
    # Pending remains intact (fail-closed: no auto-approval).
    assert pc.ref in c._pending

    failed = _events_with(audit, "keyboard_send_failed")
    assert len(failed) == 1
    fev = failed[0]
    assert _arg(fev, "ref") == pc.ref
    assert _arg(fev, "cb_ns") == "ollie_approval"
    assert _arg(fev, "cb_ver") == "v1"


# --- plain_accepted: overlong-ref path goes straight to plain --------------

def test_deliver_pending_emits_plain_accepted_for_overlong_ref(monkeypatch):
    """An overlong H-ref cannot fit a keyboard payload (limit 64 bytes), so
    the keyboard is omitted and we send plain text. The plain_accepted
    event must carry the H-ref and the resulting Telegram message_id.
    """
    audit = AuditStub()
    c = make_consent(audit)

    long_ref = "H-" + "x" * 60
    # Pre-create the pending entry directly under the long ref.
    import time as _t
    from ollie_hands.consent import PendingConsent

    with c._lock:
        c._pending[long_ref] = PendingConsent(long_ref, "do W", "", _t.monotonic() + 60)

    monkeypatch.setattr(c, "_send", lambda text, reply_markup=None: 12345)

    ok = c.deliver_pending(long_ref)
    assert ok is True
    assert long_ref in c._pending

    plains = _events_with(audit, "plain_accepted")
    assert len(plains) == 1
    pev = plains[0]
    assert _arg(pev, "ref") == long_ref
    assert _arg(pev, "message_id") == 12345
    assert _arg(pev, "mode") == "plain_no_keyboard"