# Ollie's Lab — saves-driven R&D loop (holistic architecture)

## Context

Tushar saves heavily to 4DPocket (repos, AI tools, ideas, reels — mixed with
guitar/travel/personal). Today that's a write-only pile. Ollie holds a
read-only PAT for Tushar's personal account, granted for exactly this. The
product: Tushar's saves become Ollie's curiosity feed — idle-time triage,
research, POCs on repos, headline-level findings in the daily morning brief.

Decisions locked with Tushar:
1. **Reporting** — daily lab section in the morning brief, headline-only
   ("looked at X, tried it, ✅/❌"), drill-down on demand. No per-finding pings.
2. **Isolation** — untrusted code never runs where secrets live.
3. **Ephemerality (this revision)** — POCs must not share a dirty long-lived
   environment. Light base image → fresh per-POC instance → work → harvest →
   destroy (or save). Cattle, not pets.

## Architecture (the three-layer model)

```
Gateway distro (Tier-1 brain, secrets)        ── never runs untrusted code
   │  ssh lab@host:2222 (one-way key, portproxy bridge)
   ▼
OllieLab WSL distro (lab HOST — stable, zero secrets, no tailnet)
   │  rootless podman                          ── runs nothing untrusted directly;
   ▼                                              it only ORCHESTRATES containers
Per-POC container (EPHEMERAL — one per saved idea)
      spawn from lab-base image → experiment → harvest → destroy | save
```

Why containers-in-one-distro, not distro-per-POC: WSL import/unregister is
GB-slow, requires host `wsl.exe` (gateway has interop disabled — every spawn
would need the hands engine), and leaks host registry state. Rootless podman
gives sub-second spawn, cgroup caps (cpu/mem/pids), layer-cached installs,
`podman system prune` hygiene, and a kernel namespace wall INSIDE the already
secret-free distro. OllieLab itself stays near-pristine because untrusted
code only ever executes inside containers; reimaging it stays a 5-minute
break-glass option, not routine hygiene.

### Base image — `lab-base`
- `ollie-lab/base/Containerfile` in this repo (versioned, reviewed like code):
  ubuntu:24.04 + git, curl, ca-certs, jq, ripgrep, build-essential, python3 +
  uv, node LTS + npm. Target ≤1.5GB. NOTHING else — POCs install their own
  deps inside their container; layer cache keeps that fast, ephemerality keeps
  it clean.
- Rebuilt by `lab rebuild-base` (and a monthly systemd timer in OllieLab);
  tagged `lab-base:latest` + dated tag for rollback.

### Per-POC container lifecycle
states: `queued → running → harvested → destroyed | saved`
- **spawn**: `podman run -d --name poc-<itemid> --cpus 4 --memory 6g
  --pids-limit 512 --storage-opt size=10G lab-base sleep infinity`
- **work**: every command via `podman exec` (driven over SSH from gateway).
  Workdir `/work` inside the container.
- **harvest**: copy ONLY `/work/OUT/` (note draft, logs, screenshots) to
  OllieLab `/lab/artifacts/<itemid>/`; gateway pulls that over scp. Harvested
  files are DATA, never executed, treated as untrusted (prompt-injection rule).
- **destroy** (default): `podman rm -f` immediately after harvest.
- **save** (rare, on a 🌟 verdict): `podman commit poc-<id> saved/<slug>` then
  rm the container. Max 10 saved images, LRU-evicted; listed in the ledger.
- **Reapers** (mechanical, not LLM-dependent): TTL reaper kills any container
  older than 6h; nightly `podman system prune -f`; disk watermark (85%) blocks
  new spawns and flags the brief.

### The `lab` CLI — the only interface Ollie gets
Gateway-side `~/bin/lab` (bash, SSH transport), subcommands:
`lab spawn <itemid>` · `lab exec <itemid> "<cmd>" [--timeout 600]` ·
`lab harvest <itemid>` · `lab destroy <itemid>` · `lab save <itemid> <slug>` ·
`lab list` · `lab rebuild-base`
- Caps enforced IN THE SCRIPT (not in the prompt): max 1 concurrent POC
  container, per-exec timeout, output truncation (100KB), every invocation
  appended to `workspace/lab/audit.log` on the gateway.
- Ollie's doctrine says `lab` is the ONLY way to touch lab compute; the
  mechanical guarantee is that gateway's ssh key only reaches OllieLab.

### Security walls (defense in depth)
1. Gateway never executes save-derived code — only the `lab` CLI.
2. OllieLab: no secrets, no tailscale, no Windows mounts, interop off; SSH in
   on :2222 via host portproxy (firewall: WSL subnet only); key is one-way
   gateway→lab.
3. Container: rootless + namespaced + resource-capped; network ON (needs
   clone/install) — accepted risk on a dedicated box, revisit egress filtering
   if abuse appears.
4. Harvest path is file-copy only; content quarantined as untrusted data.
5. POC job prompt: never feed secrets/env into lab; a repo demanding
   credentials → mark ❌, note why, stop.

## Product lifecycle (end-to-end walkthrough)

1. **Capture** — Tushar saves a repo/reel/idea to 4DPocket (existing habit,
   zero new friction).
2. **Detect** — `lab_watcher.py` (hourly timer, mechanical) polls
   `GET /api/v1/items?sort_by=created_at&sort_order=desc&limit=30` (Host:
   `localhost:4040` quirk, Tushar's read PAT from
   `secrets/fourdpocket-tushar-read.pat`), diffs against `lab/state.json`,
   drops new saves into `lab/inbox/<id>.json`.
3. **Triage** — next heartbeat (LLM): each inbox item → lane:
   **poc** (runnable repo/tool) · **research** (idea/technique) · **index**
   (personal: guitar/travel/finance — remembered, recalled when timely) ·
   **skip** (noise). One ledger line each, inbox file deleted.
4. **Experiment** —
   - research lane: silent background job (≤2/day) → web research → 1-pager
     note in `lab/notes/`.
   - poc lane: queued; the 01:00–06:00 beats submit ≤1/night as a silent
     background job that drives `lab spawn/exec/harvest/destroy`, writes the
     lab note (what it claims / what I ran / outcome / verdict-for-onllm).
5. **Report** — morning brief (04:00–06:00 beat) carries the lab section:
   one line per ledger entry since last brief. Tushar drills down by asking;
   Ollie answers from the note.
6. **Feedback / taste** — Tushar's reactions get logged to the ledger
   (`feedback:` lines); triage instructions tell Ollie to weight lanes by
   accumulated feedback. (Mechanism v2: a TASTE.md distilled monthly by
   Dreaming.)
7. **Graduation** — 🌟 verdicts: Ollie proposes next step in the brief —
   issue/PR on the relevant onllm repo (self-PR doctrine already deployed) or
   `lab save` + a "we should build this" pitch. Tushar decides.
8. **Decay** — saved images LRU-capped; notes/ledger are the permanent record;
   everything compute is disposable.

## Failure modes & ops

| Failure | Behavior |
|---|---|
| 4DPocket down | watcher logs + skips; no inbox churn; heartbeat unaffected |
| Container runaway | cgroup caps + exec timeout + 6h TTL reaper |
| Disk fill on OllieLab | 85% watermark blocks spawns, surfaces in brief; nightly prune |
| OllieLab corrupted/compromised | break-glass reimage (RUNBOOK 3-liner: unregister → reinstall script → re-key); nothing of value lives there |
| Watcher/timer dies | existing watchdog pattern: add lab-watcher freshness to its checks (state.json mtime < 3h) |
| Malicious repo | can trash its container; walls 1-5 above bound everything else |
| LLM ignores budgets | budgets enforced mechanically in `lab` CLI + job-count check in watcher state, not just prompts |

## Build phases

**Phase 1 — the loop without POC compute** (no sandbox dependency)
1. `ollie-jobs/lab_watcher.py` + `ollie-lab-watcher.{service,timer}` (mirror
   heartbeat units); secret file `fourdpocket-tushar-read.pat` (600).
2. `--silent` flag: `ollie-jobs/job-submit.sh` + `ollie_jobs_runner.py`
   (`"deliver": false` → skip deliver()).
3. `ollie_heartbeat.py`: `lab_inbox_summary()` in the prompt (pattern:
   existing `jobs_summary()`).
4. `workspace/HEARTBEAT.md`: lab duties (triage lanes, ledger format, research
   job template, budgets, "lab never pings — findings ride the brief") +
   morning-brief lab section.
5. `workspace/AGENTS.md`: lab doctrine (paths, untrusted rule, personal-lane
   recall).
   POC lane in this phase: triage + `poc-queued` ledger lines only.

**Phase 2 — OllieLab host + container runtime + POC lane**
6. `scripts/setup-ollielab.ps1` (host, one-time): install Ubuntu-24.04 as
   OllieLab, wsl.conf hardening (no mounts/interop), user `lab`,
   openssh-server, rootless podman, portproxy+firewall scheduled task
   (ONLOGON, like `OllieHands`).
7. `ollie-lab/base/Containerfile` + first `lab-base` build.
8. Gateway `~/bin/lab` CLI (+ audit log) + ssh keypair; repo copy in
   `ollie-lab/lab` (deployed like other bin scripts).
9. HEARTBEAT.md/AGENTS.md: activate POC lane (1/night, lifecycle verbs,
   safety rules); RUNBOOK: bridge details + break-glass reimage.

## Files

| Action | Path |
|---|---|
| new | `ollie-jobs/lab_watcher.py`, `ollie-jobs/ollie-lab-watcher.{service,timer}` |
| edit | `ollie-jobs/ollie_heartbeat.py`, `ollie-jobs/ollie_jobs_runner.py`, `ollie-jobs/job-submit.sh` |
| edit | `workspace/HEARTBEAT.md`, `workspace/AGENTS.md` |
| new (P2) | `scripts/setup-ollielab.ps1`, `ollie-lab/base/Containerfile`, `ollie-lab/lab` (CLI), RUNBOOK section |
| box | secret `fourdpocket-tushar-read.pat`; `workspace/lab/{inbox,notes,artifacts}` + `LAB_LEDGER.md`, `state.json`, `audit.log` |

Reuse: `deliver()`/`sanitize()` (`ollie_jobs_runner.py`), heartbeat prompt assembly (`ollie_heartbeat.py`), 4DPocket REST quirks + PAT-file pattern (`reel_understand.py`), systemd unit + deploy-and-sha256 patterns, `OllieHands` scheduled-task pattern for the bridge.

## Verification

**Phase 1**
1. Watcher run → inbox JSONs for recent saves; second run → zero duplicates.
2. Manual heartbeat → ledger triage lines, inbox empty, ≤2 silent research
   jobs, NO Telegram pings.
3. Research job → note file + `jobs/done/` entry with `delivered: false`.
4. Next morning brief carries the lab section; Tushar confirms signal level.

**Phase 2**
5. `lab spawn test && lab exec test "uname -a"` → container responds; from
   inside OllieLab confirm no `~/.openclaw`, no tailnet route.
6. Spawn cap: second concurrent `lab spawn` refuses.
7. Seed a tiny known-good repo as a save → night POC → note + ✅ ledger +
   brief mention; confirm container destroyed after harvest (`lab list` empty).
8. Adversarial: seed a repo whose README demands env secrets → ❌ + reason,
   `grep -r` OllieLab fs for secret fragments → none.
9. TTL reaper: spawn, wait past TTL (short test value) → auto-killed.

**Rollback:** disable lab-watcher timer; HEARTBEAT/AGENTS lab sections are
additive; `wsl --unregister OllieLab` erases Phase 2 entirely.
