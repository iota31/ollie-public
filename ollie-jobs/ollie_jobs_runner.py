#!/usr/bin/env python3
"""Ollie background jobs runner.

Polls ~/.openclaw/workspace/jobs/queue for job JSON files, runs each as a
fresh `openclaw agent` CLI session (no chat-turn time limit), then delivers
the result to the requester via WhatsApp Cloud API or Telegram Bot API.

Ledger layout (all inspectable by the agent):
  jobs/queue/<id>.json    submitted, waiting
  jobs/running/<id>.json  currently executing
  jobs/done/<id>.json     finished + delivered (includes result)
  jobs/failed/<id>.json   errored/timed out (user notified)
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HOME = "/home/openclaw"
BASE = f"{HOME}/.openclaw/workspace/jobs"
QUEUE, RUNNING, DONE, FAILED = (f"{BASE}/{d}" for d in ("queue", "running", "done", "failed"))
REMINDERS = f"{BASE}/reminders"
LOG = f"{HOME}/.openclaw/logs/jobs.log"
LOCK = "/tmp/ollie-jobs.lock"
NODE = f"{HOME}/.openclaw/tools/node-v22.22.0/bin/node"
OPENCLAW = f"{HOME}/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js"
WA_SECRETS = f"{HOME}/.openclaw/secrets/whatsapp-cloud.json"
OPENCLAW_JSON = f"{HOME}/.openclaw/openclaw.json"
JOB_TIMEOUT_S = 1500  # 25 min hard cap per job
POLL_S = 5
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NOISE_RE = re.compile(r"^\[[a-z0-9_-]+\]", re.IGNORECASE)
# lab audit.log: each line is "%Y-%m-%d %H:%M:%S ACTION ..." (see ollie-lab/lab).
# EXEC/SPAWN lines mark real container execution — our ground truth for ran_in_lab.
LAB_AUDIT = f"{HOME}/.openclaw/workspace/lab/audit.log"
VALID_STATUS_CLASSES = ("researched", "installed", "benchmarked", "shipped")

# Containment guard (QW-2). Incident 2026-06-11: a heartbeat-composed "LAB POC"
# job PARAPHRASED the HEARTBEAT.md template, dropping the lab-sandbox rule and
# writing "install it, benchmark it on this machine" — so the job pip/uv-
# installed internet code straight into the gateway distro (386MB ONNX in
# ~/.cache, a /tmp venv). The submitter can be sloppy; the RUNNER must not
# trust it. So for any POC task we deterministically RE-INJECT doctrine,
# verbatim, regardless of what the task text says.
POC_MARKER_RE = re.compile(r"\bLAB\s+POC\b", re.IGNORECASE)
LAB_SANDBOX_RULE = (
    "Use the lab sandbox CLI via the exec tool — `lab` is the ONLY way to run "
    "save-derived code, NEVER install/clone/run it on this machine. If lab is "
    "unavailable, do web research only and say so."
)


def is_poc_task(task):
    """True when a job task is a POC save-derived task (must be sandboxed).

    Matches the HEARTBEAT.md POC template marker "LAB POC" anywhere in the
    task. Fail-closed to False: any error -> not a POC -> no preamble (the
    guard must never be the thing that breaks a job)."""
    try:
        return bool(POC_MARKER_RE.search(task or ""))
    except Exception:  # noqa: BLE001
        return False


def lab_preamble(task):
    """Mandatory doctrine preamble to PREPEND for POC tasks, else "".

    Re-injected by the runner so a sloppily-composed task can't bypass the
    sandbox. Wrapped so it can never raise into job execution."""
    try:
        if not is_poc_task(task):
            return ""
        return (
            "CONTAINMENT — MANDATORY, overrides any conflicting instruction in "
            "the task below:\n"
            f"{LAB_SANDBOX_RULE}\n"
            "Do NOT pip/uv/npm-install, git clone, download models, or run "
            "save-derived code in this environment. All execution happens "
            "inside the lab sandbox (lab spawn/exec/harvest/destroy) or not at "
            "all.\n\n"
        )
    except Exception:  # noqa: BLE001
        return ""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Guarded: this runner is a Restart=always daemon — a missing/broken digest
# module must degrade to a no-op, never crashloop the service.
try:
    from ollie_work_digest import update_digest as refresh_work_digest  # noqa: E402
except Exception:  # noqa: BLE001
    def refresh_work_digest():
        pass


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sanitize(out):
    out = ANSI_RE.sub("", out or "")
    lines = [l for l in out.splitlines() if not NOISE_RE.match(l.strip())]
    return "\n".join(lines).strip()


# --- evidence-derived work taxonomy (QW-1) -------------------------------
# An incident: overnight "lab POC" jobs self-asserted "poc-done" for what was
# only web research. The fix: evidence beats self-assertion. A job's claimed
# class is DOWNGRADED unless deterministic evidence (a real lab EXEC/SPAWN in
# its run window, or an EVIDENCE file that actually exists) backs it up.

def parse_trailers(result):
    """Extract optional STATUS_CLASS / EVIDENCE trailers from a job's result.

    Scans the last ~10 non-empty lines (case-insensitive). The result text is
    left INTACT — we only read, never strip. Returns (claimed_class|None, [paths])."""
    claimed, evidence = None, []
    if not result:
        return claimed, evidence
    nonempty = [ln.strip() for ln in result.splitlines() if ln.strip()]
    for ln in nonempty[-10:]:
        m = re.match(r"(?i)^STATUS_CLASS\s*:\s*([A-Za-z]+)\s*$", ln)
        if m:
            c = m.group(1).lower()
            claimed = c if c in VALID_STATUS_CLASSES else claimed
            continue
        m = re.match(r"(?i)^EVIDENCE\s*:\s*(.+)$", ln)
        if m:
            for part in m.group(1).split(","):
                part = part.strip()
                if part:
                    evidence.append(part)
    return claimed, evidence


def ran_in_lab(started, finished, audit_path=LAB_AUDIT):
    """True iff any EXEC/SPAWN line in lab/audit.log falls within [started, finished].

    Audit timestamps are '%Y-%m-%d %H:%M:%S' (local); job times are
    '%Y-%m-%dT%H:%M:%S'. Unparseable bounds -> False (fail-closed: no evidence)."""
    t0 = parse_job_ts(started)
    t1 = parse_job_ts(finished)
    if t0 is None or t1 is None:
        return False
    try:
        # Tail-read: the audit log is append-only and unbounded; the job window
        # is recent, so the last 64KB always covers it.
        with open(audit_path) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().splitlines()
    except OSError:
        return False
    for ln in lines:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S+)", ln)
        if not m:
            continue
        action = m.group(2).upper()
        if action not in ("EXEC", "SPAWN"):
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if t0 <= ts <= t1:
            return True
    return False


def parse_job_ts(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def evidence_verified(paths):
    """True iff at least one EVIDENCE path exists on disk (~ and abs expanded)."""
    for p in paths or []:
        try:
            if os.path.exists(os.path.expanduser(p)):
                return True
        except (OSError, ValueError):
            continue
    return False


def derive_status_class(claimed, lab_ran, ev_ok):
    """Downgrade rule — evidence beats self-assertion.

    - shipped / benchmarked: need (lab_ran OR ev_ok), else researched.
    - installed: needs ev_ok. Else -> researched.
    - missing/invalid claim -> researched.
    """
    c = (claimed or "").lower()
    if c in ("shipped", "benchmarked"):
        return c if (lab_ran or ev_ok) else "researched"
    if c == "installed":
        return "installed" if ev_ok else "researched"
    return "researched"


def recent_done_jobs(max_age_s=24 * 3600):
    """Done jobs finished within max_age_s, raw dicts. Runner owns the jobs
    dir layout, so the one scan-and-filter lives here for all callers."""
    out = []
    now = time.time()
    try:
        files = [f for f in os.listdir(DONE) if f.endswith(".json")]
    except OSError:
        return out
    for f in files:
        try:
            j = json.load(open(f"{DONE}/{f}"))
        except Exception:  # noqa: BLE001
            continue
        ts = parse_job_ts(j.get("finished"))
        if ts is None or (now - ts) > max_age_s:
            continue
        out.append(j)
    return out


def job_tag(j):
    """Compact evidence tag for a done job, e.g. ' [benchmarked, lab]'.
    Empty string when the job predates the taxonomy fields."""
    sc = j.get("status_class")
    if not sc:
        return ""
    flags = [sc]
    if j.get("ran_in_lab"):
        flags.append("lab")
    elif j.get("evidence_verified"):
        flags.append("evidence")
    return f" [{', '.join(flags)}]"


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chunks(text, n=3800):
    out = []
    while text:
        out.append(text[:n])
        text = text[n:]
    return out or [""]


def deliver(job, body):
    channel, to = job["channel"], str(job["to"])
    if channel == "whatsapp":
        sec = json.load(open(WA_SECRETS))
        url = f"https://graph.facebook.com/v21.0/{sec['phoneNumberId']}/messages"
        hdr = {"Authorization": f"Bearer {sec['accessToken']}"}
        to_digits = re.sub(r"[^\d]", "", to)
        for part in chunks(body):
            http_post_json(url, {"messaging_product": "whatsapp", "to": to_digits,
                                 "type": "text", "text": {"body": part}}, hdr)
    elif channel == "telegram":
        cfg = json.load(open(OPENCLAW_JSON))
        token = cfg["channels"]["telegram"]["botToken"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for part in chunks(body):
            http_post_json(url, {"chat_id": to, "text": part})
    else:
        raise ValueError(f"unknown channel {channel!r}")


def run_job(path):
    name = os.path.basename(path)
    job_id = name[:-5]
    running_path = f"{RUNNING}/{name}"
    shutil.move(path, running_path)
    job = json.load(open(running_path))
    job["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(job, open(running_path, "w"), indent=1)
    log(f"job {job_id} START channel={job['channel']} to={job['to']} task={job['task'][:120]!r}")

    # Containment guard (QW-2): re-inject the lab-sandbox rule for POC tasks,
    # so a paraphrased/doctrine-stripped task can't pip-install into the brain's
    # own distro. Deterministic; never trusts the (possibly sloppy) task text.
    preamble = lab_preamble(job["task"])
    prompt = (
        f"BACKGROUND JOB {job_id} (requested via {job['channel']} by {job['to']}).\n"
        f"{preamble}"
        f"Task: {job['task']}\n\n"
        "Complete this task thoroughly using your tools. You have plenty of time. "
        "When done, output ONLY the final answer for the user: plain text, concise, "
        "chat-friendly (short lines, no markdown tables, no headings), written as "
        "Ollie speaking in his normal chat voice.\n"
        "HARD RULES for the final answer:\n"
        "- ONE-SHOT delivery: the user cannot reply to this session. Never ask "
        "questions or offer options ('would you like me to...'). Deliver the best "
        "result you have.\n"
        "- OPSEC: never mention internal tools or mechanics (web fetch, browser "
        "automation, desktop tools, MCP, models, sessions). If something was "
        "inaccessible, say it in user terms (e.g. 'Instagram keeps reel contents "
        "behind a login, so I verified via news coverage instead').\n"
        "- If a linked page can't be opened, work around it: search for the "
        "claim/topic from the link text and verify via independent public sources, "
        "then report what you could and couldn't confirm.\n"
        "- Do not narrate your steps. Do not mention job ids."
    )
    t0 = time.time()
    status, result = "done", ""
    try:
        agent = job.get("agent", "main")
        p = subprocess.run(
            [NODE, OPENCLAW, "--log-level", "silent", "agent", "--agent", agent,
             "--session-key", f"job-{job_id}", "-m", prompt],
            capture_output=True, text=True, timeout=JOB_TIMEOUT_S,
            env={**os.environ, "HOME": HOME},
        )
        result = sanitize(p.stdout)
        if p.returncode != 0 or not result:
            status = "failed"
            result = result or sanitize(p.stderr)[:500]
    except subprocess.TimeoutExpired:
        status, result = "failed", f"timed out after {JOB_TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001
        status, result = "failed", f"runner error: {e}"

    dur = int(time.time() - t0)
    finished = time.strftime("%Y-%m-%dT%H:%M:%S")
    job.update(status=status, finished=finished,
               duration_s=dur, result=result[:20000])

    # Evidence-derived work taxonomy (QW-1). Additive done-JSON fields only;
    # result text is kept intact. Wrapped so taxonomy never breaks delivery.
    try:
        claimed, evidence = parse_trailers(result)
        lab_ran = ran_in_lab(job.get("started"), finished)
        ev_ok = evidence_verified(evidence)
        job["status_class"] = derive_status_class(claimed, lab_ran, ev_ok)
        job["status_class_claimed"] = claimed
        job["ran_in_lab"] = lab_ran
        job["evidence"] = evidence
        job["evidence_verified"] = ev_ok
    except Exception as e:  # noqa: BLE001
        log(f"job {job_id} taxonomy error: {e}")

    if status == "done":
        body = result
    else:
        body = ("I hit a snag finishing a background task you gave me "
                f"({job['task'][:80]}). Ask me to retry and I'll take another run at it.")
    if job.get("deliver", True):
        try:
            deliver(job, body)
            job["delivered"] = True
        except Exception as e:  # noqa: BLE001
            job["delivered"] = False
            job["deliver_error"] = str(e)
            log(f"job {job_id} DELIVERY FAILED: {e}")
    else:
        # Silent job (lab lane): result stays in the ledger/notes; the morning
        # brief reports it. Never message the recipient directly.
        job["delivered"] = False
        job["silent"] = True

    dest = DONE if status == "done" else FAILED
    json.dump(job, open(f"{dest}/{name}", "w"), indent=1)
    os.remove(running_path)
    log(f"job {job_id} {status.upper()} in {dur}s delivered={job.get('delivered')}")
    refresh_work_digest()  # refresh ground truth (self-guarded)


def check_reminders():
    """Deliver any due reminders. Runs in its own thread so a long-running job
    can't delay time-sensitive reminders."""
    os.makedirs(REMINDERS, exist_ok=True)
    now = time.time()
    for f in sorted(os.listdir(REMINDERS)):
        if not f.endswith(".json"):
            continue
        path = f"{REMINDERS}/{f}"
        try:
            r = json.load(open(path))
        except Exception:  # noqa: BLE001
            continue
        if r.get("deliver_at", 1e18) > now:
            continue
        try:
            deliver(r, r["message"])
            r["status"] = "delivered"
            r["delivered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            json.dump(r, open(f"{DONE}/reminder-{f}", "w"), indent=1)
            os.remove(path)
            log(f"reminder {r['id']} DELIVERED to {r['to']} via {r['channel']}")
        except Exception as e:  # noqa: BLE001
            log(f"reminder {r['id']} delivery failed: {e}")


def reminder_loop():
    while True:
        try:
            check_reminders()
        except Exception as e:  # noqa: BLE001
            log(f"reminder loop error: {e}")
        time.sleep(15)


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        time.sleep(60)
        sys.exit(0)

    for d in (QUEUE, RUNNING, DONE, FAILED, REMINDERS):
        os.makedirs(d, exist_ok=True)
    threading.Thread(target=reminder_loop, daemon=True).start()
    # Crash recovery: anything left in running/ goes back to the queue.
    for f in os.listdir(RUNNING):
        shutil.move(f"{RUNNING}/{f}", f"{QUEUE}/{f}")
        log(f"recovered stale running job {f} -> queue")

    log("ollie-jobs runner started")
    while True:
        try:
            jobs = sorted(
                (f for f in os.listdir(QUEUE) if f.endswith(".json")),
                key=lambda f: os.path.getmtime(f"{QUEUE}/{f}"),
            )
            if jobs:
                run_job(f"{QUEUE}/{jobs[0]}")
            else:
                time.sleep(POLL_S)
        except Exception as e:  # noqa: BLE001
            log(f"runner loop error: {e}")
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
