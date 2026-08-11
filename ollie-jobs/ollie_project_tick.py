#!/usr/bin/env python3
"""Project tick — the mechanical scheduler of Ollie's project tier.

Fired every 2h (07:00-23:00 box time) by a systemd timer. Picks the
highest-priority ACTIVE project with session budget left, runs ONE bounded
worker session (fresh agent, durable state on disk), parses the strict
session protocol, updates state, and pings Tushar on Telegram only for
MILESTONE / BLOCKED / DONE / FAILED — never per-session noise.

Projects live in ~/.openclaw/workspace/projects/<slug>/:
  PROJECT.md PLAN.md JOURNAL.md inbox.md state.json repo/
Protocol (final line of session output, fail-closed):
  CONTINUE | MILESTONE: <msg> | BLOCKED: <question> | DONE | FAILED: <why>
Two consecutive protocol failures auto-block the project (a confused model
must not grind a project into mush).
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ollie_jobs_runner import deliver, sanitize  # noqa: E402
# Guarded: a missing/broken digest module must cost us the digest, not the tick.
try:
    from ollie_work_digest import update_digest as refresh_work_digest  # noqa: E402
except Exception:  # noqa: BLE001
    def refresh_work_digest():
        pass

HOME = "/home/openclaw"
WS = f"{HOME}/.openclaw/workspace"
PROJECTS = f"{WS}/projects"
DOCTRINE = f"{WS}/PROJECT_DOCTRINE.md"
LOG = f"{HOME}/.openclaw/logs/project-tick.log"
NODE = f"{HOME}/.openclaw/tools/node-v22.22.0/bin/node"
OPENCLAW = f"{HOME}/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js"
LOCK = "/tmp/ollie-project-tick.lock"
OWNER_TELEGRAM = "<OWNER_TELEGRAM_CHAT_ID>"

SESSION_TIMEOUT_S = 2700      # 45 min hard cap per worker session
MAX_SESSIONS_PER_DAY = 4
MAX_ACTIVE_PROJECTS = 2
JOURNAL_TAIL_CHARS = 6000
MIN_MEM_AVAILABLE_MB = 3072
MAX_DISK_PCT = 85


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read(path, max_chars=8000, tail=False):
    try:
        s = open(path).read()
    except OSError:
        return "(missing)"
    return s[-max_chars:] if tail else s[:max_chars]


def load_state(slug):
    try:
        return json.load(open(f"{PROJECTS}/{slug}/state.json"))
    except Exception:  # noqa: BLE001
        return None


def save_state(slug, st):
    path = f"{PROJECTS}/{slug}/state.json"
    tmp = f"{path}.tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def health_ok():
    try:
        mem = int(re.search(r"MemAvailable:\s+(\d+)", open("/proc/meminfo").read()).group(1)) // 1024
        if mem < MIN_MEM_AVAILABLE_MB:
            return f"memory low ({mem}MB)"
        st = os.statvfs("/")
        pct = 100 - (st.f_bavail * 100 // st.f_blocks)
        if pct >= MAX_DISK_PCT:
            return f"disk {pct}%"
    except Exception as e:  # noqa: BLE001
        return f"health probe failed: {e}"
    return None


def pick_project():
    """Highest-priority active project with budget left today."""
    today = time.strftime("%Y-%m-%d")
    candidates = []
    try:
        slugs = [d for d in os.listdir(PROJECTS)
                 if not d.startswith("_") and os.path.isdir(f"{PROJECTS}/{d}")]
    except OSError:
        return None, None
    active = []
    for slug in slugs:
        st = load_state(slug)
        if not st or st.get("status") != "active":
            continue
        active.append((slug, st))
    for slug, st in sorted(active, key=lambda x: x[1].get("priority", 9))[:MAX_ACTIVE_PROJECTS]:
        if st.get("last_date") != today:
            st["sessions_today"] = 0
            st["last_date"] = today
        if st.get("sessions_today", 0) < MAX_SESSIONS_PER_DAY:
            candidates.append((slug, st))
    return candidates[0] if candidates else (None, None)


def run_session(slug, st):
    p = f"{PROJECTS}/{slug}"
    inbox = read(f"{p}/inbox.md", 4000)
    prompt = (
        f"PROJECT WORK SESSION — project '{slug}' — {time.strftime('%Y-%m-%d %H:%M')} box time.\n"
        f"Session {st.get('sessions_today', 0) + 1}/{MAX_SESSIONS_PER_DAY} today, "
        f"{st.get('sessions_total', 0)} total so far. Hard cap: 45 minutes.\n\n"
        f"=== DOCTRINE (follow EXACTLY, incl. the final protocol line) ===\n{read(DOCTRINE)}\n\n"
        f"=== PROJECT.md (charter) ===\n{read(f'{p}/PROJECT.md')}\n\n"
        f"=== PLAN.md ===\n{read(f'{p}/PLAN.md')}\n\n"
        f"=== JOURNAL.md (tail) ===\n{read(f'{p}/JOURNAL.md', JOURNAL_TAIL_CHARS, tail=True)}\n\n"
        f"=== inbox.md (messages from Tushar — consume these) ===\n{inbox}\n\n"
        f"Project dir: {p} (repo at {p}/repo). Advance the project by ONE "
        f"meaningful, VERIFIED increment now."
    )
    r = subprocess.run(
        [NODE, OPENCLAW, "--log-level", "silent", "agent", "--agent", "main",
         "--session-key", f"project-{slug}-{int(time.time())}", "-m", prompt],
        capture_output=True, text=True, timeout=SESSION_TIMEOUT_S,
        env={**os.environ, "HOME": HOME},
    )
    return sanitize(r.stdout)


def parse_protocol(out):
    """Find the session's protocol line. Scans the last few non-empty lines
    (a stray tool/cleanup line can print AFTER the protocol line — that bug
    mislabeled a session that had done real, verified work as FAILED).
    Closest-to-end protocol token wins. A missing protocol token is NOT a
    failure: the work persists on disk and the next session re-verifies it,
    so we CONTINUE rather than fail-close toward auto-block. Only a truly
    empty session (crash/timeout produces that) is a hard FAILED."""
    if not out or not out.strip():
        return "FAILED", "empty session output"
    nonempty = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for line in reversed(nonempty[-8:]):
        m = re.match(r"^(CONTINUE|DONE)\b\.?$|^(MILESTONE|BLOCKED|FAILED):\s*(.+)$", line, re.IGNORECASE)
        if m:
            kind = (m.group(1) or m.group(2)).upper()
            return kind, (m.group(3) or "").strip()
    return "CONTINUE", "(no protocol line emitted; work persists, continuing)"


def notify(text):
    try:
        deliver({"channel": "telegram", "to": OWNER_TELEGRAM}, text)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"telegram notify failed: {e}")
        return False


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("previous tick still running -> skip")
        return 0

    bad = health_ok()
    if bad:
        log(f"tick skipped: {bad}")
        return 0

    slug, st = pick_project()
    if not slug:
        log("tick: no active project with budget -> idle")
        return 0

    # Reserve both the project lane and global autonomous-spend budget before
    # launching a costly session. The deterministic id survives a crash
    # between reservation and state persistence without double charging.
    reservation_id = f"project:{slug}:{st.get('sessions_total', 0) + 1}"
    try:
        gate = subprocess.run(
            ["python3", f"{HOME}/bin/budget.py", "reserve", "project", reservation_id],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log(f"tick skipped: project budget unavailable ({e})")
        return 0
    if gate.returncode != 0:
        why = (gate.stdout or gate.stderr or "budget refused").strip()
        log(f"tick skipped: {why}")
        return 0

    log(f"tick: session start for '{slug}' "
        f"({st.get('sessions_today', 0) + 1}/{MAX_SESSIONS_PER_DAY} today)")
    st["sessions_today"] = st.get("sessions_today", 0) + 1
    st["sessions_total"] = st.get("sessions_total", 0) + 1
    st["last_session"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(slug, st)  # persist BEFORE the long run (crash-safe budget)
    try:
        out = run_session(slug, st)
        kind, payload = parse_protocol(out)
    except subprocess.TimeoutExpired:
        kind, payload = "FAILED", f"session hit the {SESSION_TIMEOUT_S}s cap"
    except Exception as e:  # noqa: BLE001
        kind, payload = "FAILED", f"runner error: {e}"

    st = load_state(slug) or st  # session may have edited state.json
    if kind == "FAILED":
        st["protocol_failures"] = st.get("protocol_failures", 0) + 1
        if st["protocol_failures"] >= 2:
            st["status"] = "blocked"
            notify(f"⚠️ project {slug}: two failed sessions in a row — auto-paused. "
                   f"Last: {payload[:160]}. Say 'resume project {slug}' after a look.")
        log(f"tick: '{slug}' FAILED ({payload[:160]}) "
            f"[failures={st['protocol_failures']}]")
    else:
        st["protocol_failures"] = 0
        if kind == "MILESTONE":
            notify(f"📍 {slug}: {payload[:600]}")
        elif kind == "BLOCKED":
            st["status"] = "blocked"
            notify(f"🚧 {slug} needs you: {payload[:600]}\n"
                   f"(answer me in chat — I'll file it and resume)")
        elif kind == "DONE":
            st["status"] = "review"
            notify(f"✅ {slug}: definition of done met — ready for your review. "
                   f"Details in the journal; deliverable links in my last journal entry.")
        log(f"tick: '{slug}' {kind} {('- ' + payload[:120]) if payload else ''}")
    save_state(slug, st)
    # Refresh the deterministic ground-truth digest after the session lands
    # (same guarded no-op pattern as the runner/heartbeat).
    refresh_work_digest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
