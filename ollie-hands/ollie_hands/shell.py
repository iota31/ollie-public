"""L0 — shell execution (PowerShell on the host).

The lowest, most deterministic rung of the capability ladder. Policy
classification happens in the executor BEFORE we get here; this module just
runs an already-approved command and captures the result.

D3 hardening (H2) — privilege separation: when provisioned (a `shelluser.cred`
exists), the brain's PowerShell runs as a de-privileged standard user
(`OllieShell`) via `CreateProcessWithLogonW`, NOT as the elevated engine
principal. That user is denied read on the vault/audit dirs (NTFS) and — being a
different user — cannot DPAPI-decrypt the engine's secrets even with the
ciphertext. Output is captured via files in a neutral `work` dir because handle
inheritance does not survive the secondary-logon boundary. Fail-closed: if the
de-privileged spawn cannot run, we raise — we never silently fall back to an
elevated shell (that would reopen the exfil hole).

Without the cred file (dev box, mac, pre-setup) it uses the legacy in-process
``subprocess`` path so tests and non-Windows stay green.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

WINDOWS = sys.platform == "win32"

_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ollie-hands"
_WORK = _ROOT / "work"
_CRED = _ROOT / "shelluser.cred"
_SHELL_USER = "OllieShell"
_PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _privsep_enabled() -> bool:
    """Privilege separation is active iff the shell-user credential is present."""
    return WINDOWS and _CRED.exists()


def run(command: str, *, cwd: str | None = None, timeout: int = 60) -> dict:
    """Run a PowerShell command. Returns exit code + captured output."""
    if not WINDOWS:
        raise RuntimeError("shell.run only executes on the Windows host")
    if _privsep_enabled():
        return _run_deprivileged(command, cwd=cwd, timeout=timeout)
    return _run_legacy(command, cwd=cwd, timeout=timeout)


# --------------------------------------------------------------- legacy path ---

def _run_legacy(command: str, *, cwd: str | None, timeout: int) -> dict:
    """In-process subprocess as the engine user (pre-priv-sep / dev fallback)."""
    argv = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", command,
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-16000:],
            "stderr": proc.stderr[-4000:],
            "timed_out": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": None,
            "stdout": (e.stdout or "")[-16000:] if isinstance(e.stdout, str) else "",
            "stderr": f"TIMEOUT after {timeout}s",
            "timed_out": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }


# ----------------------------------------------------- privilege-separated ---

def _ps_sq(s: str) -> str:
    """PowerShell single-quoted literal (escape embedded single quotes)."""
    return "'" + str(s).replace("'", "''") + "'"


def _read_text(p: Path) -> str:
    """Read captured output, tolerating PowerShell's encodings. The `1>`/`2>`
    redirection operators in Windows PowerShell 5.1 write UTF-16LE (with BOM);
    handle that, a UTF-8 BOM, and plain UTF-8."""
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig", errors="replace")
    return data.decode("utf-8", errors="replace")


def _read_shell_password() -> str:
    """Decrypt the OllieShell password (raw DPAPI blob, engine-user scope)."""
    from .vault import _dpapi_unprotect  # same CryptUnprotectData as the vault
    return _dpapi_unprotect(_CRED.read_bytes()).decode("utf-8")


def _run_deprivileged(command: str, *, cwd: str | None, timeout: int) -> dict:
    _WORK.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    runner = _WORK / f"r_{rid}.ps1"
    outp = _WORK / f"o_{rid}.txt"
    errp = _WORK / f"e_{rid}.txt"

    # The runner runs the (already policy-approved) command as OllieShell and
    # redirects its own streams to files we read back as the engine user. The
    # command text is written verbatim — no extra shell re-parse.
    setloc = f"Set-Location -LiteralPath {_ps_sq(cwd)}\n" if cwd else ""
    runner.write_text(
        f"$ErrorActionPreference='Continue'\n"
        f"{setloc}"
        f"& {{\n{command}\n}} 1> {_ps_sq(str(outp))} 2> {_ps_sq(str(errp))}\n"
        f"exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    cmdline = (f'"{_PS}" -NoProfile -NonInteractive -ExecutionPolicy Bypass '
               f'-File "{runner}"')

    t0 = time.monotonic()
    pw = _read_shell_password()
    try:
        rc, timed_out = _create_process_with_logon(
            _SHELL_USER, ".", pw, cmdline, str(_WORK), timeout)
    finally:
        pw = None  # drop the reference promptly

    out, err = _read_text(outp), _read_text(errp)
    for f in (runner, outp, errp):
        try:
            f.unlink()
        except OSError:
            pass

    return {
        "exit_code": None if timed_out else rc,
        "stdout": out[-16000:],
        "stderr": (f"TIMEOUT after {timeout}s" if timed_out else err)[-4000:],
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def _create_process_with_logon(user: str, domain: str, password: str,
                               cmdline: str, workdir: str,
                               timeout: int) -> tuple[int | None, bool]:
    """Spawn `cmdline` as `user` via CreateProcessWithLogonW; wait with timeout.

    Returns (exit_code, timed_out). Raises on spawn failure (fail-closed)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LOGON_WITH_PROFILE = 0x00000001
    CREATE_NO_WINDOW = 0x08000000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    WAIT_TIMEOUT = 0x00000102

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    fn = advapi32.CreateProcessWithLogonW
    fn.restype = wintypes.BOOL
    fn.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,  # user, domain, pw
        wintypes.DWORD,                                        # logon flags
        wintypes.LPCWSTR, wintypes.LPWSTR,                     # app, cmdline
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,     # flags, env, cwd
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    cmdbuf = ctypes.create_unicode_buffer(cmdline)  # must be writable

    ok = fn(user, domain, password, LOGON_WITH_PROFILE,
            None, cmdbuf, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            None, workdir, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise RuntimeError(
            f"CreateProcessWithLogonW failed (err={ctypes.get_last_error()}) — "
            "de-privileged shell unavailable; refusing to run elevated (fail-closed)")
    try:
        wait = kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
        if wait == WAIT_TIMEOUT:
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.WaitForSingleObject(pi.hProcess, 5000)
            return (None, True)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return (int(code.value), False)
    finally:
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
