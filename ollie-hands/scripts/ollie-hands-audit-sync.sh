#!/usr/bin/env bash
# ollie-hands-audit-sync.sh — off-box sync of the tamper-evident hands audit.
#
# The other half of the hourly sync (plan Track D2). Runs in the WSL gateway
# (OpenClawGateway), which already holds `age`, an authed `gh`, the off-box repo
# recipient, and push access — everything EXCEPT the audit data, which lives on
# the host behind a deliberate filesystem-isolation boundary. The host pipes the
# audit chain here as base64(tar.gz) over stdin (no shared filesystem), and this
# script age-encrypts it to the Mac-only recipient and pushes it to
# onllm-dev/ollie-state as ollie-hands-audit-<UTC>.age.
#
# Reuses the existing off-box pattern; pushes only the encrypted .age (never
# plaintext). A dedicated working clone avoids racing the nightly state backup;
# pull --rebase + a bounded push retry handle the shared-repo case.
#
#   <host audit-export.py | base64> | ollie-hands-audit-sync.sh
set -euo pipefail

PATH="$HOME/.local/bin:$PATH"
readonly OC="$HOME/.openclaw"
readonly RECIPIENT_FILE="$OC/secrets/backup-recipient.age"
readonly REPO_DIR="$OC/hands-audit-repo"
readonly REPO_URL="https://github.com/onllm-dev/ollie-state.git"
readonly LOG="$OC/logs/hands-audit-sync.log"
readonly LOCK="$OC/hands-audit-sync.lock"
readonly PREFIX="ollie-hands-audit"
readonly KEEP=240          # ~10 days at hourly cadence
readonly GH="$HOME/.local/bin/gh"

mkdir -p "$OC/logs"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG" >&2; }

# single-flight
exec 9>"$LOCK"
if ! flock -n 9; then log "another sync is running; skip"; exit 0; fi

UTC="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d /tmp/hands-audit-XXXXXX)"
TGZ="$WORK.tgz"
CIPHER="/tmp/${PREFIX}-${UTC}.age"
cleanup() { rm -rf "$WORK" "$TGZ" "$CIPHER"; }
trap cleanup EXIT

# 1) receive base64(tar.gz) on stdin -> tarball
base64 -d > "$TGZ"
sz=$(wc -c < "$TGZ")
if [ "$sz" -lt 30 ]; then log "ERROR: empty/short stdin ($sz bytes)"; exit 1; fi
tar xzf "$TGZ" -C "$WORK"
n=$(ls -1 "$WORK"/audit-*.jsonl 2>/dev/null | wc -l | tr -d ' ')
if [ "$n" -lt 1 ]; then log "ERROR: no audit-*.jsonl in payload"; exit 1; fi
log "received $n audit files ($sz bytes gz)"

# 2) encrypt the tarball to the Mac-only recipient
rec="$(cat "$RECIPIENT_FILE")"
age -r "$rec" -o "$CIPHER" "$TGZ"
if ! head -c 22 "$CIPHER" | grep -q "age-encryption"; then
  log "ERROR: output is not age ciphertext"; exit 1
fi

# 3) ensure a dedicated working clone exists
if [ ! -d "$REPO_DIR/.git" ]; then
  log "cloning $REPO_URL -> $REPO_DIR"
  "$GH" repo clone onllm-dev/ollie-state "$REPO_DIR" >>"$LOG" 2>&1 \
    || git clone "$REPO_URL" "$REPO_DIR" >>"$LOG" 2>&1
fi
cd "$REPO_DIR"

# 4) place the .age, prune our own prefix, commit, push (with rebase+retry)
cp "$CIPHER" "$REPO_DIR/$(basename "$CIPHER")"
# shellcheck disable=SC2012
ls -1tr ${PREFIX}-*.age 2>/dev/null | head -n -"$KEEP" | while read -r old; do
  git rm -q -- "$old" 2>/dev/null || rm -f "$old"
done
git add "${PREFIX}"-*.age
if git diff --cached --quiet; then log "nothing to commit"; exit 0; fi
git commit -q -m "hands audit sync ${UTC}" >>"$LOG" 2>&1

ok=0
for attempt in 1 2 3; do
  git pull --rebase --autostash origin main >>"$LOG" 2>&1 || true
  if git push origin main >>"$LOG" 2>&1; then ok=1; break; fi
  log "push attempt $attempt failed; retrying"
  sleep 3
done
if [ "$ok" -eq 1 ]; then
  log "pushed $(basename "$CIPHER") (kept last $KEEP)"
else
  log "ERROR: push failed after retries"; exit 1
fi
