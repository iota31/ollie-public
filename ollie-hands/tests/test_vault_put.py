"""Pure tests for vault-put.py CLI (plan Track D3).

Run on any platform (no DPAPI needed). Mocks vault functions to test CLI behavior.

Run:
  python -m pytest tests/test_vault_put.py -q
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from ollie_hands import vault as V

# Load the hyphenated script via importlib so tests can call its functions directly.
_script_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "vault-put.py"
_spec = importlib.util.spec_from_file_location("vault_put_mod", _script_path)
VP = importlib.util.module_from_spec(_spec)
# Provide a package context so its internal relative sys.path.insert still works if executed.
# We execute the module so top-level code (imports, etc.) runs.
_spec.loader.exec_module(VP)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ helpers ---

def _make_args(cmd: str, ref: str | None = None, stdin: bool = False) -> argparse.Namespace:
    """Build a minimal argparse.Namespace matching what build_parser produces."""
    ns = argparse.Namespace()
    ns.cmd = cmd
    if ref is not None:
        ns.ref = ref
    ns.stdin = stdin
    # Attach a no-op func; tests call the cmd_* functions directly.
    ns.func = None
    return ns


def _cap(func, *a, **k):
    """Capture stdout+stderr while calling func; return (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = func(*a, **k)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ principal banner (always printed) ---

def test_main_prints_principal_banner(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    rc, out, err = _cap(VP.main, ["list"])
    # Banner must appear; we don't assert exact wording beyond key signals.
    assert "principal:" in out
    assert "DPAPI user-scope" in out or "user-scope" in out
    # list with no refs should succeed and print nothing else (empty vault)
    assert rc == 0


# ------------------------------------------------------------------ ref validation ---

@pytest.mark.parametrize("bad_ref", [
    "", "A1", "Mixed", "with space", "a/b", "a\\b", "../x", "a.b", "a:b", "x" * 65, "toolong" + "x" * 60
])
def test_put_rejects_bad_refs(monkeypatch, bad_ref):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    # Even if secret reading would happen, validation must fail first.
    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref=bad_ref))
    assert rc == 1
    assert "invalid ref" in err.lower() or "invalid ref" in out.lower()
    # Ensure the bad ref string itself is mentioned for clarity.
    blob = (out + err).lower()
    # Not all bad refs are safe to echo, but at least one indicator should appear.
    assert "invalid" in blob


def test_delete_rejects_bad_ref(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    rc, out, err = _cap(VP.cmd_delete, _make_args("delete", ref="BadRef"))
    assert rc == 1
    assert "invalid ref" in (out + err).lower()


def test_verify_rejects_bad_ref(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    rc, out, err = _cap(VP.cmd_verify, _make_args("verify", ref="no/slash"))
    assert rc == 1
    assert "invalid ref" in (out + err).lower()


# ------------------------------------------------------------------ secret input (stdin vs prompt) ---

def test_put_reads_from_stdin_when_flag(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    calls = []
    def fake_put(ref, value):
        calls.append(("put", ref, value))
    def fake_get(ref):
        calls.append(("get", ref))
        return "sekret-from-stdin"

    monkeypatch.setattr(V, "put", fake_put)
    monkeypatch.setattr(V, "get", fake_get)

    # Provide stdin content
    monkeypatch.setattr(sys, "stdin", io.StringIO("sekret-from-stdin\n"))

    args = _make_args("put", ref="site_pw", stdin=True)
    rc, out, err = _cap(VP.cmd_put, args)

    assert rc == 0
    assert ("put", "site_pw", "sekret-from-stdin") in calls
    assert ("get", "site_pw") in calls
    # Secret must not appear in output
    blob = out + err
    assert "sekret-from-stdin" not in blob


def test_put_rejects_empty_from_stdin(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))  # empty after strip

    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="x", stdin=True))
    assert rc == 1
    assert "empty" in (out + err).lower()


def test_put_rejects_empty_from_prompt(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="x", stdin=False))
    assert rc == 1
    assert "empty" in (out + err).lower()


# ------------------------------------------------------------------ put round-trip behavior ---

def test_put_calls_put_then_get_and_succeeds(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekret-xyz")

    order = []
    def fake_put(ref, value):
        order.append("put")
        assert ref == "k"
        assert value == "sekret-xyz"
    def fake_get(ref):
        order.append("get")
        assert ref == "k"
        return "sekret-xyz"

    monkeypatch.setattr(V, "put", fake_put)
    monkeypatch.setattr(V, "get", fake_get)

    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="k", stdin=False))
    assert rc == 0
    assert order == ["put", "get"]
    # Must not leak secret
    blob = out + err
    assert "sekret-xyz" not in blob
    # Success message must mention verified decryptable and user
    assert "verified decryptable" in out
    assert "alice" in out


def test_put_fails_loudly_on_roundtrip_mismatch(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekret-1")

    monkeypatch.setattr(V, "put", lambda ref, value: None)
    monkeypatch.setattr(V, "get", lambda ref: "different-value")

    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="k", stdin=False))
    assert rc == 2
    assert "round-trip mismatch" in (out + err).lower()
    # Still must not leak the attempted secret
    assert "sekret-1" not in (out + err)


def test_put_fails_on_vault_put_exception(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekret")

    def boom(ref, value):
        raise RuntimeError("disk full")
    monkeypatch.setattr(V, "put", boom)

    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="k", stdin=False))
    assert rc == 2
    assert "vault.put failed" in (out + err).lower()


def test_put_fails_on_vault_get_exception_after_put(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekret")

    monkeypatch.setattr(V, "put", lambda ref, value: None)
    monkeypatch.setattr(V, "get", lambda ref: (_ for _ in ()).throw(RuntimeError("decrypt fail")))

    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="k", stdin=False))
    assert rc == 2
    assert "round-trip get failed" in (out + err).lower()


# ------------------------------------------------------------------ list ---

def test_list_prints_only_names(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    monkeypatch.setattr(V, "list_refs", lambda: ["a", "b", "c"])

    rc, out, err = _cap(VP.cmd_list, _make_args("list"))
    assert rc == 0
    assert out.strip().splitlines() == ["a", "b", "c"]
    # list must never try to fetch values; if it did and we didn't stub get, it would fail.
    # (no additional assertion needed — absence of crash + only names printed is the check)


def test_list_handles_empty(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "list_refs", lambda: [])
    rc, out, err = _cap(VP.cmd_list, _make_args("list"))
    assert rc == 0
    assert out.strip() == ""


def test_list_propagates_error(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "list_refs", lambda: (_ for _ in ()).throw(RuntimeError("io")))
    rc, out, err = _cap(VP.cmd_list, _make_args("list"))
    assert rc == 2
    assert "list_refs failed" in (out + err).lower()


# ------------------------------------------------------------------ delete ---

def test_delete_reports_removed(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "delete", lambda ref: True)
    rc, out, err = _cap(VP.cmd_delete, _make_args("delete", ref="k"))
    assert rc == 0
    assert "deleted: k" in out


def test_delete_reports_not_found(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "delete", lambda ref: False)
    rc, out, err = _cap(VP.cmd_delete, _make_args("delete", ref="k"))
    assert rc == 0
    assert "not found: k" in out


def test_delete_propagates_error(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "delete", lambda ref: (_ for _ in ()).throw(RuntimeError("perm")))
    rc, out, err = _cap(VP.cmd_delete, _make_args("delete", ref="k"))
    assert rc == 2
    assert "delete failed" in (out + err).lower()


# ------------------------------------------------------------------ verify ---

def test_verify_success_without_printing_plaintext(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    secret = "sekret-verify"
    monkeypatch.setattr(V, "get", lambda ref: secret)

    rc, out, err = _cap(VP.cmd_verify, _make_args("verify", ref="k"))
    assert rc == 0
    assert "SUCCESS" in out
    assert "len=11" in out or "len=" in out
    # Must not contain the actual secret
    assert secret not in out and secret not in err


def test_verify_failure(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(V, "get", lambda ref: (_ for _ in ()).throw(RuntimeError("missing")))
    rc, out, err = _cap(VP.cmd_verify, _make_args("verify", ref="k"))
    assert rc == 2
    assert "FAILURE" in (out + err)


# ------------------------------------------------------------------ secret must never appear in output across commands ---

def test_no_secret_in_any_output_path(monkeypatch):
    """Across put/list/delete/verify, the secret value must never leak to stdout/stderr."""
    secret = "HUNTER2"
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    # put path (stdin)
    monkeypatch.setattr(V, "put", lambda ref, value: None)
    monkeypatch.setattr(V, "get", lambda ref: secret)
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    rc, out, err = _cap(VP.cmd_put, _make_args("put", ref="k", stdin=True))
    blob = out + err
    assert secret not in blob

    # verify path
    rc, out, err = _cap(VP.cmd_verify, _make_args("verify", ref="k"))
    blob = out + err
    assert secret not in blob

    # list path (should not touch values at all)
    monkeypatch.setattr(V, "list_refs", lambda: ["k"])
    rc, out, err = _cap(VP.cmd_list, _make_args("list"))
    blob = out + err
    assert secret not in blob

    # delete path
    monkeypatch.setattr(V, "delete", lambda ref: True)
    rc, out, err = _cap(VP.cmd_delete, _make_args("delete", ref="k"))
    blob = out + err
    assert secret not in blob


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
