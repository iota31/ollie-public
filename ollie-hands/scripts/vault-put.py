#!/usr/bin/env python3
r"""vault-put — owner-only tool to store/list/delete secrets in the engine's encrypted vault.

This is the missing "D3 putter": the vault module already exists and the engine can READ
secrets (via vault.get), but there is currently no way to WRITE one.

CRITICAL — DPAPI principal:
    The vault uses DPAPI **user-scope** (no LOCALMACHINE). A secret encrypted by user A
    can ONLY be decrypted by user A. The ollie-hands engine runs as the host's interactive
    user (Source). Therefore this putter MUST run as that SAME Windows user, or the engine
    won't be able to decrypt what you stored.

    On every run, this script prints the current principal (getpass.getuser()) and a
    one-line reminder that it must match the engine's user.

    The `put` self-test round-trip only proves THIS principal can decrypt — the success
    message explicitly says: "verified decryptable as user <X>; ensure the engine also
    runs as <X>".

Usage examples:
    python scripts/vault-put.py put myref
        # prompts for secret (hidden), writes, then round-trips to verify

    echo -n 'sekret' | python scripts/vault-put.py put myref --stdin
        # reads secret from stdin (for piping), writes, verifies

    python scripts/vault-put.py list
    python scripts/vault-put.py delete myref
    python scripts/vault-put.py verify myref

Ref rules (enforced via vault.valid_ref):
    ^[A-Za-z0-9_-]{1,64}$
    lowercase-only (mixed-case rejected for NTFS case-collision defense)

Exit codes:
    0 = success
    1 = usage / validation error
    2 = vault operation failed (e.g. round-trip mismatch on put)

Run this on the WINDOWS HOST over SSH as the SAME user that runs the ollie-hands engine.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import sys
from pathlib import Path

# allow running from anywhere without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ollie_hands import vault as V  # noqa: E402


def _print_principal() -> None:
    """Print the current user principal and DPAPI reminder."""
    user = getpass.getuser()
    print(f"principal: {user}")
    print("NOTE: vault uses DPAPI user-scope; this secret is decryptable ONLY by this user.")
    print("      The engine must run as the SAME user or decryption will fail.")


def _read_secret(args: argparse.Namespace) -> str:
    """Read the secret value from hidden prompt or stdin.

    Rules:
      - If --stdin: read from sys.stdin (strip trailing newline only once).
      - Else: use getpass.getpass() for hidden input.
      - Reject empty after stripping.
    """
    if getattr(args, "stdin", False):
        # Read raw bytes to avoid any encoding surprises, then decode as utf-8.
        # Support both real binary stdin (.buffer) and test doubles (StringIO/text).
        sin = sys.stdin
        if hasattr(sin, "buffer"):
            data = sin.buffer.read()
            if data.endswith(b"\n"):
                data = data[:-1]
            value = data.decode("utf-8")
        else:
            # Fallback for test mocks that provide text streams
            data = sin.read()
            if data.endswith("\n"):
                data = data[:-1]
            value = data
    else:
        value = getpass.getpass("secret: ")

    if not value:
        raise ValueError("secret value must not be empty")
    return value


def cmd_put(args: argparse.Namespace) -> int:
    """Handle `put <ref>`: validate ref, read secret, write, round-trip verify."""
    ref = args.ref

    if not V.valid_ref(ref):
        print(f"ERROR: invalid ref {ref!r} (must match ^[A-Za-z0-9_-]{{1,64}}$ and be lowercase)", file=sys.stderr)
        return 1

    try:
        value = _read_secret(args)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Write
    try:
        V.put(ref, value)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: vault.put failed: {e}", file=sys.stderr)
        return 2

    # Round-trip self-test
    try:
        got = V.get(ref)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: round-trip get failed after put: {e}", file=sys.stderr)
        return 2

    if got != value:
        print("ERROR: round-trip mismatch: stored value does not decrypt to what was written", file=sys.stderr)
        return 2

    user = getpass.getuser()
    # Success: never print the value; report length + sha256 prefix for auditability.
    sha = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    print(f"ok: stored ref={ref} len={len(value)} sha256[:16]={sha}")
    print(f"verified decryptable as user {user}; ensure the engine also runs as {user}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Handle `list`: print stored ref names only (never values)."""
    try:
        refs = V.list_refs()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: vault.list_refs failed: {e}", file=sys.stderr)
        return 2
    for r in refs:
        print(r)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Handle `delete <ref>`: delete and report whether something was removed."""
    ref = args.ref
    if not V.valid_ref(ref):
        print(f"ERROR: invalid ref {ref!r}", file=sys.stderr)
        return 1
    try:
        removed = V.delete(ref)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: vault.delete failed: {e}", file=sys.stderr)
        return 2
    if removed:
        print(f"deleted: {ref}")
    else:
        print(f"not found: {ref}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Handle `verify <ref>`: confirm round-trip without printing plaintext.

    Reports SUCCESS/FAILURE with byte length or sha256 prefix, never the value.
    """
    ref = args.ref
    if not V.valid_ref(ref):
        print(f"ERROR: invalid ref {ref!r}", file=sys.stderr)
        return 1
    try:
        got = V.get(ref)
    except Exception as e:  # noqa: BLE001
        print(f"FAILURE: {e}", file=sys.stderr)
        return 2
    sha = hashlib.sha256(got.encode("utf-8")).hexdigest()[:16]
    print(f"SUCCESS: ref={ref} len={len(got)} sha256[:16]={sha}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vault-put.py",
        description="Owner-only tool to store/list/delete secrets in the ollie-hands encrypted vault.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_put = sub.add_parser("put", help="store a secret under <ref>")
    p_put.add_argument("ref", help="secret reference name")
    p_put.add_argument("--stdin", action="store_true", help="read secret from stdin instead of prompt")
    p_put.set_defaults(func=cmd_put)

    p_list = sub.add_parser("list", help="list stored secret refs (names only)")
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="delete a secret ref")
    p_del.add_argument("ref", help="secret reference name")
    p_del.set_defaults(func=cmd_delete)

    p_vfy = sub.add_parser("verify", help="verify a ref round-trips (no plaintext shown)")
    p_vfy.add_argument("ref", help="secret reference name")
    p_vfy.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    _print_principal()
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
