#!/usr/bin/env python3
"""Ollie watchdog: kills silent degradation.

Every CHECK_INTERVAL_S (15 min):
  - gateway: local webhook verify-challenge answers
  - public path: same challenge via the ngrok domain (proves ngrok+gateway)
  - jobs runner process alive
  - tailnet/4dpocket reachable (VPS <TAILSCALE_IP_VPS>:4040)
  - stale background jobs (running/ > STALE_JOB_S)
  - disk usage on /

Once per day additionally probes LLM/search provider quotas:
  - MiniMax LLM, Groq LLM, NVIDIA LLM, Brave search

Alerts the owner on Telegram on state changes (fail->ok, ok->fail),
error content changes, and periodic reminders for persistent failures.
All credentials are read at runtime from existing files — nothing stored here.
"""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOME = "/home/openclaw"
OPENCLAW_JSON = f"{HOME}/.openclaw/openclaw.json"
AUTH_PROFILES = f"{HOME}/.openclaw/agents/main/agent/auth-profiles.json"
WA_SECRETS = f"{HOME}/.openclaw/secrets/whatsapp-cloud.json"
STATE = f"{HOME}/.openclaw/plugin-state/watchdog-state.json"
LOG = f"{HOME}/.openclaw/logs/watchdog.log"
LOCK = "/tmp/ollie-watchdog.lock"
JOBS_RUNNING = f"{HOME}/.openclaw/workspace/jobs/running"
# Power state bridged in by the host scheduled task (OlliePowerSentinel ->
# host-power-sentinel.ps1). The WSL distro has interop disabled and cannot see
# the battery itself; the host writes this file via \\wsl$ every 5 min.
HOST_POWER_JSON = f"{HOME}/.openclaw/workspace/host-power.json"

OWNER_CHAT_ID = "<OWNER_TELEGRAM_CHAT_ID>"
NGROK_DOMAIN = "<NGROK_DOMAIN>"
CHECK_INTERVAL_S = 900
STALE_JOB_S = 35 * 60
DISK_ALERT_PCT = 90

# ---- lab-bypass detector -----------------------------------------------------
# Save-derived code must run inside the OllieLab sandbox, never installed/run in
# this gateway distro. We catch the bypass by watching the filesystem for new
# large code/model trees outside the lab.
CACHE_DIR = f"{HOME}/.cache"
# Known-legit package-manager / toolchain caches in ~/.cache — these grow on
# their own and are NOT a bypass signal, so they never alert even when large.
# Anything NOT in this set (e.g. a new `supertonic3` model tree) DOES alert.
CACHE_ALLOWLIST = {
    "uv", "pip", "ms-playwright", "node", "go-build", "huggingface",
    "puppeteer", "yarn", "pypoetry", "fontconfig", "mesa_shader_cache",
    "matplotlib", "chromium", "typescript", "deno", "bun",
}
BYPASS_CACHE_MIN_MB = 50          # new ~/.cache tree must exceed this to alert
BYPASS_ALERT_COOLDOWN_S = 3600    # global: at most one bypass alert per hour
# Lab paths a build process may legitimately run in (cwd/cmdline substrings).
LAB_PATH_HINTS = ("/workspace/lab", "/OllieLab", "/ollie-lab", "/.openclaw/workspace/lab")
# venv directory names to flag when newly created outside the lab.
VENV_NAMES = ("venv", ".venv")
VENV_SUFFIX = "-env"
# Known-legit Ollie-owned venvs outside the lab — these are deliberate, deployed
# subsystems (not save-derived bypasses), so they must not trip the detector.
# ollie-research/.venv holds the Curiosity Engine's crawl4ai + fastembed.
VENV_ALLOWLIST_HINTS = ("/ollie-research/.venv",)

# --- proactive-brain liveness thresholds (added 2026-06-15 after a ~2-day ------
# proactive-silence outage went undetected: heartbeat stuck in false-belief
# SILENCE loop, no process to pgrep, watchdog stayed green the whole time).
# Heartbeat oneshot timer fires every ~30 min; 90 min ≈ 3 missed beats,
# tolerant of one transient skip without false-alarming.
HEARTBEAT_MAX_AGE_S = 90 * 60
# Morning brief lands daily; 28h = full day + slack for late/off-peak delivery.
BRIEF_MAX_AGE_S = 28 * 3600
HEARTBEAT_LOG  = f"{HOME}/.openclaw/logs/heartbeat.log"
LAST_BRIEF     = f"{HOME}/.openclaw/workspace/LAST_BRIEF.md"

# --- power sentinel thresholds -------------------------------------------------
POWER_LOW_PCT = 30              # escalate below this while discharging
POWER_STALE_S = 15 * 60         # file/ts older than this => sentinel blind
POWER_ESCALATE_COOLDOWN_S = 30 * 60   # low-battery escalation: at most every 30m
POWER_BLIND_COOLDOWN_S = 6 * 3600     # blind alert: at most every 6h
POWER_BLIND_GRACE_S = 3600     # don't cry "blind" in the first hour after start
                               # (the host task may not exist yet at deploy)

# --- re-alerting thresholds (latching bug fix) --------------------------------
# Health-check failures are re-paged when:
#   1. The error content changes (different failure mode)
#   2. Cooldown expired since last alert (periodic reminder)
REPAGE_COOLDOWN_S = 6 * 3600   # don't re-page same error faster than every 6h
REMIND_INTERVAL_S = 24 * 3600  # daily reminder for persistent failures


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http(url, method="GET", payload=None, headers=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) ollie-watchdog/1.0")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


def cfg():
    return json.load(open(OPENCLAW_JSON))


def telegram_alert(text):
    try:
        token = cfg()["channels"]["telegram"]["botToken"]
        http(f"https://api.telegram.org/bot{token}/sendMessage", "POST",
             {"chat_id": OWNER_CHAT_ID, "text": text})
        return True
    except Exception as e:  # noqa: BLE001
        log(f"ALERT SEND FAILED: {e}")
        return False


# ---------------- health checks (return None if ok, else failure string) ----


def verify_token():
    return json.load(open(WA_SECRETS))["verifyToken"]


def check_gateway():
    try:
        q = urllib.parse.urlencode({"hub.mode": "subscribe",
                                    "hub.verify_token": verify_token(),
                                    "hub.challenge": "wd-ok"})
        st, body = http(f"http://127.0.0.1:18789/plugins/whatsapp-cloud/webhook?{q}")
        return None if body == "wd-ok" else f"unexpected reply ({st})"
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def check_public():
    try:
        q = urllib.parse.urlencode({"hub.mode": "subscribe",
                                    "hub.verify_token": verify_token(),
                                    "hub.challenge": "wd-pub"})
        st, body = http(f"https://{NGROK_DOMAIN}/plugins/whatsapp-cloud/webhook?{q}")
        return None if body == "wd-pub" else f"unexpected reply ({st})"
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def check_jobs_runner():
    r = subprocess.run(["pgrep", "-f", "ollie_jobs_runner.py"], capture_output=True, text=True)
    return None if r.stdout.strip() else "process not running"


def check_tailnet():
    try:
        http("http://<TAILSCALE_IP_VPS>:4040/api/v1/health", timeout=10)
        return None
    except Exception:
        # any HTTP response (even 404) means the host was reachable
        try:
            http("http://<TAILSCALE_IP_VPS>:4040/", timeout=10)
            return None
        except urllib.error.HTTPError:
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)[:120]


def check_stale_jobs():
    try:
        now = time.time()
        stale = [f for f in os.listdir(JOBS_RUNNING)
                 if now - os.path.getmtime(f"{JOBS_RUNNING}/{f}") > STALE_JOB_S]
        return f"stale running jobs: {', '.join(stale)}" if stale else None
    except OSError as e:
        return str(e)[:120]


def check_disk():
    st = os.statvfs("/")
    used_pct = 100 - (st.f_bavail * 100 // st.f_blocks)
    return f"root disk {used_pct}% full" if used_pct >= DISK_ALERT_PCT else None


def check_lab_watcher():
    """Lab watcher freshness: its hourly timer should touch state.json."""
    state = "/home/openclaw/.openclaw/workspace/lab/state.json"
    try:
        age = time.time() - os.path.getmtime(state)
    except OSError:
        return "lab state.json missing (watcher never ran?)"
    return f"lab watcher stale ({int(age / 60)}min since last run)" if age > 3 * 3600 else None


def check_heartbeat_firing():
    """Heartbeat-firing check: the proactive-brain oneshot timer fires every ~30 min.
    Scans HEARTBEAT_LOG for the most recent 'YYYY-MM-DD HH:MM:SS' prefix line and
    fails if that timestamp is older than HEARTBEAT_MAX_AGE_S (90 min ≈ 3 missed
    beats). Detection-only — no auto-recovery (see 2026-06-15 incident post-mortem)."""
    try:
        last_ts = None
        with open(HEARTBEAT_LOG) as fh:
            for line in fh:
                # Each log line starts with 'YYYY-MM-DD HH:MM:SS'; verify shape cheaply.
                if len(line) >= 19 and line[4] == "-" and line[7] == "-":
                    last_ts = line[:19]
        if last_ts is None:
            return "heartbeat.log has no timestamp lines — heartbeat may never have run"
        beat_epoch = time.mktime(time.strptime(last_ts, "%Y-%m-%d %H:%M:%S"))
        age = time.time() - beat_epoch
        if age > HEARTBEAT_MAX_AGE_S:
            return (f"heartbeat not firing — last beat {last_ts} "
                    f"({int(age / 60)}min ago, threshold {HEARTBEAT_MAX_AGE_S // 60}min)")
        return None
    except FileNotFoundError:
        return "heartbeat.log missing — heartbeat timer has never fired"
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def check_brief_delivered():
    """Brief-delivered check: a proactive morning brief should land at least once per day.
    Uses LAST_BRIEF.md mtime; fails if absent or older than BRIEF_MAX_AGE_S (28h).
    Detection-only — no auto-recovery (see 2026-06-15 incident post-mortem)."""
    try:
        age = time.time() - os.path.getmtime(LAST_BRIEF)
        if age > BRIEF_MAX_AGE_S:
            return (f"no proactive brief delivered in >{int(age / 3600)}h "
                    f"— proactivity may be stuck (threshold {BRIEF_MAX_AGE_S // 3600}h)")
        return None
    except FileNotFoundError:
        return "LAST_BRIEF.md missing — no brief ever delivered (or file was removed)"
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def check_state_backup():
    """Off-box DR backup should push daily. Alert if the most recent run failed
    to push (git rejected) or the timer hasn't run in >26h. Reads the backup
    log's latest terminal outcome only — markers from ollie-state-backup.sh:
    success 'OK: backup ... pushed' / 'exiting clean', failure 'ERROR: git push failed'."""
    p = f"{HOME}/.openclaw/logs/state-backup.log"
    try:
        age = time.time() - os.path.getmtime(p)
    except OSError:
        return "no state-backup.log — off-box DR backup may never have run"
    if age > 26 * 3600:
        return f"off-box state backup hasn't run in >{int(age / 3600)}h (timer dead?)"
    try:
        with open(p) as f:
            tail = f.readlines()[-60:]
    except OSError as e:  # noqa: BLE001
        return f"can't read state-backup.log: {e}"[:120]
    for line in reversed(tail):  # newest terminal outcome wins
        if "ERROR: git push failed" in line:
            return "off-box state backup FAILING (git push rejected) — DR gap, fix the repo"
        if "OK: backup" in line or "exiting clean" in line:
            return None
    return None


# --- S1.3: registry drift detection ------------------------------------------
# The manifest is generated from the reviewed repo (ollie-self/build_manifest.py)
# and deployed alongside registry.yaml. A deployed file whose sha256 differs
# from the manifest is drift: the box is running something nobody reviewed.
# Until S1.5 (formal deploy) the manifest is deployed by hand — after ANY file
# deploy, regenerate + redeploy it or the probe will (correctly) page.
REGISTRY_MANIFEST = f"{HOME}/.openclaw/registry/manifest.json"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_registry_drift():
    """Compare deployed file hashes against the registry manifest.

    Returns None when clean. A MISSING manifest means drift detection isn't
    deployed yet — feature off, NOT an error (same contract as other probes
    whose subsystem is absent). Any unreadable/!matching tracked file is
    reported by component + basename so the page is actionable.
    """
    try:
        with open(REGISTRY_MANIFEST) as f:
            manifest = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 — corrupt manifest is itself a failure
        return f"registry manifest unreadable: {str(e)[:80]}"

    drifted, missing = [], []
    for entry in manifest.get("entries", []):
        path = entry.get("path", "")
        label = f"{entry.get('component', '?')}:{os.path.basename(path)}"
        try:
            actual = _sha256_file(path)
        except FileNotFoundError:
            missing.append(label)
            continue
        except Exception as e:  # noqa: BLE001
            missing.append(f"{label}(unreadable:{str(e)[:30]})")
            continue
        if actual != entry.get("sha256"):
            drifted.append(label)

    if not drifted and not missing:
        return None
    parts = []
    if drifted:
        shown = ", ".join(drifted[:4]) + ("…" if len(drifted) > 4 else "")
        parts.append(f"{len(drifted)} deployed file(s) differ from reviewed source: {shown}")
    if missing:
        shown = ", ".join(missing[:4]) + ("…" if len(missing) > 4 else "")
        parts.append(f"{len(missing)} tracked file(s) missing: {shown}")
    gen = manifest.get("generated_at", "?")
    return "REGISTRY DRIFT — " + "; ".join(parts) + f" (manifest from {gen})"


# --- D4: hands liveness (added for Track D4) ---------------------------------
HANDS_URL = "http://<TAILSCALE_IP>:3200/mcp"

def _mcp_call_hands(tool_name, args=None, timeout=15):
    token = None
    try:
        oc = json.load(open(OPENCLAW_JSON))
        auth = oc["mcp"]["servers"]["hands"]["headers"]["Authorization"]
        token = auth if str(auth).startswith("Bearer ") else "Bearer " + auth
    except Exception as e:
        raise RuntimeError("cannot load hands bearer from openclaw.json: " + str(e)) from e
    hdr = {"Authorization": token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    def _post(body, to=timeout):
        req = urllib.request.Request(HANDS_URL, data=json.dumps(body).encode(), headers=hdr, method="POST")
        r = urllib.request.urlopen(req, timeout=to)
        sid = r.headers.get("mcp-session-id")
        out = None
        for line in r.read().decode(errors="replace").splitlines():
            if line.startswith("data: "):
                try: out = json.loads(line[6:])
                except Exception: pass
        return sid, out
    sid, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "ollie-watchdog", "version": "d4"}}})
    if sid: hdr["mcp-session-id"] = sid
    _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    _, res = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": args or {}}}, to=timeout)
    c = (res or {}).get("result", {}).get("content", [])
    txt = next((x.get("text") for x in c if isinstance(x, dict) and x.get("type") == "text"), None)
    if txt is None: return {"raw": str(c)[:300]}
    try: return json.loads(txt)
    except Exception: return {"raw": txt[:300]}

def check_hands_reachable():
    try:
        _mcp_call_hands("session_info", timeout=12)
        return None
    except Exception as e: return "engine unreachable: " + str(e)[:120]

def check_hands_enabled():
    try:
        info = _mcp_call_hands("session_info", timeout=12)
        if isinstance(info, dict) and info.get("hands_enabled") is False:
            return "hands unexpectedly disabled"
        return None
    except Exception as e: return "hands-enabled check failed: " + str(e)[:80]

def check_screenshot_status():
    try:
        ob = _mcp_call_hands("observe", timeout=25)
        if isinstance(ob, dict):
            status = ob.get("screenshot_status") or (ob.get("result") or {}).get("screenshot_status") or ""
            if status and status != "ok":
                return "screenshot degraded: " + status[:180]
        return None
    except Exception as e: return "observe failed: " + str(e)[:80]

# ---------------- lab-bypass detector ---------------------------------------
# Stateful, opportunistic. Persists its baseline + alert bookkeeping inside the
# watchdog's existing state dict (saved by save_state every cycle) so we add no
# new persistence mechanism. First run learns the baseline silently; thereafter
# only NEW outside-lab code trees alert, rate-limited per-path + a global 1h
# cooldown. Every branch is wrapped so the detector can never crash the daemon.


def _dir_size_mb(path):
    """Best-effort recursive size in MB; capped walk so a huge tree can't hang
    the cycle. Returns float MB (0.0 on any error)."""
    total = 0
    seen = 0
    try:
        for root, dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
            seen += 1
            if seen > 20000:  # safety cap: stop walking pathological trees
                break
    except Exception:  # noqa: BLE001
        return 0.0
    return total / (1024 * 1024)


def _scan_cache_newtrees(baseline_cache):
    """New top-level ~/.cache dirs (vs baseline) over the size threshold and not
    allowlisted. Returns (alerts, current_names) where alerts is list of
    (path, size_mb) and current_names is the full current top-level set."""
    alerts = []
    current = []
    try:
        entries = os.listdir(CACHE_DIR)
    except OSError:
        return alerts, baseline_cache  # cache dir gone/unreadable: keep baseline
    for name in entries:
        full = os.path.join(CACHE_DIR, name)
        try:
            if not os.path.isdir(full):
                continue
        except OSError:
            continue
        current.append(name)
        if name in baseline_cache or name in CACHE_ALLOWLIST:
            continue
        size_mb = _dir_size_mb(full)
        if size_mb >= BYPASS_CACHE_MIN_MB:
            alerts.append((full, round(size_mb)))
    return alerts, current


def _scan_home_venvs(baseline_venvs):
    """New venv-like dirs in the top 2 levels of $HOME (vs baseline). Returns
    (alerts, current_paths). Lab-path venvs are ignored — those are legit."""
    alerts = []
    current = []

    def _looks_venv(name):
        return name in VENV_NAMES or name.endswith(VENV_SUFFIX)

    def _consider(path):
        if any(h in path for h in LAB_PATH_HINTS):
            return
        if any(h in path for h in VENV_ALLOWLIST_HINTS):
            return  # deliberate Ollie-owned venv (e.g. Curiosity Engine), not a bypass
        current.append(path)
        if path in baseline_venvs:
            return
        alerts.append((path, _dir_size_mb(path)))

    try:
        for name in os.listdir(HOME):  # level 1
            lvl1 = os.path.join(HOME, name)
            try:
                if not os.path.isdir(lvl1):
                    continue
            except OSError:
                continue
            if _looks_venv(name):
                _consider(lvl1)
                continue  # don't descend into a venv
            try:  # level 2
                for sub in os.listdir(lvl1):
                    lvl2 = os.path.join(lvl1, sub)
                    try:
                        if os.path.isdir(lvl2) and _looks_venv(sub):
                            _consider(lvl2)
                    except OSError:
                        continue
            except OSError:
                continue
    except OSError:
        pass
    return alerts, current


def _scan_install_procs():
    """Opportunistic /proc scan for pip|uv pip|git clone running OUTSIDE any lab
    path (by cwd or cmdline). Best-effort only — the FS baseline is the reliable
    signal. Returns a list of short description strings."""
    hits = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return hits
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if not cmd:
            continue
        low = cmd.lower()
        is_install = ("pip install" in low or "uv pip" in low or "git clone" in low)
        if not is_install:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        haystack = f"{cwd} {cmd}"
        if any(h in haystack for h in LAB_PATH_HINTS):
            continue  # running inside the lab — legit
        hits.append(f"pid {pid}: {cmd[:80]} (cwd={cwd or '?'})")
    return hits


def check_lab_bypass(state):
    """Detect code being installed/run in the gateway distro instead of the lab.

    Mutates and reads bypass bookkeeping in `state`:
      fs_baseline_cache  : list[str] of known top-level ~/.cache dir names
      fs_baseline_venvs  : list[str] of known venv-like paths under $HOME
      bypass_learned     : bool, baseline established (silent first run)
      bypass_alerted     : list[str] of paths already alerted (per-path dedup)
      bypass_last_alert  : float epoch of last bypass alert (global cooldown)

    Fully defensive: any failure is logged and swallowed so the daemon's other
    duties continue uninterrupted.
    """
    try:
        base_cache = set(state.get("fs_baseline_cache", []))
        base_venvs = set(state.get("fs_baseline_venvs", []))

        cache_alerts, current_cache = _scan_cache_newtrees(base_cache)
        venv_alerts, current_venvs = _scan_home_venvs(base_venvs)

        # First run (or upgrade from older state): learn baseline silently.
        if not state.get("bypass_learned"):
            state["fs_baseline_cache"] = sorted(set(current_cache) | base_cache)
            state["fs_baseline_venvs"] = sorted(set(current_venvs) | base_venvs)
            state["bypass_learned"] = True
            state.setdefault("bypass_alerted", [])
            state.setdefault("bypass_last_alert", 0)
            log(f"lab-bypass baseline learned "
                f"({len(state['fs_baseline_cache'])} cache, "
                f"{len(state['fs_baseline_venvs'])} venv)")
            return

        proc_hits = _scan_install_procs()

        alerted = set(state.get("bypass_alerted", []))
        last_alert = float(state.get("bypass_last_alert", 0) or 0)

        # Build candidate list (path, size_mb); procs ride along on any FS alert.
        candidates = list(cache_alerts) + list(venv_alerts)
        fresh = [(p, mb) for (p, mb) in candidates if p not in alerted]

        now = time.time()
        for path, size_mb in fresh:
            if now - last_alert < BYPASS_ALERT_COOLDOWN_S:
                # In cooldown: stay silent but do NOT mark as alerted — the
                # path alerts on a later cycle once the cooldown expires.
                continue
            telegram_alert(
                f"⚠️ lab-bypass? new code tree outside sandbox: {path} "
                f"({int(size_mb)}MB) — save-derived code must run via lab"
            )
            log(f"LAB-BYPASS {path} ({int(size_mb)}MB)")
            if proc_hits:
                log("LAB-BYPASS procs: " + " | ".join(proc_hits))
            alerted.add(path)
            last_alert = now

        # Persist bookkeeping. The baseline stays as learned (NOT refreshed with
        # current trees) so a tree that appears small and only later crosses the
        # size threshold still alerts; per-path dedup lives in bypass_alerted.
        state["bypass_alerted"] = sorted(alerted)
        state["bypass_last_alert"] = last_alert
    except Exception as e:  # noqa: BLE001 — detector must never crash the daemon
        log(f"lab-bypass check error: {e}")


# ---------------- power sentinel (host-bridged battery state) ----------------


def _parse_power_ts(ts):
    """ISO ts (host writes UTC '...Z' or '+00:00') -> epoch seconds, or None."""
    try:
        s = ts.replace("Z", "+00:00")
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return None


def check_power(state):
    """Read host-power.json (written by the host scheduled task) and alert on:
      (a) AC->battery transition  : "plug me back in" (once per discharge episode)
      (b) low pct while discharging: escalate (rate-limited to every 30 min)
      (c) file missing / ts stale  : "sentinel blind" (rate-limited to every 6h),
          suppressed during the first hour after this watchdog started so we
          don't alarm before the host task has had a chance to write.

    All bookkeeping lives in the shared state dict under "power"; wrapped in a
    blanket try/except so a malformed file can never take the daemon down.
    """
    p = state.setdefault("power", {})
    now = time.time()
    # Anchor the grace window to the first time we ever ran this check.
    p.setdefault("baseline_ts", now)

    try:
        try:
            raw = json.load(open(HOST_POWER_JSON))
        except FileNotFoundError:
            raw = None
        except Exception as e:  # noqa: BLE001 — corrupt/half-written: treat as stale
            log(f"power: unreadable host-power.json ({e})")
            raw = None

        # --- (c) blind detection: missing file OR stale timestamp ---
        ts_epoch = _parse_power_ts(raw.get("ts", "")) if raw else None
        stale = raw is None or ts_epoch is None or (now - ts_epoch) > POWER_STALE_S
        if stale:
            in_grace = (now - p["baseline_ts"]) < POWER_BLIND_GRACE_S
            last_blind = p.get("last_blind_ts", 0)
            if not in_grace and (now - last_blind) > POWER_BLIND_COOLDOWN_S:
                age_txt = "missing" if ts_epoch is None else f"{int((now - ts_epoch) / 60)}min old"
                telegram_alert(f"🔌 power sentinel blind (host task dead? state {age_txt})")
                p["last_blind_ts"] = now
                log(f"power BLIND ({age_txt})")
            return  # nothing trustworthy to evaluate transitions against

        on_ac = bool(raw.get("on_ac"))
        pct = raw.get("pct")

        if on_ac:
            # Recovery: reset the discharge episode so the next unplug re-alerts.
            # Recovery is intentionally SILENT (no spam on replug).
            if p.get("discharging"):
                log(f"power: back on AC ({pct}%) — discharge episode cleared")
            p["discharging"] = False
            p.pop("transition_alerted", None)
            p.pop("last_escalate_ts", None)
            return

        # --- on battery ---
        # (a) AC->battery transition: alert ONCE per discharge episode.
        if not p.get("transition_alerted"):
            pct_txt = f"{pct}%" if pct is not None else "??%"
            telegram_alert(f"⚡ I'm on battery ({pct_txt}) — plug me back in")
            p["transition_alerted"] = True
            log(f"power TRANSITION ac->battery ({pct_txt})")
        p["discharging"] = True

        # (b) low + falling escalation, rate-limited.
        if pct is not None and pct < POWER_LOW_PCT:
            last_esc = p.get("last_escalate_ts", 0)
            if (now - last_esc) > POWER_ESCALATE_COOLDOWN_S:
                telegram_alert(f"🪫 {pct}% and falling — I die soon")
                p["last_escalate_ts"] = now
                log(f"power ESCALATE ({pct}%)")
    except Exception as e:  # noqa: BLE001 — never let power checks break the loop
        log(f"power check error: {e}")


# ---------------- daily quota probes ----------------------------------------


def auth_key(profile):
    return json.load(open(AUTH_PROFILES))["profiles"][profile]["key"]


def classify(fn):
    """Run probe fn; return None if healthy, else short failure reason."""
    try:
        fn()
        return None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        return f"HTTP {e.code}: {body}"
    except Exception as e:  # noqa: BLE001
        return str(e)[:160]


def probe_minimax():
    http("https://api.minimax.io/anthropic/v1/messages", "POST",
         {"model": "MiniMax-M3", "max_tokens": 8,
          "messages": [{"role": "user", "content": "ok"}]},
         {"Authorization": f"Bearer {auth_key('minimax:global')}"}, timeout=45)


def probe_groq():
    http("https://api.groq.com/openai/v1/chat/completions", "POST",
         {"model": "llama-3.3-70b-versatile", "max_tokens": 5,
          "messages": [{"role": "user", "content": "ok"}]},
         {"Authorization": f"Bearer {auth_key('groq:global')}"}, timeout=30)


def probe_nvidia():
    http("https://integrate.api.nvidia.com/v1/chat/completions", "POST",
         {"model": "nvidia/llama-3.3-nemotron-super-49b-v1.5", "max_tokens": 8,
          "messages": [{"role": "user", "content": "ok"}]},
         {"Authorization": f"Bearer {auth_key('nvidia:global')}"}, timeout=60)


def probe_brave():
    key = cfg()["mcp"]["servers"]["brave-search"]["env"]["BRAVE_API_KEY"]
    http("https://api.search.brave.com/res/v1/web/search?q=health&count=1",
         headers={"X-Subscription-Token": key, "Accept": "application/json"})


def probe_zeus():
    http("https://lightningzeus.com/v1/messages", "POST",
         {"model": "claude-opus-4.8", "max_tokens": 8,
          "messages": [{"role": "user", "content": "ok"}]},
         {"Authorization": f"Bearer {auth_key('zeus:global')}"}, timeout=45)


QUOTA_PROBES = {
    "minimax-llm": probe_minimax,
    "zeus-opus": probe_zeus,
    "groq-llm": probe_groq,
    "nvidia-llm": probe_nvidia,
    "brave-search": probe_brave,
}


HEALTH_CHECKS = {
    "gateway": check_gateway,
    "public-webhook": check_public,
    "jobs-runner": check_jobs_runner,
    "tailnet-4dpocket": check_tailnet,
    "stale-jobs": check_stale_jobs,
    "disk": check_disk,
    "lab-watcher": check_lab_watcher,
    # Proactive-brain liveness (added 2026-06-15 after ~2-day silent outage).
    "heartbeat-firing": check_heartbeat_firing,
    "brief-delivered": check_brief_delivered,
    "state-backup": check_state_backup,
    # S1.3 registry drift (deployed files vs reviewed source)
    "registry-drift": check_registry_drift,
    # D4 hands liveness
    "hands-reachable": check_hands_reachable,
    "hands-enabled": check_hands_enabled,
    "hands-screenshot": check_screenshot_status,
}


# ---------------- state + alerting ------------------------------------------


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:  # noqa: BLE001
        return {"failures": {}, "last_quota_day": ""}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=1)


def run_cycle(state):
    # Power is stateful (transition / cooldown bookkeeping in the state dict)
    # and self-alerting, so it runs outside the stateless HEALTH_CHECKS map.
    check_power(state)

    results = {name: fn() for name, fn in HEALTH_CHECKS.items()}

    # Lab-bypass detector: stateful + self-rate-limited, so it alerts directly
    # rather than feeding the fail/recover state machine above. Runs every cycle.
    check_lab_bypass(state)

    today = time.strftime("%Y-%m-%d")
    if state.get("last_quota_day") != today:
        for name, fn in QUOTA_PROBES.items():
            results[name] = classify(fn)
        state["last_quota_day"] = today

    prev = state.get("failures", {})
    alert_state = state.setdefault("alert_state", {})
    now = time.time()
    new_failures, recovered, reminded = [], [], []
    for name, err in results.items():
        if err is None and name in prev:
            recovered.append(name)
            alert_state.pop(name, None)
            continue
        if err and name not in prev:
            new_failures.append(f"{name}: {err}")
            alert_state[name] = {"err": err, "ts": now, "remind_ts": now}
            continue
        if err is None or name not in prev:
            continue
        # err is truthy AND name was already failing — check for re-alert
        try:
            as_ = alert_state.get(name, {})
            prev_err = as_.get("err", "")
            last_ts = as_.get("ts", 0)
            err_changed = err != prev_err
            cooldown_ok = (now - last_ts) > REPAGE_COOLDOWN_S
            remind_ok = (now - as_.get("remind_ts", 0)) > REMIND_INTERVAL_S
            if err_changed or cooldown_ok or remind_ok:
                new_failures.append(f"{name}: {err}")
                as_["err"] = err
                as_["ts"] = now
                if not err_changed and remind_ok:
                    as_["remind_ts"] = now
                    reminded.append(name)
                alert_state[name] = as_
        except Exception:  # noqa: BLE001 — never break alerting on bookkeeping
            new_failures.append(f"{name}: {err}")
            alert_state[name] = {"err": err, "ts": now, "remind_ts": now}

    failures = {n: e for n, e in results.items() if e}
    # carry forward failures of checks not run this cycle (quota probes off-day)
    for n, e in prev.items():
        if n not in results:
            failures[n] = e
    state["failures"] = failures

    if new_failures:
       telegram_alert("🚨 Ollie watchdog — problems detected:\n" +
                       "\n".join(f"• {f}" for f in new_failures))
    if recovered:
        telegram_alert("✅ Ollie watchdog — recovered: " + ", ".join(recovered))
    for f in new_failures:
        log(f"FAIL {f}")
    for r in recovered:
        log(f"RECOVERED {r}")
    if reminded:
        log(f"REMIND {', '.join(reminded)}")
    if not new_failures and not recovered:
        log(f"ok ({len(failures)} known issues)" if failures else "all healthy")


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        time.sleep(60)
        sys.exit(0)
    log("watchdog started")
    state = load_state()
    while True:
        try:
            run_cycle(state)
            save_state(state)
        except Exception as e:  # noqa: BLE001
            log(f"cycle error: {e}")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
