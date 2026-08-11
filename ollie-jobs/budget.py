#!/usr/bin/env python3
"""budget — Ollie's spend governance: measurement (audit) + ceilings (gate).

Two independent halves:

MEASUREMENT (read-only): openclaw writes a `model.completed` event with
`data.usage = {input, output, cacheRead, total}` to every session's
trajectory .jsonl. We just READ those — full token history, zero
instrumentation. `audit()` classifies each session by its sessionKey into a
lane and aggregates tokens by lane/day.

CEILINGS (pre-spend gate): `check(lane)` / `record(lane)` count today's
self-directed actions against a configurable per-lane + global daily cap,
enforced mechanically (immune to model state). Counts live in
spend-state.json; ceilings in budget-config.json.

CLI:
  budget.py audit [days]     # token cost by lane/day (default 7)
  budget.py check <lane>     # exit 0 = allowed, 3 = over cap
  budget.py reserve <lane> <item-id>  # atomically check + increment
  budget.py record <lane>    # increment today's counter
  budget.py status           # today's counts vs ceilings
"""
import glob
import fcntl
import json
import os
import sys
import time

HOME = "/home/openclaw"
SESS = f"{HOME}/.openclaw/agents/main/sessions"
WS = f"{HOME}/.openclaw/workspace"
CONFIG = f"{WS}/budget-config.json"
STATE = f"{HOME}/.openclaw/logs/spend-state.json"
LOCK = f"{STATE}.lock"

# Self-directed lanes the gate governs. Reactive lanes (telegram/whatsapp =
# Tushar talking to Ollie) are NOT capped — answering the owner is never
# rationed. heartbeat is timer-bounded (fixed cadence), so it's measured but
# not count-gated.
DEFAULT_CONFIG = {
    "ceilings": {"research": 6, "poc": 2, "project": 6},
    "global_self_directed": 10,
    "note": "Edit these dials to govern Ollie's autonomous spend. "
            "Reactive replies to Tushar are never capped.",
}


def classify(session_key):
    # Keys arrive as "agent:main:<key>" — match on substring, not prefix.
    k = session_key or ""
    if "heartbeat" in k:
        return "heartbeat"
    if ":project-" in k:
        return "project"
    if ":job-" in k:
        return "job"          # research/poc — split by the jobs ledger, not here
    if "dreaming" in k:
        return "dreaming"
    if "subagent" in k:
        return "subagent"
    if "telegram" in k:
        return "telegram"
    if "whatsapp" in k:
        return "whatsapp"
    # dev/debug sessions (probe-*, mem-test, chain-verify, *-demo, etc.) —
    # ad-hoc human-run, not part of Ollie's autonomous spend.
    return "dev/other"


def audit(days=7):
    """Aggregate token usage by lane/day from trajectory files."""
    cutoff = time.time() - days * 86400
    # lane -> day -> {calls, sessions, input, output, cacheRead, total}
    agg = {}
    sessions_seen = {}
    for f in glob.glob(f"{SESS}/*.trajectory.jsonl"):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        lane, day = "other", None
        try:
            for line in open(f):
                d = json.loads(line)
                t = d.get("type")
                if t == "session.started":
                    lane = classify(d.get("sessionKey"))
                    day = (d.get("ts") or "")[:10]
                    sessions_seen.setdefault((lane, day), set()).add(d.get("sessionId"))
                elif t == "model.completed":
                    u = (d.get("data") or {}).get("usage") or {}
                    day_k = (d.get("ts") or day or "")[:10]
                    cell = agg.setdefault(lane, {}).setdefault(day_k, {
                        "calls": 0, "input": 0, "output": 0, "cacheRead": 0, "total": 0})
                    cell["calls"] += 1
                    for k in ("input", "output", "cacheRead", "total"):
                        cell[k] += int(u.get(k) or 0)
        except Exception:  # noqa: BLE001
            continue
    return agg, sessions_seen


def print_audit(days=7):
    agg, sess = audit(days)
    lane_tot = {}
    print(f"=== spend audit, last {days}d (tokens) ===")
    print(f"{'lane':<11}{'day':<12}{'sessions':>9}{'calls':>7}{'input':>12}{'output':>10}{'cacheRead':>11}{'total':>13}")
    grand = 0
    for lane in sorted(agg):
        for day in sorted(agg[lane]):
            c = agg[lane][day]
            ns = len(sess.get((lane, day), set()))
            print(f"{lane:<11}{day:<12}{ns:>9}{c['calls']:>7}{c['input']:>12,}{c['output']:>10,}{c['cacheRead']:>11,}{c['total']:>13,}")
            lane_tot[lane] = lane_tot.get(lane, 0) + c["total"]
            grand += c["total"]
    print("--- lane totals ---")
    for lane in sorted(lane_tot, key=lambda x: -lane_tot[x]):
        pct = 100 * lane_tot[lane] / grand if grand else 0
        print(f"{lane:<11}{lane_tot[lane]:>15,} tok  ({pct:4.1f}%)")
    print(f"{'GRAND':<11}{grand:>15,} tok over {days}d  (~{grand // max(days,1):,}/day)")


# ---------------- ceilings (pre-spend gate) ----------------

def load_config():
    try:
        c = json.load(open(CONFIG))
        return {**DEFAULT_CONFIG, **c, "ceilings": {**DEFAULT_CONFIG["ceilings"], **c.get("ceilings", {})}}
    except Exception:  # noqa: BLE001
        return DEFAULT_CONFIG


def load_state():
    today = time.strftime("%Y-%m-%d")
    try:
        s = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        s = {}
    if s.get("date") != today:
        s = {"date": today, "counts": {}}
    s.setdefault("counts", {})
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = f"{STATE}.tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def _locked_state():
    """Open the spend lock and return it with today's state.

    Callers must keep the returned file open for the whole read/modify/write
    transaction and close it when finished.
    """
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock, load_state()


def check(lane):
    cfg = load_config()
    st = load_state()
    counts = st["counts"]
    ceil = cfg["ceilings"].get(lane)
    used = counts.get(lane, 0)
    if ceil is not None and used >= ceil:
        return False, f"{lane} daily cap reached ({used}/{ceil})"
    gcap = cfg.get("global_self_directed")
    gused = sum(counts.get(l, 0) for l in cfg["ceilings"])
    if gcap is not None and gused >= gcap:
        return False, f"global self-directed cap reached ({gused}/{gcap})"
    return True, f"{lane} {used}/{ceil if ceil is not None else '∞'}, global {gused}/{gcap}"


def record(lane):
    lock, st = _locked_state()
    try:
        st["counts"][lane] = st["counts"].get(lane, 0) + 1
        save_state(st)
        return st["counts"][lane]
    finally:
        lock.close()


def reserve(lane, item_id):
    """Atomically reserve one governed action.

    ``item_id`` makes retries idempotent: reserving the same item in the same
    lane succeeds without consuming another slot. Reusing an id for a
    different lane is rejected because it indicates a caller bug.
    """
    if not item_id:
        return False, "reservation item id must not be empty"

    cfg = load_config()
    # Reactive and other explicitly uncapped lanes are neither rationed nor
    # counted. This keeps owner replies out of the autonomous-spend ledger.
    if lane not in cfg["ceilings"]:
        return True, f"{lane} is uncapped"

    lock, st = _locked_state()
    try:
        reservations = st.setdefault("reservations", {})
        existing = reservations.get(item_id)
        if existing is not None:
            if existing == lane:
                return True, f"{lane} already reserved for {item_id}"
            return False, f"item {item_id} already reserved in lane {existing}"

        counts = st["counts"]
        ceil = cfg["ceilings"].get(lane)
        used = counts.get(lane, 0)
        if ceil is not None and used >= ceil:
            return False, f"{lane} daily cap reached ({used}/{ceil})"
        gcap = cfg.get("global_self_directed")
        gused = sum(counts.get(l, 0) for l in cfg["ceilings"])
        if gcap is not None and gused >= gcap:
            return False, f"global self-directed cap reached ({gused}/{gcap})"

        counts[lane] = used + 1
        reservations[item_id] = lane
        save_state(st)
        return True, (f"reserved {lane} {counts[lane]}/{ceil if ceil is not None else '∞'}, "
                      f"global {gused + 1}/{gcap}")
    finally:
        lock.close()


def main(argv):
    if not argv or argv[0] == "audit":
        print_audit(int(argv[1]) if len(argv) > 1 else 7)
        return 0
    cmd = argv[0]
    if cmd == "check":
        ok, why = check(argv[1])
        print(why)
        return 0 if ok else 3
    if cmd == "record":
        print(f"{argv[1]} -> {record(argv[1])}")
        return 0
    if cmd == "reserve":
        if len(argv) != 3:
            print("usage: budget.py reserve <lane> <item-id>", file=sys.stderr)
            return 2
        ok, why = reserve(argv[1], argv[2])
        print(why)
        return 0 if ok else 3
    if cmd == "status":
        cfg, st = load_config(), load_state()
        print(f"date {st['date']}  counts {st['counts']}")
        print(f"ceilings {cfg['ceilings']}  global {cfg.get('global_self_directed')}")
        return 0
    print(f"unknown: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
