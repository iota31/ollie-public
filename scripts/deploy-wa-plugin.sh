#!/usr/bin/env bash
# Deploy the WhatsApp Cloud plugin from this repo to the box, safely.
#
#   ./scripts/deploy-wa-plugin.sh            # deploy + restart gateway + verify
#   ./scripts/deploy-wa-plugin.sh --no-restart
#
# Steps: local syntax check -> remote backup -> push -> hash compare ->
# remote syntax check -> gateway restart -> webhook challenge verify.
# Any failure after push restores the backup and restarts the gateway.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$REPO_DIR/openclaw-ollie-whatsapp-cloud/index.js"
REMOTE="/home/openclaw/.openclaw/plugins/ollie-whatsapp-cloud/index.js"
SSH=(ssh -i "$HOME/.ssh/id_ed25519" source@<TAILSCALE_IP>)
WSL_BASH=(wsl -d OpenClawGateway -u openclaw -- bash -)
NODE=/home/openclaw/.openclaw/tools/node-v22.22.0/bin/node
RESTART=true
[[ "${1:-}" == "--no-restart" ]] && RESTART=false

noise() { grep -vE "post-quantum|store now|may need|openssh.com|^\*\*$" || true; }
remote() { "${SSH[@]}" "wsl -d OpenClawGateway -u openclaw -- bash -" 2>&1 | noise; }

echo "1/6 local syntax check"
node --check "$LOCAL"

TS=$(date +%Y%m%d-%H%M%S)
echo "2/6 remote backup -> index.js.bak-$TS"
echo "cp $REMOTE $REMOTE.bak-$TS" | remote

echo "3/6 push"
cat "$LOCAL" | "${SSH[@]}" "wsl -d OpenClawGateway -u openclaw -- bash -c \"cat > $REMOTE\"" 2>/dev/null

echo "4/6 hash compare"
LH=$(shasum -a 256 "$LOCAL" | cut -d' ' -f1)
RH=$(echo "sha256sum $REMOTE" | remote | cut -d' ' -f1)
if [[ "$LH" != "$RH" ]]; then
  echo "HASH MISMATCH ($LH vs $RH) — restoring backup"
  echo "cp $REMOTE.bak-$TS $REMOTE" | remote
  exit 1
fi

echo "5/6 remote syntax check"
if ! echo "$NODE --check $REMOTE && echo NODE_OK" | remote | grep -q NODE_OK; then
  echo "REMOTE SYNTAX FAIL — restoring backup"
  echo "cp $REMOTE.bak-$TS $REMOTE" | remote
  exit 1
fi

if ! $RESTART; then
  echo "done (no restart requested — changes apply on next gateway restart)"
  exit 0
fi

echo "6/6 restart gateway + verify webhook"
# We do NOT use `systemctl --user` here. Over SSH→WSL there is no user D-Bus session
# bus (/run/user/1000/bus), so `systemctl --user` silently no-ops and the old gateway
# keeps running old code — the exact silent-failure that caused repo↔box drift.
# Instead: SIGTERM the gateway PID; the unit is Restart=always (RestartSec=5) so systemd
# respawns it with fresh code. We then require a *new* PID listening on :18789 — a
# missing new PID fails loudly. (Owner decision 2026-06-27: dbus fix abandoned.)
OUT=$(cat <<'RSH' | remote
OLD=$(pgrep -f 'dist/index.js gateway' | head -1)
kill -TERM "$OLD" 2>/dev/null
NEW=""
for i in $(seq 1 30); do
  sleep 1
  NEW=$(pgrep -f 'dist/index.js gateway' | head -1)
  if [[ -n "$NEW" && "$NEW" != "$OLD" ]] && ss -ltn 2>/dev/null | grep -q ':18789 '; then break; fi
  NEW=""
done
echo "OLD_PID=$OLD NEW_PID=$NEW"
if [[ -z "$NEW" ]]; then exit 2; fi
VT=$(python3 -c "import json;print(json.load(open('/home/openclaw/.openclaw/secrets/whatsapp-cloud.json'))['verifyToken'])")
BODY=$(curl -s "http://127.0.0.1:18789/plugins/whatsapp-cloud/webhook?hub.mode=subscribe&hub.verify_token=$VT&hub.challenge=deploy-ok")
echo "BODY=$BODY"
RSH
)
echo "$OUT"
# Fail loudly if the restart itself did not land, BEFORE checking the webhook.
# A "green webhook" against a stale gateway is the silent-failure mode we are fixing.
if ! grep -qE "NEW_PID=[0-9]+" <<<"$OUT"; then
  echo "GATEWAY RESTART FAILED (no new PID listening on :18789) — NOT proceeding to webhook verify; restoring backup"
  echo "cp $REMOTE.bak-$TS $REMOTE" | remote
  exit 1
fi
if ! grep -q "BODY=deploy-ok" <<<"$OUT"; then
  echo "WEBHOOK VERIFY FAILED — restoring backup and restarting"
  echo "cp $REMOTE.bak-$TS $REMOTE; kill -TERM \$(pgrep -f 'dist/index.js gateway' | head -1)" | remote
  exit 1
fi
echo "deploy OK"
