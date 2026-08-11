"""Fail-closed consequence envelopes and the per-op effect allowlist.

The dual-envelope `_effect` and the browser fail-closed allowlist were written
directly on the box with no repo test coverage. The whole point of the envelope
is that an ABSENT or AMBIGUOUS declaration is never read as a benign one, so
these tests pin the negative cases rather than the happy path.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import policy as P  # noqa: E402
from ollie_hands import actscript as A  # noqa: E402


# ------------------------------------------------------------- _effect ---

def test_absent_envelope_is_valid_but_distinct_from_a_benign_one():
    """None means 'undeclared', which callers must NOT treat as low risk."""
    assert P._effect(None) == (None, False, True)


@pytest.mark.parametrize("category, commit", [
    ("observe", False), ("navigation", False), ("session_preference", False),
    ("draft", False), ("progress", False),
    ("external_commit", True), ("identity_commit", True), ("destructive", True),
])
def test_category_envelope_maps_commit_effects(category, commit):
    assert P._effect({"category": category}) == (category, commit, True)


@pytest.mark.parametrize("envelope", [
    "not-a-dict",
    123,
    {"category": "bogus"},                        # unknown category
    {"category": "draft", "scope": "local"},      # mixed legacy + current keys
    {"category": "draft", "commit": False},       # mixed legacy + current keys
    {"category": "draft", "extra": 1},            # unknown sibling key
    {"scope": "local", "extra": 1},               # unknown sibling key
    {"scope": "nowhere"},                         # unknown legacy scope
    {"scope": "local", "commit": "yes"},          # commit must be a real bool
    {},                                           # neither key present
])
def test_ambiguous_or_unknown_envelopes_fail_closed(envelope):
    assert P._effect(envelope) == (None, False, False)


def test_legacy_scope_envelope_still_parses():
    assert P._effect({"scope": "local", "commit": False}) == ("local", False, True)
    assert P._effect({"scope": "identity", "commit": True}) == ("identity", True, True)


def test_invalid_envelope_forces_confirm_on_browser_and_shell():
    assert P.classify_browser("click", effect={"category": "bogus"}).consent == P.CONFIRM
    assert P.classify_shell("dir", effect={"category": "bogus"}).consent == P.CONFIRM


# --------------------------------------------- per-op effect allowlist ---

@pytest.mark.parametrize("op", ["click", "select", "press", "fill", "type_text"])
def test_every_write_op_fails_closed_without_a_declaration(op):
    """Bare act() interaction with no declared consequence must ask first."""
    d = P.classify_browser(op)
    assert d.consent == P.CONFIRM
    assert "undeclared consequence" in d.reason


@pytest.mark.parametrize("op, category", [
    ("fill", "draft"),
    ("type_text", "draft"),
    ("click", "navigation"),
    ("click", "session_preference"),
    ("click", "progress"),
    ("select", "session_preference"),
    ("press", "navigation"),
])
def test_allowed_op_category_pairs_stay_narrated(op, category):
    assert P.classify_browser(op, effect={"category": category}).consent == P.NOTIFY


@pytest.mark.parametrize("op, category", [
    # A text input can never be a navigation or a commit.
    ("fill", "navigation"),
    ("fill", "progress"),
    ("fill", "external_commit"),
    ("type_text", "session_preference"),
    ("type_text", "identity_commit"),
    # A click cannot be a "draft".
    ("click", "draft"),
    ("click", "destructive"),
    ("select", "navigation"),
])
def test_incompatible_op_category_pairs_escalate(op, category):
    d = P.classify_browser(op, effect={"category": category})
    assert d.consent == P.CONFIRM


def test_commit_categories_always_confirm_regardless_of_op():
    for category in sorted(P.COMMIT_EFFECTS):
        for op in ["click", "fill", "press", "select", "type_text"]:
            assert P.classify_browser(op, effect={"category": category}).consent == P.CONFIRM


# ------------------------------------------------------- signup wording ---

@pytest.mark.parametrize("text", [
    "Sign up", "signup", "Sign Up Free", "Register", "Create account",
    "Join now", "Sign in", "Log in",
])
def test_account_creation_button_text_is_a_commit(text):
    """Creating an account acts as Tushar in the world; it must ask first even
    when the planner declared something reversible."""
    d = P.classify_browser("click", target_text=text,
                           effect={"category": "navigation"})
    assert d.consent == P.CONFIRM


# ----------------------------------------- actscript envelope validation ---

# Write steps must carry a postcondition (verify-after-act); that rule is
# checked before the authorization arithmetic, so supply one throughout.
_POST = {"type": "web_url", "contains": "example.com"}


def _plan(step_args, authorization=None, postcondition=None):
    step = {"id": "s1", "kind": "browser", "args": step_args}
    if postcondition is not None:
        step["postcondition"] = postcondition
    return {"title": "t", "steps": [step], "authorization": authorization}


def test_parse_rejects_an_invalid_effect_envelope():
    with pytest.raises(A.ScriptError, match="invalid browser effect envelope"):
        A.parse(_plan({"op": "click", "selector": "#x",
                       "effect": {"category": "draft", "scope": "local"}}))


def test_parse_rejects_a_legacy_scope_envelope_as_a_category():
    """Legacy scope values are not effect CATEGORIES; grants are category-only."""
    with pytest.raises(A.ScriptError, match="valid effect category"):
        A.parse(_plan({"op": "click", "selector": "#x",
                       "effect": {"scope": "external", "commit": True}}))


def test_parse_rejects_a_credentialed_or_non_web_url():
    for bad in ["https://user:pw@example.com/", "ftp://example.com/"]:
        with pytest.raises(A.ScriptError):
            A.parse(_plan({"op": "goto", "url": bad}))


def test_authorization_plan_cannot_reach_an_unlisted_origin():
    auth = {"family": "shop", "resources": ["https://example.com"],
            "effects": ["navigation"], "ttl_seconds": 600}
    with pytest.raises(A.ScriptError, match="outside authorization.resources"):
        A.parse(_plan({"op": "goto", "url": "https://evil.test/"}, auth))


def test_authorization_plan_cannot_exceed_declared_effects():
    auth = {"family": "shop", "resources": ["https://example.com"],
            "effects": ["navigation"], "ttl_seconds": 600}
    with pytest.raises(A.ScriptError, match="exceed authorization.effects"):
        A.parse(_plan({"op": "click", "selector": "#pay",
                       "effect": {"category": "external_commit"}}, auth,
                      postcondition=_POST))


def test_authorization_plan_defaults_required_resources_to_the_whole_scope():
    """A repair plan with no explicit URL is still bound to the approved
    origins — the executor's live-URL check has something to enforce."""
    auth = {"family": "shop", "resources": ["https://example.com"],
            "effects": ["draft"], "ttl_seconds": 600}
    script = A.parse(_plan({"op": "fill", "selector": "#q", "value": "x",
                            "effect": {"category": "draft"}}, auth,
                           postcondition=_POST))
    assert script.required_resources == {"https://example.com"}
    assert script.required_effects == {"draft"}


def test_authorization_is_currently_browser_only():
    plan = {"title": "t",
            "steps": [{"id": "s1", "kind": "shell", "args": {"command": "dir"}}],
            "authorization": {"family": "f", "resources": ["https://example.com"],
                              "effects": ["navigation"], "ttl_seconds": 600}}
    with pytest.raises(A.ScriptError, match="browser steps only"):
        A.parse(plan)
