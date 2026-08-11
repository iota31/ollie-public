"""
Control (POST) endpoints for Mission Control (batch 1).

Auto-discovered by mc.load_handlers() via the controls*.py glob.

All endpoints are POST, require BOTH bearer AND control-PIN (X-Ollie-Control),
and require a JSON body field {"confirm": true} (or action-specific token).
They are rate-limited server-side and every call (accepted or denied) is audited
to the activity log so the SSE feed surfaces the attempt.

Batch 1 (lowest blast radius):
- POST /api/ctrl/heartbeat/beat
- POST /api/ctrl/watchdog/mute
- POST /api/ctrl/watchdog/ack
"""
import json
import os
import subprocess
import sys
import time

import research_dashboard as rd

from . import route
from .auth import _audit, _control_authed, _get_or_create_control_pin, _rate_limit
from .io import _load, _save


# ── Module config (box paths; overridable for tests via attribute patch) ──────
_HOME = rd.HOME
_OPENCLAW = os.path.join(_HOME, ".openclaw")

WATCHDOG_STATE = os.path.join(_HOME, "plugin-state", "watchdog-state.json")
ACTIVITY_LOG   = os.path.join(_OPENCLAW, "logs", "mission-control.log")

# Heartbeat script path (idempotent; self-locks inside the script).
HEARTBEAT_SCRIPT = os.path.join(_HOME, "bin", "ollie_heartbeat.py")


def _require_control(handler) -> bool:
    """Return True if authorized for control actions; otherwise write error and return False."""
    if not _control_authed(handler):
        # Distinguish: if bearer missing/invalid → 401; if PIN missing/invalid → 403
        # We mirror existing behavior: _authed already returns False for bad bearer.
        # If bearer was good but PIN bad, _control_authed returns False.
        # We emit 401 for missing/bad bearer, 403 if bearer ok but PIN bad.
        # A simple heuristic: re-check bearer alone.
        from .auth import _authed as _authed_token
        if not _authed_token(handler, getattr(rd, "BEARER_TOKEN", "")):
            handler._err(401, "unauthorized")
        else:
            handler._err(403, "control unauthorized")
        return False
    return True


def _require_confirm(handler, body) -> bool:
    """Return True if body carries confirm: true (or truthy confirm)."""
    if not isinstance(body, dict):
        handler._err(400, "confirm required"); return False
    if not body.get("confirm"):
        handler._err(400, "confirm required"); return False
    return True


def _audit_denied(action: str, reason: str):
    _audit(action, "owner", f"denied {reason}")


def _audit_accepted(action: str, detail: str = ""):
    _audit(action, "owner", f"accepted {detail}".strip())


# ── Endpoints ─────────────────────────────────────────────────────────────────

@route("POST", "/api/ctrl/heartbeat/beat")
def post_ctrl_heartbeat_beat(handler):
    """
    Trigger a single heartbeat beat.

    - Requires bearer + X-Ollie-Control PIN.
    - Requires JSON body {"confirm": true}.
    - Rate-limited to 1 call per 60s.
    - Invokes: $HOME/bin/ollie_heartbeat.py (subprocess, 120s timeout).
    - The script is self-locking and idempotent.
    - Returns {ok, rc, note}. On timeout: {ok:false, note:"timeout"}.
    """
    # Auth (bearer + PIN)
    if not _require_control(handler):
        _audit_denied("heartbeat/beat", "auth")
        return
    # Confirm
    body, err = handler._read_body()
    if err:
        _audit_denied("heartbeat/beat", "bad-body")
        handler._err(400, err); return
    if not _require_confirm(handler, body):
        _audit_denied("heartbeat/beat", "no-confirm")
        return
    # Rate limit (1 per 60s)
    if not _rate_limit("heartbeat/beat", per_seconds=60, max_calls=1):
        _audit_denied("heartbeat/beat", "rate-limit")
        handler._err(429, "rate limited"); return

    # Invoke the heartbeat script (never double-run; the script self-locks).
    try:
        proc = subprocess.run(
            [sys.executable, HEARTBEAT_SCRIPT],
            timeout=120,
            env={**os.environ, "HOME": _HOME},
            capture_output=True,
            text=True,
        )
        rc = proc.returncode
        note = (proc.stdout or "").strip() or (proc.stderr or "").strip() or ""
        _audit_accepted("heartbeat/beat", f"rc={rc}")
        handler._json(200, {"ok": True, "rc": rc, "note": note})
    except subprocess.TimeoutExpired:
        _audit("heartbeat/beat", "owner", "accepted timeout")
        handler._json(200, {"ok": False, "note": "timeout"})
    except Exception as exc:
        _audit("heartbeat/beat", "owner", f"accepted error {exc}")
        handler._json(200, {"ok": False, "note": str(exc)})


@route("POST", "/api/ctrl/watchdog/mute")
def post_ctrl_watchdog_mute(handler):
    """
    Mute a watchdog alert key for N minutes.

    Body: { "key": "<alert_key>", "minutes": <int> }
    - minutes must be > 0 and <= 24h (1440 minutes).
    - We read-modify-write ONLY the top-level key "mc_mutes" in watchdog-state.json.
    - The watchdog process owns the rest of the file and only READS mc_mutes,
      so writing only our key avoids a lost-update race with the watchdog writer.
    - Returns the updated mc_mutes mapping.
    """
    if not _require_control(handler):
        _audit_denied("watchdog/mute", "auth")
        return
    body, err = handler._read_body()
    if err:
        _audit_denied("watchdog/mute", "bad-body")
        handler._err(400, err); return
    if not _require_confirm(handler, body):
        _audit_denied("watchdog/mute", "no-confirm")
        return

    key = (body or {}).get("key")
    minutes = (body or {}).get("minutes")
    if not isinstance(key, str) or not key.strip():
        _audit_denied("watchdog/mute", "bad-key")
        handler._err(400, "key required"); return
    try:
        minutes = int(minutes)
    except Exception:
        minutes = 0
    if minutes <= 0:
        _audit_denied("watchdog/mute", "bad-minutes")
        handler._err(400, "minutes must be > 0"); return
    if minutes > 1440:
        minutes = 1440

    # Read-modify-write: touch ONLY mc_mutes
    st = _load(WATCHDOG_STATE)
    if not isinstance(st, dict):
        st = {}
    mutes = st.get("mc_mutes") if isinstance(st.get("mc_mutes"), dict) else {}
    until = int(time.time()) + minutes * 60
    mutes[key] = until
    st["mc_mutes"] = mutes
    _save(WATCHDOG_STATE, st)

    _audit_accepted("watchdog/mute", f"key={key} minutes={minutes}")
    handler._json(200, {"mc_mutes": mutes})


@route("POST", "/api/ctrl/watchdog/ack")
def post_ctrl_watchdog_ack(handler):
    """
    Acknowledge a watchdog alert key.

    Body: { "key": "<alert_key>" }
    - Records an ack under the top-level key "mc_acks": { alert_key: ack_epoch }.
    - Reversible, low risk.
    - Returns the updated mc_acks mapping.
    """
    if not _require_control(handler):
        _audit_denied("watchdog/ack", "auth")
        return
    body, err = handler._read_body()
    if err:
        _audit_denied("watchdog/ack", "bad-body")
        handler._err(400, err); return
    if not _require_confirm(handler, body):
        _audit_denied("watchdog/ack", "no-confirm")
        return

    key = (body or {}).get("key")
    if not isinstance(key, str) or not key.strip():
        _audit_denied("watchdog/ack", "bad-key")
        handler._err(400, "key required"); return

    st = _load(WATCHDOG_STATE)
    if not isinstance(st, dict):
        st = {}
    acks = st.get("mc_acks") if isinstance(st.get("mc_acks"), dict) else {}
    acks[key] = int(time.time())
    st["mc_acks"] = acks
    _save(WATCHDOG_STATE, st)

    _audit_accepted("watchdog/ack", f"key={key}")
    handler._json(200, {"mc_acks": acks})
