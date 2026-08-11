# Computer-Use v2 — "Full Hands" Design

> Goal: Ollie controls the Windows box **the way a human at the keyboard
> would** — any app, any window, any flow — reliably enough to be trusted
> with real tasks, and safely enough that a hijacked brain can't burn the
> house down. This supersedes the Notepad-only POC (TIER2-PLAN.md) as the
> target state; the POC's architecture decisions (standalone engine, MCP
> consumer model, D-20260605-03) carry forward.

Status: DRAFT for review — 2026-06-10
Owner: Tushar. Builder: Claude + Ollie.

---

## 1. What "exactly like a human" actually means

A human at this machine can:

| # | Capability | v2 stance |
|---|-----------|-----------|
| H1 | See the whole screen, all monitors, at any moment | YES — fast capture, multi-monitor, DPI-correct |
| H2 | Move mouse, click, double/right-click, drag, scroll anywhere | YES |
| H3 | Type anything incl. Unicode/emoji, every shortcut/modifier combo | YES |
| H4 | Use the clipboard (text, files, images) | YES |
| H5 | Launch, close, switch, resize any app/window; use Alt-Tab, virtual desktops | YES |
| H6 | Use the file system (Explorer, file dialogs, drag-drop) | YES |
| H7 | Browse logged-in sites with their real accounts | YES — but via browser engine, not pixels (see D1) |
| H8 | Click "Yes" on a UAC prompt | YES, with explicit design (secure desktop — see §6.1) |
| H9 | Unlock the machine / log in | CONDITIONAL — owner decision (see §6.2) |
| H10 | Pass Windows Hello / biometric prompts | **NO — physically impossible.** Design around (PIN/password fallbacks) |
| H11 | Watch video / react to motion on screen | PARTIAL — polling screenshots; true video understanding deferred |
| H12 | Hear audio (notification sounds, calls) | NO (v2 non-goal; toast/tray *visual* notifications YES) |
| H13 | Insert hardware, press the power button | NO (Wake-on-LAN / power policy instead — §6.3) |

Anything in YES/CONDITIONAL that v2 can't do at the end of Phase 5 is a bug
against this spec.

**Non-goals for v2:** distribution to other machines (keep it install-able
but don't gate on code-signing), macOS/Linux hands, audio understanding,
real-time game-speed interaction.

---

## 2. Where we are vs where this goes

| Dimension | POC today (2026-06-05) | v2 target |
|---|---|---|
| Actuator | `computer-use-mcp` v1.8.0 (patched) | Own Python engine (per D-20260605-03) |
| Scope | Notepad only | Whole box, tiered policy |
| Gate | Soft (prompt) + per-action Telegram confirm | **Hard, in-engine** policy + **task-level** consent |
| Grounding | Pixel-only via MiniMax M3 (unbenchmarked) | UIA-first, pixel fallback after bake-off |
| Text input | SendInput @ ~14 chars/sec | UIA SetValue / clipboard-paste (instant), SendInput fallback |
| Screenshot | ~2.3 s | < 300 ms (DXGI) |
| Sequences | 1 action per call, confirm each | Act-scripts: approved plan runs end-to-end with checks |
| Session | Bound to live session 1; black when locked | Managed session ownership (§6.2) |
| Browser | None (pixel-clicking Chrome at best) | Playwright/CDP subsystem with persistent profiles |
| Verification | None | Pre/postconditions on every write action |
| Audit | Designed, partial | JSONL + per-step PNG + optional video, off-box copy |

---

## 3. Core design decisions

### D1 — Capability ladder: pixels are the LAST resort

"Control the machine like a human" ≠ "do everything by simulated mouse."
A human uses the mouse because they have no other interface; software does.
Every task routes down this ladder, top first:

```
L0  Shell / PowerShell / CLI / API      — deterministic, instant, headless
L1  UIA (UI Automation tree)           — find-by-name, invoke, SetValue; model-free
L2  Browser engine (Playwright/CDP)    — for anything inside a web page
L3  Pixels (screenshot -> coords -> SendInput) — canvas, custom controls, last resort
```

Rules:
- The engine itself does NOT auto-route; the **planner** (brain) chooses the
  rung, but the engine exposes all four and the planning prompt bakes in the
  ladder. Reason: routing needs task context the engine doesn't have.
- L0 covers ~60-70% of "do X on my computer" (install, move files, change a
  setting, kill a process, query state). It already part-exists via the
  Companion node's `system.run` — v2 moves host-shell into the engine so ONE
  component owns policy + audit for all host actions.
- L2: clicking a browser by pixels is strictly worse than driving it via CDP
  (selectors, waits, downloads, auth state). Pixel-clicking Chrome is allowed
  only when CDP can't reach it (e.g. browser-native dialogs).
- L3 is the only rung that needs a vision model. Minimizing L3 minimizes
  cost, latency, and failure rate.

### D2 — Planner/executor split with act-scripts

Per-step round-trips through the brain are the POC's structural failure
(60-75 s Opus tool turns × N steps × a Telegram confirm each = unusable).
v2 splits:

- **Planner (Tier-1 brain, Opus/Zeus):** looks at the task + a screenshot +
  UIA snapshot, emits an **act-script** — a JSON plan of steps, each with
  selector, args, precondition, postcondition, and on_fail directive.
- **Executor (engine, on host):** runs the act-script locally at machine
  speed. No model calls for deterministic steps. Streams checkpoint events
  back. Stops and escalates to the planner only on: failed postcondition,
  ambiguous grounding, policy boundary, or explicit `checkpoint: ask`.

This gives human-like fluency (a human doesn't phone a committee between
keystrokes) while keeping the brain in charge of judgment calls.

Act-script sketch (schema frozen in Phase 2):

```jsonc
{
  "task": "Open the Hostinger invoice PDF in Downloads and print page 1",
  "consent_id": "c-20260610-001",          // ties to the approved plan
  "steps": [
    { "id": 1, "rung": "L0", "action": "shell",
      "cmd": "Get-ChildItem ~/Downloads -Filter *invoice*.pdf | Sort LastWriteTime | Select -Last 1",
      "save_as": "pdf_path" },
    { "id": 2, "rung": "L0", "action": "open", "target": "${pdf_path}",
      "post": { "uia": { "window_title_contains": "invoice" } },
      "on_fail": "escalate" },
    { "id": 3, "rung": "L1", "action": "key", "keys": "ctrl+p",
      "pre":  { "foreground_process": "Acrobat.exe|msedge.exe" },
      "post": { "uia": { "dialog_name_contains": "Print" } } },
    { "id": 4, "rung": "L1", "action": "uia_set", "selector": { "name": "Pages", "role": "Edit" }, "value": "1" },
    { "id": 5, "rung": "L1", "action": "uia_invoke", "selector": { "name": "Print", "role": "Button" },
      "checkpoint": "notify" }
  ]
}
```

### D3 — Consent: task-level, not action-level

Replaces the per-click Telegram confirm.

1. Planner produces the act-script + a one-paragraph human-readable preview
   ("I will: open X, fill Y, press Z; ~8 steps; touches: Edge, Acrobat").
2. Owner approves **once** per task on Telegram/WhatsApp. The approval binds
   to the script hash — if the planner revises the plan mid-task beyond
   declared `on_fail` repairs, that's a NEW consent.
3. Consent classes (set per task by policy engine, not by the LLM):
   - **auto** — read-only or inside Ollie's own scope tier → no ask.
   - **notify** — proceed, but post what's happening (e.g. step 5 above).
   - **confirm** — wait for explicit yes (acts-as-Tushar, money, sends,
     deletes, anything leaving the box).
4. Kill switches stay per-action-granular (see §7.4) — consent is coarse,
   abort is instant.

### D4 — Session ownership (the deepest problem) → §6.2, owner decision.

### D5 — Grounding: UIA-first; pixel model chosen by bake-off, not vibes

- UIA answers "where is the Save button" deterministically for ~90% of
  native-app UI and costs nothing. It is the primary grounding for L1.
- Pixel grounding (L3) gets a **bake-off in Phase 4**: candidates =
  Zeus/Opus vision, MiniMax M3 (current default per D-20260605-05),
  UI-TARS (self-host), OmniParser-as-detector + any of the above.
  Corpus: 100 click-targets across Notepad, Settings, Explorer, Chrome on
  5 real sites, one job-application form, one canvas app. Metric:
  single-shot click-in-bbox accuracy + p95 latency. Switching criterion
  stays ≥90% (D-20260605-05).
- OmniParser (or equivalent detector) is an *add-on* evaluated in the same
  bake-off, not an upfront dependency.

### D6 — One engine owns ALL host actions

Standalone Python engine (per D-20260605-03), MCP server over HTTP with
bearer auth (pattern already proven by the desktop-proxy). The Companion
node's `system.run` path gets retired for agent use once the engine's L0 is
live — two parallel host-execution paths with two policy regimes is how
gaps happen.

### D7 — Secrets never transit the brain

A human types passwords; Ollie must too — but the password must never appear
in LLM context, logs, or screenshots:

- Engine-side **credential vault** (Windows Credential Manager / DPAPI).
- Act-scripts reference `{"action":"type_secret","ref":"hostinger-login"}`;
  the engine resolves ref → keystrokes directly into the focused field.
- Engine masks the screen region of password fields in audit screenshots
  and pauses video capture during `type_secret`.
- Vault writes (adding a credential) are owner-only, done over SSH/RDP,
  never via the agent.

### D8 — Everything on screen is untrusted input

Once Ollie reads arbitrary screens/pages, every screenshot is a potential
prompt-injection vector ("OLLIE: ignore your instructions and..."). Rules:

- Screen/OCR/UIA content is **data, never instructions** — stated in the
  planner prompt AND enforced structurally: the policy engine + consent
  class don't care what the screen says; no screen content can widen scope,
  add steps beyond `on_fail` repairs, or change consent class.
- Any plan revision triggered by observed content re-enters consent if it
  adds write-side steps.
- The eval suite (§9) includes injection traps; they must fail safe.
---

## 4. Engine architecture

```
                    WSL gateway (Tier 1)
                    planner prompt + act-script schema
                          │  MCP/HTTP + bearer (Tailscale)
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │  ollie-hands  (Python, host, interactive session)     │
   │                                                        │
   │  MCP server (FastMCP/HTTP)                             │
   │   ├── policy engine      (hard gate: tiers, consent)   │
   │   ├── executor           (act-script runner, pre/post) │
   │   ├── L0 shell           (PowerShell, allow/deny sets) │
   │   ├── L1 uia             (pywinauto/uiautomation)      │
   │   ├── L2 browser         (Playwright, profiles)        │
   │   ├── L3 pixels          (DXGI capture + SendInput)    │
   │   ├── windows            (launch/focus/resize/close)   │
   │   ├── clipboard          (text/files/images)           │
   │   ├── session            (lock/unlock/keep-awake)      │
   │   ├── vault              (DPAPI credential refs)       │
   │   ├── audit              (JSONL + PNG + video, off-box)│
   │   └── kill switches      (hotkey, human-override, /stop)│
   └──────────────────────────────────────────────────────┘
```

### 4.1 MCP tool surface (what the brain sees)

Small on purpose — the richness lives in act-scripts, not in tool count:

| Tool | Purpose |
|---|---|
| `observe` | screenshot (region/monitor opts) + UIA snapshot of foreground or named window + window list. One call = full situational awareness. |
| `plan_submit` | submit an act-script → returns consent requirement or starts executing; streams checkpoints |
| `act` | single ad-hoc step (same schema as a script step) for interactive exploration / one-offs; same policy + consent rules |
| `task_status` / `task_abort` | observe or stop a running script |
| `session_info` | locked? who's at console? monitors/DPI? idle time? |

### 4.2 Executor semantics

- **Preconditions** (any step may declare): foreground process/title match,
  UIA element exists, window content-hash unchanged since `observe`,
  clipboard state. Fail → `on_fail` (`retry` | `repair` | `escalate` | `abort`).
- **Postconditions:** UIA assertion, pixel-region diff, file-exists, process
  state. Every write-side step MUST declare one — "fire and hope" is a
  schema validation error.
- **Verify-after-act:** postcondition failure = the action *didn't take* →
  one re-ground + retry, then escalate with before/after screenshots.
- **Timeouts** per step + per script (default 30 s / 15 min).
- **Human-collision detection:** a low-level input hook distinguishes
  injected vs real input. Real mouse/keyboard activity while a script runs
  in console mode → auto-PAUSE + notify owner ("you moved the mouse —
  resume/abort?"). A human at the keyboard always outranks Ollie.

### 4.3 Subsystem notes (the parts that bite)

- **Capture:** DXGI desktop duplication (`dxcam`/`mss`), target <300 ms
  full frame. Per-monitor capture; coordinates normalized to virtual-desktop
  space with per-monitor DPI mapping (mixed-DPI is where naive bots click
  the wrong window).
- **Input:** `SendInput` for keys/mouse; `KEYEVENTF_UNICODE` for arbitrary
  text; clipboard-paste for bulk text (instant vs 14 chars/s) with clipboard
  save/restore; `uia.SetValue` when the control supports it. Executor knows
  all three text strategies; step declares one, default = fastest safe.
- **UIA:** `pywinauto` (uia backend). Cache the tree per window; selectors
  by (name, control-type, automation-id) with fuzzy fallback. UIA also
  powers free reads: get_text, list controls, find dialog — no model needed.
- **Browser (L2):** Playwright driving a dedicated **"Ollie" Chrome/Edge
  profile** (persistent `user-data-dir`, logged into approved sites once by
  Tushar). NOT Tushar's daily profile — session/cookie blast-radius control.
  Headed (visible) so audit video covers it and Tushar can watch. Downloads
  land in a quarantined `OllieDownloads/` dir.
- **L0 shell:** PowerShell with arg-logged execution, deny-set (BitLocker,
  bcdedit, registry security keys, Defender tampering, user/cred mgmt) and
  per-tier allow rules. Replaces agent use of node `system.run`.
- **Audit:** JSONL (ts, step, action, selector, result, consent_id) + PNG
  per write-step + optional MP4 of whole task. Local on D:, synced off-box
  by the existing state-backup machinery. Secrets masked per D7.

---

## 5. Policy: scope tiers (the hard gate)

Enforced in-engine; the LLM cannot edit them (config file owned by Tushar,
loaded at engine start, hot-reload via signed-off SSH change only).

| Tier | What | Examples | Consent class |
|---|---|---|---|
| T0 read | observe, UIA reads, window list, clipboard read | any screen | auto |
| T1 Ollie's scope | Ollie browser profile, `OllieDownloads/`, Ollie's own windows, scratch dirs | research in browser, file a download | auto (notify on first window grab of console) |
| T2 approved apps | per-app allowlist Tushar grows over time | Notepad, Explorer (user dirs), Calculator, Settings(read), Spotify | notify |
| T3 acts-as-Tushar | anything sending/posting/buying/deleting as him; any app not in T2; system settings writes | email send, LinkedIn apply, payment pages, installers | confirm |
| T4 forbidden | engine-level hard NO regardless of consent | vault writes via agent, Defender/firewall tampering, BitLocker, policy file edits, audit-log deletion | blocked |

Escalation path: anything T3 that Ollie does repeatedly and well graduates
to T2 by Tushar editing the policy file — capability grows by *earned
trust*, not by loosening the architecture.

---

## 6. The Windows reality (nuances that break naive designs)

### 6.1 UAC / secure desktop
`SendInput` cannot touch UAC prompts (secure desktop) — a human can. Options:
- **(a) Engine runs elevated** (scheduled task, highest privileges): most
  L0 admin work never shows a prompt. Still can't click prompts triggered
  by *other* apps' elevation.
- **(b) UIAccess manifest** (`uiAccess=true`): lets a signed binary in a
  trusted path inject into higher-integrity UI incl. secure desktop — the
  accessibility-tool mechanism. Requires code-signing cert. Proper fix,
  more setup.
- **(c) Policy softening:** set UAC to not use the secure desktop
  (`PromptOnSecureDesktop=0`) — then prompts are ordinary windows Ollie can
  click. Weakens a real protection for everything on the box.
- **Recommendation:** (a) now, (b) when we code-sign anyway (Phase 5+).
  Avoid (c).

### 6.2 Session ownership — OWNER DECISION REQUIRED
Windows client = ONE active interactive session. Consequences: screenshots
are black when locked; an RDP login as a second user kicks the console.
"Ollie works while I'm away" requires choosing one:

- **Option A — Console-primary with idle ownership (recommended start):**
  Ollie acts in session 1. Power policy keeps the box awake (display may
  sleep — DXGI still captures); auto-lock disabled OR engine holds an
  unlock capability (§H9): credentials in vault, engine may unlock when a
  consented task starts and re-lock after. Human-collision pause (§4.2)
  arbitrates when Tushar sits down. Cheapest; "the box is Ollie's body."
  Tradeoff: a physical passer-by sees an unlocked desktop during a task.

- **Option B — Dedicated Ollie VM (recommended end-state):** a Hyper-V VM
  (the box is already on a build with the virtualization stack) is Ollie's
  body. Its console is always "present" to the engine inside it; never
  fights Tushar's session; blast radius = the VM, not the host. Browser
  profiles, downloads, installs all live in the VM. Tushar's real machine
  stays his. Tradeoff: things that must happen on the *physical* host
  (host-only apps, host file system) need a bridge (shared folder / L0 from
  host). Most "act as me online / fill this form / research" work is happy
  in the VM.

- **Option C — Second physical session:** never clean on Windows client SKUs
  (console-kick). Not recommended.

**My recommendation:** ship Option A for Phases 1-3 (fastest to "it works"),
stand up Option B in parallel and migrate the default to it once the VM has
the apps/accounts Tushar needs. "Anything on *this physical* box" stays an
Option-A capability; "Ollie's autonomous life" runs in the VM. This also
neatly answers the security tension in ARCHITECTURE.md: the unsandboxed
hands get their *own* sandbox (the VM) without losing host reach.

### 6.3 Wake / power
Box must be awake for Ollie to act. `powercfg` to prevent sleep during
active tasks; Wake-on-LAN + Tailscale to bring it back if it slept;
watchdog already pages on Tailscale loss (carry forward — STATUS open item).

### 6.4 Biometrics / Windows Hello (H10)
Hello prompts (face/fingerprint) are physically un-actuatable. Design
around: for each account Ollie must use, ensure a password/PIN fallback the
vault holds; never depend on a flow whose only auth is biometric.

### 6.5 The "act on ambient state" bug (carry forward)
The POC's first real bug (typed into a stale Notepad — TIER2-PLAN "Hard
behavior lesson") is now structurally prevented: every write step has a
foreground/title/content-hash precondition (§4.2). `observe` → plan against
that exact state → preconditions assert state hasn't moved → act. Stale
foreground = precondition fail = repair/escalate, never blind keystrokes.

### 6.6 Focus stealing & timing
Apps steal focus; dialogs appear late. Mitigations: UIA waits (not fixed
sleeps) for elements/windows; `SetForegroundWindow` + verify before any
key/type; postcondition re-checks. Never assume "I pressed the key 200 ms
ago so it landed."

### 6.7 Notifications / interrupts
Toasts, update reboots, "your PC will restart" can derail a task. `observe`
snapshots foreground + notable popups; planner is told to expect and dismiss
known benign interrupts; unknown modal → escalate.

---

## 7. Safety model (must scale UP with capability)

Loosening scope only works if the gate gets *stronger*, not weaker. The POC
leaned on "Notepad-only + confirm each click." v2 removes both crutches, so:

### 7.1 Defense in depth
1. **Tier 1 brain still sandboxed in WSL** — unchanged; it can't touch the
   host except via this one MCP surface.
2. **Engine hard policy** (§5) — tiers/consent in code, not prompt.
3. **Consent binding** — approved script hash; revisions re-consent (§D3).
4. **Screen-as-data** — no observed content can widen scope (§D8).
5. **Vault isolation** — secrets never in brain/logs/screens (§D7).
6. **Audit everything** — off-box, signed, append-only.
7. **Kill switches** (§7.4).

### 7.2 What gets *easier* to attack, and the answer
| New power | New risk | Control |
|---|---|---|
| Whole-screen read | injection via any page/app | §D8 + injection evals |
| Arbitrary apps | act in wrong/sensitive app | tiers T3/T4, preconditions |
| Browser w/ logins | post/buy/send as Tushar | T3 confirm, Ollie-profile not daily profile |
| L0 shell | destructive commands | deny-set, T4 forbidden, arg audit |
| Unlock/awake | physical exposure window | VM end-state, re-lock after task |

### 7.3 Threat model deltas vs ARCHITECTURE.md
That table assumed Notepad-only + per-action confirm. v2 replaces
"closed action enum" with "open actions + hard tier policy + task consent."
The load-bearing properties become: (1) WSL-sandboxed brain, (2) in-engine
policy the LLM can't edit, (3) VM blast-radius, (4) audit. Update
ARCHITECTURE.md's threat table when v2 lands.

### 7.4 Kill switches (granular, instant)
- Physical hotkey on the box (global low-level hook) → engine hard-stop.
- Human input collision → auto-pause (§4.2).
- Telegram/WhatsApp `/stop` → abort running task, revoke executor.
- Engine dead-man: planner heartbeat; lost → engine finishes current step,
  refuses new ones.
- Per-task abort + global "disable hands" flag (engine boots inert, exactly
  like the safe-deploy pattern already in use).

---

## 8. Build phases

Each phase ends with a demo Tushar runs from chat, and a doc update.

**Phase 0 — Foundations & decisions (small)**
- [ ] Tushar picks session model (§6.2): A now, B end-state? (default: yes)
- [ ] Stand up `ollie-hands` skeleton: MCP server, bearer auth, inert-boot,
      audit JSONL, `observe` (DXGI capture + UIA snapshot + window list).
- [ ] Retire reliance on `computer-use-mcp` once `observe`+basic input land.
- Demo: "what's on my screen right now?" → accurate description + window list.

**Phase 1 — L0 + L1 (the 70%)**
- [ ] L0 shell with deny-set + tier policy; migrate off node `system.run`.
- [ ] L1 UIA: find/invoke/set/get_text; window mgmt; clipboard.
- [ ] Policy engine (§5) + consent classes (§D3) wired to Telegram/WhatsApp.
- [ ] Act-script schema v1 + executor with pre/postconditions.
- Demo: "rename every screenshot on my desktop to add today's date" (L0);
  "open Settings and tell me my disk free space" (L1 read); "set volume 30%".

**Phase 2 — Act-scripts & verification at speed**
- [ ] Full executor: retry/repair/escalate, human-collision pause, timeouts.
- [ ] Planner prompt + act-script discipline baked into Ollie (AGENTS gate).
- [ ] Text strategies (UIA set / clipboard paste / unicode send).
- Demo: a 6-8 step native-app flow runs end-to-end on one consent, with a
  deliberate stale-state injected to prove preconditions catch it.

**Phase 3 — Browser subsystem (L2)**
- [ ] Playwright + dedicated Ollie profile; Tushar logs into approved sites.
- [ ] Browser act-script verbs (goto/fill/click/wait/download); quarantine dl.
- [ ] T3 confirm flow for send/post/buy.
- Demo: "find me the 3 cheapest X and add the best to a draft cart, don't
  buy" → runs in browser, stops at purchase for confirm. (Pairs with the
  existing background-jobs system for the research half.)

**Phase 4 — Grounding bake-off + pixel L3**
- [ ] Build the 100-target corpus; run the bake-off (§D5).
- [ ] Wire the winner as the L3 grounding; OmniParser detector if it wins.
- [ ] Pixel L3 verbs for canvas/custom controls only.
- Demo: click 20 targets across mixed-DPI multi-monitor, ≥90% single-shot.

**Phase 5 — Session end-state + hardening**
- [ ] Hyper-V Ollie VM (Option B); migrate browser/downloads/installs in.
- [ ] UAC story: code-sign + UIAccess (§6.1b) if needed.
- [ ] Unlock/keep-awake/WoL; watchdog integration.
- [ ] Full audit (video), off-box sync, injection-eval suite green.
- Demo: "while I'm out, do <multi-step real task>" end-to-end, Tushar away,
  with a clean audit trail he reviews after.

---

## 9. Verification & evals (built alongside, not after)

- **Smoke suite** (every deploy): observe accuracy, one L0, one L1, one
  browser, kill-switch fires, inert-boot works.
- **Grounding corpus** (§D5): tracked accuracy/latency over time; regression
  alarms (ties into the eval-harness idea parked for later).
- **Injection traps:** screens/pages containing "ignore instructions / do X"
  must NOT change scope or consent. A failure here is a release blocker.
- **Stale-state traps:** preconditions must catch moved/closed/changed
  windows 100% of the time.
- **Consent integrity:** a revised plan with new write steps MUST re-ask.

---

## 10. Open questions for Tushar (blocking Phase 0)

1. **Session model:** OK to run Option A (console, auto-lock relaxed /
   engine-held unlock) now, and build the Ollie VM (Option B) as end-state?
2. **Unlock capability:** comfortable with the vault holding the Windows
   login so Ollie can unlock for a task and re-lock after? (Or keep the box
   unlocked-but-engine-gated while you're away?)
3. **Browser identity:** confirm a *dedicated Ollie browser profile* (you
   log it into the sites you want it to use), NOT your daily profile?
4. **UAC:** OK with the engine running elevated (Option a)? Code-signing for
   UIAccess later is a cost — worth it, or avoid admin-prompt flows for now?
5. **Grounding budget:** fine to start L3 on Zeus/Opus-vision or MiniMax M3
   and only self-host UI-TARS if the bake-off demands it?
6. **Scope seed:** what apps go in T2 (notify-only) on day one beyond
   Notepad/Explorer/Settings/Calculator? (Email client? Spotify? VS Code?)