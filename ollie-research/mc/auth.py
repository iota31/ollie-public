"""
Bearer-token auth for Mission Control (stdlib only).

Constant-time comparison via `secrets.compare_digest`, identical to the
original `DashboardHandler._authed`. The token is passed in by the caller
(the server reads its live module global `BEARER_TOKEN` at request time, so
test harnesses that patch the token keep working).

Control harness (batch 1):
- Control-PIN: second secret at ~/.openclaw/secrets/mission-control-pin
  (auto-generated on first read, chmod 600, 16 random hex bytes).
- _control_authed(handler): requires BOTH valid bearer AND valid
  X-Ollie-Control header (compare_digest). Controls are POST-only.
- Per-action confirm: handlers require JSON body {"confirm": true}
  (or action-specific token); reject 400 if absent.
- Rate-limiter: stdlib in-process token-bucket per action key, guarded by
  threading.Lock. _rate_limit(action, per_seconds, max_calls) -> bool.
- Audit: _audit(action, actor_tier, detail) appends a timestamped line to
  mission-control.log (create-tolerant). Every control call (accepted or
  denied) is audited so the SSE feed surfaces it.
"""
import os
import secrets
import threading
import time

import research_dashboard as rd

from .io import _utcnow


def _authed(handler, token: str) -> bool:
    """True iff the request carries `Authorization: Bearer <token>`."""
    hdr = handler.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False
    return secrets.compare_digest(hdr[7:].strip(), token)


# ── Control-PIN (second factor for controls) ──────────────────────────────────
_CONTROL_PIN_FILE = None
_CONTROL_PIN = None


def _get_control_pin_file():
    if _CONTROL_PIN_FILE is not None:
        return _CONTROL_PIN_FILE
    home = getattr(rd, "HOME", os.environ.get("HOME", "/home/openclaw"))
    return os.path.join(home, ".openclaw", "secrets", "mission-control-pin")


def _get_or_create_control_pin():
    """Return the control PIN, creating the file (chmod 600) on first use."""
    global _CONTROL_PIN
    if _CONTROL_PIN is not None:
        return _CONTROL_PIN
    path = _get_control_pin_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            pin = fh.read().strip()
        if pin:
            _CONTROL_PIN = pin
            return pin
    except FileNotFoundError:
        pass
    pin = secrets.token_hex(16)
    with open(path, "w") as fh:
        fh.write(pin + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _CONTROL_PIN = pin
    return pin


def _control_authed(handler) -> bool:
    """True iff BOTH bearer and X-Ollie-Control PIN are valid (constant time)."""
    # Bearer (live token from research_dashboard at request time)
    if not _authed(handler, getattr(rd, "BEARER_TOKEN", "")):
        return False
    # Control PIN
    pin = _get_or_create_control_pin()
    hdr = handler.headers.get("X-Ollie-Control", "")
    return secrets.compare_digest(hdr.strip(), pin)


# ── Rate limiter (in-process token bucket, thread-safe) ───────────────────────
_RATE_BUCKETS = {}
_RATE_LOCK = threading.Lock()


def _rate_limit(action: str, per_seconds: int, max_calls: int) -> bool:
    """
    Return True if allowed; False if this call exceeds the bucket.
    Allows up to `max_calls` in any rolling `per_seconds` window.
    """
    now = time.monotonic()
    with _RATE_LOCK:
        calls = _RATE_BUCKETS.setdefault(action, [])
        cutoff = now - float(per_seconds)
        # drop expired timestamps
        i = 0
        while i < len(calls) and calls[i] <= cutoff:
            i += 1
        if i:
            del calls[:i]
        if len(calls) >= max_calls:
            return False
        calls.append(now)
        return True


def _reset_rate_limits():
    """Test helper: clear all in-process rate-limit buckets. Idempotent."""
    global _RATE_BUCKETS
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()


# ── Audit (append-only, create-tolerant) to the activity log ──────────────────
_AUDIT_LOG_PATH = None


def _audit_log_path():
    if _AUDIT_LOG_PATH is not None:
        return _AUDIT_LOG_PATH
    home = getattr(rd, "HOME", os.environ.get("HOME", "/home/openclaw"))
    return os.path.join(home, ".openclaw", "logs", "mission-control.log")


def _audit(action: str, actor_tier: str, detail: str):
    """Append a timestamped audit line for this control attempt (best-effort)."""
    path = _audit_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = f"{_utcnow()} CTRL {action} actor={actor_tier} {detail}\n"
        with open(path, "a") as fh:
            fh.write(line)
    except Exception:
        # Never raise from audit; the control result is more important.
        pass
