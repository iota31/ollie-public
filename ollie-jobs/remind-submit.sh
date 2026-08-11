#!/usr/bin/env bash
# Schedule a reminder for Ollie to deliver at a future time.
#   remind-submit.sh --channel whatsapp|telegram --to <recipient> \
#                    --at <ISO8601> --message "<text>"
# --at: ISO 8601, e.g. 2026-06-10T07:00:00 (no offset => assumed IST +05:30),
#       or with offset 2026-06-10T07:00:00+05:30.
# Exit codes: 0 scheduled; 2 usage; 3 bad time; 4 past; 10 WHATSAPP_24H_LIMIT.
set -euo pipefail
CHANNEL="" TO="" AT="" MSG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="$2"; shift 2;;
    --to)      TO="$2";      shift 2;;
    --at)      AT="$2";      shift 2;;
    --message) MSG="$2";     shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$CHANNEL" && -n "$TO" && -n "$AT" && -n "$MSG" ]] || { echo "usage: remind-submit.sh --channel whatsapp|telegram --to <recipient> --at <ISO8601> --message <text>" >&2; exit 2; }
[[ "$CHANNEL" == "whatsapp" || "$CHANNEL" == "telegram" ]] || { echo "channel must be whatsapp or telegram" >&2; exit 2; }
export CHANNEL TO AT MSG
python3 - <<'PY'
import json, os, time, datetime
ch = os.environ["CHANNEL"]; to = os.environ["TO"]; at = os.environ["AT"]; msg = os.environ["MSG"]
# Naive datetimes are interpreted in the SYSTEM local timezone (what the user
# sees on their own devices), not a hardcoded zone.
LOCAL = datetime.datetime.now().astimezone().tzinfo
try:
    dt = datetime.datetime.fromisoformat(at.strip())
except ValueError:
    print("ERROR_BAD_TIME"); raise SystemExit(3)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=LOCAL)
epoch = dt.timestamp(); now = time.time()
if epoch <= now + 5:
    print("ERROR_PAST"); raise SystemExit(4)
if ch == "whatsapp" and (epoch - now) > 24 * 3600:
    print("WHATSAPP_24H_LIMIT"); raise SystemExit(10)
rid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
human = dt.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M %Z")
rem = {"id": rid, "created": datetime.datetime.now(LOCAL).isoformat(),
       "channel": ch, "to": to, "deliver_at": epoch,
       "deliver_at_human": human,
       "message": msg, "status": "scheduled", "kind": "reminder"}
d = "/home/openclaw/.openclaw/workspace/jobs/reminders"
os.makedirs(d, exist_ok=True)
json.dump(rem, open(f"{d}/{rid}.json", "w"), indent=1)
print(f"SCHEDULED {rid} for {rem['deliver_at_human']}")
PY
