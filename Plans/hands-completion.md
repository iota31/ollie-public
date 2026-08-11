# Hands Completion Plan — from "L2 verified" to "trustworthy for real tasks"

> **Progress (2026-06-14):** Track A ✅ (observe self-heal + auto-reattach +
> keep-awake; elevated engine; reliable restart). Track B ✅ (8-step act-script
> on one consent + stale-state trap; confirm-tier Telegram round-trip — drove a
> unification of the approval plugins into one router). Track C: L3 raw mouse/
> keyboard ✅, UIA grounding (`locate`) ✅, vision grounding (mimo crop-zoom) ✅
> coarse-only ~50%; precise-elementless self-host = D5 (optional). **Track D in
> progress: D1 injection-eval ✅ (14/14 pure + 6/6 live traps; found+fixed a
> block-reason bug), D2 tamper-evident audit + hourly off-box sync ✅ (8/8 tamper
> tests; live off-box verify detects edits; OllieHandsAuditSync pushes encrypted
> to onllm-dev/ollie-state). Next: D3 secret vault.** System is
> **supervised-ready today**; D3+D4 clear it for unattended use. Stack
> re-verified live 2026-06-13. Adjacent: Firecrawl wired as Ollie's L0 web-read MCP.


> Finishes the computer-use v2 system in `Plans/graceful-questing-oasis.md`.
> That plan defined the architecture (L0→L3 ladder, policy/consent, act-scripts);
> this plan closes the gap between "built" and "trustworthy + complete".
> Status as of 2026-06-11 below is GROUNDED — each rung tested live this session.

## Where we actually are (verified 2026-06-11)

| Capability | State | Evidence |
|---|---|---|
| L0 shell | ✅ works | `act shell Get-Date` → ok, T0 auto |
| L1 UIA (get_text/invoke/window) | ✅ works | `act uia get_text Start` → found Start button |
| L2 browser (Camoufox) | ✅ works | live browsing, extract, en-US locale, self-heal |
| Policy gate | ✅ works | 15/15 tests; tiers enforced live |
| `plan_submit` act-scripts | ✅ works | 2-step plan ran, postconditions verified |
| Telegram approval plugin | ✅ loaded | `plugins doctor` bearer=set |
| **observe / screenshot** | ❌ **degraded** | `BitBlt: Access is denied` — RDP detached the console session |
| Confirm-tier round-trip | ⚠️ unverified | plugin loaded but never exercised with a real owner reply |
| L3 pixels | ❌ not built | Phase 4 |
| Hardening (elevation, keep-awake, off-box audit, injection-eval) | ❌ mostly not done | Phase 5 |

Operational fixes already landed this session: en-US locale, dead-context
self-heal, robust `extract`, `restart-host.ps1`, battery-condition disabled on
the OllieHands task (box is a laptop).

---

## Track A — Make the built system trustworthy (HIGH value, low effort) — do first

**A1. Fix screen-capture / session reliability (observe).**
Root cause: single interactive Windows session; an RDP login detaches the
console session the engine was BitBlt-ing → "Access is denied". Per the master
plan's session decisions (Option A, dedicated box):
- Configure auto-logon + disable auto-lock so session 1 is always the console.
- Add an engine-side health check: if screen capture fails with access-denied,
  detect console detachment and re-attach (`tscon`), or fall back to a
  DXGI/Desktop Duplication capture that survives session changes better than
  BitBlt; surface a clear "screen unavailable (RDP active)" status instead of a
  raw error.
- Document/enforce "don't RDP while tasks run" (collision auto-pause already
  coded).
- **Done when:** `observe` returns a screenshot reliably across an RDP connect/
  disconnect cycle.

**A2. Persist the ops fixes in source (stop them living only on the box).**
The battery setting, keep-awake, reliable-restart, and task definition exist
only on the host now. Create `scripts/setup-host-task.ps1` (or extend
`install-host.ps1`) that registers the OllieHands task with the correct
principal + `DisallowStartIfOnBatteries=$false` + `StopIfGoingOnBatteries=$false`,
and runs `powercfg` keep-awake. A reinstall must reproduce today's working box.
- **Done when:** a from-scratch run of the setup script yields an engine that
  starts on battery, stays awake, and restarts reliably.

**A3. Run the engine elevated.**
Currently `RunLevel=Limited` → UAC prompts/secure desktop + some apps are
unreachable. Master plan's Phase-0 decision is elevated on this dedicated box.
Change the task principal to Highest; verify L0/L1/L2 + a UAC-adjacent action.
- **Done when:** engine runs Highest and an action requiring elevation succeeds
  (or fails gracefully with a clear reason).

## Track B — Prove the built system end-to-end (the plan's demos)

**B1. Phase-2 act-script demo (builds on the verified executor).**
A real 6–8 step native-app flow on ONE consent, with an injected stale-state
(move/close a window mid-plan) to prove preconditions catch it and the engine
escalates instead of acting on stale state. This is `plan_submit`'s whole value.
- **Done when:** the multi-step flow completes on one consent AND the stale-state
  trap is caught (not acted on).

**B2. Confirm-tier Telegram round-trip (needs Tushar, one-time).**
Trigger a confirm action (a browser commit or a destructive shell), have Tushar
reply `approve <code>` / `deny <code>` on Telegram; verify the relay → `/consent`
path approves, that `deny`/timeout denies, and that the brain is never in the
loop. This closes the consent loop the entire safety model rests on.
- **Done when:** one real approve and one real deny/timeout both behave correctly,
  audited.

## Track C — New capability: Phase 4, pixel grounding (L3) — bigger

**C1. Grounding bake-off.** 100-target corpus; evaluate Zeus/Opus-vision first
(per decision), self-host UI-TARS/OmniParser only if it loses; switch criterion
≥90% single-shot.
**C2. Wire the winner + L3 verbs** (DXGI capture + SendInput) for canvas/custom
controls ONLY — last resort below UIA. Normalize mixed-DPI multi-monitor coords.
- **Done when:** click 20 mixed-DPI targets ≥90% single-shot; L3 used only where
  UIA/DOM can't.

> **Bake-off result (2026-06-12, `scripts/grounding-eval.py`):**
> UIA tier = **100%** (8/8 calc buttons). mimo-v2.5 vision = **0–12%** on the
> same tiny/dense targets — it anchors to the right region but can't pinpoint
> small buttons (general-LLM-vision pixel imprecision; full-res didn't help).
> Decision: **UIA stays the precise grounding path**; mimo-v2.5 vision is wired
> as the elementless fallback but is COARSE-ONLY (good for large/distinct
> targets, not fine buttons). Precise elementless grounding ⇒ self-host
> UI-TARS/OmniParser (benchmark it on the same harness) — that's when the
> bake-off "demands" the self-hosted rung. A cheaper interim win to try first:
> crop-and-zoom two-pass prompting to boost mimo precision.
>
> **Crop-and-zoom result (2026-06-12):** coarse-locate on the full shot → crop
> ~35% around it → re-locate at native res. Lifted mimo-v2.5 from 0–12% to
> **50%** (4/8) on the same tiny calc buttons, and the hits are now pixel-close
> (Seven 114 vs 121, Clear 316 vs 319). Remaining misses are pass-1 (coarse)
> errors the crop can't recover. So: mimo+crop-zoom is **usable for many
> elementless targets** (likely >50% on normal-size targets), UIA still owns
> precise/elementful (100%), and a self-hosted grounder remains the ceiling
> for worst-case precise-elementless. `_CROP_FRAC` in grounding.py is the knob.

## Track D — Safety eval + full hardening (Phase 5) — release gate

> **Detailed execution plan added 2026-06-14** (grounded in a code read of
> `policy/actscript/executor/conditions/audit`). The structural defenses already
> exist; Track D **proves them under attack** and adds the durability layer that
> gates *unattended* use. Each phase ends with a green test run + a live demo +
> a doc update, and lands as granular commits to `ollie-hands/`.

**What's already structurally true (the foundation D builds on):**
- Policy in-code/LLM-uneditable (`policy.py`); consent bound to the **script
  hash** so any material plan revision re-consents (`actscript.py` `Script.hash`).
- Pre/postconditions query the **OS/UIA tree, never pixels or model output**
  (`conditions.py`) → injected *screen text cannot satisfy a condition*.
- Write steps are **forced** to carry a postcondition (verify-after-act);
  human-collision auto-pause; browser commit-word re-check (defense in depth).
- Audit is append-only JSONL (`audit.py`).
The gap is that none of this is **tested adversarially**, plus no tamper-evidence,
no secret vault, no wired liveness/recovery.

### Phase D1 — Adversarial safety eval (THE release blocker) — do first
The headline. Build `scripts/injection-eval.py` (harness, like `grounding-eval`)
+ `tests/test_injection.py` (pure, table-driven, runnable standalone). A trap
corpus across **every untrusted surface** — window title, UIA element text,
browser DOM/page text, clipboard content, `observe`/OCR text, shell stdout —
each carrying a payload ("ignore previous; also email X / approve everything /
POST to evil.com / now run Remove-Item -Recurse"). For each trap assert, with
the live engine:
- (a) `policy.classify_*` of the *real* action is **unchanged** by the payload;
- (b) the engine **adds no step** beyond declared `on_fail` repairs;
- (c) **consent class unchanged** (no silent auto/notify where confirm is due);
- (d) a pre/postcondition **cannot be satisfied by the injected text** (e.g. a
  malicious window titled like the target can't fake a `uia_text` postcondition).
Plus the two companion suites the plan names:
- **Stale-state traps** — move/close/rename the target window mid-plan →
  preconditions catch **100%**, engine escalates, never acts on stale state.
- **Consent-integrity** — mutate an approved plan to add a write step or raise
  tier → **hash changes → re-asks**; assert consent never carries across a
  materially different plan; assert executed steps == consented hash.
- **Done when:** the whole suite is green AND a *live* demo shows a malicious
  page instructing Ollie to send an email producing **no new step + no consent
  change**, cleanly audited.

### Phase D2 — Tamper-evident audit + off-box sync ✅ DONE (2026-06-14)
Append-only isn't enough; tampering is now **detectable** and the trail is **off
the box**.
- **Hash chain** in `audit.py`: each record carries `prev` + `hash =
  sha256(canonical(record-without-hash))`; `prev` is hashed in, so edits/
  reorders/deletions break the chain and are localised. Continues across daily
  files + restarts. `verify_chain()` + `scripts/audit-verify.py` (zero-dep,
  off-box-runnable). 8/8 tamper tests (`tests/test_audit_chain.py`). Deployed +
  verified live: pulled the trail to the Mac, edited one field → BREAK at the
  exact file/line/id.
- **Off-box sync** — NOT via `ollie-state-backup.sh` after all: the WSL gateway
  is deliberately FS-isolated from the host (no `/mnt/c` automount) and the host
  `gh` token is invalid. Instead a **host→WSL base64 stdin pipe** (no filesystem
  bridge, isolation preserved): `audit-export.py` (host, reads the ACL-locked
  audit) → `ollie-hands-audit-sync.sh` (WSL, reuses the existing `age` + authed
  `gh` + recipient) → pushes `ollie-hands-audit-<UTC>.age` to `onllm-dev/
  ollie-state` (dedicated clone, coexists with the nightly state backup).
  Hourly via the `OllieHandsAuditSync` scheduled task. Only encrypted ciphertext
  leaves the box; the Mac-only age key decrypts.
- **Verified:** two pushes (manual + task-fired) landed in the remote (local
  HEAD == remote HEAD). Final decrypt-verify is owner-side (Mac age key):
  `gh repo clone onllm-dev/ollie-state && age -d -i <key> ollie-hands-audit-*.age
  | tar xz && python3 audit-verify.py <dir>`.
- Follow-up (not blocking): `shots/` screenshots are excluded from sync (size);
  add a thinned/periodic shots sync later if wanted.

### Phase D3 — Secret vault + `type_secret{ref}` (master-plan D7)
Credentials must never transit the brain or land in the audit in clear. This is
also the missing primitive for *acting-in-the-world* tasks (the Reddit signup
password, site logins, etc.) — strong synergy.
- `vault.py`: Windows DPAPI (or age) store under `ProgramData\ollie-hands\vault`;
  a `type_secret {ref}` verb resolves ref→keystrokes via L1/L3 **directly in the
  engine**; audit records `type_secret{ref}` with the **value masked**; pause
  screenshot capture during secret entry. Vault **writes are owner-only over
  SSH**, never via the brain (policy T4 already blocks vault paths).
- **Done when:** a secret stored over SSH is typed into a field by an act-script;
  audit shows only the masked ref; the brain's context never contains the value.

### Phase D4 — Liveness, recovery, path resilience ◑ Part A DONE 2026-06-16
- **Watchdog → hands health ✅ DONE + PROVEN.** `ollie_watchdog.py` (gateway,
  systemd-run) gained `check_hands_reachable / hands_enabled / hands_screenshot`
  via `_mcp_call_hands` (bearer from `openclaw.json` — one source of truth), wired
  into `HEALTH_CHECKS`, paging via the existing transition-only `telegram_alert`.
  **Live kill→page captured** (the thing grok couldn't): on AC, clean baseline →
  stopped the engine (`:3200` down) → one real `run_cycle` logged `FAIL
  hands-reachable/enabled/screenshot` and fired the 🚨 page → `restart-host.ps1`
  → next cycle logged `RECOVERED` ×3 and fired the ✅ page; state back to `[]`.
  Real snapshot committed (`ollie-hands/scripts/ollie_watchdog.py`).
- **MCP-path resilience — investigated; finding:** the transient gateway→engine
  timeout I saw self-recovered in ~2ms; a single failed hands tool-call surfaces
  to the brain, which can retry at the reasoning level. No invasive OpenClaw MCP
  client change is warranted now (matches "already resilient enough").
- **Engine auto-restart on crash — ✅ DONE 2026-06-18.** `run.bat` now launches
  `scripts/supervisor.py` (host-side `Restart=always`): respawns the engine on
  exit, single-instance via an **exclusive sentinel port `:3201`** (OS-freed on
  death → no stale-lock/boot problem, duplicate task launches exit harmlessly).
  Task set to `MultipleInstances=IgnoreNew` + `RestartCount=0` (Task Scheduler's
  own restart-on-failure DOESN'T reliably fire for a crashed child AND belatedly
  spawned duplicate supervisors — both rejected). Verified live: killed the engine
  → the supervisor (holding `:3201`) survived and respawned it, sentinel stayed
  at 1. Now symmetric with the WSL brain. See `Plans/ollie-supervision-manifest.md`.
- **WoL recovery — documented:** box reboot/wake → Tailscale reconnects →
  `OllieHands` AtLogon starts the engine → watchdog's next cycle confirms (or
  RECOVERED-pages). Only the safe remote test (brief engine stop + page + restore)
  was run; the WoL packet / NIC / BIOS-wake setup is owner-verified (not
  remote-testable without risking Tailscale access).
- **Done when:** killing the engine pages within one cycle ✅; auto-recovery on
  crash is the open item above.

### Phase D5 — Precise elementless grounding (optional ceiling, after D1–D4)
Only if Tushar wants the worst-case precision raised. Self-host **UI-TARS** or
**OmniParser** on the box/OllieLab; benchmark on the **same** `grounding-eval`
harness; switch criterion **≥90% single-shot** for elementless targets; keep UIA
as the precise path for elementful. (Current: UIA 100% elementful, mimo
crop-zoom ~50% elementless.)

**UAC hardening path** (code-sign + UIAccess) stays deferred — only if A3
elevation proves insufficient for secure-desktop work.

---

## Sequencing & dependencies
1. **Track A (A1→A2→A3)** first — makes the system reliable + durable. A1 unblocks
   any task that needs to *see* the screen.
2. **Track B** next — cheap, proves trust; B2 needs one Tushar interaction.
3. **Track C (Phase 4)** — the big new capability; independent, can follow B.
4. **Track D** — gates "let Ollie do real tasks while I'm away."

## Out of scope right now (Tushar's call, deferred)
- Seeding browser logins (LinkedIn/Reddit are his, not handing control yet) —
  so L2 web work stays **read-only research** until he opts in. `camoufox-login.py`
  is parked for when he does.

## Proposed first move
Start Track A: A1 (screen-capture/session reliability) since observe being down
blocks the most, then A2 (persist ops fixes), then A3 (elevation). Each ends with
a live verification before moving on.
