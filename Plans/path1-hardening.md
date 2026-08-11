# Path-1 Hardening — make Ollie's live code immutable to itself

**Status:** spec / not started (2026-06-20)
**Goal:** The agent (`openclaw` user in WSL `OpenClawGateway`) must not be able to
*persist* changes to its own code, config, gates, or service definitions. Every
self-change must flow through the PR loop → owner merge → owner (root) deploy.

This is the "path 1" lockdown. Background: Ollie has three ways to change itself —
(1) direct hot-edit of live box code [ungated], (2) the gated `hands` engine,
(3) the GitHub self-PR loop [owner merges]. Hardening path 3 (e.g. routing it
through lab) is pointless while path 1 is open, because the agent just takes the
easier ungated path. So **close path 1 first.**

---

## Scope

- **In scope:** integrity/persistence of trusted code paths on the box (WSL side).
- **Out of scope:** the agent keeps `exec security:full`, so it can still run
  arbitrary code *as openclaw in the moment*. We are protecting persistence, not
  execution. The PR loop is the only way a self-change survives a restart.
- Host-side `ollie-hands` (`C:\ollie-hands\`) is a separate boundary, not covered here.

---

## Why naive `chmod 444` fails (the three bypasses)

1. **User systemd units.** All services are `systemctl --user` units in
   `~/.config/systemd/user/`, owned and editable by openclaw. Even if
   `/home/openclaw/bin/*.py` is locked, the agent rewrites `ExecStart=` to point
   at code it controls, then `daemon-reload && restart`.
2. **Parent-directory rename.** Deleting/renaming a file depends on *parent dir*
   write permission, not file ownership. A root-owned file inside an
   openclaw-owned dir can be `rm`'d or the parent `mv`'d and recreated. So locking
   files while `/home/openclaw` and `~/.openclaw` stay openclaw-owned is defeated.
3. **PATH / toolchain hijack.** Every unit sets
   `PATH=/home/openclaw/.openclaw/tools/node-v22.22.0/bin:/home/openclaw/.local/bin:/home/openclaw/bin:...`
   — all three dirs are agent-writable, and the Node toolchain lives there. Any
   subprocess spawned by bare name (`node`, `curl`, `python3`, `git`, `uv`) can be
   shadowed by a planted binary earlier in PATH.
4. **The OpenClaw PLATFORM itself is in the agent's home.** Per Ollie's own
   self-report, the gateway runtime is installed at
   `~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/` (src/, dist/, …).
   It calls this a "read-only install," but that path is inside `/home/openclaw`
   and is almost certainly openclaw-owned → the agent can `chmod +w` and rewrite
   the gateway code, **including whatever enforces the exec gate.** If true, this
   makes every other lock moot (the agent edits the enforcer). MUST verify
   ownership on box (see P4) and bring this tree into the locked set.

The design below closes all four.

---

## Audit findings (write-safety) — verified against repo source

**Safe to lock root:root 0444 (no runtime writer found):**
- `~/.openclaw/exec-approvals.json`  ← the gate file; highest value
- `~/.openclaw/openclaw.json`        ← config = plugin/provider/gate surface
- `~/.openclaw/workspace/{AGENTS,HEARTBEAT,OPEN_LOOPS,PROJECT_DOCTRINE,SOUL,IDENTITY,USER}.md`
- `~/ollie-research/.venv/` (site-packages) — only written by `setup-venv.sh` at deploy time

**Must stay writable (runtime state, openclaw:openclaw):**
- `~/.openclaw/workspace/{jobs,heartbeat,lab,projects}/`, `~/.openclaw/logs/`,
  `~/.openclaw/plugin-state/`, `~/.openclaw/agents/`, `~/.openclaw/credentials/`,
  `~/.openclaw/secrets/whatsapp-contacts.json`, `MEMORY.md`, `LAST_BRIEF.md`,
  `~/.openclaw/state-backup-repo/`

**Special case — plugins:** `~/.openclaw/plugins/ollie-whatsapp-cloud/` holds BOTH
code (`index.js`) AND runtime state (`dedupe.json`, `epoch.json`, written by the
plugin at runtime — `openclaw-ollie-whatsapp-cloud/index.js` mkdir+writeFile+rename).
So the plugin dir cannot be locked until that state is relocated (see Step 5).

**Confirmed non-issues:**
- No `EnvironmentFile=` anywhere — all env is inline `Environment=` (no env-file injection vector).
- No runtime `systemctl`/`daemon-reload` in agent code — only `deploy-wa-plugin.sh`
  (owner-run). So **the agent needs no restart authority**; `Restart=always` self-heals.
- No `crontab`/`at`/runtime-timer creation in any code.

---

## Two prerequisites to verify ON THE BOX (blocking — model collapses if wrong)

```bash
# P1. openclaw must have ZERO sudo. Must print nothing usable:
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> \
  'wsl -d OpenClawGateway -u root -- bash -lc "sudo -l -U openclaw; grep -rn openclaw /etc/sudoers /etc/sudoers.d/ 2>/dev/null"'

# P2. Can WSL run system-scope systemd (so we can use root-owned system units)?
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> \
  'wsl -d OpenClawGateway -u root -- bash -lc "cat /etc/wsl.conf; systemctl is-system-running; pidof systemd"'
#  - If systemd is PID1 (systemd=true in wsl.conf) -> use system units (preferred).
#  - If NOT -> fall back to root-owned ~/.config/systemd/user skeleton (see Step 3 alt).

# P3 (informational). Confirm the CWD + actual deploy path of bin/*.py:
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> \
  'wsl -d OpenClawGateway -u openclaw -- bash -lc "ls -la /home/openclaw/bin | head; systemctl --user show ollie-jobs -p WorkingDirectory"'

# P4. Is the OpenClaw PLATFORM install actually immutable to openclaw, or just
# "read-only" in Ollie's perception? Check ownership/writability of the tree:
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> \
  'wsl -d OpenClawGateway -u openclaw -- bash -lc "ls -ld ~/.openclaw/tools ~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw; touch ~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/.wtest && echo WRITABLE && rm -f ~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/.wtest || echo readonly"'
#  - "WRITABLE" => the agent can rewrite the gateway runtime + exec gate; HIGH priority.
```

If P1 shows any `NOPASSWD` for openclaw — **stop**, fix that first; perms are theater otherwise.

---

## Box verification results (2026-06-20)

- **P1 — sudo:** `openclaw is not allowed to run sudo`; empty sudoers. ✅ No escalation
  → ownership/immutable locks are enforceable; agent cannot revert root-set state.
- **P2 — systemd:** `wsl.conf systemd=true`, PID1=systemd, state `degraded`
  (one+ failed unit — cause TBD, user-bus unreachable from probe). ✅ System units usable.
- **P3 — bin:** `/home/openclaw/bin/*` all `openclaw:openclaw`; `node`/`npm` symlink
  into the in-home toolchain. Code fully agent-mutable.
- **P4 — platform:** 🔴 `~/.openclaw/tools/.../node_modules/openclaw` is
  `openclaw:openclaw` and **WRITABLE** by the agent. The "read-only install" is a
  perception, not a control — the agent can rewrite the gateway + exec-gate enforcer.
- **Skeleton:** `~/.openclaw` is `drwxrwxr-x` (group-writable!), `/home/openclaw` 0750,
  all openclaw-owned → no `chmod`-only lock survives (parent-dir delete/rename).
- **chattr:** ✅ `chattr +i` supported on this ext4; verified agent (no root) **cannot**
  remove an immutable file. No writes inside `node_modules/openclaw` in last 3 days.

### Revised approach — `chattr +i` first (immutable bit beats chmod here)
Because the agent has no sudo (P1) but owns its home dirs, `chmod 444` is defeated by
parent-dir rename/delete. `chattr +i` (set by root) is **not** — it blocks
modify/delete/rename regardless of directory ownership, and the agent can't clear it.
This gives an immediate, surgical, reversible lock without the risky Step-4 ownership flip.

- **Tier 1 (safe now — verified zero runtime writers):** `chattr +i` on
  `exec-approvals.json`, the 7 doctrine `.md`s. (Locking just means future owner edits
  go: root `chattr -i` → edit → `chattr +i`.)
- **Tier 2 (verify-first):** `openclaw.json` — confirm the gateway doesn't rewrite it on
  startup/migration before `+i`. The platform tree + node toolchain — `chattr +i -R`
  after a longer no-internal-writes confirmation (3-day window was clean).
- **Tier 3 (thorough, later):** the full `/opt/ollie` relocation + system units below,
  for defense-in-depth and to also cover `bin/*.py` + plugins.

## Design

**Principle:** root owns the directory *skeleton*; openclaw is granted write only on
specific *leaf* state paths. Code/config/gates/units/toolchain are root-owned and
immutable to the agent (which has no sudo).

### Step 1 — Relocate immutable code + toolchain to root-owned `/opt/ollie`
```
/opt/ollie/bin/*.py            0755 root:root   (jobs, heartbeat, dream-promoter,
                                                  lab_watcher, project_tick, watchdog,
                                                  research_*, dashboard, lab)
/opt/ollie/research/.venv/     root:root, r-x   (no site-packages writes)
/opt/ollie/node/               root:root        (relocated node-v22.22.0 toolchain)
/opt/ollie/plugins/<id>/       root:root        (if OpenClaw supports relocated base;
                                                  else lock in place per Step 5)

# OpenClaw PLATFORM tree (the gateway runtime + exec-gate enforcer) — bring into
# the locked set. Simplest: keep in place but chown to root so it can't be edited,
# AND parent must be root-owned (Step 4) so it can't be renamed:
#   chown -R root:root ~/.openclaw/tools          # node toolchain + node_modules/openclaw
#   chmod -R a-w       ~/.openclaw/tools
# (Verify via P4 first — if already root-owned, this is a no-op.)
/opt/ollie/env/<unit>.env      root:root 0640   (optional, if we move secrets out of units)
```
`/opt` and `/opt/ollie` owned by root → agent cannot rename/replace subtrees (closes bypass #2 for code).

### Step 2 — Fix PATH (closes bypass #3)
In every unit, replace the agent-writable PATH with root-owned dirs only:
```
Environment=PATH=/opt/ollie/node/bin:/opt/ollie/bin:/usr/local/bin:/usr/bin:/bin
```
Drop `/home/openclaw/.local/bin` and `/home/openclaw/bin` and the in-home node path.
(If something genuinely needs `~/.local/bin`, relocate that tool to `/opt/ollie/bin`.)

### Step 3 — Convert user units → system units (closes bypass #1)
Move units to `/etc/systemd/system/` (root-owned), run as the openclaw user:
```ini
[Service]
User=openclaw
Group=openclaw
ExecStart=/usr/bin/python3 /opt/ollie/bin/ollie_jobs_runner.py
WorkingDirectory=/opt/ollie          # locked dir, no CWD module-shadowing
Restart=always
RestartSec=10
Environment=HOME=/home/openclaw
Environment=PATH=/opt/ollie/node/bin:/opt/ollie/bin:/usr/local/bin:/usr/bin:/bin
Environment=BRAVE_API_KEY=...        # or EnvironmentFile=/opt/ollie/env/jobs.env (root:root)
[Install]
WantedBy=multi-user.target           # was default.target (user scope)
```
Timers (`*.timer`) move likewise; oneshots keep `Type=oneshot`. Agent can no longer
edit `ExecStart`. No agent restart authority is granted (none is needed).

**Alt (if P2 fails / no system systemd in WSL):** keep user units BUT make
`~/.config/systemd/user/` a root-owned skeleton (root:root dir, unit files
root:root 0444, only the `*.wants` symlink dirs left writable if systemd needs them).
Less clean — prefer system units.

### Step 4 — Root-own the home skeleton, carve writable leaves
```bash
# directories root-owned so the agent can't rename/replace them:
chown root:root /home/openclaw /home/openclaw/.openclaw /home/openclaw/.openclaw/plugins
chmod 0755      /home/openclaw /home/openclaw/.openclaw /home/openclaw/.openclaw/plugins
# carve back ONLY the writable leaves to openclaw:
for d in workspace/jobs workspace/heartbeat workspace/lab workspace/projects \
         logs plugin-state agents credentials state-backup-repo; do
  chown -R openclaw:openclaw "/home/openclaw/.openclaw/$d"
done
chown openclaw:openclaw /home/openclaw/.openclaw/secrets/whatsapp-contacts.json
chown openclaw:openclaw /home/openclaw/.openclaw/workspace/MEMORY.md \
                        /home/openclaw/.openclaw/workspace/LAST_BRIEF.md
# NOTE: ~/.openclaw being root-owned means the agent cannot create NEW top-level
# entries under it (good) — confirm the gateway doesn't need to at runtime.
```

### Step 5 — Lock the safe set (the cheap, high-value win)
```bash
cd /home/openclaw/.openclaw
chown root:root exec-approvals.json openclaw.json \
   workspace/AGENTS.md workspace/HEARTBEAT.md workspace/OPEN_LOOPS.md \
   workspace/PROJECT_DOCTRINE.md workspace/SOUL.md workspace/IDENTITY.md workspace/USER.md
chmod 0444 exec-approvals.json openclaw.json workspace/AGENTS.md workspace/HEARTBEAT.md \
   workspace/OPEN_LOOPS.md workspace/PROJECT_DOCTRINE.md workspace/SOUL.md \
   workspace/IDENTITY.md workspace/USER.md
chown -R root:root /opt/ollie/research/.venv && chmod -R a-w /opt/ollie/research/.venv
```

### Step 6 — Plugins: relocate runtime state, then lock code
The WA plugin writes `dedupe.json`/`epoch.json` inside its own code dir. To lock
the code dir, first relocate that state (small code change to
`openclaw-ollie-whatsapp-cloud/index.js`, shipped via the PR loop):
```
state path:  ~/.openclaw/plugins/ollie-whatsapp-cloud/{dedupe,epoch}.json
       ->    ~/.openclaw/plugin-state/whatsapp-cloud/{dedupe,epoch}.json   (writable leaf)
```
Then `chown root:root` the plugin code dirs and `chmod 0444 index.js` (dir 0755 root).
Audit other plugins (`ollie-wa-approval`, `ollie-hands-approval`) for in-dir writes first.

### Step 7 — Deploys run as root (not openclaw)
`scripts/deploy-wa-plugin.sh` changes:
- `WSL_BASH` / every `wsl ... -u openclaw` → `-u root`
- target paths → `/opt/ollie/...` where code moved
- restart line: `systemctl --user restart openclaw-gateway.service`
  → `systemctl restart openclaw-gateway.service` (drop `XDG_RUNTIME_DIR=/run/user/1000`)
  (only if the gateway itself becomes a system unit; if gateway stays user-scope,
   keep `--user` for the gateway restart but write code as root.)
- backups (`$REMOTE.bak-$TS`) now land in a root-owned dir → fine, deploy is root.

A matching change is needed for the (currently untracked) `bin/*.py` deploy
mechanism — make it `rsync`/`scp` as root into `/opt/ollie/bin`. Codify it as a
script under `scripts/` so it's reviewable.

---

## Migration order (low-risk first)

1. **Verify** P1 (no sudo) and P2 (system systemd). Blocking.
2. **Step 5 only** — lock `exec-approvals.json`, `openclaw.json`, doctrine `.md`s,
   venv. Huge risk reduction, near-zero breakage, trivially reversible. Watch the
   watchdog + one heartbeat cycle.
3. **Steps 1–3** — relocate code+node to `/opt/ollie`, fix PATH, convert to system
   units. Restart everything as root, run a full heartbeat + research cycle, check watchdog.
4. **Step 4** — root-own the home skeleton, carve writable leaves.
5. **Step 6** — plugin state relocation (PR) + lock plugin code.
6. **Step 7** — switch deploy scripts to root; re-test a WA plugin deploy end-to-end.

---

## Rollback

Each step is reversible by reverting ownership/perms:
```bash
chmod 0644 <file> && chown openclaw:openclaw <file>      # unlock a file
# units: stop+disable system unit, restore the ~/.config/systemd/user copy, daemon-reload
```
Keep the pre-change `~/.config/systemd/user/*.service` copies until Step 3 is proven.

---

## Residual risks (note; not in path-1 scope)

- Agent retains full in-the-moment execution as openclaw (by design).
- Agent could still create *new* persistence it controls (it writes a script and
  runs it) — but that's new code, not corruption of trusted paths; root-owning
  `~/.config` + ensuring no writable cron closes most of it.
- `~/.local/lib/pythonX/site-packages` (user site) could shadow imports if on
  sys.path — confirm system python doesn't add user-site for these services
  (`PYTHONNOUSERSITE=1` in units is a cheap belt-and-suspenders).
- Secrets remain readable by the agent (separate hardening track).

---

## Open decisions (for owner)

1. **System units vs user-unit skeleton** — depends on P2. Prefer system units.
2. **Plugin state relocation** requires a small WA-plugin code change (a PR). OK?
3. **`/opt/ollie` relocation** touches every unit + the bin deploy path. Do all at
   once, or land Step 5 (locks) first and schedule the relocation separately?
4. **Who runs the on-box steps** — owner-driven as root, or authorize the assistant
   to SSH and execute? (These are destructive-ish, shared-system changes.)
