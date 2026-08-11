# Computer-Use v2 — "Full Hands" (Final Plan)

> Authoritative plan. Consolidates and **supersedes** the working draft
> `Plans/computer-use-v2.md` (kept for long-form rationale). Where this file
> and the draft disagree, this file wins — notably the **browser rung**,
> which now adopts OpenCLI.

Status: FINAL for approval — 2026-06-10

---

## Context

Ollie's "hands" today are a 4-day-old POC: `domdomegg/computer-use-mcp`
v1.8.0, **Notepad-only**, **soft prompt gating**, **per-action Telegram
confirm**, **un-benchmarked pixel grounding**, ~14 chars/sec, black screens
when locked. It proves the wiring but is structurally unusable for real work
(N steps × 60-75s brain turns × a confirm each).

The goal: **Ollie controls the Windows box the way a human would** — any app,
any window, any flow — *reliably* enough to trust with real tasks and
*safely* enough that a hijacked brain can't burn the house down. That needs a
redesign of the trust + execution model, not a loosened prompt.

Two decisions taken this session:
- **Browser = OpenCLI, adopted fully** (`jackwener/OpenCLI`): drive the real
  logged-in Chrome via its extension+daemon bridge, DOM snapshots not pixels,
  plus its 100+ deterministic site adapters and `adapter-author` skill.
  Resolves the Playwright flakiness objection.
- **Browser identity = dedicated "Ollie" Chrome profile** (not the daily
  profile): blast radius limited to accounts Tushar explicitly logs in.

---

## The capability ladder (core principle)

"Like a human" ≠ "simulate a human's mouse." A human uses the mouse only
because they lack a better interface; software has better ones. Every task
routes down this ladder, top-first. Pixels are the LAST resort.

```
L0  Shell / CLI / API          deterministic, instant, headless     ~60-70% of tasks
L1  UIA (UI Automation tree)   find-by-name, invoke, set-value      native-app actions
L2  Browser = OpenCLI          real logged-in Chrome, DOM snapshots  all web work
    └ L2a deterministic adapters (linkedin/twitter/reddit/amazon/upwork/…) — preferred
    └ L2b generic primitives (open/click/fill/extract/wait/network)  — when no adapter
L3  Pixels (screenshot→coords→SendInput)  canvas/custom controls only — last resort
```

- **L0** already part-exists via the host node's `system.run`; v2 moves host
  shell *into the engine* so one component owns policy + audit. OpenCLI's
  CLI-hub (`gh`/`docker`/`notion`/`tg`/…) also lives here.
- **L2 (OpenCLI)** removes the vision model from the web entirely. Web tasks
  that have an adapter become L0-grade deterministic commands; Ollie authors
  new adapters for Tushar's sites over time (the "learned playbooks" idea,
  productized).
- **L3** is the only rung needing a vision model → minimizing it minimizes
  cost, latency, and failure.

The **planner (brain)** chooses the rung (it has task context the engine
doesn't); the **engine** exposes all four with uniform policy + audit.

---

## Core design decisions

- **D1 — Capability ladder** (above): pixels last.
- **D2 — Planner/executor split + act-scripts.** Brain emits a JSON plan
  (steps with selector, args, pre/postcondition, on_fail); the engine runs it
  *locally at machine speed*, no model call per deterministic step. Escalates
  to the brain only on failed postcondition, ambiguous grounding, policy
  boundary, or explicit checkpoint. Kills the per-step round-trip problem.
- **D3 — Task-level consent, not per-click.** Brain produces plan + plain-
  English preview; owner approves ONCE per task (bound to the script hash;
  material revisions re-consent). Consent classes set by policy, not the LLM:
  `auto` (read/own-scope) · `notify` (proceed + narrate) · `confirm` (wait for
  yes: acts-as-Tushar, money, sends, deletes, anything leaving the box).
  Abort stays per-action-instant (§ kill switches).
- **D4 — Session ownership.** **Option A, permanently** (console-primary;
  the box is a dedicated spare — Ollie's body; auto-lock disabled +
  auto-logon; human-collision auto-pause kept as cheap insurance for the
  rare manual visit). *Resolved 2026-06-10: no VM — the box has no human
  user to isolate from.*
- **D5 — Grounding: UIA-first; pixel model by bake-off.** UIA answers "where
  is the Save button" deterministically/free for ~90% of native UI. Pixel
  (L3) grounding picked by a Phase-4 bake-off (Zeus/Opus-vision vs MiniMax M3
  vs UI-TARS vs OmniParser-detector) on a 100-target corpus; switch criterion
  ≥90% single-shot.
- **D6 — One engine owns ALL host actions.** Standalone Python MCP server
  (per D-20260605-03), bearer auth (desktop-proxy pattern). Retire agent use
  of node `system.run` once engine L0 lands — no two policy regimes.
- **D7 — Secrets never transit the brain.** Engine-side DPAPI/Credential-
  Manager vault; act-scripts reference `type_secret{ref}`; engine resolves
  ref→keystrokes directly; masks password regions in audit; pauses video
  during secret entry. Vault writes are owner-only over SSH, never via agent.
- **D8 — Everything on screen/page is untrusted input.** Screen/DOM/OCR
  content is DATA, never instructions — enforced structurally: no observed
  content can widen scope, add steps beyond declared `on_fail` repairs, or
  change consent class. Injection traps in the eval suite must fail safe.
- **D9 — Browser = OpenCLI (this session).** Adopt fully behind an audit gate;
  dedicated Ollie Chrome profile; runs on the **host** (daemon+extension+
  Chrome), reached from WSL over the port bridge like the desktop-proxy.

---

## Engine architecture

```
        WSL gateway (Tier-1 brain, sandboxed)
        planner prompt + act-script schema
              │ MCP/HTTP + bearer (Tailscale)
              ▼
   ┌───────────────────────────────────────────────┐
   │  ollie-hands  (Python, host, session/VM)        │
   │   MCP server (FastMCP/HTTP, inert-boot)         │
   │    ├ policy engine   (tiers + consent, in-code) │
   │    ├ executor        (act-scripts, pre/post)    │
   │    ├ L0 shell        (PowerShell, deny-set)     │
   │    ├ L1 uia          (pywinauto/uiautomation)   │
   │    ├ L3 pixels       (DXGI capture + SendInput) │
   │    ├ windows/clipboard/session/vault            │
   │    ├ audit           (JSONL + PNG + MP4, off-box)│
   │    └ kill switches   (hotkey/collision/stop)    │
   └───────────────────────────────────────────────┘
              │ L2: shell out to `opencli … -f json`
              ▼
   ┌───────────────────────────────────────────────┐
   │  OpenCLI (host): daemon :19825 + Chrome ext     │
   │   drives the dedicated "Ollie" Chrome profile   │
   │   (logged-in sites Tushar approves) — DOM, not  │
   │   pixels. 100+ adapters + generic primitives.   │
   └───────────────────────────────────────────────┘
```

### MCP tool surface (small on purpose)
`observe` (screenshot + UIA snapshot + window list, one call = situational
awareness) · `plan_submit` (act-script → consent/exec, streams checkpoints) ·
`act` (single ad-hoc step) · `task_status`/`task_abort` · `session_info`.
The richness lives in act-scripts, not tool count. L2 web actions are just L0
steps that invoke `opencli` and parse JSON.

### Executor semantics
Preconditions (foreground process/title, UIA element exists, content-hash
unchanged since `observe`) → `on_fail` = retry|repair|escalate|abort. Every
write step MUST declare a postcondition (UIA assert / pixel-region diff /
file-exists). **Verify-after-act**: postcondition fail = action didn't take →
one re-ground+retry then escalate with before/after shots. Per-step + per-
script timeouts. **Human-collision**: real (vs injected) input during a
console-mode task → auto-pause + notify ("you moved the mouse — resume/abort?").
A human at the keyboard always outranks Ollie.

---

## Policy: scope tiers (the hard gate, in-engine, LLM-uneditable)

| Tier | What | Consent |
|---|---|---|
| T0 read | observe, UIA/DOM reads, window list, clipboard read | auto |
| T1 Ollie scope | Ollie Chrome profile, OllieDownloads/, Ollie's windows, scratch dirs | auto (notify on first console grab) |
| T2 approved apps | per-app allowlist Tushar grows | notify |
| T3 acts-as-Tushar | send/post/buy/delete as him; any app not in T2; system-setting writes; **all OpenCLI write verbs** (post/reply/connect/safe-send/purchase) | confirm |
| T4 forbidden | vault writes via agent, Defender/firewall/BitLocker, policy-file edits, audit deletion | blocked |

Earned-trust graduation T3→T2 by Tushar editing the policy file.

---

## Windows reality (nuances that break naive designs)
- **UAC/secure desktop:** SendInput can't touch it. Engine runs **elevated**
  (a) now; code-sign + **UIAccess** (b) later. Avoid disabling secure desktop.
- **Single interactive session:** black when locked, RDP kicks console →
  auto-lock disabled + auto-logon (dedicated box); avoid RDP-ing in while
  tasks run (collision auto-pause covers it).
- **Biometrics/Hello (un-actuatable):** ensure PIN/password fallbacks the
  vault holds; never depend on a biometric-only flow.
- **Mixed-DPI multi-monitor:** normalize to virtual-desktop space w/ per-
  monitor DPI (where naive bots misclick).
- **Ambient-state bug** (POC typed into stale Notepad): structurally killed by
  preconditions — observe → plan against that state → assert unchanged → act.
- **Focus-steal/timing:** UIA waits not fixed sleeps; SetForegroundWindow +
  verify before keys. **Interrupts** (toasts/update-reboots): observe popups;
  dismiss known-benign; unknown modal → escalate.
- **Wake/power:** powercfg keep-awake during tasks; WoL + Tailscale recover;
  watchdog already pages on Tailscale loss.

---

## Safety (must scale UP as scope widens)
Defense in depth: (1) brain still WSL-sandboxed, one MCP surface; (2) engine
hard policy in code; (3) consent bound to script hash; (4) screen/DOM-as-data;
(5) vault isolation; (6) off-box signed append-only audit; (7) kill switches:
physical hotkey hard-stop · human-collision auto-pause · `/stop` abort+revoke ·
planner-heartbeat dead-man · global "disable hands" inert-boot flag.
**OpenCLI is the biggest new trust surface** (full access to logged-in sites)
→ audit (provenance/deps/network-egress, like computer-use-mcp), dedicated
profile, all write verbs are T3-confirm.

---

## Build phases (each ends with a chat-run demo + doc update)

**Phase 0 — Foundations**
- Confirm kickoff decisions (session model, unlock, UAC-elevated, grounding
  budget, T2 seed list — see "Open").
- `ollie-hands` skeleton: MCP server, bearer auth, inert-boot, audit JSONL,
  `observe` (DXGI + UIA + window list).
- **Audit OpenCLI** (provenance, deps, egress, extension permissions); pin a
  version + verify hash, like computer-use-mcp.
- Demo: "what's on my screen right now?" → accurate description + window list.

**Phase 1 — L0 + L1 (the 70%)**
- L0 shell (deny-set + tiers); migrate off node `system.run`. L1 UIA
  (find/invoke/set/get_text), window mgmt, clipboard. Policy + consent wired
  to Telegram/WhatsApp. Act-script schema v1 + executor w/ pre/postconditions.
- Demo: "rename every screenshot on my desktop with today's date"; "open
  Settings, tell me disk free"; "set volume 30%".

**Phase 2 — Act-scripts & verification at speed**
- Full executor (retry/repair/escalate, collision-pause, timeouts); planner
  prompt + act-script discipline in AGENTS; three text strategies.
- Demo: 6-8 step native-app flow on one consent, with an injected stale-state
  to prove preconditions catch it.

**Phase 3 — Browser via OpenCLI (L2)**
- Install OpenCLI on the host (Node ≥20) + Browser-Bridge extension on a
  **dedicated "Ollie" Chrome profile**; Tushar logs that profile into approved
  sites. Engine L2 verbs shell out to `opencli … -f json`; map exit codes
  (69 bridge-down / 75 timeout / 77 auth) to executor on_fail. Adapters =
  L2a; generic primitives = L2b. All write verbs T3-confirm. Downloads →
  quarantined OllieDownloads/.
- Demo: "find the 3 cheapest X, draft a cart, don't buy" (research via the
  existing background-jobs system + OpenCLI; stops at purchase for confirm);
  "summarize my LinkedIn job matches" via `opencli linkedin jobs`.

**Phase 4 — Grounding bake-off + pixel L3**
- 100-target corpus; bake-off (§D5); wire winner; OmniParser detector if it
  wins; L3 verbs for canvas/custom controls only.
- Demo: click 20 mixed-DPI multi-monitor targets ≥90% single-shot.

**Phase 5 — Hardening**
- UAC: code-sign + UIAccess if needed. Keep-awake/WoL; auto-logon +
  auto-lock-off configured; watchdog integration. Full audit (video)
  off-box; injection-eval suite green.
- Demo: "while I'm out, do <multi-step real task>" end-to-end, Tushar away,
  clean reviewable audit trail.

---

## Critical files / components
- NEW: `ollie-hands/` — Python MCP engine (skeleton Phase 0). Pattern off the
  existing `openclaw-ollie-whatsapp-cloud/` plugin + `desktop-proxy` bearer.
- NEW: act-script schema + planner prompt → fold into `workspace/AGENTS.md`
  (+ a `DESKTOP_GATE.md` successor) once stable.
- REUSE: bearer-proxy pattern (desktop-proxy), inert→active safe-boot
  (`scripts/deploy-wa-plugin.sh`), background-jobs (`ollie-jobs/`) for the
  research half of web tasks, watchdog (`ollie-watchdog/`) for liveness,
  off-box backup (`scripts/ollie-state-backup.sh`) for audit sync.
- EXTERNAL: `jackwener/OpenCLI` (npm `@jackwener/opencli`, Node ≥20) on host.
- SUPERSEDES: `TIER2-PLAN.md` Notepad-only target; update + `ARCHITECTURE.md`
  threat table when v2 lands (doc-drift is already a tracked problem).

## Verification
- Smoke suite each deploy: observe accuracy, one L0, one L1, one OpenCLI web
  read, kill-switch fires, inert-boot works.
- Grounding corpus tracked over time (regression alarms).
- **Injection traps** (malicious screen/page text) must NOT change scope/
  consent — release blocker.
- **Stale-state traps** — preconditions catch moved/closed windows 100%.
- **Consent integrity** — revised plan w/ new write steps re-asks.
- OpenCLI: `opencli doctor` green; a read adapter (e.g. `hackernews top`) and
  a generic `browser get` round-trip on the Ollie profile.

## Kickoff decisions — RESOLVED 2026-06-10

> Tushar: **"Ollie can completely own the system, it is a spare system I
> don't use."** The box is dedicated to Ollie. This resolves all five:

1. **Session model: Option A, permanently.** The box is Ollie's body. No
   human-collision concern (keep the auto-pause code anyway — it's cheap and
   covers the rare manual RDP/console visit). **Hyper-V VM dropped entirely**
   — there is no human user on this box to isolate from.
2. **Unlock: disable auto-lock + enable auto-logon** on the box. No
   vault-held Windows PIN needed. Vault still exists for site/app secrets.
3. **UAC: engine runs elevated.** Fine on a dedicated box; code-sign +
   UIAccess remains the later hardening path.
4. **Grounding budget: Zeus/Opus-vision first**, self-host UI-TARS only if
   the Phase-4 bake-off demands.
5. **T2: the entire local machine is Ollie scope** (T1/T2 merge in practice
   on this box). The tier table's real boundary is now local-vs-external:
   **T3-confirm stays mandatory** for anything acting as Tushar in the world
   — sends, posts, purchases, logged-in browser write verbs, money. That
   gate protects his identity/accounts, not the hardware.

Phase-5 scope updated accordingly: VM removed from the plan; keep
keep-awake/WoL, watchdog integration, off-box audit, injection-eval suite.
