"""
Watchdog & Budget read endpoints for Mission Control (drop-in panel module).

GET /api/watchdog
  - plugin-state/watchdog-state.json (create-tolerant via _load).
  - failures map → list of active alerts [{subsystem, message, severity}].
  - Includes power info block and last_beat.
  - Tails logs/watchdog.log for recent ok/FAIL/RECOVERED/LAB-BYPASS/power history.

GET /api/budget/tokens
  - Aggregate token usage by scanning agents/main/sessions/*.trajectory.jsonl.
  - Sums input/output/cacheRead/total + calls from model.completed usage events.
  - Also returns by_lane breakdown for richer UI.
  - EXPENSIVE → TTLCache (120s) from mc/cache.py.
  - Create-tolerant: missing dir or files → zeros/empty.

All paths derive from rd.HOME (like reads_system). Missing files/dirs never crash.
"""

import glob
import json
import os

import research_dashboard as rd
from . import route
from .cache import TTLCache
from .io import _load, _tail


# ── Module config (box paths; overridable for tests via attribute patch) ──────
_HOME = rd.HOME
_OPENCLAW = os.path.join(_HOME, ".openclaw")

WATCHDOG_STATE = os.path.join(_HOME, "plugin-state", "watchdog-state.json")
WATCHDOG_LOG   = os.path.join(_OPENCLAW, "logs", "watchdog.log")

BUDGET_CONFIG  = os.path.join(_OPENCLAW, "workspace", "budget-config.json")
SPEND_STATE    = os.path.join(_OPENCLAW, "logs", "spend-state.json")
SESSIONS_DIR   = os.path.join(_OPENCLAW, "agents", "main", "sessions")

_TOKENS_CACHE = TTLCache(ttl=120.0)


def _load_watchdog():
    """Read watchdog-state.json. Returns {} on absence (honest empty)."""
    v = _load(WATCHDOG_STATE)
    return v if isinstance(v, dict) else {}


def _tail_watchdog_log(n=100):
    """Tail the watchdog log and keep lines that look like status/history events."""
    lines = _tail(WATCHDOG_LOG, n)
    if not lines:
        return []
    kept = []
    for ln in lines:
        low = ln.lower()
        if any(k in low for k in (
            "fail ", "fail:", "recovered",
            "ok (", "all healthy",
            "lab-bypass",
            "power ", "blind", "escalate", "transition"
        )):
            kept.append(ln)
    # Prefer the filtered set; if nothing matched, fall back to a short raw tail.
    return kept[-50:] if kept else lines[-20:]


def _severity_for(msg: str) -> str:
    """Map a failure message to a ladder severity for the alerts list."""
    m = (msg or "").lower()
    if any(k in m for k in ("critical", "fatal", "disk 9", "kill", "down")):
        return "critical"
    return "warn"


def _failures_to_alerts(failures):
    """Transform the watchdog failures map into a list of active alerts."""
    alerts = []
    for sub, msg in (failures or {}).items():
        if msg:  # only active (truthy) entries
            alerts.append({
                "subsystem": sub,
                "message": str(msg),
                "severity": _severity_for(str(msg)),
            })
    return alerts


def _load_budget_lanes():
    """Create-tolerant load of budget ceilings + today's counts for lane gauges."""
    cfg = _load(BUDGET_CONFIG)
    st = _load(SPEND_STATE)
    if not isinstance(cfg, dict):
        cfg = None
    if not isinstance(st, dict):
        st = None
    return {"config": cfg, "state": st}


def _compute_tokens():
    """
    Scan trajectory files for model.completed usage and aggregate tokens.
    Returns a snapshot with grand totals and a per-lane breakdown.
    Fully tolerant of missing directory or malformed lines.
    """
    totals = {"input": 0, "output": 0, "cacheRead": 0, "total": 0, "calls": 0}
    by_lane = {}
    sessions_seen = 0
    try:
        pattern = os.path.join(SESSIONS_DIR, "*.trajectory.jsonl")
        for f in glob.glob(pattern):
            lane = "other"
            try:
                for line in open(f):
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "session.started":
                        sk = d.get("sessionKey") or ""
                        # Minimal inline classify (matches budget.classify semantics)
                        if "heartbeat" in sk:
                            lane = "heartbeat"
                        elif ":project-" in sk:
                            lane = "project"
                        elif ":job-" in sk:
                            lane = "job"
                        elif "dreaming" in sk:
                            lane = "dreaming"
                        elif "subagent" in sk:
                            lane = "subagent"
                        elif "telegram" in sk:
                            lane = "telegram"
                        elif "whatsapp" in sk:
                            lane = "whatsapp"
                        else:
                            lane = "dev/other"
                        sessions_seen += 1
                    elif t == "model.completed":
                        u = (d.get("data") or {}).get("usage") or {}
                        cell = by_lane.setdefault(lane, {
                            "input": 0, "output": 0, "cacheRead": 0, "total": 0, "calls": 0
                        })
                        for k in ("input", "output", "cacheRead", "total"):
                            v = int(u.get(k) or 0)
                            totals[k] += v
                            cell[k] += v
                        totals["calls"] += 1
                        cell["calls"] += 1
            except Exception:
                continue
    except Exception:
        pass
    return {
        "totals": totals,
        "by_lane": by_lane,
        "sessions_scanned": sessions_seen,
        "as_of": rd._utcnow(),
    }


def get_tokens_cached():
    """Memoized (120s) token aggregation. Shared by the endpoint."""
    return _TOKENS_CACHE.get_or_set("budget:tokens", _compute_tokens)


# ── Routes ────────────────────────────────────────────────────────────────────

@route("GET", "/api/watchdog")
def get_watchdog(handler):
    st = _load_watchdog()
    alerts = _failures_to_alerts(st.get("failures"))
    power = st.get("power")
    last_beat = st.get("last_beat") or st.get("last_heartbeat")
    hist = _tail_watchdog_log(100)
    lanes = _load_budget_lanes()
    handler._json(200, {
        "alerts": alerts,
        "power": power,
        "last_beat": last_beat,
        "history": hist,
        "budget": lanes,
        "checked_at": rd._utcnow(),
    })


@route("GET", "/api/budget/tokens")
def get_budget_tokens(handler):
    handler._json(200, get_tokens_cached())
