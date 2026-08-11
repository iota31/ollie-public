"""
System / aggregate read endpoints for Mission Control (the SHELL).

These feed the persistent frame: the status strip, the 7-pill subsystem
health rail, and the activity/audit feed. They are intentionally cheap local
reads EXCEPT `/api/system/liveness`, which probes ports + reads /proc + `df`
and is therefore memoized via a process-wide `TTLCache` (20s).

All box paths key off HOME=/home/openclaw (see research_dashboard). Reads are
create-tolerant: a missing file yields honest absence (`None` / `[]`), never a
fabricated green/zero. We never cross the OllieLab 2222 hop here.

Endpoints
---------
  GET /api/health           aggregate verdict + per-pill state (strip + rail)
  GET /api/system/liveness  process/port checks + cpu/mem/disk (CACHED 20s)
  GET /api/system/power     host-power.json passthrough
  GET /api/activity         last N lines of mission-control.log ([] if absent)
  GET /api/activity/stream  SSE tail-follow of mission-control.log

Verdict computation (documented for manager sanity-check)
---------------------------------------------------------
Each of the 7 subsystems (gateway, hands, factcheck, jobs, watchdog,
curiosity, lab) is reduced to ONE pill state from the 5-color ladder:

    "ok"          green   — alive / healthy
    "warn"        amber   — degraded but functioning (e.g. watchdog reports a
                            non-critical failure, or a soft/stale signal)
    "critical"    red     — a hard failure (process/port down, or watchdog
                            critical failure)
    "maintenance" blue    — intentionally off / paused (off-heat, not a fault)
    "stale"       gray    — no data / unknown (e.g. hands on the Windows host
                            is unreachable from WSL → unknown, NOT critical)

A pill's state is the WORST-of-its-children. The overall strip verdict is the
worst pill rolled up to a single word:

    any critical          -> CRITICAL
    else any warn          -> ATTENTION
    else any stale/degraded-> DEGRADED
    else any maintenance   -> MAINTENANCE
    else                   -> NOMINAL

(MAINTENANCE outranks NOMINAL but is below DEGRADED: an intentional pause is
"calmer" than missing data, but we still surface that something is off-heat.)
"""
import json
import os
import socket
import subprocess
import time

import research_dashboard as rd
from . import route
from .cache import TTLCache
from .io import _load, _tail

# ── Module config (box paths; overridable for tests via attribute patch) ────
# HOME=/home/openclaw on the box. research_dashboard already resolved HOME.
_HOME = rd.HOME
_OPENCLAW = os.path.join(_HOME, ".openclaw")

WATCHDOG_STATE = os.path.join(_HOME, "plugin-state", "watchdog-state.json")
HOST_POWER     = os.path.join(_OPENCLAW, "workspace", "host-power.json")
ACTIVITY_LOG   = os.path.join(_OPENCLAW, "logs", "mission-control.log")

# Port map for liveness probes. hands(:3200) lives on the WINDOWS host and is
# typically unreachable from inside WSL → treated as unknown/stale, not down.
PORTS = {
    "gateway":   18789,
    "hands":     3200,
    "dashboard": 3400,
    "ollielab":  2222,
}
# Subsystems we treat as "unknown when unreachable" rather than critical.
_UNREACHABLE_IS_UNKNOWN = {"hands"}

# Process-name fragments for pgrep-style liveness (factcheck, jobs-runner, watchdog).
_PROC_PATTERNS = {
    "factcheck": "factcheck",
    "jobs":      "jobs-runner",
    "watchdog":  "ollie_watchdog",
}

# ── Ladder ordering (worst-first reduction) ────────────────────────────────
# Numeric rank: higher = worse. Used to roll children up to a pill, and pills
# up to the strip verdict.
_RANK = {"ok": 0, "maintenance": 1, "stale": 2, "warn": 3, "critical": 4}
_VERDICT_FOR_STATE = {
    "critical":    "CRITICAL",
    "warn":        "ATTENTION",
    "stale":       "DEGRADED",
    "maintenance": "MAINTENANCE",
    "ok":          "NOMINAL",
}

# 20s liveness cache (the only expensive read in the shell).
_LIVENESS_CACHE = TTLCache(ttl=20.0)


def _worst(states):
    """Reduce an iterable of ladder states to the single worst one."""
    worst = "ok"
    for s in states:
        if _RANK.get(s, 0) > _RANK.get(worst, 0):
            worst = s
    return worst


# ── Liveness probes (cached) ────────────────────────────────────────────────
def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _pgrep(pattern: str) -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _read_meminfo():
    """Return {total_kb, available_kb, used_pct} from /proc/meminfo, or None."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                info[k.strip()] = int(rest.strip().split()[0])
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if not total:
            return None
        used_pct = round(100.0 * (total - (avail or 0)) / total, 1)
        return {"total_kb": total, "available_kb": avail, "used_pct": used_pct}
    except Exception:
        return None


def _read_loadavg():
    """Return the 1-minute load average, or None."""
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0])
    except Exception:
        return None


def _read_disk(path="/"):
    """Return {total_gb, free_gb, used_pct} for `path` via os.statvfs, or None."""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if not total:
            return None
        used_pct = round(100.0 * (total - free) / total, 1)
        return {
            "total_gb": round(total / 1e9, 1),
            "free_gb":  round(free / 1e9, 1),
            "used_pct": used_pct,
        }
    except Exception:
        return None


def _compute_liveness():
    """The uncached liveness snapshot (probes + system stats)."""
    services = {}

    # Port-based services.
    for name, port in PORTS.items():
        up = _port_open(port)
        if up:
            services[name] = {"state": "ok", "detail": f"port {port} open", "port": port}
        elif name in _UNREACHABLE_IS_UNKNOWN:
            services[name] = {
                "state": "stale",
                "detail": f"port {port} unreachable (host-side; unknown)",
                "port": port,
            }
        else:
            services[name] = {"state": "critical", "detail": f"port {port} closed", "port": port}

    # Process-based services.
    for name, pat in _PROC_PATTERNS.items():
        alive = _pgrep(pat)
        services[name] = {
            "state": "ok" if alive else "critical",
            "detail": f"pgrep -f {pat}: {'found' if alive else 'not found'}",
        }

    return {
        "services": services,
        "system": {
            "mem":     _read_meminfo(),
            "loadavg": _read_loadavg(),
            "disk":    _read_disk("/"),
        },
        "checked_at": rd._utcnow(),
    }


def get_liveness_cached():
    """Memoized (20s) liveness snapshot. Shared by /liveness and /health."""
    return _LIVENESS_CACHE.get_or_set("liveness", _compute_liveness)


# ── Watchdog state ──────────────────────────────────────────────────────────
def _load_watchdog():
    """Read watchdog-state.json. Returns {} on absence (honest empty)."""
    v = _load(WATCHDOG_STATE)
    return v if isinstance(v, dict) else {}


def _watchdog_state_for(subsystem: str, wd: dict) -> str:
    """Derive a ladder state for `subsystem` from watchdog-state.

    Tolerant of several shapes the watchdog might use:
      - wd["subsystems"][name] = {"failures": int, "critical": bool,
                                  "muted"/"paused"/"maintenance": bool,
                                  "state": "<ladder>"}
      - or a flat wd[name] of the same shape.
    Unknown subsystem -> "ok" (watchdog isn't tracking it; not a fault here).
    """
    node = None
    subs = wd.get("subsystems")
    if isinstance(subs, dict):
        node = subs.get(subsystem)
    if node is None and isinstance(wd.get(subsystem), dict):
        node = wd.get(subsystem)
    if not isinstance(node, dict):
        return "ok"  # intentional: genuinely untracked/absent subsystems default to ok (not a fault)

    # An explicit ladder state wins if the watchdog already supplies one.
    explicit = node.get("state")
    if explicit in _RANK:
        return explicit

    if node.get("maintenance") or node.get("paused") or node.get("muted"):
        return "maintenance"
    if node.get("critical"):
        return "critical"
    failures = node.get("failures") or node.get("fail_count") or 0
    try:
        failures = int(failures)
    except (TypeError, ValueError):
        failures = 0
    if failures > 0:
        return "warn"
    return "ok"


# ── Pill model: which liveness children belong to which subsystem pill ──────
# Each of the 7 pills is the worst-of-its-children, where children = the
# relevant liveness service state(s) combined with the watchdog signal.
_PILL_SERVICES = {
    "gateway":   ["gateway"],
    "hands":     ["hands"],
    "factcheck": ["factcheck"],
    "jobs":      ["jobs"],
    "watchdog":  ["watchdog"],  # now driven by pgrep liveness for ollie_watchdog (plus watchdog-state signal)
    "curiosity": ["dashboard"], # the curiosity engine surfaces via the dashboard
    "lab":       ["ollielab"],
}
PILLS = ["gateway", "hands", "factcheck", "jobs", "watchdog", "curiosity", "lab"]


def _compute_health():
    """Aggregate verdict + per-pill state. Drives the strip + rail."""
    liveness = get_liveness_cached()
    wd = _load_watchdog()
    services = liveness.get("services", {})

    pills = {}
    for pill in PILLS:
        child_states = []
        for svc in _PILL_SERVICES.get(pill, []):
            s = services.get(svc, {}).get("state", "stale")
            child_states.append(s)
        # Watchdog signal for this subsystem is also a child.
        child_states.append(_watchdog_state_for(pill, wd))
        if not child_states:
            child_states = ["stale"]
        pills[pill] = _worst(child_states)

    overall_state = _worst(pills.values())
    verdict = _VERDICT_FOR_STATE.get(overall_state, "NOMINAL")

    # last-beat-age: prefer watchdog's last heartbeat, else liveness check time.
    last_beat = wd.get("last_beat") or wd.get("last_heartbeat") or None

    return {
        "verdict": verdict,
        "overall_state": overall_state,
        "pills": pills,
        "last_beat": last_beat,
        "checked_at": liveness.get("checked_at"),
    }


# ── Routes ──────────────────────────────────────────────────────────────────
@route("GET", "/api/health")
def get_health(handler):
    handler._json(200, _compute_health())


@route("GET", "/api/system/liveness")
def get_system_liveness(handler):
    handler._json(200, get_liveness_cached())


@route("GET", "/api/system/power")
def get_system_power(handler):
    # Honest absence: missing file -> null, never a fabricated state.
    handler._json(200, _load(HOST_POWER))


@route("GET", "/api/activity")
def get_activity(handler):
    # Create-tolerant: missing log -> [].
    handler._json(200, _tail(ACTIVITY_LOG, 200))


@route("GET", "/api/activity/stream")
def get_activity_stream(handler):
    """SSE tail-follow of mission-control.log.

    ThreadingHTTPServer gives us one thread per connection, so a blocking
    follow loop is safe. We seek to EOF, then poll for appended lines and emit
    each as an SSE `data:` event. A keepalive comment every ~15s keeps proxies
    and the browser EventSource from timing out the idle stream. The loop ends
    when the client disconnects (write raises) or after a hard cap so a wedged
    socket can't pin a thread forever.
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()

    def _send(payload: bytes) -> bool:
        try:
            handler.wfile.write(payload)
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # Opening retry hint + initial keepalive.
    if not _send(b"retry: 3000\n: connected\n\n"):
        return

    # Emit the current tail first so a fresh client isn't blank.
    for line in _tail(ACTIVITY_LOG, 50):
        if not _send(b"data: " + line.encode("utf-8", "replace") + b"\n\n"):
            return

    last_keepalive = time.monotonic()
    deadline = time.monotonic() + 3600  # 1h hard cap per connection
    fh = None
    try:
        try:
            fh = open(ACTIVITY_LOG)
            fh.seek(0, os.SEEK_END)
        except FileNotFoundError:
            fh = None

        while time.monotonic() < deadline:
            line = fh.readline() if fh else ""
            if line:
                if not _send(b"data: " + line.rstrip("\n").encode("utf-8", "replace") + b"\n\n"):
                    return
                continue
            # No new data: maybe the log was just created.
            if fh is None:
                try:
                    fh = open(ACTIVITY_LOG)
                    fh.seek(0, os.SEEK_END)
                except FileNotFoundError:
                    fh = None
            now = time.monotonic()
            if now - last_keepalive >= 15:
                if not _send(b": keepalive\n\n"):
                    return
                last_keepalive = now
            time.sleep(0.5)
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
