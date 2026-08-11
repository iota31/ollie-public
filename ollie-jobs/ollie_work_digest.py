#!/usr/bin/env python3
"""Ollie work digest — the deterministic ground-truth record.

Builds {WORKSPACE}/WORK_DIGEST.md from pure disk reads (NO LLM calls): a
compact, authoritative summary of recent autonomous work that the brief-writing
LLM and the conversational agent can cite and must never contradict. Born from
an incident where overnight "lab POC" jobs wrote inflated "poc-done" labels
(web research mislabeled as executed POCs), the brief amplified them, and the
chat agent had no ground truth. This is that ground truth.

Pure stdlib; runs on the box's bare WSL python3. The whole build is wrapped so
no exception ever reaches the caller — a digest failure must never break a job,
a beat, or a tick.
"""
import json
import os
import time

HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
WORKSPACE = f"{HOME}/.openclaw/workspace"
LOGS = f"{HOME}/.openclaw/logs"

DIGEST_PATH = f"{WORKSPACE}/WORK_DIGEST.md"
DIGEST_LOG = f"{LOGS}/work-digest.log"
MAX_CHARS = 2000
DAY_S = 24 * 3600


def _paths():
    """Resolve runtime paths fresh each call so tests can repoint HOME.
    (Module constants are computed at import; tests reassign the module
    globals WORKSPACE/LOGS and we read those reassigned globals here.)"""
    return {
        "jobs": f"{WORKSPACE}/jobs",
        "ledger": f"{WORKSPACE}/lab/LAB_LEDGER.md",
        "projects": f"{WORKSPACE}/projects",
        "last_brief": f"{WORKSPACE}/LAST_BRIEF.md",
        "digest": f"{WORKSPACE}/WORK_DIGEST.md",
        "log": f"{LOGS}/work-digest.log",
    }


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _parse_ts(s):
    """Parse the runner's '%Y-%m-%dT%H:%M:%S' timestamps to epoch; None on fail."""
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _count_json(d):
    try:
        return len([f for f in os.listdir(d) if f.endswith(".json")])
    except OSError:
        return 0


def _jobs_section(p):
    """Done jobs finished within 24h, newest first, plus queue/running/failed counts."""
    jobs_dir = p["jobs"]
    done_dir = f"{jobs_dir}/done"
    now = time.time()
    rows = []  # (finished_epoch, line)
    try:
        files = [f for f in os.listdir(done_dir) if f.endswith(".json")]
    except OSError:
        files = []
    for f in files:
        try:
            j = json.load(open(f"{done_dir}/{f}"))
        except Exception:  # noqa: BLE001
            continue
        fin = _parse_ts(j.get("finished"))
        if fin is None or (now - fin) > DAY_S:
            continue
        hhmm = time.strftime("%H:%M", time.localtime(fin))
        sc = j.get("status_class") or j.get("status") or "?"
        task = (j.get("task") or "?").replace("\n", " ").strip()[:70]
        ev = j.get("evidence") or []
        ev_str = ", ".join(ev)[:120] if ev else "-"
        rows.append((fin, f"- {hhmm} {sc} | {task} | evidence: {ev_str}"))
    rows.sort(key=lambda r: r[0], reverse=True)

    qn = _count_json(f"{jobs_dir}/queue")
    rn = _count_json(f"{jobs_dir}/running")
    fn = _count_json(f"{jobs_dir}/failed")
    lines = [f"## Jobs (last 24h) — queue={qn} running={rn} failed={fn}"]
    if rows:
        lines.extend(r[1] for r in rows)
    else:
        lines.append("- (none in last 24h)")
    return lines, rows


def _ledger_section(p):
    """Last ~6 non-empty ledger lines, verbatim, truncated to 120 chars each."""
    lines = ["## Lab ledger (tail)"]
    try:
        # Tail-read: the ledger is append-only and unbounded; 6 lines always
        # fit in the last 4KB (same pattern as _journal_heading).
        with open(p["ledger"]) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read()
        raw = [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
        if size > 4096 and raw:
            raw = raw[1:]  # drop a possibly mid-line first entry
    except OSError:
        raw = []
    if not raw:
        lines.append("- (no ledger)")
        return lines
    for ln in raw[-6:]:
        lines.append(ln[:120])
    return lines


def _journal_heading(proj_dir):
    """Last JOURNAL.md entry heading (a '## ...' line), cheaply — last 4KB only."""
    try:
        with open(f"{proj_dir}/JOURNAL.md") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
            except OSError:
                pass
            tail = f.read()
    except OSError:
        return None
    heads = [ln.strip() for ln in tail.splitlines() if ln.strip().startswith("## ")]
    return heads[-1] if heads else None


def _projects_section(p):
    lines = ["## Projects"]
    pdir = p["projects"]
    try:
        slugs = sorted(d for d in os.listdir(pdir)
                       if not d.startswith("_") and os.path.isdir(f"{pdir}/{d}"))
    except OSError:
        slugs = []
    if not slugs:
        lines.append("- (no projects)")
        return lines
    for slug in slugs:
        d = f"{pdir}/{slug}"
        try:
            st = json.load(open(f"{d}/state.json"))
        except Exception:  # noqa: BLE001
            continue
        status = st.get("status", "?")
        total = st.get("sessions_total", 0)
        last = st.get("last_session") or st.get("last_date") or "-"
        line = f"- {slug}: {status}, sessions_total={total}, last={last}"
        head = _journal_heading(d)
        if head:
            line += f" | {head[:60]}"
        lines.append(line)
    if len(lines) == 1:
        lines.append("- (no readable project state)")
    return lines


def host_section(home=None):
    """One-line '## Host' digest section from host-power.json (bridged in by
    the host's OlliePowerSentinel task — the distro can't see the battery).

    Returns the markdown section string, or "" if the file is missing or
    unreadable (section simply omitted — guarded, never raises).

        on AC, battery 100%   -> on AC
        ON BATTERY, 64%       -> discharging
        power state unknown   -> file present but shape off
    """
    if home is not None:
        path = f"{home}/.openclaw/workspace/host-power.json"
    else:
        path = f"{WORKSPACE}/host-power.json"
    try:
        raw = json.load(open(path))
    except Exception:  # noqa: BLE001 — missing/corrupt: omit the section entirely
        return ""
    try:
        pct = raw.get("pct")
        on_ac = raw.get("on_ac")
        if on_ac and pct is not None:
            body = f"on AC, battery {pct}%"
        elif on_ac:
            body = "on AC"
        elif not on_ac and pct is not None:
            body = f"ON BATTERY, {pct}%"
        else:
            body = "power state unknown"
    except Exception:  # noqa: BLE001
        body = "power state unknown"
    return f"## Host\n{body}\n"


def _last_brief_section(p):
    path = p["last_brief"]
    if not os.path.exists(path):
        return []
    try:
        txt = open(path).read()
    except OSError:
        return []
    first = txt.splitlines()[0] if txt else ""
    snippet = txt[:300].replace("\n", " ").strip()
    return ["## Last brief", first[:120], snippet]


def _truncate(text):
    """Hard cap at MAX_CHARS. Sections are ordered newest-work-first (jobs,
    then ledger/projects/brief), so trimming from the end drops the oldest /
    least-critical context; we mark the cut explicitly."""
    if len(text) <= MAX_CHARS:
        return text
    cut = text[: MAX_CHARS - len("\n…(truncated)")]
    # don't cut mid-line — drop the partial trailing line
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + "\n…(truncated)"


def build_digest():
    """Build the digest, write it to WORK_DIGEST.md, and return the text.
    May raise — callers in long-running daemons should use update_digest()."""
    p = _paths()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = [
        "# WORK DIGEST (ground truth — auto-generated)",
        f"Generated: {now_iso}",
        "This is the authoritative record of recent autonomous work. Cite it; "
        "never contradict it; never claim or deny work without checking it.",
        "",
    ]

    host = host_section()
    if host:
        out.append(host.rstrip())
        out.append("")

    jobs_lines, _ = _jobs_section(p)
    out.extend(jobs_lines)
    out.append("")
    out.extend(_ledger_section(p))
    out.append("")
    out.extend(_projects_section(p))
    brief = _last_brief_section(p)
    if brief:
        out.append("")
        out.extend(brief)

    text = _truncate("\n".join(out) + "\n")

    os.makedirs(os.path.dirname(p["digest"]), exist_ok=True)
    tmp = f"{p['digest']}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, p["digest"])
    return text


def update_digest():
    """Crash-proof wrapper: build the digest, swallow & log ANY exception so a
    digest failure never propagates into a job/beat/tick."""
    try:
        build_digest()
    except Exception as e:  # noqa: BLE001
        _log(f"work-digest build failed: {e}")


if __name__ == "__main__":
    build_digest()
