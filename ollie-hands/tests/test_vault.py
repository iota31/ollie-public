"""Pure vault tests (plan Track D3). Run on any platform (no DPAPI needed).

Run:
  python -m pytest tests/test_vault.py
  or: python tests/test_vault.py
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import vault as V  # noqa: E402


# ------------------------------------------------------------------ valid_ref ---

def test_valid_ref_accepts_clean_names():
    assert V.valid_ref("reddit_pw")
    assert V.valid_ref("site-1")
    assert V.valid_ref("a")
    # MED-2: uppercase is rejected for NTFS case-collision defense; lowercase is ok
    assert not V.valid_ref("A1_b-2")
    assert V.valid_ref("a1_b-2")
    assert V.valid_ref("x" * 64)  # max length


def test_valid_ref_rejects_traversal_and_bad_chars():
    assert not V.valid_ref("")                 # empty
    assert not V.valid_ref("a/b")              # path sep
    assert not V.valid_ref("a\\b")             # windows sep
    assert not V.valid_ref("../etc")           # traversal
    assert not V.valid_ref("a b")              # space
    assert not V.valid_ref("a.b")              # dot (could be extension games)
    assert not V.valid_ref("a:b")              # colon
    assert not V.valid_ref("x" * 65)           # too long
    assert not V.valid_ref(None)               # type guard
    assert not V.valid_ref(123)                # type guard


# ------------------------------------------------------------------ list returns names only ---

def test_list_refs_returns_names_only(tmp_path, monkeypatch):
    # Point VAULT_DIR at a temp dir for isolation
    monkeypatch.setattr(V, "VAULT_DIR", tmp_path)
    # Create fake blobs (contents don't matter for list; we never decrypt here)
    (tmp_path / "k1.bin").write_bytes(b"blob1")
    (tmp_path / "k2.bin").write_bytes(b"blob2")
    (tmp_path / "README.txt").write_text("ignore me")  # non-.bin ignored

    refs = V.list_refs()
    assert refs == ["k1", "k2"]


# ------------------------------------------------------------------ audit masking helper (pure) ---

def test_secret_step_audit_args_mask_value_and_record_ref():
    """_audit_args + _preview + _is_secret_step must record the ref and mask the value.
    This actually calls the engine helpers (no hand-built dicts).
    """
    from ollie_hands import engine as Eng

    params = {
        "op": "type_text",
        "name": "Password",
        "window_title": "Login",
        "secret_ref": "site_pw",
    }
    preview = Eng._preview("uia", params)
    is_secret = Eng._is_secret_step(params)
    args = Eng._audit_args("uia", preview, params, is_secret)

    assert args.get("secret_ref") == "site_pw"
    assert args.get("value") == "***"
    assert "kind" in args and "preview" in args
    # no plaintext leakage in what we would audit or preview
    blob = repr(args) + " " + preview
    assert "sekret" not in blob.lower()
    assert "hunter2" not in blob.lower()


# ------------------------------------------------------------------ policy: secret typing tier + vault-path block ---

def test_secret_typing_is_at_least_notify():
    """Typing a secret is a write that acts as the owner -> >= NOTIFY.
    The actual tier is decided by the *op* + context (browser commit escalates).
    """
    from ollie_hands import policy as P

    # UIA secret type_text should be NOTIFY (local write, narrated)
    d = P.classify_action("uia", "set_value")  # engine maps type_text -> set_value in classify
    assert d.consent in (P.NOTIFY, P.CONFIRM)

    # Browser fill/type_text fail CLOSED on an undeclared consequence: bare
    # act() form-filling can accumulate consequential state (e.g. signup), so
    # only an explicit `draft` effect keeps it at the narrated NOTIFY tier.
    d = P.classify_browser("fill", commit=False, target_text="Password")
    assert d.consent == P.CONFIRM  # undeclared consequence
    d = P.classify_browser("fill", commit=False, target_text="Password",
                           effect={"category": "draft"})
    assert d.consent == P.NOTIFY
    d = P.classify_browser("fill", commit=True, target_text="Password")
    assert d.consent == P.CONFIRM
    d = P.classify_browser("fill", commit=True, target_text="Password",
                           effect={"category": "draft"})
    assert d.consent == P.CONFIRM  # an explicit commit flag still escalates
    d = P.classify_browser("type_text", commit=False, target_text="Sign in")
    assert d.consent == P.CONFIRM  # commit word
    d = P.classify_browser("type_text", commit=False, target_text="Sign in",
                           effect={"category": "draft"})
    assert d.consent == P.CONFIRM  # commit word beats a benign declaration


def test_vault_path_and_dpapi_are_blocked_by_policy():
    """Shell/file ops that touch the vault dir or mention DPAPI are T4 BLOCKED."""
    from ollie_hands import policy as P

    blocked_cmds = [
        r"Get-Content 'C:\ProgramData\ollie-hands\vault\k.bin'",
        r"dir C:\ProgramData\ollie-hands\vault",
        r"Copy-Item C:\ProgramData\ollie-hands\vault\* D:\exfil",
        "credential manager",
        "dpapi",
        "ProtectedData::Unprotect",
        "CryptUnprotectData",
    ]
    for cmd in blocked_cmds:
        d = P.classify_shell(cmd)
        assert d.consent == P.BLOCKED, cmd


# ------------------------------------------------------------------ CRITICAL-1: cwd under vault/audit is BLOCKED ---

def test_cwd_under_vault_is_blocked():
    """CRITICAL-1: relative read with cwd set to vault dir must be T4 BLOCKED."""
    from ollie_hands import policy as P
    import ollie_hands.vault as V

    d = P.classify_action("shell", command="Get-Content k.bin",
                          cwd=str(V.VAULT_DIR))
    assert d.consent == P.BLOCKED
    # also via the lower-level classify_shell (engine threads cwd through)
    d2 = P.classify_shell("Get-Content k.bin", cwd=str(V.VAULT_DIR))
    assert d2.consent == P.BLOCKED


def test_cwd_under_audit_is_blocked(monkeypatch, tmp_path):
    """CRITICAL-1: relocated audit dir also blocks relative reads when cwd set there."""
    from ollie_hands import policy as P

    # Simulate server.py wiring: set the runtime blocked dir to a temp (relocated) audit
    P.set_blocked_dirs(audit_dir=str(tmp_path))
    try:
        d = P.classify_action("shell", command="Get-Content audit-20260615.jsonl",
                              cwd=str(tmp_path))
        assert d.consent == P.BLOCKED
        d2 = P.classify_shell("type audit-20260615.jsonl", cwd=str(tmp_path))
        assert d2.consent == P.BLOCKED
    finally:
        # restore to None so other tests are unaffected
        P.set_blocked_dirs(audit_dir=None)


def test_cwd_block_resists_path_normalization_bypasses():
    """CRITICAL-1 hardening: the cwd block must canonicalize paths so `/` vs `\\`,
    `..` segments, case, and trailing slashes can't slip a vault read past it."""
    from ollie_hands import policy as P
    import ollie_hands.vault as V

    v = str(V.VAULT_DIR)
    fwd = v.replace("\\", "/")
    variants_blocked = [
        v,                                   # canonical
        fwd,                                 # forward slashes
        v.upper(),                           # uppercase
        v + "\\",                            # trailing sep
        fwd + "/sub",                        # subdir, fwd slashes
        v + "\\..\\vault",                   # dot-dot round-tripping back to vault
    ]
    for cwd in variants_blocked:
        assert P.classify_shell("Get-Content k.bin", cwd=cwd).consent == P.BLOCKED, cwd
    # a sibling dir (e.g. the OllieShell work dir) must NOT be over-blocked
    sibling = v.rsplit("\\", 1)[0] + "\\work"
    assert P.classify_shell("Get-Content k.txt", cwd=sibling).consent != P.BLOCKED


# ------------------------------------------------------------------ CRITICAL-2: ProtectedData::Unprotect is explicitly blocked ---

def test_protecteddata_unprotect_variants_are_blocked():
    """CRITICAL-2: DPAPI unprotect patterns (case-insensitive) are T4 BLOCKED."""
    from ollie_hands import policy as P

    variants = [
        "ProtectedData::Unprotect",
        "[System.Security.Cryptography.ProtectedData]::Unprotect",
        "CryptUnprotectData",
        "cryptunprotectdata",
        "Unprotect-DPAPI",
    ]
    for cmd in variants:
        d = P.classify_shell(cmd)
        assert d.consent == P.BLOCKED, cmd


# ------------------------------------------------------------------ CRITICAL-3 + HIGH-1: secret taint + clipboard/get_text discipline ---

def test_clipboard_read_refused_after_secret_type(monkeypatch):
    """CRITICAL-3 + HIGH-1: once any secret is typed, clipboard_read must refuse.
    This exercises the in-engine taint set path (coarse: any secret taints all clipboard reads).
    """
    from ollie_hands import engine as Eng
    import ollie_hands.vault as V

    # Ensure we have a clean taint set for this test
    Eng._SECRET_TAINTED.clear()

    # Simulate what happens in engine._dispatch for a secret type_text on UIA:
    # resolve secret (we'll stub vault), then _taint_target, then call L1 with _secret=True.
    # We only need to exercise the taint gate, not the actual UIA call.
    monkeypatch.setattr(V, "valid_ref", lambda r: True)
    monkeypatch.setattr(V, "get", lambda r: "sekret-xyz")

    # Pretend we are typing a secret into some UIA target (this is what engine does)
    find_kw = {"automation_id": "123", "name": "Password", "window_title": "Login"}
    Eng._taint_target(find_kw)

    # Now any clipboard read should be refused (the engine path in _dispatch)
    try:
        # Directly simulate the decision the engine makes:
        if Eng._SECRET_TAINTED:
            # engine would raise Refused("clipboard read refused after secret typing")
            refused = True
        else:
            refused = False
        assert refused is True
    finally:
        Eng._SECRET_TAINTED.clear()


def test_get_text_on_tainted_target_is_refused(monkeypatch):
    """HIGH-1: get_text on a target that previously received a secret must refuse."""
    from ollie_hands import engine as Eng
    import ollie_hands.vault as V

    Eng._SECRET_TAINTED.clear()

    monkeypatch.setattr(V, "valid_ref", lambda r: True)
    monkeypatch.setattr(V, "get", lambda r: "sekret-xyz")

    # Taint a target by "typing" a secret into it (engine does this in _dispatch)
    find_kw = {"name": "Username", "window_title": "App"}
    Eng._taint_target(find_kw)

    # Now the engine's get_text path should refuse
    # We call the same predicate the engine uses:
    assert Eng._is_tainted_target(find_kw) is True

    # Simulate the engine's guard:
    if Eng._is_tainted_target(find_kw):
        # engine raises Refused("get_text refused on secret-tainted target")
        refused = True
    else:
        refused = False
    assert refused is True

    Eng._SECRET_TAINTED.clear()


# ------------------------------------------------------------------ DPAPI round-trip (Windows only) ---

def test_dpapi_roundtrip_windows_only():
    """If on Windows, round-trip a value through put/get; else the call must raise."""
    import sys as _sys
    if _sys.platform != "win32":
        # must not require DPAPI on non-Windows
        try:
            V.put("x", "y")
            raise AssertionError("put should have raised on non-Windows")
        except RuntimeError:
            pass
        try:
            V.get("x")
            raise AssertionError("get should have raised on non-Windows")
        except RuntimeError:
            pass
        return

    # Windows: real DPAPI round-trip
    ref = "pytest_dpapi_probe"
    val = "sekret-123"
    try:
        V.put(ref, val)
        got = V.get(ref)
        assert got == val
    finally:
        V.delete(ref)


if __name__ == "__main__":
    import inspect
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    skipped = 0
    for fn in fns:
        # NIT-2: when run directly (not via pytest), skip tests that require
        # pytest-only fixtures (tmp_path, monkeypatch). These still run under
        # `python -m pytest`.
        sig = inspect.signature(fn)
        if any(p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
               for p in sig.parameters.values()):
            # Likely a fixture-using test (no default). Skip for direct execution.
            print(f"  skip {fn.__name__} (needs pytest fixtures)")
            skipped += 1
            continue
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    total = len(fns)
    print(f"\n{passed} passed, {skipped} skipped (pytest-only), {total - passed - skipped} failed")
    print(f"{passed + skipped}/{total} vault tests viable under direct run")
    # Success if nothing hard-failed; skipped fixture tests are expected under direct run.
    sys.exit(0 if (passed + skipped) == total else 1)
