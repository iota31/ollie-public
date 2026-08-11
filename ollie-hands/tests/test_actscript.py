"""Act-script schema regression tests (pure, no Windows)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from ollie_hands import actscript as A, policy as P  # noqa: E402


def test_auto_script():
    s = A.parse({"title": "read", "steps": [
        {"id": "s1", "kind": "shell", "args": {"command": "Get-Volume"}},
        {"id": "s2", "kind": "clipboard", "args": {"op": "read"}},
    ]})
    assert s.consent.consent == P.AUTO
    assert s.blocked_step is None


def test_overall_is_max_tier():
    s = A.parse({"title": "mix", "steps": [
        {"id": "a", "kind": "shell", "args": {"command": "Get-Date"}},
        {"id": "b", "kind": "shell", "args": {"command": "Restart-Computer -WhatIf"},
         "postcondition": {"type": "shell_exit_zero"}},
    ]})
    assert s.consent.consent == P.CONFIRM


def test_blocked_detected():
    s = A.parse({"title": "bad", "steps": [
        {"id": "x", "kind": "shell", "args": {"command": "Stop-Service WinDefend"},
         "postcondition": {"type": "shell_exit_zero"}},
    ]})
    assert s.consent.consent == P.BLOCKED and s.blocked_step == "x"


def test_write_requires_postcondition():
    with pytest.raises(A.ScriptError):
        A.parse({"title": "t", "steps": [
            {"id": "c", "kind": "uia", "args": {"op": "invoke", "name": "OK"}}]})


def test_read_needs_no_postcondition():
    A.parse({"title": "t", "steps": [
        {"id": "c", "kind": "uia", "args": {"op": "get_text", "name": "X"}}]})


@pytest.mark.parametrize("field,value", [
    ("preconditions", [{"type": "uia_text_contains", "contains": "Done"}]),
    ("postcondition", {"type": "browser_value_equals", "equals": "x"}),
])
def test_unsupported_condition_type_rejected_before_execution(field, value):
    step = {"id": "c", "kind": "browser", "args": {"op": "extract"},
            field: value}
    with pytest.raises(A.ScriptError, match="unsupported .*condition type"):
        A.parse({"title": "t", "steps": [step]})


@pytest.mark.parametrize("bad", [
    {}, {"steps": []}, {"steps": [{"kind": "magic", "args": {}}]},
    {"steps": [{"id": "d", "kind": "uia",
                "args": {"op": "get_text", "name": "X"}, "on_fail": "nope"}]},
    {"steps": [{"id": "a", "kind": "shell", "args": {"command": "Get-Date"}},
               {"id": "a", "kind": "shell", "args": {"command": "Get-Host"}}]},
    # captcha + pixels are now valid kinds
])
def test_malformed_rejected(bad):
    with pytest.raises(A.ScriptError):
        A.parse(bad)


def test_hash_stable_and_sensitive():
    mk = lambda c: A.parse({"title": "t", "steps": [
        {"id": "s1", "kind": "shell", "args": {"command": c}}]}).hash
    assert mk("Get-Date") == mk("Get-Date")
    assert mk("Get-Date") != mk("Get-Host")


def test_captcha_and_pixels_valid_in_scripts():
    # captcha step (write) requires a postcondition
    s = A.parse({"title": "cap", "steps": [
        {"id": "c1", "kind": "captcha",
         "args": {"task": {"type": "ReCaptchaV2TaskProxyless", "websiteURL": "u", "websiteKey": "k"}},
         "postcondition": {"type": "shell_exit_zero"}}]})
    assert s.consent.consent in (P.NOTIFY, P.CONFIRM)

    # pixels cursor query is a read (no postcond needed)
    s2 = A.parse({"title": "pix", "steps": [
        {"id": "p1", "kind": "pixels", "args": {"op": "cursor_pos"}}]})
    assert s2.consent.consent == P.AUTO


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
