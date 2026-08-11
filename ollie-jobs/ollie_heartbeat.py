#!/usr/bin/env python3
"""Ollie heartbeat — the inner loop.

Fired by a systemd user timer every 30 min. Assembles a context-rich prompt
(HEARTBEAT.md instructions + OPEN_LOOPS.md + jobs state + recent beat log),
runs ONE fresh agent turn, and parses a strict output protocol:

    SILENCE[: reason]   -> logged, nothing sent (the normal outcome)
    MESSAGE: <text>     -> delivered to the owner on TELEGRAM (primary;
                           WhatsApp fallback only — its 24h customer-service
                           window makes it unreliable for proactive sends)

Anything malformed is treated as SILENCE (fail-closed: a confused model
must never spam the owner). Delivery reuses the jobs runner's deliver().
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ollie_jobs_runner import (  # noqa: E402
    deliver, sanitize, recent_done_jobs, job_tag, parse_job_ts,
)
# Guarded: a missing/broken digest module must cost us the digest, not the beat.
try:
    from ollie_work_digest import update_digest as refresh_work_digest  # noqa: E402
except Exception:  # noqa: BLE001
    def refresh_work_digest():
        pass

HOME = "/home/openclaw"
WS = f"{HOME}/.openclaw/workspace"
HEARTBEAT_MD = f"{WS}/HEARTBEAT.md"
OPEN_LOOPS_MD = f"{WS}/OPEN_LOOPS.md"
JOBS = f"{HOME}/.openclaw/workspace/jobs"
LAST_BRIEF_MD = f"{WS}/LAST_BRIEF.md"
BEAT_LOG = f"{WS}/heartbeat/log.md"   # agent-visible history of past beats
RUN_LOG = f"{HOME}/.openclaw/logs/heartbeat.log"
WA_SECRETS = f"{HOME}/.openclaw/secrets/whatsapp-cloud.json"
NODE = f"{HOME}/.openclaw/tools/node-v22.22.0/bin/node"
OPENCLAW = f"{HOME}/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js"
LOCK = "/tmp/ollie-heartbeat.lock"
# Tushar's Telegram chat id (owner-locked channel; see ARCHITECTURE.md).
# Telegram is the PRIMARY proactive channel: WhatsApp Cloud API only allows
# business-initiated sends within 24h of the user's last message.
OWNER_TELEGRAM = "<OWNER_TELEGRAM_CHAT_ID>"
# Additional cofounder recipients of the SAME brief (best-effort, SECONDARY to
# the owner). Prakersh. Curiosity findings ride the brief (recent_done_jobs),
# so this also delivers curiosity to him — no separate research dispatch needed.
EXTRA_TELEGRAM = ["<EXTRA_TELEGRAM_CHAT_ID>"]
TURN_TIMEOUT_S = 300
BEAT_LOG_TAIL = 30  # last N beat lines shown to the agent


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(RUN_LOG), exist_ok=True)
        with open(RUN_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read(path, max_chars=6000):
    try:
        return open(path).read()[:max_chars]
    except OSError:
        return "(missing)"


def jobs_summary():
    out = []
    for state in ("queue", "running", "failed", "done"):
        d = f"{JOBS}/{state}"
        try:
            files = sorted(
                (f for f in os.listdir(d) if f.endswith(".json")),
                key=lambda f: os.path.getmtime(f"{d}/{f}"), reverse=True)
        except OSError:
            continue
        if state == "done":
            files = files[:3]  # recent only
        out.append(f"{state}: {len(files)}")
        for f in files[:5]:
            try:
                j = json.load(open(f"{d}/{f}"))
                # QW-4: surface the evidence class so the brief-writing LLM
                # sees what was actually executed vs merely researched.
                tag = job_tag(j) if state == "done" else ""
                out.append(f"  - [{state}] {j.get('task','?')[:90]}{tag} (delivered={j.get('delivered')})")
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(out) or "(no jobs dirs)"


def lab_inbox_summary():
    """New 4DPocket saves awaiting triage (written by lab_watcher.py)."""
    inbox = f"{WS}/lab/inbox"
    try:
        files = sorted(f for f in os.listdir(inbox) if f.endswith(".json"))
    except OSError:
        return "(no lab inbox)"
    if not files:
        return "(empty — nothing new to triage)"
    out = [f"{len(files)} new save(s) awaiting triage:"]
    for f in files[:15]:
        try:
            it = json.load(open(f"{inbox}/{f}"))
            out.append(
                f"  - id={it.get('id','?')[:8]} [{it.get('source_platform','?')}] "
                f"{(it.get('title') or it.get('url') or '?')[:80]}")
        except Exception:  # noqa: BLE001
            out.append(f"  - {f} (unreadable)")
    if len(files) > 15:
        out.append(f"  ... and {len(files) - 15} more")
    return "\n".join(out)


def beat_log_tail():
    try:
        lines = open(BEAT_LOG).read().splitlines()
        return "\n".join(lines[-BEAT_LOG_TAIL:])
    except OSError:
        return "(no beats yet)"


def append_beat(outcome):
    os.makedirs(os.path.dirname(BEAT_LOG), exist_ok=True)
    if not os.path.exists(BEAT_LOG):
        with open(BEAT_LOG, "w") as f:
            f.write("# Heartbeat log (runner-maintained; newest last)\n")
    with open(BEAT_LOG, "a") as f:
        f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {outcome}\n")


def run_turn(prompt):
    p = subprocess.run(
        [NODE, OPENCLAW, "--log-level", "silent", "agent", "--agent", "main",
         "--session-key", f"heartbeat-{time.strftime('%Y%m%d')}", "-m", prompt],
        capture_output=True, text=True, timeout=TURN_TIMEOUT_S,
        env={**os.environ, "HOME": HOME},
    )
    return sanitize(p.stdout)


def parse_outcome(out):
    """Strict protocol. Returns (kind, payload). Fail-closed to silence.

    Order matters: an explicit leading SILENCE/MESSAGE wins. Only if neither
    leads do we look for a MESSAGE: marker buried after a reasoning preamble
    and EXTRACT it (rather than discarding the whole turn). The old behaviour
    dropped a buried MESSAGE as "malformed (MESSAGE not at start)" — that
    silently ate the 2026-06-15 morning brief and cascaded into a multi-day
    false-belief SILENCE loop. A real brief must never be lost to preamble.
    """
    if not out:
        return "silence", "empty output"
    text = out.strip()
    if text.upper().startswith("SILENCE"):
        return "silence", text[:160]
    if text.upper().startswith("MESSAGE:"):
        body = text[len("MESSAGE:"):].strip()
        return ("message", body) if body else ("silence", "empty MESSAGE")
    # Decision buried after reasoning: extract from the first MESSAGE: marker.
    m = re.search(r"^\s*MESSAGE:\s*(.+)", text, re.DOTALL | re.MULTILINE)
    if m:
        body = m.group(1).strip()
        return ("message", body) if body else ("silence", "empty MESSAGE")
    # A bare SILENCE anywhere also resolves to silence (model narrated then
    # declined). Anything else is genuinely malformed.
    if re.search(r"\bSILENCE\b", text.upper()):
        return "silence", text[:160]
    return "silence", f"malformed: {text[:120]!r}"


# --- QW-3: deterministic grounding of the brief against last-24h ground truth -
# The incident in one sentence: the brief claimed work was "executed/benchmarked"
# when the underlying job had only RESEARCHED it. These helpers are pure string
# work over the done-JSON status_class — no LLM, fully reversible to the original
# brief if anything throws (the caller wraps them).
EXEC_VERBS_RE = re.compile(
    r"\b(ran|run|tested|benchmark|benchmarked|built|installed|executed|deployed|shipped)\b",
    re.IGNORECASE,
)
# classes that count as genuinely executed (vs researched-only)
EXECUTED_CLASSES = ("benchmarked", "shipped", "installed")


def _distinctive_token(task):
    """A distinctive token from a task title: longest Capitalized word >=4 chars,
    else the first token >=5 chars. None if nothing qualifies."""
    if not task:
        return None
    words = re.findall(r"[A-Za-z0-9]+", task)
    caps = [w for w in words if len(w) >= 4 and w[0].isupper()]
    if caps:
        return max(caps, key=len)
    for w in words:
        if len(w) >= 5:
            return w
    return None


def _short_names(jobs):
    """Distinctive short names for a job list, capped at 3 + '…'."""
    names = []
    for j in jobs:
        tok = _distinctive_token(j.get("task", "")) or (j.get("task", "?")[:12])
        names.append(tok)
    if len(names) > 3:
        return names[:3] + ["…"]
    return names


def ground_brief(brief, jobs):
    """Annotate + footer the brief against ground truth. Pure deterministic.

    (a) For each RESEARCHED-only job, if its distinctive token appears in a
        sentence that ALSO contains an execution verb, append a one-time
        '[researched only — not executed]' to that sentence.
    (b) ALWAYS append a ground-truth footer when any done jobs exist."""
    if not jobs:
        return brief

    researched = [j for j in jobs if (j.get("status_class") == "researched")]
    executed = [j for j in jobs if (j.get("status_class") in EXECUTED_CLASSES)]

    # (a) line-level annotation. The brief is line-structured (bullets,
    # headers) — annotating per line preserves its formatting exactly, where
    # sentence-splitting + rejoining would flatten newlines.
    annotated = brief
    research_tokens = []
    for j in researched:
        tok = _distinctive_token(j.get("task", ""))
        if tok:
            research_tokens.append(tok)
    if research_tokens:
        new_lines = []
        for line in brief.splitlines():
            tagged = line
            if EXEC_VERBS_RE.search(line):
                for tok in research_tokens:
                    if re.search(r"\b" + re.escape(tok) + r"\b", line, re.IGNORECASE):
                        tagged = line.rstrip() + " [researched only — not executed]"
                        break  # only once per line
            new_lines.append(tagged)
        annotated = "\n".join(new_lines)

    # (b) always-on footer
    ex_names = _short_names(executed)
    re_names = _short_names(researched)
    footer = (f"\n\n— ground truth: {len(executed)} executed "
              f"({', '.join(ex_names) if ex_names else '-'}) · "
              f"{len(researched)} researched "
              f"({', '.join(re_names) if re_names else '-'})")
    return annotated + footer


def write_last_brief(payload):
    """NT-2: persist the full delivered payload for the digest / continuity."""
    try:
        os.makedirs(os.path.dirname(LAST_BRIEF_MD), exist_ok=True)
        with open(LAST_BRIEF_MD, "w") as f:
            f.write(f"# Last brief — {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log(f"LAST_BRIEF write failed: {e}")


def _newest_delivered_brief_ts(jobs):
    """Newest finished-ts among delivered brief jobs, or None.
    ponytail: identifies a brief by 'brief' in the task text — tighten to a real
    job-type flag if briefs ever get one."""
    ts = [parse_job_ts(j.get("finished")) for j in jobs
          if j.get("delivered") and "brief" in (j.get("task") or "").lower()]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def backfill_brief_mtime():
    """The brief sometimes ships via the jobs path (LLM submits a 'brief' job)
    instead of the MESSAGE: protocol, so write_last_brief() never runs and the
    watchdog's LAST_BRIEF.md mtime goes stale -> false 'proactivity stuck' alert.
    Bump LAST_BRIEF.md's mtime to the REAL delivery time (not now) so the 28h
    staleness math stays honest regardless of which path delivered."""
    try:
        newest = _newest_delivered_brief_ts(recent_done_jobs(28 * 3600))  # watchdog BRIEF_MAX_AGE_S
        if newest and (not os.path.exists(LAST_BRIEF_MD)
                       or newest > os.path.getmtime(LAST_BRIEF_MD)):
            open(LAST_BRIEF_MD, "a").close()
            os.utime(LAST_BRIEF_MD, (newest, newest))
    except Exception as e:  # noqa: BLE001
        log(f"brief mtime backfill skipped ({e})")


# --- cheap pre-filter: skip the expensive LLM beat when nothing changed ---
# The heartbeat is the single biggest token spender (~37% of all spend, mostly
# 48 daily "nothing changed -> SILENCE" beats). This computes a cheap
# filesystem-only signature; the LLM beat runs ONLY when the signature changed,
# OR a brief is due, OR a forced-interval beat is due (so date-based due-loops
# still get caught). Quiet hours force less often. No state change => no spend.
HEARTBEAT_STATE = f"{HOME}/.openclaw/logs/heartbeat-state.json"
FORCE_INTERVAL_DAY_S = 2 * 3600
FORCE_INTERVAL_NIGHT_S = 4 * 3600


def cheap_signature():
    """Filesystem-only fingerprint of everything a beat reacts to. No LLM."""
    import hashlib
    parts = []

    def count(d):
        try:
            return len([f for f in os.listdir(d) if f.endswith(".json")])
        except OSError:
            return 0

    parts.append(f"labinbox:{count(f'{WS}/lab/inbox')}")
    for state in ("queue", "running", "failed"):
        parts.append(f"jobs.{state}:{count(f'{JOBS}/{state}')}")
    # done-but-undelivered (a lost result the beat should flag)
    try:
        und = 0
        for f in os.listdir(f"{JOBS}/done"):
            if f.endswith(".json"):
                j = json.load(open(f"{JOBS}/done/{f}"))
                if j.get("delivered") is False and not j.get("silent"):
                    und += 1
        parts.append(f"undelivered:{und}")
    except OSError:
        pass
    for path in (OPEN_LOOPS_MD,):
        try:
            parts.append(f"loops:{int(os.path.getmtime(path))}")
        except OSError:
            pass
    # project inboxes + statuses
    try:
        for slug in sorted(os.listdir(f"{WS}/projects")):
            pd = f"{WS}/projects/{slug}"
            if slug.startswith("_") or not os.path.isdir(pd):
                continue
            try:
                st = json.load(open(f"{pd}/state.json")).get("status")
            except Exception:  # noqa: BLE001
                st = "?"
            try:
                ib = int(os.path.getmtime(f"{pd}/inbox.md"))
            except OSError:
                ib = 0
            parts.append(f"proj.{slug}:{st}:{ib}")
    except OSError:
        pass
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def is_quiet_hours():
    h = time.localtime().tm_hour
    return h >= 19 or h < 4  # 19:00-04:00 box time (mirrors HEARTBEAT.md)


def brief_due_now():
    h = time.localtime().tm_hour
    if not (4 <= h < 6):
        return False
    # 2026-06-15 false-belief incident: the previous implementation grepped
    # BEAT_LOG for "MESSAGED" to decide whether a brief was sent.  The beat-log
    # is model-influenced — the LLM hallucinated "brief sent at 04:01" in its
    # SILENCE reasoning, that text got logged and fed back into the next prompt,
    # creating a self-reinforcing suppression loop that silenced proactive
    # output for multiple days.
    #
    # Fix: use the confirmed-delivery artifact LAST_BRIEF_MD as the sole gate.
    # write_last_brief() only touches that file AFTER a successful deliver(),
    # so its mtime is tamper-proof evidence that a brief actually landed.
    # Missing artifact → brief IS due (never suppress on absent evidence).
    # The beat-log is still written for human/agent visibility but no longer
    # controls this decision.
    try:
        mtime = os.path.getmtime(LAST_BRIEF_MD)
        today = time.localtime()
        brief_day = time.localtime(mtime)
        already_delivered = (
            brief_day.tm_year == today.tm_year
            and brief_day.tm_yday == today.tm_yday
        )
    except OSError:
        already_delivered = False  # artifact absent → not delivered
    return not already_delivered


def brief_delivered_today():
    """True iff a brief was ACTUALLY delivered today (LAST_BRIEF.md mtime = today).
    Window-INDEPENDENT, unlike brief_due_now() which also gates on the 04:00-06:00
    window. Used for the MESSAGE/SILENCE decision so a brief MISSED during the
    early window still gets recovered on a later daytime beat (artifact is the
    sole, tamper-proof source of truth — never the model-written beat log)."""
    try:
        bd = time.localtime(os.path.getmtime(LAST_BRIEF_MD))
        td = time.localtime()
        return bd.tm_year == td.tm_year and bd.tm_yday == td.tm_yday
    except OSError:
        return False


def should_run_real(sig):
    """Decide whether this beat warrants the full LLM turn.

    Policy (deliberately NOT cost-minimal — Tushar: don't cheap out / don't
    make it stupid): DAYTIME beats ALWAYS run the full smart model — that's
    when emergent initiative matters and the owner's awake to receive a
    proactive ping; intelligence is never traded for tokens here. We only
    skip during QUIET HOURS, and only when the world is byte-identical to a
    state already judged SILENCE — those overnight beats are provably
    redundant (same input -> same SILENCE), so skipping loses nothing. A
    forced real beat every 4h overnight + the brief still run, so date-based
    due-loops and genuine overnight events are never missed."""
    if not is_quiet_hours():
        return True, "daytime — full beat"
    try:
        prev = json.load(open(HEARTBEAT_STATE))
    except Exception:  # noqa: BLE001
        prev = {}
    age = time.time() - prev.get("last_real_ts", 0)
    if brief_due_now():
        return True, "brief due"
    if sig != prev.get("sig"):
        return True, "state changed overnight"
    if age >= FORCE_INTERVAL_NIGHT_S:
        return True, f"forced ({int(age/60)}min since last real beat)"
    return False, f"quiet-hours no-op ({int(age/60)}min since last real beat)"


def save_heartbeat_state(sig, ran_real):
    try:
        prev = json.load(open(HEARTBEAT_STATE))
    except Exception:  # noqa: BLE001
        prev = {}
    prev["sig"] = sig
    if ran_real:
        prev["last_real_ts"] = time.time()
    os.makedirs(os.path.dirname(HEARTBEAT_STATE), exist_ok=True)
    json.dump(prev, open(HEARTBEAT_STATE, "w"))


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("previous beat still running -> skip")
        return 0

    # Cheap pre-filter — the single biggest spend lever. Most beats skip here
    # with zero LLM cost.
    sig = cheap_signature()
    run_real, why = should_run_real(sig)
    if not run_real:
        log(f"beat SKIP ({why})")
        save_heartbeat_state(sig, ran_real=False)
        return 0
    log(f"beat will run real LLM turn: {why}")

    now = time.strftime("%Y-%m-%d %H:%M (%A)")
    # Authoritative brief-delivery ground truth from the confirmed-delivery
    # artifact (LAST_BRIEF.md mtime) — NOT the model-written beat log. Counters
    # the false-belief "brief already sent today" SILENCE loop: the LLM was
    # inferring delivery from RECENT BEATS (its own past reasoning) and
    # suppressing the brief for days. Surface the truth so the decision is grounded.
    try:
        _last_brief = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(LAST_BRIEF_MD)))
    except Exception:  # noqa: BLE001
        _last_brief = "never"
    if not brief_delivered_today():
        brief_status = (
            f"Last brief ACTUALLY delivered: {_last_brief}. A brief has NOT been delivered today.\n"
            "A daily brief IS DUE. The daily brief is a SCHEDULED summary — it goes out even if the\n"
            "world state looks unchanged; 'same state' is NOT a reason to skip it. Your decision MUST\n"
            "be MESSAGE: compose and send today's brief now (summarise recent work, jobs, open loops).\n"
            "IGNORE any 'brief already sent today' / SILENCE precedent in RECENT BEATS — that text is\n"
            "model-written and has wrongly suppressed the brief for two days."
        )
        recent_beats = "(omitted while a brief is DUE — prior SILENCE beats must not anchor today's decision)"
    else:
        brief_status = (
            f"A brief was already delivered today ({_last_brief}). Only MESSAGE again for genuinely "
            "new, urgent developments; otherwise SILENCE."
        )
        recent_beats = beat_log_tail()
    prompt = (
        f"HEARTBEAT — {now} box-local time (IST = box time +3h30m).\n"
        "Nobody messaged you; you woke up on your own. Follow the\n"
        "instructions below EXACTLY, including the output protocol.\n\n"
        f"=== BRIEF DELIVERY STATUS (authoritative; from the delivery artifact, NOT the beat log — READ FIRST) ===\n{brief_status}\n\n"
        f"=== HEARTBEAT INSTRUCTIONS ===\n{read(HEARTBEAT_MD)}\n\n"
        f"=== OPEN_LOOPS.md (path: {OPEN_LOOPS_MD}) ===\n{read(OPEN_LOOPS_MD)}\n\n"
        f"=== JOBS LEDGER SUMMARY ===\n{jobs_summary()}\n\n"
        f"=== LAB INBOX (new saves to triage; see Lab duties) ===\n{lab_inbox_summary()}\n\n"
        f"=== RECENT BEATS ===\n{recent_beats}\n"
    )
    log("beat START")
    save_heartbeat_state(sig, ran_real=True)  # mark real-beat time up front
    try:
        out = run_turn(prompt)
    except subprocess.TimeoutExpired:
        log(f"beat TIMEOUT after {TURN_TIMEOUT_S}s")
        append_beat("TIMEOUT")
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"beat ERROR: {e}")
        append_beat(f"ERROR {e}")
        return 1

    kind, payload = parse_outcome(out)
    if kind == "message":
        # QW-3: deterministically ground the brief against last-24h job ground
        # truth before sending. Never let grounding block the brief — on ANY
        # error fall back to the original payload.
        try:
            payload = ground_brief(payload, recent_done_jobs())
        except Exception as e:  # noqa: BLE001
            log(f"beat grounding skipped ({e}) -> sending original brief")
        # Telegram first (no send-window restriction); WhatsApp is best-effort
        # fallback — it only works within 24h of Tushar's last inbound message.
        delivered_via = None
        try:
            deliver({"channel": "telegram", "to": OWNER_TELEGRAM}, payload)
            delivered_via = "telegram"
        except Exception as e:  # noqa: BLE001
            log(f"beat telegram delivery failed ({e}) -> whatsapp fallback")
            try:
                sec = json.load(open(WA_SECRETS))
                owner = re.sub(r"[^\d]", "", sec.get("ownerFrom") or sec["allowFrom"][0])
                deliver({"channel": "whatsapp", "to": owner}, payload)
                delivered_via = "whatsapp"
            except Exception as e2:  # noqa: BLE001
                log(f"beat DELIVERY FAILED on both channels: {e2}")
                append_beat(f"DELIVERY-FAILED: {e2}")
                return 1
        log(f"beat MESSAGED via {delivered_via} ({len(payload)} chars)")
        append_beat(f"MESSAGED[{delivered_via}]: {payload[:80]!r}")
        write_last_brief(payload)  # NT-2: persist the full delivered brief
        # Fan out the SAME brief to additional cofounder recipients. Best-effort
        # and STRICTLY non-fatal: the owner delivery above already succeeded and
        # gated the beat + dedup artifact, so a secondary failure must never
        # affect the beat outcome. Telegram only (no 24h window like WhatsApp).
        for extra in EXTRA_TELEGRAM:
            try:
                deliver({"channel": "telegram", "to": extra}, payload)
                log(f"beat brief also delivered to {extra}")
            except Exception as e:  # noqa: BLE001
                log(f"beat secondary delivery to {extra} failed (non-fatal): {e}")
    else:
        log(f"beat SILENCE ({payload})")
        append_beat(f"SILENCE ({payload[:60]})")
        backfill_brief_mtime()  # brief may have shipped via the jobs path
    # Refresh ground-truth digest once per real beat (self-guarded, regardless
    # of MESSAGE/SILENCE outcome).
    refresh_work_digest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
