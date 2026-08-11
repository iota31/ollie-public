#!/usr/bin/env bash
# ollie-state-backup.sh
# Nightly encrypted state-backup of the Ollie agent (WSL distro OpenClawGateway).
# - Tar + gzip Ollie's living state into a tmpfile
# - Encrypt with `age` using a public recipient stored on the box
# - Push the .age file to a private GitHub repo (onllm-dev/ollie-state)
# - Prune to the last 7 backups in the repo
# - Logs to ~/.openclaw/logs/state-backup.log
#
# This script holds ONLY the public age recipient. The private key never
# touches the box; decryption requires the Mac-side private key.
#
# Idempotent + safe: set -euo pipefail, plaintext tarball always shredded.

set -euo pipefail

# ---- Config -----------------------------------------------------------------

readonly HOME_DIR="$HOME"
readonly OPENCLAW_DIR="$HOME_DIR/.openclaw"
readonly CONFIG_DIR="$HOME_DIR/.config"
readonly BIN_DIR="$OPENCLAW_DIR/bin"
readonly LOG_DIR="$OPENCLAW_DIR/logs"
readonly LOG_FILE="$LOG_DIR/state-backup.log"
readonly LOCK_FILE="$OPENCLAW_DIR/state-backup.lock"
readonly RECIPIENT_FILE="$OPENCLAW_DIR/secrets/backup-recipient.age"
readonly REPO_DIR="$OPENCLAW_DIR/state-backup-repo"
readonly REPO_URL="https://github.com/onllm-dev/ollie-state.git"
readonly REPO_BRANCH="main"
readonly KEEP_LAST=7
readonly GH_BIN="$HOME_DIR/.local/bin/gh"

# What to back up (relative to OPENCLAW_DIR for dirs, absolute for the json file).
readonly BACKUP_DIRS=(
  "workspace"
  "memory"
  "agents"
  "credentials"
  "secrets"
)
readonly BACKUP_FILES=(
  "$OPENCLAW_DIR/openclaw.json"
)
# Config dirs (relative to HOME_DIR, since tar uses -C $HOME_DIR for these).
readonly BACKUP_CONFIG_DIRS=(
  ".config/gh"
  ".config/himalaya"
)

# ---- Logging ----------------------------------------------------------------

mkdir -p "$LOG_DIR"
log() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts] $*" | tee -a "$LOG_FILE" >&2
}

# ---- Lock (prevent concurrent runs) -----------------------------------------

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "ERROR: another backup is already running (lock held: $LOCK_FILE)"
    exit 1
  fi
fi

# ---- Pre-flight checks ------------------------------------------------------

if ! command -v tar >/dev/null 2>&1; then
  log "ERROR: tar not found in PATH"
  exit 1
fi
if ! command -v gzip >/dev/null 2>&1; then
  log "ERROR: gzip not found in PATH"
  exit 1
fi
if ! command -v age >/dev/null 2>&1; then
  log "ERROR: age not found in PATH (install to ~/.local/bin/age)"
  exit 1
fi
if [[ ! -r "$RECIPIENT_FILE" ]]; then
  log "ERROR: recipient file not readable: $RECIPIENT_FILE"
  exit 1
fi
PUB_RECIPIENT="$(cat "$RECIPIENT_FILE")"
readonly PUB_RECIPIENT
if [[ ! "$PUB_RECIPIENT" =~ ^age1[0-9a-z]{58,}$ ]]; then
  log "ERROR: recipient file does not look like a valid age public key"
  exit 1
fi

# ---- Build the tarball ------------------------------------------------------

UTCDATE="$(date -u +%Y%m%dT%H%M%SZ)"
readonly UTCDATE
readonly TARBALL="/tmp/ollie-state-${UTCDATE}.tar.gz"
readonly CIPHERTEXT="/tmp/ollie-state-${UTCDATE}.age"

cleanup() {
  # Always shred the plaintext tarball on the way out.
  if [[ -f "$TARBALL" ]]; then
    rm -f "$TARBALL" || true
  fi
}
trap cleanup EXIT

log "Building tarball: $TARBALL"
tar -czf "$TARBALL" \
  -C "$OPENCLAW_DIR" \
  "${BACKUP_DIRS[@]}" \
  -C "$HOME_DIR" \
  ".openclaw/openclaw.json" \
  "${BACKUP_CONFIG_DIRS[@]}"

# Final safety check: the tarball must NOT look like an age file or contain a
# private key.
if head -c 4 "$TARBALL" | grep -q '^age'; then
  log "ERROR: tarball starts with 'age' magic - aborting before encryption"
  exit 1
fi
if grep -aq 'AGE-SECRET-KEY-1' "$TARBALL" 2>/dev/null; then
  log "ERROR: tarball appears to contain an age private key - aborting"
  exit 1
fi

TARBALL_SIZE="$(stat -c %s "$TARBALL" 2>/dev/null || stat -f %z "$TARBALL")"
readonly TARBALL_SIZE
log "Tarball size: ${TARBALL_SIZE} bytes"

# Warn (but proceed) for very large tarballs.
if [[ "$TARBALL_SIZE" -gt 524288000 ]]; then
  log "WARN: tarball > 500MB; consider pruning credentials/ or media/"
fi

# ---- Encrypt ----------------------------------------------------------------

log "Encrypting with age recipient: ${PUB_RECIPIENT:0:14}..."
age -r "$PUB_RECIPIENT" -o "$CIPHERTEXT" "$TARBALL"
# Belt-and-braces: plaintext is unlinked by the EXIT trap; remove here too.
rm -f "$TARBALL"

CIPHERTEXT_SIZE="$(stat -c %s "$CIPHERTEXT" 2>/dev/null || stat -f %z "$CIPHERTEXT")"
readonly CIPHERTEXT_SIZE
log "Ciphertext size: ${CIPHERTEXT_SIZE} bytes -> $CIPHERTEXT"

# ---- Clone / update the working repo ----------------------------------------

if [[ ! -d "$REPO_DIR/.git" ]]; then
  log "Cloning $REPO_URL into $REPO_DIR"
  "$GH_BIN" repo clone "$REPO_URL" "$REPO_DIR"
fi

# Make sure plain `git fetch`/`git pull` can authenticate to GitHub.
# `gh auth setup-git` is idempotent.
"$GH_BIN" auth setup-git >/dev/null 2>&1 || true

# Always operate from inside the repo.
cd "$REPO_DIR"

# Make sure we are on the right branch and up to date.
git checkout "$REPO_BRANCH" >/dev/null 2>&1 || true
if ! git pull --ff-only origin "$REPO_BRANCH" 2>>"$LOG_FILE"; then
  log "WARN: git pull failed (likely no upstream changes); continuing with local state"
fi

# ---- Copy in the new .age file ----------------------------------------------

cp "$CIPHERTEXT" "$REPO_DIR/ollie-state-${UTCDATE}.age"
log "Copied: $REPO_DIR/ollie-state-${UTCDATE}.age"

# ---- Prune to last KEEP_LAST backups ----------------------------------------

if [[ "$(ls -1 ollie-state-*.age 2>/dev/null | wc -l | tr -d ' ')" -gt "$KEEP_LAST" ]]; then
  # Sort by mtime ascending, drop the oldest beyond KEEP_LAST.
  ls -1tr ollie-state-*.age | head -n -"$KEEP_LAST" | while read -r old; do
    log "Pruning old backup: $old"
    git rm -f "$old" >/dev/null
  done
fi

# ---- Commit + push ----------------------------------------------------------

# Stage only .age files (defensive: never accidentally include plaintext).
git add ollie-state-*.age
if git diff --cached --quiet; then
  log "No new or changed .age files to commit; exiting clean"
  exit 0
fi

git -c user.name="Ollie" -c user.email="Ollie@onllm.dev" \
  commit -m "state backup ${UTCDATE}" >/dev/null

if ! git push origin "$REPO_BRANCH" 2>>"$LOG_FILE"; then
  log "ERROR: git push failed; backup ciphertext kept at $CIPHERTEXT"
  exit 1
fi

# Drop the local copy of the .age (the repo on the box is just a working clone;
# the canonical copy is the GitHub repo).
rm -f "$REPO_DIR/ollie-state-${UTCDATE}.age"
rm -f "$CIPHERTEXT"
git -c user.name="Ollie" -c user.email="Ollie@onllm.dev" \
  commit -am "cleanup: drop local copy of ${UTCDATE}" >/dev/null 2>&1 || true

log "OK: backup ${UTCDATE} pushed to $REPO_URL ($REPO_BRANCH)"
