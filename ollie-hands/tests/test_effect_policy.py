"""Cross-modality consequence-policy parity regression tests."""

import pytest

from ollie_hands import actscript as A
from ollie_hands import policy as P


LOCAL = {"scope": "local", "commit": False}
EXTERNAL = {"scope": "external", "commit": True}


@pytest.mark.parametrize(("kind", "op", "extra"), [
    ("uia", "invoke", {}),
    ("pixels", "click", {}),
    ("pixels", "key", {"key": "Space"}),
])
def test_ambiguous_mutation_without_effect_confirms(kind, op, extra):
    assert P.classify_action(kind, op, **extra).consent == P.CONFIRM


@pytest.mark.parametrize(("kind", "op", "extra"), [
    ("uia", "invoke", {}),
    ("pixels", "click", {}),
    ("pixels", "key", {"key": "Space"}),
])
def test_caller_local_label_cannot_downgrade_ambiguous_mutation(kind, op, extra):
    assert P.classify_action(kind, op, effect=LOCAL, **extra).consent == P.CONFIRM


@pytest.mark.parametrize(("kind", "op", "extra"), [
    ("uia", "invoke", {}),
    ("pixels", "click", {}),
    ("pixels", "key", {"key": "Space"}),
])
def test_external_effect_confirms_across_modalities(kind, op, extra):
    assert P.classify_action(kind, op, effect=EXTERNAL, **extra).consent == P.CONFIRM


@pytest.mark.parametrize("key", ["Enter", "Return", "NumpadEnter"])
def test_enter_always_confirms_even_when_declared_local(key):
    assert P.classify_browser("press", effect=LOCAL, key=key).consent == P.CONFIRM
    assert P.classify_action("pixels", "key", effect=LOCAL, key=key).consent == P.CONFIRM


def test_shell_unknown_mutation_cannot_be_downgraded_by_local_label():
    assert P.classify_shell("Set-Content a.txt hello").consent == P.CONFIRM
    assert P.classify_shell("Set-Content a.txt hello", effect=LOCAL).consent == P.CONFIRM


@pytest.mark.parametrize("command", [
    "git push origin main",
    "Send-MailMessage -To a@example.com -Subject hi",
    "Invoke-RestMethod https://example.com -Method Post -Body x",
    "curl.exe https://example.com --request DELETE",
])
def test_recognized_external_shell_commit_cannot_be_laundered_as_local(command):
    assert P.classify_shell(command, effect=LOCAL).consent == P.CONFIRM


def test_invalid_effect_envelope_fails_closed():
    assert P.classify_action("uia", "invoke", effect={"scope": "safe"}).consent == P.CONFIRM
    assert P.classify_action("pixels", "click", effect="local").consent == P.CONFIRM


@pytest.mark.parametrize(("kind", "args"), [
    ("uia", {"op": "invoke", "name": "Continue"}),
    ("pixels", {"op": "click", "x": 10, "y": 10}),
    ("pixels", {"op": "key", "key": "Enter", "effect": LOCAL}),
    ("shell", {"command": "Set-Content a.txt hello"}),
])
def test_act_script_omission_or_enter_escalates_whole_script(kind, args):
    script = A.parse({"title": "commit parity", "steps": [{
        "id": "s1", "kind": kind, "args": args,
        "postcondition": {"type": "file_exists", "path": "C:\\sentinel"},
    }]})
    assert script.consent.consent == P.CONFIRM


def test_act_script_binds_explicit_external_effect_into_hash_and_consent():
    base = {"title": "click", "steps": [{
        "id": "s1", "kind": "uia",
        "args": {"op": "invoke", "name": "Continue", "effect": LOCAL},
        "postcondition": {"type": "uia_exists", "name": "Done"},
    }]}
    local = A.parse(base)
    external_plan = {"title": "click", "steps": [{
        **base["steps"][0],
        "args": {**base["steps"][0]["args"], "effect": EXTERNAL},
    }]}
    external = A.parse(external_plan)
    assert local.consent.consent == P.CONFIRM
    assert external.consent.consent == P.CONFIRM
    assert local.hash != external.hash
