#!/usr/bin/env bash
# Submit a background job for Ollie.
# Usage: job-submit.sh --channel whatsapp|telegram --to <number-or-chatid> --task "<task text>" [--silent]
#   --silent: run the job but do NOT message the recipient with the result
#             (lab jobs: findings ride the morning brief instead).
set -euo pipefail
CHANNEL="" TO="" TASK="" SILENT="false" LANE="reactive"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="$2"; shift 2;;
    --to)      TO="$2";      shift 2;;
    --task)    TASK="$2";    shift 2;;
    --silent)  SILENT="true"; shift 1;;
    --lane)    LANE="$2";    shift 2;;   # research|poc = self-directed (capped); reactive = uncapped
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[[ -n "$CHANNEL" && -n "$TO" && -n "$TASK" ]] || { echo "usage: job-submit.sh --channel whatsapp|telegram --to <recipient> --task <text>" >&2; exit 2; }
[[ "$CHANNEL" == "whatsapp" || "$CHANNEL" == "telegram" ]] || { echo "channel must be whatsapp or telegram" >&2; exit 2; }
[[ "$LANE" == "reactive" || "$LANE" == "research" || "$LANE" == "poc" ]] || { echo "lane must be reactive, research, or poc" >&2; exit 2; }

HOME_DIR="${OLLIE_HOME:-/home/openclaw}"
QUEUE="$HOME_DIR/.openclaw/workspace/jobs/queue"
mkdir -p "$QUEUE"
ID="$(date +%Y%m%d-%H%M%S)-$RANDOM"
export ID CHANNEL TO TASK SILENT LANE QUEUE

# Check and consume the slot in one locked transaction. ID makes retries of
# the same submission idempotent; there is no check/record race at the cap.
if ! GATE=$(python3 "$HOME_DIR/bin/budget.py" reserve "$LANE" "$ID" 2>&1); then
  echo "budget: refused ($GATE) — not queuing this $LANE job" >&2
  exit 4
fi

python3 - <<'PY'
import json, os, re, time

# Permission tier is derived HERE, deterministically, from the recipient —
# never from the submitting agent (which could be guest-influenced).
OWNERS = {"<OWNER_PHONE>", "<OWNER_TELEGRAM_CHAT_ID>"}  # Tushar: WhatsApp digits, Telegram chat id
to_digits = re.sub(r"[^\d]", "", os.environ["TO"])
agent = "main" if to_digits in OWNERS else "guest"

job = {
    "id": os.environ["ID"],
    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "channel": os.environ["CHANNEL"],
    "to": os.environ["TO"],
    "agent": agent,
    "task": os.environ["TASK"],
    "deliver": os.environ.get("SILENT") != "true",
    "lane": os.environ.get("LANE", "reactive"),
    "status": "queued",
}
path = os.path.join(os.environ["QUEUE"], f"{job['id']}.json")
with open(path, "w") as f:
    json.dump(job, f, indent=1)
print(f"submitted job {job['id']}")
PY
