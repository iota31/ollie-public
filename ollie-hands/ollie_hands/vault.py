"""Encrypted secret vault (plan Track D3).

Windows DPAPI (user-scope, tied to the engine's Source principal). One file per
ref under C:\ProgramData\ollie-hands\vault\. Writes are owner-only over SSH
(scripts/vault-put.py); the brain never has a tool to read or write values.

Pure tests (mac/linux) must run cleanly — DPAPI calls are gated behind
sys.platform == "win32" and raise on non-Windows so callers can handle it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

VAULT_DIR = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "ollie-hands" / "vault"

# Ref must be a simple name: letters, digits, underscore, hyphen; 1-64 chars.
# No path traversal, no dots that could be used to escape.
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_ref(ref: str) -> bool:
    """Return True if ref is a valid secret name (no traversal, bounded).
    Case-insensitive uniqueness is enforced by the FS on NTFS; we reject
    mixed-case variants here for defense-in-depth (normalize to lower on put)."""
    if not isinstance(ref, str):
        return False
    if not bool(_REF_RE.match(ref)):
        return False
    # Reject any uppercase letters — canonical form is lowercase.
    if ref != ref.lower():
        return False
    return True


def _vault_path(ref: str) -> Path:
    """Return the filesystem path for a ref (caller must have validated)."""
    return VAULT_DIR / f"{ref}.bin"


def put(ref: str, value: str) -> None:
    """Store (or overwrite) a secret under the given ref.

    On Windows: DPAPI-encrypts with CRYPTPROTECT_LOCALMACHINE off (user-scope).
    On non-Windows: raises RuntimeError (engine only runs on the host).
    Ref is normalized to lowercase on write (case-collision defense on NTFS).
    """
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")
    if not isinstance(value, str):
        raise TypeError("secret value must be str")

    if sys.platform != "win32":
        raise RuntimeError("vault.put only supported on Windows (DPAPI)")

    # Normalize to lower (valid_ref already rejected uppercase)
    ref = ref.lower()

    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file in the same dir, then rename.
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    blob = _dpapi_protect(value.encode("utf-8"))
    tmp = VAULT_DIR / f".{ref}.bin.tmp"
    try:
        tmp.write_bytes(blob)
        tmp.replace(_vault_path(ref))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def get(ref: str) -> str:
    """Return the plaintext secret for ref. Engine-internal only.

    Raises RuntimeError if ref not found or decryption fails.
    On non-Windows: raises RuntimeError.
    """
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")

    if sys.platform != "win32":
        raise RuntimeError("vault.get only supported on Windows (DPAPI)")

    p = _vault_path(ref)
    if not p.exists():
        raise RuntimeError(f"secret not found: {ref}")
    blob = p.read_bytes()
    try:
        plain = _dpapi_unprotect(blob)
        return plain.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"failed to decrypt secret {ref}: {e}") from e


def list_refs() -> list[str]:
    """Return the list of stored secret names (VALUES NEVER RETURNED)."""
    if not VAULT_DIR.exists():
        return []
    names: list[str] = []
    for p in sorted(VAULT_DIR.glob("*.bin")):
        name = p.stem
        if valid_ref(name):
            names.append(name)
    return names


def delete(ref: str) -> bool:
    """Delete a secret by ref. Returns True if a file was removed.
    Uses atomic rename-to-.deleted then unlink to avoid torn deletes."""
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")
    ref = ref.lower()
    p = _vault_path(ref)
    if not p.exists():
        return False
    tmp = p.with_suffix(".bin.deleted")
    try:
        p.replace(tmp)
        tmp.unlink()
        return True
    except FileNotFoundError:
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# ------------------------------------------------------------------ DPAPI ---

def _dpapi_protect(data: bytes) -> bytes:
    """Encrypt bytes with DPAPI (user-scope, no LOCALMACHINE)."""
    import ctypes
    from ctypes import wintypes

    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    def _blob(b: bytes) -> DATA_BLOB:
        arr = (ctypes.c_byte * len(b))(*b)
        return DATA_BLOB(len(b), ctypes.cast(arr, ctypes.POINTER(ctypes.c_byte)))

    in_blob = _blob(data)
    out_blob = DATA_BLOB()

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,            # ppszDataDescr
        None,            # pOptionalEntropy
        None,            # pvReserved
        None,            # pPromptStruct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError("CryptProtectData failed")

    try:
        out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return out


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt DPAPI blob (user-scope)."""
    import ctypes
    from ctypes import wintypes

    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    def _blob(b: bytes) -> DATA_BLOB:
        arr = (ctypes.c_byte * len(b))(*b)
        return DATA_BLOB(len(b), ctypes.cast(arr, ctypes.POINTER(ctypes.c_byte)))

    in_blob = _blob(blob)
    out_blob = DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,            # ppszDataDescr (out)
        None,            # pOptionalEntropy
        None,            # pvReserved
        None,            # pPromptStruct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")

    try:
        out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return out
