#!/usr/bin/env python3
"""Curiosity Engine — queue ORCHESTRATOR (4DPocket-backed, re-architecture 2026-06-14).

NO LLM lives here; the only token spend is (a) the relevance gate (a sibling)
and (b) the research jobs we dispatch — both bounded. This module's job is
plumbing: READ recent items back out of 4DPocket (which is now the ingestion +
storage + extraction + RSS spine), run them through the recency+relevance gate,
score/dedup/rank into a persistent priority queue, and hand the top-N to the
existing job pipeline within the mechanical spend budget.

WHY 4DPocket-backed: 4DPocket already does scheduled RSS/Atom polling, URL
ingest + trafilatura extraction, URL-dedup and hybrid search — so the engine's
own pollers/storage are redundant. The QUEUE no longer polls RSS/Reddit/social
directly; instead:
  * research_feeds_sync registers curated feeds INTO 4DPocket (it polls them),
  * research_discovery PUSHES discovered URLs INTO 4DPocket (it extracts them),
  * this QUEUE READS recent items back out and gates/ranks/dispatches them.
The push and the read are DECOUPLED: discovery/feeds push now, 4DPocket
processes asynchronously over seconds-to-minutes, and the NEXT queue cycle
reads them. The feeder timer is OFFSET from this queue timer to give 4DPocket
processing time in between.

Design rules (match the rest of ollie-jobs):
  * Pure Python 3.12 stdlib. Runs on the box's bare WSL python3.
  * Guarded + atomic: every disk write is tmp+os.replace; the whole cycle is
    wrapped so a broken/missing sibling DEGRADES (skip + log), never crashes.
  * Testable: HOME via OLLIE_HOME, paths resolved fresh through _paths() so
    tests can repoint module globals; every subprocess/budget call is an
    overridable module-level function or an injectable callable.

SIBLINGS (imported DEFENSIVELY, coded to PUBLIC SIGNATURES):
  research_fourdpocket.search_recent(after_iso, query="", source_platform=None,
                                     limit=) -> [item dict]
  research_gate.score_and_filter(cands, interests, now_ts=None,
                                 sources_by_id=None) -> [scored]
  research_registry.load_sources() / load_interests()

A missing module, a missing function, or a raising function all degrade to a
safe default (empty list / pass-through), logged to the cycle log.

SHARED CONTRACTS:
  CANDIDATE: {source_id, source_type, url, title, text, ts(epoch|None),
              domain_tags[str], raw_id, ...}
  SCORED   : CANDIDATE augmented by the gate with `relevance` (float 0..1).
  queue.json: ranked-desc list of queue items, each = SCORED + {
                weight, score(float), status(queued|dispatched|done),
                added_at(iso), manual_priority(int|null), fingerprint }
  seen.json : {"fingerprints": [...]} capped at ~SEEN_CAP (FIFO).

PUBLIC:
  run(now_ts=None)            one cycle -> reads 4DPocket -> queue.json + seen.json
  dispatch(top_n=, dry_run=)  top-N queued -> job-submit within research budget
  main(argv)                  run() then dispatch(); flags --no-dispatch --dry-run
PURE HELPERS (for tests):
  compute_score, recency_factor, rerank, dedup, merge_into_queue,
  item_to_candidate, fingerprint
"""
import importlib
import json
import os
import subprocess
import sys
import time

# --- runtime config (module globals; _paths() reads them fresh so tests can
#     reassign HOME/WORKSPACE/LOGS/BIN before a call) ---
HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
WORKSPACE = f"{HOME}/.openclaw/workspace"
LOGS = f"{HOME}/.openclaw/logs"
BIN = f"{HOME}/bin"
AUTONOMOUS_DISPATCH_PAUSE = (
    f"{WORKSPACE}/executive/PAUSE_AUTONOMOUS_RESEARCH"
)

# Owner-locked Telegram chat id (same constant the heartbeat uses). Research
# jobs are --silent anyway (findings ride the morning brief), but job-submit.sh
# still derives the permission tier from --to.
OWNER_TELEGRAM = "<OWNER_TELEGRAM_CHAT_ID>"
LANE = "research"               # the spend lane these jobs charge against

DISPATCH_TOP_N = 2              # max jobs handed off per cycle (budget gates harder)
SEEN_CAP = 5000                 # FIFO cap on remembered fingerprints
MAX_QUEUE = 500                 # cap persisted queue length (keeps file bounded)
DEFAULT_WEIGHT = 1.0            # source weight when unset
DEFAULT_RELEVANCE = 0.5         # relevance when the gate degrades / omits it
RECENCY_HALF_LIFE_DAYS = 7.0    # decay half-life when a source sets no recency_days
RECENCY_WINDOW_DAYS = 14        # how far back we read 4DPocket items (the `after` bound)
FETCH_LIMIT = 50               # max items pulled from 4DPocket per cycle (<=100)
DAY_S = 86400.0


def _paths():
    """Resolve runtime paths fresh each call (mirrors ollie_work_digest._paths
    so tests can repoint the module globals)."""
    research = f"{WORKSPACE}/research"
    return {
        "research": research,
        "sources": f"{research}/sources.json",
        "interests": f"{research}/interests.json",
        "queue": f"{research}/queue.json",
        "seen": f"{research}/seen.json",
        "log": f"{LOGS}/research-queue.log",
        "budget_py": f"{BIN}/budget.py",
        "job_submit": f"{BIN}/job-submit.sh",
    }


# ----------------------------------------------------------------------------
# logging + atomic json io (all guarded — never raise to the caller)
# ----------------------------------------------------------------------------
def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — missing/corrupt -> default
        return default


def _write_json(path, obj):
    """Atomic write: tmp + os.replace. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"write failed {path}: {e}")
        return False


# ----------------------------------------------------------------------------
# pure helpers (unit-tested directly)
# ----------------------------------------------------------------------------
def _to_epoch(val):
    """Best-effort coerce a candidate timestamp to epoch seconds (float).
    Accepts epoch numbers and common ISO-8601 strings; None on failure."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    # numeric string
    try:
        return float(s)
    except ValueError:
        pass
    # ISO-8601 (tolerate trailing Z)
    iso = s.replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s[:19], fmt))
        except (ValueError, TypeError):
            continue
    return None


def fingerprint(cand):
    """Stable dedup key for a candidate — CANONICAL across the engine.

    Matches research_registry.fingerprint normalization exactly (sha256 hex of
    normalized url + '|' + normalized title) so a fingerprint computed here, by
    the registry, or by any poller is byte-identical for the same item — dedup
    must be consistent across every acquisition path or we re-research dupes and
    waste tokens. URL-first; falls back to source_id+title for url-less items.
    The queue re-fingerprints every candidate in _normalize, so this is the
    authoritative dedup key written to queue.json / seen.json."""
    import hashlib
    norm_url = (cand.get("url") or "").strip().lower().rstrip("/")
    norm_title = " ".join((cand.get("title") or "").strip().lower().split())
    if norm_url:
        raw = f"{norm_url}|{norm_title}"
    else:
        raw = f"{cand.get('source_id', '')}|{norm_title}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def recency_factor(ts, now_ts, half_life_days=RECENCY_HALF_LIFE_DAYS):
    """Exponential recency decay in [0,1]. factor = 0.5 ** (age_days/half_life).
    Unknown ts -> neutral 1.0 (don't punish what we can't date); future ts is
    clamped to 1.0; half_life<=0 falls back to the default."""
    if ts is None:
        return 1.0
    if not half_life_days or half_life_days <= 0:
        half_life_days = RECENCY_HALF_LIFE_DAYS
    age_days = max(0.0, (now_ts - float(ts)) / DAY_S)
    return min(1.0, 0.5 ** (age_days / half_life_days))


def compute_score(relevance, weight, recency_factor):
    """The ranking formula: relevance * source-weight * recency decay.
    All inputs coerced to float; non-numeric -> neutral so a bad field can
    never crash a cycle."""
    def f(x, default):
        try:
            return float(x)
        except (TypeError, ValueError):
            return default
    return f(relevance, DEFAULT_RELEVANCE) * f(weight, DEFAULT_WEIGHT) * f(recency_factor, 1.0)


def rerank(queue):
    """Priority order. manual_priority (UI override) wins: items with a
    manual_priority sort FIRST, ascending; everything else sorts by score
    descending. Stable, non-mutating (returns a new list)."""
    def key(item):
        mp = item.get("manual_priority")
        has_manual = mp is None  # False (0) sorts before True (1) => manual first
        try:
            mp_val = int(mp) if mp is not None else 0
        except (TypeError, ValueError):
            mp_val = 0
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return (has_manual, mp_val, -score)
    return sorted(queue, key=key)


def dedup(cands, seen):
    """Drop candidates whose fingerprint is already in `seen` (a set/list of
    fingerprints) AND drop intra-batch duplicates. Order-preserving."""
    seen_set = set(seen or [])
    out, batch = [], set()
    for c in cands:
        fp = c.get("fingerprint") or fingerprint(c)
        if fp in seen_set or fp in batch:
            continue
        batch.add(fp)
        out.append(c)
    return out


def merge_into_queue(existing, new_scored):
    """Combine the persisted queue with newly-scored items. Existing entries
    WIN on fingerprint collision (preserves status/manual_priority/added_at of
    items already queued or dispatched); genuinely new items are appended.
    Pure combine — caller reranks afterwards."""
    by_fp = {}
    order = []
    for item in existing or []:
        fp = item.get("fingerprint") or fingerprint(item)
        if fp not in by_fp:
            order.append(fp)
        by_fp[fp] = item  # existing wins (first writer)
    for item in new_scored or []:
        fp = item.get("fingerprint") or fingerprint(item)
        if fp in by_fp:
            continue  # already present (existing wins) -> skip the new copy
        by_fp[fp] = item
        order.append(fp)
    return [by_fp[fp] for fp in order]


def _tag_names(tags):
    """4DPocket tags may be [{id,name}] or flat strings (or absent). Return a
    list of name strings, defensively."""
    out = []
    for t in tags or []:
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                out.append(name)
        elif isinstance(t, str) and t:
            out.append(t)
    return out


def item_to_candidate(item):
    """Map a 4DPocket item dict (from search_recent) to the engine CANDIDATE
    contract. source_id = the item's source_platform (or '4dpocket'); text =
    content || summary || description, capped at 1500; ts = created_at;
    domain_tags = tag names. Pure — no fingerprint yet (run() normalizes)."""
    text = (item.get("content") or item.get("summary") or item.get("description") or "")
    return {
        "source_id": item.get("source_platform") or "4dpocket",
        "source_type": item.get("item_type") or "4dpocket",
        "url": item.get("url") or "",
        "title": item.get("title") or "",
        "text": str(text)[:1500],
        "ts": item.get("created_at"),
        "domain_tags": _tag_names(item.get("tags")),
        "raw_id": item.get("id"),
    }


# ----------------------------------------------------------------------------
# defensive sibling resolution
# ----------------------------------------------------------------------------
def _sibling(modname):
    """Import a sibling fresh each call (returns sys.modules[modname] if a test
    has stubbed it). None if unavailable."""
    try:
        return importlib.import_module(modname)
    except Exception as e:  # noqa: BLE001
        _log(f"sibling {modname} unavailable: {e}")
        return None


def _search_4dp(after_iso, limit):
    """Read recent items from 4DPocket via research_fourdpocket.search_recent.
    Missing module/func or a raise -> [] + log (the cycle degrades safely)."""
    mod = _sibling("research_fourdpocket")
    if mod is None:
        return []
    fn = getattr(mod, "search_recent", None)
    if not callable(fn):
        _log("research_fourdpocket.search_recent missing/not callable -> skip")
        return []
    # Scope to the curiosity-feed collection (engine-sourced items only), not
    # Ollie's whole 4DPocket account (which holds unrelated fact-check reels).
    cid = None
    try:
        ec = getattr(mod, "ensure_collection", None)
        if callable(ec):
            cid = ec()
    except Exception as e:  # noqa: BLE001
        _log(f"research_fourdpocket.ensure_collection failed: {e} -> read unscoped")
    try:
        result = fn(after_iso=after_iso, query="", source_platform=None,
                    limit=limit, collection_id=cid)
        return list(result or [])
    except Exception as e:  # noqa: BLE001
        _log(f"research_fourdpocket.search_recent raised: {e} -> degrade (no items)")
        return []


def _load_registry():
    """Sources + interests via research_registry, degrading to empties."""
    mod = _sibling("research_registry")
    sources, interests = [], {}
    if mod is not None:
        try:
            sources = list(getattr(mod, "load_sources")() or [])
        except Exception as e:  # noqa: BLE001
            _log(f"research_registry.load_sources failed: {e}")
        try:
            interests = getattr(mod, "load_interests")() or {}
        except Exception as e:  # noqa: BLE001
            _log(f"research_registry.load_interests failed: {e}")
    # fall back to on-disk files if the registry sibling is absent
    p = _paths()
    if not sources:
        sources = _read_json(p["sources"], []) or []
    if not interests:
        interests = _read_json(p["interests"], {}) or {}
    return sources, interests


def _gate(cands, interests, now_ts, sources_by_id):
    """research_gate.score_and_filter, degrading to a pass-through (every
    candidate kept with DEFAULT_RELEVANCE) so a broken gate never silently
    empties the pipeline — it just stops saving tokens until repaired."""
    mod = _sibling("research_gate")
    if mod is not None and callable(getattr(mod, "score_and_filter", None)):
        try:
            return list(mod.score_and_filter(
                cands, interests, now_ts=now_ts, sources_by_id=sources_by_id) or [])
        except Exception as e:  # noqa: BLE001
            _log(f"research_gate.score_and_filter raised: {e} -> pass-through degrade")
    else:
        _log("research_gate unavailable -> pass-through degrade")
    return [dict(c, relevance=c.get("relevance", DEFAULT_RELEVANCE)) for c in cands]


# ----------------------------------------------------------------------------
# seen.json helpers
# ----------------------------------------------------------------------------
def _load_seen():
    data = _read_json(_paths()["seen"], {}) or {}
    fps = data.get("fingerprints") if isinstance(data, dict) else None
    return list(fps or [])


def _save_seen(fingerprints):
    fps = list(fingerprints)[-SEEN_CAP:]  # FIFO cap
    return _write_json(_paths()["seen"], {"fingerprints": fps})


# ----------------------------------------------------------------------------
# run() — one poll cycle
# ----------------------------------------------------------------------------
def _normalize(cand):
    """Ensure the candidate has the contract fields + a fingerprint + epoch ts."""
    c = dict(cand)
    c.setdefault("source_id", c.get("source") or "unknown")
    c.setdefault("source_type", c.get("type") or "unknown")
    c.setdefault("url", c.get("link") or "")
    c.setdefault("title", "")
    c.setdefault("text", c.get("summary") or "")
    c.setdefault("domain_tags", [])
    c["ts"] = _to_epoch(c.get("ts"))
    c["fingerprint"] = fingerprint(c)
    return c


def _after_iso(now_ts, window_days=RECENCY_WINDOW_DAYS):
    """ISO-8601 UTC lower bound for the 4DPocket `after` filter."""
    import datetime
    dt = datetime.datetime.fromtimestamp(
        now_ts - window_days * DAY_S, tz=datetime.timezone.utc)
    return dt.isoformat()


def run(now_ts=None):
    """One cycle. READS recent items from 4DPocket, gates/ranks them into the
    persistent queue. Returns the ranked queue (also persisted to queue.json).
    Never raises — a failure anywhere degrades and logs."""
    now_ts = float(now_ts if now_ts is not None else time.time())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts))
    p = _paths()
    _log("run: queue cycle START (4DPocket-backed)")

    sources, interests = _load_registry()
    # source weights still apply when a 4DPocket item's source_platform happens
    # to match a registered source id; otherwise weight defaults (4DPocket
    # aggregates many origins under coarse source_platforms post-rearchitecture).
    sources_by_id = {s.get("id"): s for s in sources if isinstance(s, dict) and s.get("id")}

    # 1) READ recent items back out of 4DPocket (the ingestion+extraction spine).
    #    Discovery pushes + feed polling happen on the OFFSET feeder timer; by
    #    now 4DPocket has processed them, so this read picks them up.
    after_iso = _after_iso(now_ts)
    items = _search_4dp(after_iso, FETCH_LIMIT)
    cands = [_normalize(item_to_candidate(it)) for it in items if isinstance(it, dict)]
    _log(f"run: read {len(cands)} recent items from 4DPocket (after {after_iso})")

    # 2) dedup vs seen.json (token saver: never re-process what we've handled)
    seen = _load_seen()
    fresh = dedup(cands, seen)
    _log(f"run: {len(fresh)} fresh after seen-dedup")

    # 3) gate — recency hard-filter + relevance judge (the token saver). New
    #    candidates only, so the gate never re-scores items we've kept.
    scored = _gate(fresh, interests, now_ts, sources_by_id)
    _log(f"run: {len(scored)} survived the gate")

    # 4) score into queue items
    new_items = []
    for s in scored:
        if "fingerprint" not in s:
            s = _normalize(s)
        src = sources_by_id.get(s.get("source_id"), {})
        weight = src.get("weight", DEFAULT_WEIGHT)
        half_life = src.get("recency_days") or RECENCY_HALF_LIFE_DAYS
        rf = recency_factor(s.get("ts"), now_ts, half_life)
        rel = s.get("relevance", DEFAULT_RELEVANCE)
        item = dict(s)
        item.update({
            "relevance": rel,
            "weight": weight,
            "recency_factor": rf,
            "score": compute_score(rel, weight, rf),
            "status": "queued",
            "added_at": now_iso,
            "manual_priority": s.get("manual_priority"),
        })
        new_items.append(item)

    # 5) merge with the persisted queue + rerank + cap
    existing = _read_json(p["queue"], []) or []
    merged = merge_into_queue(existing, new_items)
    ranked = rerank(merged)[:MAX_QUEUE]
    _write_json(p["queue"], ranked)

    # 6) remember the new fingerprints so next cycle skips them pre-gate
    if new_items:
        _save_seen(seen + [i["fingerprint"] for i in new_items])
    _log(f"run: queue now {len(ranked)} items (+{len(new_items)} new) -> persisted")
    return ranked


# ----------------------------------------------------------------------------
# dispatch() — hand top-N queued items to the job pipeline within budget
# ----------------------------------------------------------------------------
def _budget_check(lane):
    """`budget.py check <lane>` — exit 0 = allowed, non-zero = over cap/refuse
    (mirrors job-submit.sh:22). Returns (ok, message). Overridable in tests."""
    p = _paths()
    try:
        r = subprocess.run(
            ["python3", p["budget_py"], "check", lane],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        _log(f"budget check failed ({e}) -> refuse (fail-closed)")
        return False, f"budget check error: {e}"


def _job_submit(task, lane):
    """job-submit.sh --channel telegram --to <owner> --silent --lane <lane>
    --task <task>. The script re-checks the budget at :22 and RECORDS the spend
    at :60, so dispatch must NOT record separately. Returns (ok, message).
    Overridable in tests."""
    p = _paths()
    try:
        r = subprocess.run(
            [p["job_submit"], "--channel", "telegram", "--to", OWNER_TELEGRAM,
             "--silent", "--lane", lane, "--task", task],
            capture_output=True, text=True, timeout=60,
        )
        # rc 4 == job-submit refused on budget (its own check); treat as refuse
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        _log(f"job-submit failed ({e})")
        return False, f"job-submit error: {e}"


def _task_template(item):
    """Curiosity research task (adapted from HEARTBEAT.md's lab-research
    template). Findings ride the morning brief — the job is --silent."""
    title = (item.get("title") or "(untitled)").strip()
    url = (item.get("url") or "").strip()
    text = (item.get("text") or "").strip()[:600]
    return (
        f"LAB RESEARCH on a curiosity-queue item: {title} — {url}\n"
        f"Context: {text}\n\n"
        "This item was surfaced by the curiosity engine (already recency + "
        "relevance gated, so it is fresh and on-interest). Do focused web "
        "research: what it is, why it matters to onllm / Tushar's tracked "
        "interests, the key takeaways, and whether it warrants a deeper POC. "
        "Write a 1-pager note to lab/notes/ and append ONE headline line to "
        "LAB_LEDGER.md using the status_class taxonomy. Findings ride the "
        "morning brief — do NOT ping."
    )


def dispatch(top_n=DISPATCH_TOP_N, dry_run=False, now_ts=None,
             check_fn=None, submit_fn=None):
    """Hand the top queued items to job-submit within the research budget.

    For each queued item (in priority order): `budget.py check research`; if
    over cap -> STOP (don't skip ahead — the cap is for today). If allowed,
    submit a --silent --lane research job; on success mark the item
    `dispatched` and remember its fingerprint. Stops at top_n dispatches.

    --dry-run mirrors the dream-promoter staged-rollout pattern: print what
    WOULD dispatch and write NOTHING (no queue mutation, no seen mutation, no
    job submitted).

    check_fn/submit_fn are injectable for tests; default to the real subprocess
    wrappers above.
    """
    check_fn = check_fn or _budget_check
    submit_fn = submit_fn or _job_submit
    now_ts = float(now_ts if now_ts is not None else time.time())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts))
    p = _paths()

    queue = _read_json(p["queue"], []) or []
    ranked = rerank(queue)
    queued = [it for it in ranked if it.get("status") == "queued"]
    dispatched = []

    if dry_run:
        ok, msg = check_fn(LANE)
        _log(f"dispatch --dry-run: budget {LANE} -> {'OK' if ok else 'REFUSED'} ({msg})")
        for it in queued[:top_n]:
            _log(f"[dry-run] would dispatch: {(it.get('title') or it.get('url') or '?')[:80]} "
                 f"(score={it.get('score'):.4f})")
        _log(f"dispatch --dry-run: {min(len(queued), top_n)} would dispatch, wrote nothing")
        return dispatched  # nothing written, nothing submitted

    seen = _load_seen()
    for it in queued:
        if len(dispatched) >= top_n:
            break
        ok, msg = check_fn(LANE)
        if not ok:
            _log(f"dispatch: budget refused ({msg}) -> stop")
            break
        sub_ok, sub_msg = submit_fn(_task_template(it), LANE)
        if not sub_ok:
            _log(f"dispatch: job-submit refused/failed ({sub_msg}) -> stop")
            break
        it["status"] = "dispatched"
        it["dispatched_at"] = now_iso
        seen.append(it.get("fingerprint") or fingerprint(it))
        dispatched.append(it)
        _log(f"dispatch: submitted research job for "
             f"{(it.get('title') or it.get('url') or '?')[:80]} ({sub_msg})")

    if dispatched:
        _write_json(p["queue"], ranked)   # persist status changes
        _save_seen(seen)
    _log(f"dispatch: {len(dispatched)} job(s) submitted")
    return dispatched


# ----------------------------------------------------------------------------
# entrypoint
# ----------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    no_dispatch = "--no-dispatch" in argv
    dry_run = "--dry-run" in argv
    # Course-correction safety valve: keep ingesting, deduplicating and ranking
    # candidates while preventing the curiosity subsystem from commissioning
    # work independently of the executive selector. This marker is checked on
    # every run, so pausing does not depend on a systemd daemon reload.
    if os.path.exists(AUTONOMOUS_DISPATCH_PAUSE):
        no_dispatch = True
        _log("autonomous research dispatch paused; candidates only")
    try:
        run()
    except Exception as e:  # noqa: BLE001 — a poll-cycle failure must not crash the unit
        _log(f"run() crashed (unexpected): {e}")
    if not no_dispatch:
        try:
            dispatch(dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            _log(f"dispatch() crashed (unexpected): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
