"""Consent hardening: one definition, digest-bound pendings, sanitized audit refs.

Covers three fixes made during the box back-port:
  * `Consent.confirm` was defined TWICE; the first was silently shadowed and
    therefore dead. Only one definition may exist.
  * `begin_confirm` now fails closed without an action digest — an unbound
    pending is resolvable with the ref alone, which is exactly the
    digest-binding bypass the gate exists to prevent.
  * refs arrive from the network, so a malformed one is never echoed verbatim
    into the audit trail.
"""

import ast
import pathlib
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import consent as C  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def refs(self):
        return [kw.get("args", {}).get("ref") for _, kw in self.events]

    def details(self):
        return [kw.get("detail", "") for _, kw in self.events]


def make_consent(audit):
    cfg = SimpleNamespace(telegram_bot_token="t", owner_chat_id="1",
                          confirm_timeout=60,
                          approval_rate_limit_window=60,
                          approval_rate_limit_attempts=12)
    return C.Consent(cfg, audit)


# ------------------------------------------------- no shadowed definitions ---

def test_consent_class_has_no_duplicate_method_definitions():
    """A shadowed duplicate is dead code that misleads readers about which
    approval flow is actually live."""
    tree = ast.parse(pathlib.Path(C.__file__).read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "Consent")
    counts = Counter(n.name for n in cls.body if isinstance(n, ast.FunctionDef))
    assert [name for name, n in counts.items() if n > 1] == []
    assert counts["confirm"] == 1


# ------------------------------------------------------ digest-bound only ---

def test_begin_confirm_requires_an_action_digest():
    c = make_consent(AuditStub())
    with pytest.raises(ValueError, match="digest"):
        c.begin_confirm("do X")
    with pytest.raises(ValueError, match="digest"):
        c.begin_confirm("do X", "")
    assert c._pending == {}, "a rejected challenge must not be stored"


def test_a_digest_bound_pending_cannot_be_resolved_by_ref_alone():
    c = make_consent(AuditStub())
    pc = c.begin_confirm("do X", "a" * 16)
    assert c.resolve(pc.ref, True, "")["error_code"] == "digest_required"
    assert c.resolve(pc.ref, True, "b" * 16)["error_code"] == "digest_mismatch"
    assert c.resolve(pc.ref, True, "a" * 16)["ok"] is True


def test_refs_carry_full_entropy():
    """token_urlsafe(16) == 128 bits; a guessable ref is a capability leak."""
    c = make_consent(AuditStub())
    refs = {c.begin_confirm("x", "a" * 16).ref for _ in range(50)}
    assert len(refs) == 50
    for ref in refs:
        assert ref.startswith("H-")
        assert len(ref) >= 20
        assert C.HANDS_REF_RE.fullmatch(ref)


def test_ref_prefix_is_keyword_only():
    """Prevents a positional third argument silently landing in ref_prefix."""
    c = make_consent(AuditStub())
    with pytest.raises(TypeError):
        c.begin_confirm("do X", "a" * 16, "H-")


# ------------------------------------------------------ audit sanitization ---

@pytest.mark.parametrize("hostile", [
    "H-ok\nconfirm status=approved ref=H-other",   # forged log line
    "H-ok status=approved",
    "../../etc/passwd",
    "H-" + "x" * 200,                              # over-length
    "notaref",
])
def test_hostile_refs_are_never_echoed_into_the_audit_trail(hostile):
    audit = AuditStub()
    c = make_consent(audit)
    c.resolve(hostile, True, "a" * 16, client_id="relay")

    assert hostile not in audit.refs()
    for detail in audit.details():
        assert hostile not in detail
        assert "\n" not in detail


def test_wellformed_refs_are_still_logged_for_correlation():
    audit = AuditStub()
    c = make_consent(audit)
    pc = c.begin_confirm("do X", "a" * 16)
    c.resolve(pc.ref, True, "a" * 16, client_id="relay")
    assert pc.ref in audit.refs()


def test_audit_ref_helper_accepts_only_the_minted_shape():
    assert C._audit_ref("H-abc_DEF-123") == "H-abc_DEF-123"
    for bad in ["", "abc", "H-", "H-bad ref", "H-bad\nref", None, 42,
                "h-lowercase"]:
        assert C._audit_ref(bad) == ""


def test_http_layer_rejects_a_malformed_ref_without_logging_it():
    audit = AuditStub()
    c = make_consent(audit)
    resp = C.consent_post_response({"ref": "H-ok\nforged", "approve": True}, c)
    assert resp.status_code == 400
    for detail in audit.details():
        assert "forged" not in detail
