#!/usr/bin/env python3
"""Lab watcher — Tushar's 4DPocket saves -> Ollie's lab inbox.

Mechanical half of Ollie's Lab (no LLM, no delivery). Runs hourly via
systemd timer. Polls Tushar's personal 4DPocket (READ-ONLY PAT) for new
saves, diffs against state, and drops each new save into the lab inbox
where the next heartbeat triages it (poc / research / index / skip).

State:  ~/.openclaw/workspace/lab/state.json   {"seen_ids": [...], "last_run": ...}
Inbox:  ~/.openclaw/workspace/lab/inbox/<item-id>.json
"""
import json
import os
import sys
import time
import urllib.request

HOME = "/home/openclaw"
LAB = f"{HOME}/.openclaw/workspace/lab"
INBOX = f"{LAB}/inbox"
STATE = f"{LAB}/state.json"
LOG = f"{HOME}/.openclaw/logs/lab-watcher.log"
PAT_FILE = f"{HOME}/.openclaw/secrets/fourdpocket-tushar-read.pat"
FOURDP = "http://<TAILSCALE_IP_VPS>:4040/api/v1"
HOST_HEADER = "localhost:4040"  # proxy 421s without it
POLL_LIMIT = 30
SEEN_CAP = 2000


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch_recent(pat):
    req = urllib.request.Request(
        f"{FOURDP}/items?sort_by=created_at&sort_order=desc&limit={POLL_LIMIT}",
        headers={"Host": HOST_HEADER, "Authorization": f"Bearer {pat}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    try:
        pat = open(PAT_FILE).read().strip()
    except OSError:
        log(f"FATAL: PAT file missing at {PAT_FILE}")
        return 1

    os.makedirs(INBOX, exist_ok=True)
    try:
        state = json.load(open(STATE))
    except Exception:  # noqa: BLE001
        state = {"seen_ids": []}
    seen = set(state.get("seen_ids", []))
    first_run = not seen

    try:
        items = fetch_recent(pat)
    except Exception as e:  # noqa: BLE001
        log(f"4dpocket poll failed (will retry next timer): {e}")
        return 0  # transient; not a unit failure

    new = [i for i in items if i.get("id") and i["id"] not in seen]
    for item in new:
        seen.add(item["id"])
        if first_run:
            continue  # baseline run: learn existing saves, don't flood the inbox
        slim = {k: item.get(k) for k in (
            "id", "url", "title", "description", "summary",
            "source_platform", "item_type", "created_at")}
        slim["tags"] = [t.get("name") for t in (item.get("tags") or []) if isinstance(t, dict)]
        path = f"{INBOX}/{item['id']}.json"
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(slim, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    state["seen_ids"] = list(seen)[-SEEN_CAP:]
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = f"{STATE}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)

    log(f"poll ok: {len(items)} fetched, {len(new)} new"
        + (" (baseline run, inbox skipped)" if first_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
