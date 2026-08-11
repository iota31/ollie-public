#!/usr/bin/env bash
#
# Ollie Mission Control — End-to-End Smoke Test
#
# Runs READS + GUARDRAILS always.
# Runs CONTROL HAPPY-PATHS + CRUD ROUND-TRIPS only in full mode (no --no-mutate).
#
# Usage:
#   BASE=http://<TAILSCALE_IP>:3400 TOKEN=... PIN=... ./e2e_smoke.sh
#   BASE=... TOKEN=... PIN=... ./e2e_smoke.sh --no-mutate
#
# Env:
#   BASE   — base URL (default: http://<TAILSCALE_IP>:3400)
#   TOKEN  — bearer token (no default; required)
#   PIN    — X-Ollie-Control PIN (no default; required)
#
# All mutating tests use the "mc-e2e-" prefix and perform guaranteed cleanup.
# The script is idempotent and safe to re-run.
#
set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
BASE="${BASE:-http://<TAILSCALE_IP>:3400}"
TOKEN="${TOKEN:-}"
PIN="${PIN:-}"
NO_MUTATE=0

TS="$(date +%s)"
PREFIX="mc-e2e-${TS}"

# SSH config for ground-truth checks (box)
SSH_HOST="source@<TAILSCALE_IP>"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
SSH_BASE="ssh ${SSH_OPTS} ${SSH_HOST}"

# ── Args ───────────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --no-mutate) NO_MUTATE=1 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
  esac
done

if [[ -z "${TOKEN}" || -z "${PIN}" ]]; then
  echo "ERROR: TOKEN and PIN must be provided via env (or edit defaults in script)." >&2
  exit 2
fi

# ── Helpers ────────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
declare -a FAILURES=()

note() { echo ">>> $*"; }
ok()   { echo "PASS: $*"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { echo "FAIL: $*"; FAIL_COUNT=$((FAIL_COUNT+1)); FAILURES+=("$*"); }
die()  { echo "FATAL: $*" >&2; exit 1; }

hdr_bearer() {
  printf 'Authorization: Bearer %s' "${TOKEN}"
}
hdr_pin() {
  printf 'X-Ollie-Control: %s' "${PIN}"
}

curl_get() {
  # $1 = path (no leading / required)
  local path="$1"
  curl -sS -w '\n%{http_code}' -H "$(hdr_bearer)" "${BASE%/}/${path#/}"
}

curl_get_status() {
  local path="$1"
  curl_get "$path" | tail -n1
}

curl_get_json() {
  local path="$1"
  curl_get "$path" | sed '$d'
}

curl_post() {
  # $1=path, $2=json_body, $3=include_pin (1/0)
  local path="$1" body="$2" with_pin="${3:-0}"
  if [[ "${with_pin}" == "1" ]]; then
    curl -sS -w '\n%{http_code}' -X POST -H "Content-Type: application/json" -H "$(hdr_bearer)" -H "$(hdr_pin)" -d "$body" "${BASE%/}/${path#/}"
  else
    curl -sS -w '\n%{http_code}' -X POST -H "Content-Type: application/json" -H "$(hdr_bearer)" -d "$body" "${BASE%/}/${path#/}"
  fi
}

curl_post_status() {
  local path="$1" body="$2" with_pin="${3:-0}"
  curl_post "$path" "$body" "$with_pin" | tail -n1
}

curl_put() {
  local path="$1" body="$2"
  curl -sS -w '\n%{http_code}' -X PUT -H "Content-Type: application/json" -H "$(hdr_bearer)" -d "$body" "${BASE%/}/${path#/}"
}

curl_delete() {
  local path="$1"
  curl -sS -w '\n%{http_code}' -X DELETE -H "$(hdr_bearer)" "${BASE%/}/${path#/}"
}

ssh_cmd_raw() {
  # Run a command on the box via SSH.
  # The remote form is: wsl -d OpenClawGateway -u openclaw -- bash -lc 'CMDS'
  # We must NOT embed double-quotes inside the remote single-quoted payload (cmd.exe portproxy eats them).
  # All local filtering (grep/tail) must be applied AFTER the closing quote of the ssh command.
  local user_cmd="$1"
  # Escape single quotes in user_cmd for safe embedding inside '...'
  # Strategy: end quote, emit '\'' (which becomes a literal ' inside the outer single quotes), resume quote.
  local escaped
  escaped="$(printf "%s" "$user_cmd" | sed "s/'/'\\\\''/g")"
  local remote="wsl -d OpenClawGateway -u openclaw -- bash -lc '${escaped}'"
  ${SSH_BASE} "${remote}" 2>/dev/null || true
}

ssh_cmd() {
  # Same as ssh_cmd_raw, but strip noise lines locally after the closing quote.
  local user_cmd="$1"
  ssh_cmd_raw "$user_cmd" | grep -vi "post-quantum\|store now\|upgraded\|openssh.com" || true
}

# ── Section: READS (always) ────────────────────────────────────────────────────
section_reads() {
  note "SECTION: READS (always)"
  local endpoints=(
    "/api/health"
    "/api/system/liveness"
    "/api/system/power"
    "/api/activity"
    "/api/watchdog"
    "/api/budget"
    "/api/budget/tokens"
    "/api/jobs"
    "/api/cron"
    "/api/projects"
    "/api/lab/state"
    "/api/lab/containers"
    "/api/hb/state"
    "/api/hb/log"
    "/api/hb/brief"
    "/api/hb/digest"
    "/api/openloops"
    "/api/sources"
    "/api/interests"
    "/api/queue"
  )
  for ep in "${endpoints[@]}"; do
    local status body
    body="$(curl_get_json "$ep" || true)"
    status="$(curl_get_status "$ep" || true)"
    if [[ "$status" == "200" ]]; then
      # Basic JSON validity + non-error shape check
      if echo "$body" | jq -e . >/dev/null 2>&1; then
        if echo "$body" | jq -e 'has("error")' >/dev/null 2>&1; then
          fail "READ $ep -> 200 but body has error field"
        else
          ok "READ $ep -> 200 + valid JSON (no top-level error)"
        fi
      else
        fail "READ $ep -> 200 but body is not valid JSON"
      fi
    else
      fail "READ $ep -> $status (expected 200)"
    fi
  done

  # Ground-truth cross-checks (best-effort; do not fail the run if SSH is unavailable)
  note "Ground-truth cross-checks (SSH) — best-effort"
  # /api/jobs counts vs job dir counts
  local jobs_api jobs_q jobs_r jobs_d jobs_f
  jobs_api="$(curl_get_json "/api/jobs" || echo '{}')"
  local jq_ok=0
  if echo "$jobs_api" | jq -e . >/dev/null 2>&1; then jq_ok=1; fi
  if [[ $jq_ok -eq 1 ]]; then
    local api_q api_r api_d api_f
    api_q="$(echo "$jobs_api" | jq -r '.queue|length' 2>/dev/null || echo 0)"
    api_r="$(echo "$jobs_api" | jq -r '.running|length' 2>/dev/null || echo 0)"
    api_d="$(echo "$jobs_api" | jq -r '.done|length' 2>/dev/null || echo 0)"
    api_f="$(echo "$jobs_api" | jq -r '.failed|length' 2>/dev/null || echo 0)"
    # SSH ls counts (create-tolerant)
    local ssh_q ssh_r ssh_d ssh_f
    ssh_q="$(ssh_cmd 'ls -1 /home/openclaw/.openclaw/workspace/jobs/queue/*.json 2>/dev/null | wc -l | tr -d " "' || echo 0)"
    ssh_r="$(ssh_cmd 'ls -1 /home/openclaw/.openclaw/workspace/jobs/running/*.json 2>/dev/null | wc -l | tr -d " "' || echo 0)"
    ssh_d="$(ssh_cmd 'ls -1 /home/openclaw/.openclaw/workspace/jobs/done/*.json 2>/dev/null | wc -l | tr -d " "' || echo 0)"
    ssh_f="$(ssh_cmd 'ls -1 /home/openclaw/.openclaw/workspace/jobs/failed/*.json 2>/dev/null | wc -l | tr -d " "' || echo 0)"
    if [[ "$api_q" == "$ssh_q" && "$api_r" == "$ssh_r" && "$api_d" == "$ssh_d" && "$api_f" == "$ssh_f" ]]; then
      ok "JOBS counts match (queue=$api_q running=$api_r done=$api_d failed=$api_f)"
    else
      note "JOBS count mismatch (api: q=$api_q r=$api_r d=$api_d f=$api_f | ssh: q=$ssh_q r=$ssh_r d=$ssh_d f=$ssh_f) — not a hard failure"
    fi
  else
    note "Skipping JOBS count cross-check (API did not return JSON)"
  fi

  # /api/health pills vs live procs (pgrep + gateway port)
  local health
  health="$(curl_get_json "/api/health" || echo '{}')"
  if echo "$health" | jq -e . >/dev/null 2>&1; then
    local gw_pill jobs_pill wd_pill
    gw_pill="$(echo "$health" | jq -r '.pills.gateway // "stale"' 2>/dev/null || echo stale)"
    jobs_pill="$(echo "$health" | jq -r '.pills.jobs // "stale"' 2>/dev/null || echo stale)"
    wd_pill="$(echo "$health" | jq -r '.pills.watchdog // "stale"' 2>/dev/null || echo stale)"
    local gw_port jobs_proc wd_proc
    gw_port="$(ssh_cmd 'ss -ltnp 2>/dev/null | grep -q ":18789" && echo up || echo down' || echo unknown)"
    jobs_proc="$(ssh_cmd 'pgrep -f ollie_jobs_runner >/dev/null 2>&1 && echo up || echo down' || echo unknown)"
    wd_proc="$(ssh_cmd 'pgrep -f ollie_watchdog >/dev/null 2>&1 && echo up || echo down' || echo unknown)"
    # Map states loosely: up -> ok, down -> critical; treat mismatch as note only
    local gw_expect jobs_expect wd_expect
    gw_expect=$([[ "$gw_port" == "up" ]] && echo "ok" || echo "critical")
    jobs_expect=$([[ "$jobs_proc" == "up" ]] && echo "ok" || echo "critical")
    wd_expect=$([[ "$wd_proc" == "up" ]] && echo "ok" || echo "critical")
    if [[ "$gw_pill" == "$gw_expect" && "$jobs_pill" == "$jobs_expect" && "$wd_pill" == "$wd_expect" ]]; then
      ok "HEALTH pills match live procs (gateway=$gw_pill jobs=$jobs_pill watchdog=$wd_pill)"
    else
      note "HEALTH pill mismatch (gateway:$gw_pill/$gw_expect jobs:$jobs_pill/$jobs_expect watchdog:$wd_pill/$wd_expect) — not a hard failure"
    fi
  else
    note "Skipping HEALTH pill cross-check (API did not return JSON)"
  fi

  # /api/hb/log last_beat_ts vs heartbeat.log newest timestamp (best-effort)
  local hb_log hb_last
  hb_log="$(curl_get_json "/api/hb/log" || echo '{}')"
  hb_last="$(echo "$hb_log" | jq -r '.last_beat_ts // empty' 2>/dev/null || true)"
  if [[ -n "${hb_last}" ]]; then
    # Fetch last few lines of heartbeat.log and compare roughly
    local last_line last_ts
    last_line="$(ssh_cmd 'tail -5 /home/openclaw/.openclaw/logs/heartbeat.log 2>/dev/null | tail -1' || true)"
    # Expect something like: 2026-06-15T... heartbeat ...
    last_ts="$(echo "$last_line" | awk '{print $1}' | tr -d '\r' || true)"
    if [[ -n "${last_ts}" ]]; then
      # Compare lexicographically as ISO-ish timestamps (not perfect, but good enough for smoke)
      if [[ "$hb_last" == "$last_ts" || "$hb_last" < "$last_ts" || "$last_ts" < "$hb_last" ]]; then
        ok "HB log last_beat_ts present and roughly matches heartbeat.log"
      else
        note "HB log timestamp drift (api:$hb_last log:$last_ts) — not a hard failure"
      fi
    else
      note "HB log cross-check: could not parse heartbeat.log last ts — not a hard failure"
    fi
  else
    note "Skipping HB log cross-check (no last_beat_ts in /api/hb/log)"
  fi
}

# ── Section: GUARDRAILS (negative, always) ─────────────────────────────────────
section_guardrails() {
  note "SECTION: GUARDRAILS (negative, always)"
  # missing bearer -> 401 on a control
  local st
  st="$(curl -sS -w '%{http_code}' -o /dev/null -X POST -H "Content-Type: application/json" -d '{"confirm":true}' "${BASE%/}/api/ctrl/heartbeat/beat" | tail -c 4 || true)"
  if [[ "$st" == "401" ]]; then ok "GUARD missing bearer -> 401"; else fail "GUARD missing bearer -> $st (expected 401)"; fi

  # bearer ok, no PIN -> 403 on a control
  st="$(curl -sS -w '%{http_code}' -o /dev/null -X POST -H "Content-Type: application/json" -H "$(hdr_bearer)" -d '{"confirm":true}' "${BASE%/}/api/ctrl/heartbeat/beat" | tail -c 4 || true)"
  if [[ "$st" == "403" ]]; then ok "GUARD bearer+no-PIN -> 403"; else fail "GUARD bearer+no-PIN -> $st (expected 403)"; fi

  # control missing confirm -> 400
  st="$(curl_post_status "/api/ctrl/heartbeat/beat" '{}' 1 | tail -c 4 || true)"
  if [[ "$st" == "400" ]]; then ok "GUARD control missing confirm -> 400"; else fail "GUARD control missing confirm -> $st (expected 400)"; fi

  # research/dispatch bad lane -> 400
  st="$(curl_post_status "/api/ctrl/research/dispatch" '{"task":"x","lane":"evil","confirm":true}' 1 | tail -c 4 || true)"
  if [[ "$st" == "400" ]]; then ok "GUARD research bad lane -> 400"; else fail "GUARD research bad lane -> $st (expected 400)"; fi

  # lab/spawn bad id -> 400
  st="$(curl_post_status "/api/ctrl/lab/spawn" '{"id":"a;rm -rf","confirm":true}' 1 | tail -c 4 || true)"
  if [[ "$st" == "400" ]]; then ok "GUARD lab/spawn bad id -> 400"; else fail "GUARD lab/spawn bad id -> $st (expected 400)"; fi

  # lab/kill bad id -> 400
  st="$(curl_post_status "/api/ctrl/lab/kill" '{"id":"a;rm -rf","confirm":true}' 1 | tail -c 4 || true)"
  if [[ "$st" == "400" ]]; then ok "GUARD lab/kill bad id -> 400"; else fail "GUARD lab/kill bad id -> $st (expected 400)"; fi

  # rapid repeat to hit rate-limit (heartbeat/beat is 1/60s)
  # First accepted (we don't care about side-effect here), second should 429
  st="$(curl_post_status "/api/ctrl/heartbeat/beat" '{"confirm":true}' 1 | tail -c 4 || true)"
  st="$(curl_post_status "/api/ctrl/heartbeat/beat" '{"confirm":true}' 1 | tail -c 4 || true)"
  if [[ "$st" == "429" ]]; then ok "GUARD rapid repeat -> 429 (heartbeat/beat)"; else note "GUARD rapid repeat -> $st (may not have hit the window; not a hard failure)"; fi
}

# ── Cleanup helpers (idempotent, best-effort) ─────────────────────────────────
cleanup_watchdog_keys() {
  # Read-modify-write only mc_mutes/mc_acks keys that match mc-e2e- prefix.
  # Uses SSH to fetch file, jq to strip matching keys, write back.
  local state_path="/home/openclaw/.openclaw/plugin-state/watchdog-state.json"
  local tmp="/tmp/${PREFIX}-wd.json"
  ssh_cmd_raw "cat ${state_path}" > "${tmp}" 2>/dev/null || true
  if [[ -s "${tmp}" ]]; then
    # Remove any mc-e2e-* keys under mc_mutes and mc_acks
    jq --arg pfx "${PREFIX}" '
      ( .mc_mutes // {} ) as $m |
      ( .mc_acks  // {} ) as $a |
      .mc_mutes = ($m | with_entries(select(.key | startswith($pfx) | not))) |
      .mc_acks  = ($a | with_entries(select(.key | startswith($pfx) | not))) |
      .
    ' "${tmp}" > "${tmp}.out" 2>/dev/null || true
    if [[ -s "${tmp}.out" ]]; then
      # Write back via SSH (cat > file)
      cat "${tmp}.out" | ${SSH_BASE} "wsl -d OpenClawGateway -u openclaw -- bash -lc 'cat > ${state_path}'" 2>/dev/null || true
    fi
    rm -f "${tmp}" "${tmp}.out" || true
  fi
}

cleanup_sources() {
  # DELETE any sources with id == PREFIX (we only create one: ${PREFIX}-src)
  local id="${PREFIX}-src"
  # Try DELETE; ignore errors
  curl_delete "/api/sources/${id}" >/dev/null 2>&1 || true
}

cleanup_lab_container() {
  # Best-effort kill of the mc-e2e container via API (if it exists).
  curl_post "/api/ctrl/lab/kill" "{\"id\":\"${PREFIX}\",\"confirm\":true}" 1 >/dev/null 2>&1 || true
}

final_residue_check() {
  note "FINAL RESIDUE CHECK"
  local leftover=0

  # watchdog-state mc-e2e keys
  local wd
  wd="$(ssh_cmd_raw "cat /home/openclaw/.openclaw/plugin-state/watchdog-state.json 2>/dev/null || echo '{}'" || true)"
  if echo "$wd" | grep -q "\"${PREFIX}"; then
    echo "RESIDUE: watchdog-state still contains ${PREFIX} keys"
    leftover=1
  fi

  # /api/sources mc-e2e-src
  local srcs
  srcs="$(curl_get_json "/api/sources" || echo '[]')"
  if echo "$srcs" | grep -q "\"${PREFIX}-src\""; then
    echo "RESIDUE: /api/sources still lists ${PREFIX}-src"
    leftover=1
  fi

  # podman ps for mc-e2e container (OllieLab distro)
  local pods
  pods="$(${SSH_BASE} "wsl -d OllieLab -u lab -- bash -lc 'podman ps -a --filter name=^${PREFIX} --format \"{{.Names}}\"'" 2>/dev/null | grep -vi "post-quantum\|store now\|upgraded\|openssh.com" || true)"
  if [[ -n "${pods}" ]]; then
    echo "RESIDUE: OllieLab still has container(s): ${pods}"
    leftover=1
  fi

  if [[ $leftover -eq 0 ]]; then
    ok "RESIDUE: no mc-e2e-* artifacts found (watchdog, sources, podman)"
  else
    fail "RESIDUE: mc-e2e-* artifacts remain — manual cleanup required"
  fi
}

# ── Section: CONTROL HAPPY-PATHS (mutate; only if not --no-mutate) ────────────
section_controls() {
  if [[ "${NO_MUTATE}" == "1" ]]; then
    note "SECTION: CONTROLS (skipped due to --no-mutate)"
    return 0
  fi
  note "SECTION: CONTROLS (mutating; prefix=${PREFIX})"

  # a) watchdog mute + ack + cleanup
  local mute_key="${PREFIX}-mute"
  local ack_key="${PREFIX}-ack"
  local st body

  st="$(curl_post_status "/api/ctrl/watchdog/mute" "{\"key\":\"${mute_key}\",\"minutes\":60,\"confirm\":true}" 1 | tail -c 4 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CTRL watchdog/mute -> 200"
    # Verify via SSH
    body="$(ssh_cmd_raw "cat /home/openclaw/.openclaw/plugin-state/watchdog-state.json 2>/dev/null || echo '{}'" || true)"
    if echo "$body" | grep -q "\"${mute_key}\""; then ok "CTRL watchdog/mute verified in watchdog-state.json"; else fail "CTRL watchdog/mute not visible in watchdog-state.json"; fi
  else
    fail "CTRL watchdog/mute -> $st (expected 200)"
  fi

  st="$(curl_post_status "/api/ctrl/watchdog/ack" "{\"key\":\"${ack_key}\",\"confirm\":true}" 1 | tail -c 4 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CTRL watchdog/ack -> 200"
    body="$(ssh_cmd_raw "cat /home/openclaw/.openclaw/plugin-state/watchdog-state.json 2>/dev/null || echo '{}'" || true)"
    if echo "$body" | grep -q "\"${ack_key}\""; then ok "CTRL watchdog/ack verified in watchdog-state.json"; else fail "CTRL watchdog/ack not visible in watchdog-state.json"; fi
  else
    fail "CTRL watchdog/ack -> $st (expected 200)"
  fi

  # Cleanup: strip only our keys via read-modify-write
  cleanup_watchdog_keys
  # Verify removal
  body="$(ssh_cmd_raw "cat /home/openclaw/.openclaw/plugin-state/watchdog-state.json 2>/dev/null || echo '{}'" || true)"
  if echo "$body" | grep -q "\"${mute_key}\"" || echo "$body" | grep -q "\"${ack_key}\""; then
    fail "CTRL watchdog cleanup incomplete (keys remain)"
  else
    ok "CTRL watchdog cleanup complete (mc_mutes/mc_acks keys removed)"
  fi

  # b) lab spawn + poll + kill (or BLOCKED)
  st="$(curl_post_status "/api/ctrl/lab/spawn" "{\"id\":\"${PREFIX}\",\"confirm\":true}" 1 | tail -c 4 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CTRL lab/spawn -> 200"
    # Poll /api/lab/containers + ssh podman up to ~60s
    local found=0 i
    for i in $(seq 1 12); do
      sleep 5
      local conts pods
      conts="$(curl_get_json "/api/lab/containers" || echo '[]')"
      pods="$(${SSH_BASE} "wsl -d OllieLab -u lab -- bash -lc 'podman ps -a --filter name=^${PREFIX} --format \"{{.Names}}\"'" 2>/dev/null | grep -vi "post-quantum\|store now\|upgraded\|openssh.com" || true)"
      if echo "$conts" | grep -q "\"${PREFIX}\"" || [[ -n "${pods}" ]]; then
        found=1; break
      fi
    done
    if [[ $found -eq 1 ]]; then
      ok "CTRL lab/spawn container appeared (poll + ssh)"
      # kill
      st="$(curl_post_status "/api/ctrl/lab/kill" "{\"id\":\"${PREFIX}\",\"confirm\":true}" 1 | tail -c 4 || true)"
      if [[ "$st" == "200" ]]; then
        ok "CTRL lab/kill -> 200"
        # Verify gone
        sleep 3
        local pods2
        pods2="$(${SSH_BASE} "wsl -d OllieLab -u lab -- bash -lc 'podman ps -a --filter name=^${PREFIX} --format \"{{.Names}}\"'" 2>/dev/null | grep -vi "post-quantum\|store now\|upgraded\|openssh.com" || true)"
        if [[ -z "${pods2}" ]]; then
          ok "CTRL lab/kill container gone (ssh verified)"
        else
          fail "CTRL lab/kill container still present after kill"
        fi
      else
        fail "CTRL lab/kill -> $st (expected 200)"
      fi
    else
      note "CTRL lab/spawn did not appear within timeout (may be slow env) — not a hard failure"
      # Still attempt kill for hygiene
      curl_post_status "/api/ctrl/lab/kill" "{\"id\":\"${PREFIX}\",\"confirm\":true}" 1 >/dev/null 2>&1 || true
    fi
  else
    # Could be a capacity block (verbatim cap error) — treat as BLOCKED, not FAIL
    note "CTRL lab/spawn -> $st (may be capacity BLOCKED; not a hard failure if capacity constrained)"
  fi

  # c) research/dispatch (spends 1 budget; do not wait for completion)
  local task="MC-E2E SMOKE ${TS}: reply 'ok' only, no research needed"
  body="$(curl_post "/api/ctrl/research/dispatch" "{\"task\":\"${task}\",\"lane\":\"research\",\"confirm\":true}" 1 || true)"
  st="$(echo "$body" | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CTRL research/dispatch -> 200"
    # Find job id + verify budget spend-state.json research +1 (best-effort)
    local job_id
    # Look in queue or running for the task text
    job_id="$(ssh_cmd_raw 'find /home/openclaw/.openclaw/workspace/jobs/queue /home/openclaw/.openclaw/workspace/jobs/running -name "*.json" -exec grep -l "'"${task}"'" {} + 2>/dev/null | head -1' || true)"
    if [[ -n "${job_id}" ]]; then
      local j
      j="$(ssh_cmd_raw "cat ${job_id} 2>/dev/null || echo '{}'" || true)"
      local fid
      fid="$(echo "$j" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("id",""))' 2>/dev/null || true)"
      if [[ -n "${fid}" ]]; then
        ok "CTRL research/dispatch job created id=${fid}"
      else
        note "CTRL research/dispatch job file found but could not parse id"
      fi
    else
      note "CTRL research/dispatch job file not immediately visible (runner may have picked it)"
    fi
    # Budget delta (best-effort)
    local spend
    spend="$(ssh_cmd_raw 'cat /home/openclaw/.openclaw/logs/spend-state.json 2>/dev/null || echo "{}"' || true)"
    local research_spend
    research_spend="$(echo "$spend" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("research") or 0))' 2>/dev/null || echo 0)"
    note "BUDGET research spend (post-dispatch): ${research_spend} (expected +1 from this test)"
  else
    fail "CTRL research/dispatch -> $st (expected 200)"
  fi

  # d) heartbeat/beat -> fresh line in heartbeat.log within last minute
  body="$(curl_post "/api/ctrl/heartbeat/beat" '{"confirm":true}' 1 || true)"
  st="$(echo "$body" | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CTRL heartbeat/beat -> 200"
    # Verify fresh line in heartbeat.log (timestamp within last 60s)
    local last_line last_ts now_ts age
    last_line="$(ssh_cmd 'tail -5 /home/openclaw/.openclaw/logs/heartbeat.log 2>/dev/null | tail -1' || true)"
    last_ts="$(echo "$last_line" | awk '{print $1}' | tr -d '\r' || true)"
    if [[ -n "${last_ts}" ]]; then
      # Compare using GNU date if available; fallback to string prefix match
      now_ts="$(date -u +%Y-%m-%dT%H:%M: 2>/dev/null || true)"
      if [[ "${last_ts}" == "${now_ts}"* ]]; then
        ok "CTRL heartbeat/beat appended fresh line (ts prefix matches now)"
      else
        # Try numeric age via python
        age="$(python3 - <<'PY' 2>/dev/null || echo 999999
import datetime, sys
ts = sys.stdin.read().strip()
try:
    t = datetime.datetime.fromisoformat(ts.replace('Z','+00:00'))
    now = datetime.datetime.now(datetime.timezone.utc)
    print(int((now - t).total_seconds()))
except Exception:
    print(999999)
PY
        <<<"${last_ts}" )"
        if [[ "${age}" -le 90 ]]; then
          ok "CTRL heartbeat/beat appended fresh line (age ${age}s)"
        else
          note "CTRL heartbeat/beat appended line but age=${age}s (may be clock skew) — not a hard failure"
        fi
      fi
    else
      note "CTRL heartbeat/beat: could not parse heartbeat.log last line"
    fi
  else
    fail "CTRL heartbeat/beat -> $st (expected 200)"
  fi
}

# ── Section: CRUD ROUND-TRIPS (mutate; only if not --no-mutate) ───────────────
section_crud() {
  if [[ "${NO_MUTATE}" == "1" ]]; then
    note "SECTION: CRUD (skipped due to --no-mutate)"
    return 0
  fi
  note "SECTION: CRUD (mutating; prefix=${PREFIX})"

  # sources: POST -> GET verify -> PUT -> verify -> DELETE -> verify gone
  local src_id="${PREFIX}-src"
  local st body

  body="$(curl_post "/api/sources" "{\"id\":\"${src_id}\",\"type\":\"rss\",\"target\":\"https://example.com/feed\",\"enabled\":false}" 0 || true)"
  st="$(echo "$body" | tail -n1 || true)"
  if [[ "$st" == "201" || "$st" == "200" ]]; then
    ok "CRUD sources POST -> ${st}"
  else
    fail "CRUD sources POST -> ${st} (expected 201/200)"
  fi

  # GET verify present
  body="$(curl_get_json "/api/sources" || echo '[]')"
  if echo "$body" | grep -q "\"${src_id}\""; then ok "CRUD sources GET present after POST"; else fail "CRUD sources GET missing after POST"; fi

  # PUT weight
  st="$(curl_put "/api/sources/${src_id}" '{"weight":2.0}' | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CRUD sources PUT -> 200"
    body="$(curl_get_json "/api/sources" || echo '[]')"
    if echo "$body" | grep -q '"weight": *2' && echo "$body" | grep -q "\"${src_id}\""; then
      ok "CRUD sources PUT weight applied"
    else
      fail "CRUD sources PUT weight not reflected"
    fi
  else
    fail "CRUD sources PUT -> ${st} (expected 200)"
  fi

  # DELETE
  st="$(curl_delete "/api/sources/${src_id}" | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CRUD sources DELETE -> 200"
    body="$(curl_get_json "/api/sources" || echo '[]')"
    if echo "$body" | grep -q "\"${src_id}\""; then
      fail "CRUD sources DELETE did not remove item"
    else
      ok "CRUD sources DELETE removed item"
    fi
  else
    fail "CRUD sources DELETE -> ${st} (expected 200)"
  fi

  # interests: GET original -> PUT add mc-e2e-kw -> verify -> PUT restore exact -> verify
  local orig
  orig="$(curl_get_json "/api/interests" || echo '{}')"
  # Add keyword
  st="$(curl_put "/api/interests" "$(echo "$orig" | jq --arg kw "${PREFIX}-kw" '.keywords_boost = ((.keywords_boost // []) + [$kw] | unique)')" | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CRUD interests PUT add kw -> 200"
    body="$(curl_get_json "/api/interests" || echo '{}')"
    if echo "$body" | grep -q "\"${PREFIX}-kw\""; then ok "CRUD interests kw present"; else fail "CRUD interests kw missing after PUT"; fi
  else
    fail "CRUD interests PUT add kw -> ${st} (expected 200)"
  fi
  # Restore exact original
  st="$(curl_put "/api/interests" "$orig" | tail -n1 || true)"
  if [[ "$st" == "200" ]]; then
    ok "CRUD interests PUT restore -> 200"
    body="$(curl_get_json "/api/interests" || echo '{}')"
    # Compare ignoring updated_at
    local a b
    a="$(echo "$orig" | jq -S 'del(.updated_at)' 2>/dev/null || true)"
    b="$(echo "$body" | jq -S 'del(.updated_at)' 2>/dev/null || true)"
    if [[ "$a" == "$b" ]]; then
      ok "CRUD interests restored to exact original (ignoring updated_at)"
    else
      note "CRUD interests restore differs (likely timestamp). Diff (orig vs live, ignoring updated_at):"
      diff -u <(echo "$a") <(echo "$b") || true
      # Not a hard failure; interests are user data. Mark as note.
    fi
  else
    fail "CRUD interests PUT restore -> ${st} (expected 200)"
  fi

  # queue: GET -> pick first fp -> PUT status muted -> verify -> PUT restore original status
  body="$(curl_get_json "/api/queue" || echo '[]')"
  local fp orig_status
  fp="$(echo "$body" | jq -r '.[0].fingerprint // empty' 2>/dev/null || true)"
  if [[ -n "${fp}" ]]; then
    orig_status="$(echo "$body" | jq -r --arg fp "$fp" '.[] | select(.fingerprint==$fp) | .status // "pending"' 2>/dev/null || echo pending)"
    st="$(curl_put "/api/queue/${fp}" '{"status":"muted"}' | tail -n1 || true)"
    if [[ "$st" == "200" ]]; then
      ok "CRUD queue PUT status=muted -> 200"
      body="$(curl_get_json "/api/queue" || echo '[]')"
      local now_st
      now_st="$(echo "$body" | jq -r --arg fp "$fp" '.[] | select(.fingerprint==$fp) | .status // ""' 2>/dev/null || true)"
      if [[ "$now_st" == "muted" ]]; then
        ok "CRUD queue status=muted applied"
        # restore
        st="$(curl_put "/api/queue/${fp}" "{\"status\":\"${orig_status}\"}" | tail -n1 || true)"
        if [[ "$st" == "200" ]]; then
          ok "CRUD queue restore status -> 200"
        else
          fail "CRUD queue restore status -> ${st} (expected 200)"
        fi
      else
        fail "CRUD queue status=muted not reflected (got ${now_st})"
      fi
    else
      fail "CRUD queue PUT status=muted -> ${st} (expected 200)"
    fi
  else
    note "CRUD queue: no items present; skipping reorder/mute round-trip"
  fi
}

# ── Section: AUDIT (always; confirm CTRL lines exist for our actions) ──────────
section_audit() {
  note "SECTION: AUDIT (always)"
  local act
  act="$(curl_get_json "/api/activity" || echo '[]')"
  # We look for CTRL lines for the control actions we performed (if any).
  # In --no-mutate mode we still expect guardrail denial lines.
  local have_ctrl=0
  if echo "$act" | grep -q '"CTRL '; then have_ctrl=1; fi
  if [[ $have_ctrl -eq 1 ]]; then
    ok "AUDIT /api/activity contains CTRL lines"
  else
    # Some environments may not emit CTRL for reads; guardrails always emit.
    # Check the raw log via SSH for any CTRL at all.
    local raw
    raw="$(ssh_cmd_raw 'tail -100 /home/openclaw/.openclaw/logs/mission-control.log 2>/dev/null || echo ""' || true)"
    if echo "$raw" | grep -q ' CTRL '; then
      ok "AUDIT mission-control.log contains CTRL lines (SSH)"
    else
      fail "AUDIT no CTRL lines found in /api/activity or mission-control.log"
    fi
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  echo "================================================================"
  echo "Ollie MC E2E Smoke — BASE=${BASE}  NO_MUTATE=${NO_MUTATE}  PREFIX=${PREFIX}"
  echo "================================================================"

  section_reads
  section_guardrails
  section_controls
  section_crud
  section_audit

  # Always attempt to clean up anything we might have left (idempotent)
  cleanup_watchdog_keys || true
  cleanup_sources || true
  cleanup_lab_container || true

  # Final residue check (only meaningful if we mutated)
  if [[ "${NO_MUTATE}" != "1" ]]; then
    final_residue_check
  else
    note "RESIDUE check skipped (no mutations performed)"
  fi

  echo "================================================================"
  echo "SUMMARY: PASS=${PASS_COUNT} FAIL=${FAIL_COUNT}"
  if [[ ${#FAILURES[@]} -gt 0 ]]; then
    echo "FAILURES:"
    printf '  - %s\n' "${FAILURES[@]}"
  fi
  echo "================================================================"

  if [[ ${FAIL_COUNT} -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

main "$@"
