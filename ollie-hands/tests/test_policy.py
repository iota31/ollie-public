"""Policy regression tests — the hard gate must never silently loosen.

Run anywhere (pure, no Windows deps):  python -m pytest tests/test_policy.py
or:  python tests/test_policy.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import policy as P  # noqa: E402

SHELL = [
    ("Get-Volume", P.AUTO),
    ("Get-PSDrive C", P.AUTO),
    ("whoami /all", P.AUTO),
    (r"dir C:\Users", P.AUTO),
    ("Get-ChildItem | Sort-Object Name", P.AUTO),
    ("Get-Process; Get-Service", P.AUTO),
    ("Get-PSDrive C | Select-Object Used,Free | Format-List", P.AUTO),
    ("Get-Service | Format-Table -AutoSize", P.AUTO),
    ("format-volume -DriveLetter D", P.CONFIRM),
    ("format D:", P.CONFIRM),
    ("Get-Content x.txt > out.txt", P.NOTIFY),
    ("New-Item foo.txt", P.NOTIFY),
    ("Rename-Item a b", P.NOTIFY),
    ("Start-Process notepad", P.NOTIFY),
    (r"Set-MpPreference -DisableRealtimeMonitoring $true", P.BLOCKED),
    ("netsh advfirewall set allprofiles state off", P.BLOCKED),
    ("Disable-BitLocker -MountPoint C:", P.BLOCKED),
    ("Stop-Service WinDefend", P.BLOCKED),
    (r"Remove-Item C:\ -Recurse -Force", P.CONFIRM),
    (r"Remove-Item -Force -Recurse C:\data", P.CONFIRM),
    ("shutdown /r /t 0", P.CONFIRM),
    ("Restart-Computer", P.CONFIRM),
    (r"reg delete HKLM\Software\X", P.CONFIRM),
    ("format-volume -DriveLetter D", P.CONFIRM),
    ("Stop-Process -Name chrome", P.CONFIRM),
    ("Stop-Process -Id 1234", P.NOTIFY),
    (r"del /s C:\temp", P.CONFIRM),
    (r"echo C:\ProgramData\ollie-hands\audit > x", P.BLOCKED),
]

ACTIONS = [
    ("observe", "", P.AUTO),
    ("clipboard_read", "", P.AUTO),
    ("uia", "get_text", P.AUTO),
    ("uia", "locate", P.AUTO),       # grounding query is read-only
    ("uia", "invoke", P.NOTIFY),
    ("uia", "set_value", P.NOTIFY),
    ("window", "focus", P.NOTIFY),
    ("window", "close", P.NOTIFY),
    ("clipboard_write", "", P.NOTIFY),
    ("pixels", "cursor_pos", P.AUTO),  # L3 read
    ("pixels", "move", P.NOTIFY),      # L3 raw input — local, narrated
    ("pixels", "click", P.NOTIFY),
    ("pixels", "drag", P.NOTIFY),
    ("pixels", "type_text", P.NOTIFY),
    ("pixels", "key", P.NOTIFY),
    ("captcha", "", P.NOTIFY),           # external solve — narrated by default (commit=True tested separately)
]


def test_shell():
    for cmd, exp in SHELL:
        actual = P.classify_action("shell", command=cmd).consent
        # Arbitrary shell mutation now fails closed. Historical NOTIFY cases
        # remain documented in the table but are intentionally tightened.
        if exp == P.NOTIFY:
            exp = P.CONFIRM
        assert actual == exp, cmd


def test_actions():
    for kind, op, exp in ACTIONS:
        actual = P.classify_action(kind, op).consent
        if exp == P.NOTIFY and kind in (
            "uia", "window", "clipboard", "clipboard_write", "pixels"
        ) and not (kind == "pixels" and op == "move"):
            exp = P.CONFIRM
        assert actual == exp, f"{kind}.{op}"


def test_external_marker():
    assert P.classify_action("uia", "invoke", external=True).consent == P.CONFIRM


def test_unknown_kind_confirms():
    assert P.classify_action("teleport").consent == P.CONFIRM


def test_captcha_commit_escalates():
    # Without commit: narrated (T2)
    d = P.classify_action("captcha", "")
    assert d.consent == P.NOTIFY
    # With commit=True: acts-as-Tushar → confirm (T3)
    d2 = P.classify_action("captcha", "", commit=True)
    assert d2.consent == P.CONFIRM


# --- P0-B: dangerous read-listed commands must NOT classify as T0 auto ---

P0B_SHELL = [
    # P0-B regression: these were T0 auto — must be T3 confirm
    ("wmic process call create calc.exe", P.CONFIRM),
    ("echo $(Get-Process)", P.CONFIRM),
    ("where.exe /R C:\\evil.exe", P.CONFIRM),
    # safe forms must remain T0 auto
    ("wmic os get caption", P.AUTO),
    ("echo hello world", P.AUTO),
    ("where.exe python", P.AUTO),
]


def test_p0b_dangerous_read_commands():
    """P0-B: wmic process call create, echo $(subexpr), where.exe /R must not be T0."""
    for cmd, exp in P0B_SHELL:
        actual = P.classify_action("shell", command=cmd).consent
        assert actual == exp, f"P0-B: {cmd!r} expected {exp}, got {actual}"


# --- P0-C: approval.token must be blocked under T4 ---

P0C_BLOCKED_PATHS = [
    r"ProgramData\ollie-hands\approval.token",
    r"C:\ProgramData\ollie-hands\approval.token",
    r"c:\programdata\ollie-hands\approval.token",
]


def test_p0c_approval_token_blocked():
    """P0-C: reading the approval token file is T4 blocked (authority-split credential)."""
    for path in P0C_BLOCKED_PATHS:
        d = P.classify_action("shell", command=f"type {path}")
        assert d.tier == "T4", f"P0-C: {path!r} should be T4, got {d.tier}"
        assert d.consent == P.BLOCKED, f"P0-C: {path!r} should be BLOCKED, got {d.consent}"


def test_p0c_bearer_token_still_blocked():
    """P0-C: bearer.token was already blocked — verify no regression."""
    d = P.classify_action("shell", command=r"type ProgramData\ollie-hands\bearer.token")
    assert d.tier == "T4"
    assert d.consent == P.BLOCKED


if __name__ == "__main__":
    test_shell(); test_actions(); test_external_marker(); test_unknown_kind_confirms()
    test_captcha_commit_escalates(); test_p0b_dangerous_read_commands()
    test_p0c_approval_token_blocked(); test_p0c_bearer_token_still_blocked()
    print("all policy tests passed")
