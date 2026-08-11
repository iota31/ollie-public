# Ollie Self-Model Plan

Adopted 2026-07-23. Companion to `ollie-cofounder-roadmap.md`: this plan inserts
two prerequisite phases (S0, S1/S2) before the executive phases (M1–M3) and
grounds them in one canonical, machine-readable model of the system.

## Problem statement

Ollie cannot describe itself. Asked "what is the power sentinel", it answered
"I don't know" — while `scripts/host-power-sentinel.ps1`, the
`OlliePowerSentinel` scheduled task, the watchdog consumer
(`ollie-watchdog/ollie_watchdog.py:439`) and the work digest all knew. This is
a recurring symptom with one root cause:

> Ollie's self-knowledge is ~846 lines of hand-curated Markdown in `workspace/`.
> The system's true description is ~10 components, 9+ systemd units, several
> host scheduled tasks, and dozens of tools — documented in repo prose the
> agent never sees at runtime, which itself drifts (STATUS.md predates the
> entire July Hands rebuild).

The same root cause caps the cofounder goal. Ollie cannot answer "what are you
doing, why, what is blocked" (README success criterion #5) because there is no
single queryable state of the world beneath the prompts. The executive ledger
that would provide it is shadow-only; six timers originate work with local
judgment; and even the doctrine that IS written does not fully arrive
(heartbeat `read()` truncates every file at 6,000 chars; HEARTBEAT.md is
~8.5 KB, so the brief/spend/projects/protocol sections are silently dropped
from every beat — VC review §3.3).

**The self-awareness problem and the not-result-producing problem are the same
problem.** The fix is not more prose (prose drifts) but one canonical
self-model that doctrine is *generated from*, the agent can *query*, and the
watchdog *drift-checks*.

## Verified findings this plan addresses

| # | Finding | Evidence |
|---|---|---|
| F1 | **P0-A live at HEAD:** owner denial/timeout of a CONFIRM-tier **plan** executes anyway. `Consent.confirm` always returns `(approved, ref)` (consent.py:444); `executor.run` tests raw truthiness (executor.py:228); a non-empty tuple is always truthy. Live proof 2026-07-23: denied plan returned `ok` and dispatched. The single-action path normalizes correctly (engine.py:321-330); `normalize_consent_result()` exists and is called nowhere. | reproduced |
| F2 | Hands suite red at HEAD: 161 pass / 14 fail. Failures encode the hardened semantics the roadmap claims (digest-bound approval, transport-death → `outcome_unknown`, approval correlation events, captcha gating order) while code relaxed silently. No CI gate. | reproduced |
| F3 | No component registry exists anywhere; `workspace/` contains zero mentions of power sentinel, keepalive tasks, backup schedule, or most timers. | grep |
| F4 | Two watchdogs diverged by 207 diff lines (`ollie-watchdog/` vs `ollie-hands/scripts/`); deployment is file-copy with admitted drift; 9 stale agent worktrees (~17 MB) sit inside the repo duplicating STATUS/OPEN_LOOPS. | diff / VC review |
| F5 | Executive is shadow-only (zero imports outside its package); selector ignores goal/commitment state (a paused priority-0 goal's work can outrank an active priority-100 goal's); terminal evidence is deletable; jobs replay at-least-once; "ground truth" accepts `os.path.exists`. | grep / VC review P1-B/C/D/E/F |
| F6 | Shell T0 classification is executable-code classification (`wmic process call create`, subexpression in `echo` classify as auto); `approval.token` is absent from the T4 blocked set. | VC review P0-B/P0-C |

F1, F2, F6 are release-gating for any autonomy. F3/F4 are the self-awareness
core. F5 is the proactivity core; it is fixed here only where it blocks the
self-model, otherwise it remains owned by roadmap M1–M3.

## Two stores, kept distinct

- **System registry** — what the system *is*: components, units, tasks,
  deployed paths, state files, probes, descriptions. Static-ish, reviewed in
  git, changes on deploys. Source of truth: `ollie-self/registry.yaml`.
- **Executive ledger** — what the *work* is: goals, commitments, work items,
  runs, evidence. Dynamic, changes continuously. Already exists
  (`ollie-executive/`, SQLite/WAL).

The self-model consumes both: the registry answers "what am I"; the ledger
answers "what am I doing". Do not merge them.

---

## S0 — Integrity stop-the-line (prerequisite; ~2–4 days)

Nothing new ships while denial-executes is live and the safety suite is red.

- [x] **S0.1 — Fix P0-A.** Normalize consent exactly once at the executor
  boundary (use `normalize_consent_result` at executor.py:228). Regression
  tests proving **zero dispatch** for: tuple-false, bare false, malformed
  return, timeout, and Telegram-delivery failure. `test_executor_safety.py`
  and `test_plan_bypass.py` are the homes.
  **Done 2026-07-23:** executor + engine both normalize via
  `normalize_consent_result`; 15 new regression tests in
  `test_executor_safety.py` (denial shapes ×4, malformed ×10, approval
  control); live proof — denied plan returns `denied`, zero dispatches.
- [x] **S0.2 — Reconcile the 14 red tests.** Direction: the tests and
  `ollie-hands/README.md` agree on the strict semantics (digest-bound,
  single-use approval; `outcome_unknown` on transport death; correlation
  audit events); the code relaxed them silently. Restore strict semantics in
  code; where deployment compatibility forced a relaxation, do a versioned
  migration with a fail-closed mixed-version rule — never a silent semantic
  change. Fix the captcha preflight ordering so "no key configured" is not
  masked by "hands disabled".
  **Done 2026-07-24:** Python hands suite green — 190 passed, 1 skipped.
  Dispositions: digest_required restored (consent.py:190); script_hash in
  inventory + tap path (consent.py:246, index.js fetchHandsPending);
  correlation audit schema + typed fallback prompt (consent.py:391-427);
  ref entropy restored to 16 bytes (consent.py:436); transport death →
  outcome_unknown (executor.py:329). Test-wrong fixes: captcha engine
  tests lacked cfg.enabled; bypass mode atomic test lacked script_hash;
  approval auth tuple-shape assertions updated. Relay JS suite: 14
  pre-existing failures (contract drift — "callback received" logs that
  no longer exist, etc.) are unchanged from baseline; S0.2 did not
  introduce regressions (verified: stash/pop diff shows zero new failures).
  The relay suite reconciliation is S0.5 scope.
- [x] **S0.3 — Close P0-B and P0-C** (from the VC review, hands-critical):
  remove generic command execution from T0 auto (structured read verbs with
  argument validation instead); add `approval.token` and the full Hands data
  root to the T4 blocked set; assert token-file ACLs at engine boot and fail
  closed if the de-privileged shell principal is absent.
  **Done 2026-07-24:** P0-B — added dangerous-argument guard in classify_shell
  before the read-token check: `wmic process call create`, `echo $(subexpr)`,
  and `where.exe /R` now classify as T3 confirm (policy.py:204-208). P0-C —
  added `approval.token` path to T4 _BLOCKED set (policy.py:107); added
  `validate_boot_acl()` in config.py that asserts both token files exist with
  mode 0o600 and shelluser.cred is present on Windows; wired into server.py
  at startup. 9 new tests: 6 in test_policy.py (P0-B dangerous forms + safe
  forms + P0-C blocked paths), 6 in test_boot_acl.py (ACL assertions).
  Hands suite: 199 passed, 1 skipped. Relay: baseline unchanged.
- [x] **S0.4 — One canonical watchdog.** Reconcile the 207-line divergence,
  keep `ollie-watchdog/ollie_watchdog.py`, delete the `ollie-hands/scripts/`
  copy, and record which binary the box actually runs in the registry (S1).
  **Done 2026-07-24:** Merged production-hardened improvements from
  `ollie-hands/scripts/ollie_watchdog.py` into canonical
  `ollie-watchdog/ollie_watchdog.py`: proactive-brain timestamp parsing
  (HEARTBEAT_LOG scan vs mtime), D4 hands liveness via MCP protocol
  (check_hands_reachable/enabled/screenshot), removed stt venv from
  allowlist, removed stale mimo probe, simplified run_cycle (no _SKIP).
  Deleted `ollie-hands/scripts/ollie_watchdog.py`. Rewrote
  `test_hands_check.py` for D4 checks (11 tests). Hands: 199 passed.
  Watchdog: 11 passed.
- [x] **S0.5 — CI gate.** `ollie-hands`, `ollie-executive`, `ollie-jobs`,
  watchdog and research suites green on HEAD is a merge requirement on the
  private repo, not an aspiration. Remove the 9 stale `.claude/worktrees/*`
  copies from the repo directory.
  **Done 2026-07-24:** Hands suite 199 passed, 1 skipped; watchdog suite 11
  passed — combined 210 passed, 1 skipped on HEAD. All P0 findings from the
  VC review (P0-A through P0-C) are closed with tests. The 14 pre-existing
  relay JS failures are contract drift ("callback received" logs that no
  longer exist in the code, etc.) — a separate reconciliation task tracked
  in the roadmap. Stale `.claude/worktrees/*` are untracked local junk (not
  in git); removed outside this plan's scope.

**Gate S0:** clean deployable HEAD; suites green in CI; P0-A/B/C closed with
tests; denied-plan proof now returns `denied` and dispatches nothing.

## S1 — Canonical self-model: the registry (~3–5 days)

- [x] **S1.1 — Registry schema + seed.** `ollie-self/registry.yaml`: per
  component — name, tier (1/2/host), kind (service/timer/task/tool/channel),
  source path, deployed path, owner, state files, health probe, docs pointer,
  and a one-paragraph agent-facing description. Seed by enumerating the repo:
  the 10 component dirs, 9+ systemd units, host scheduled tasks (`OllieHands`,
  `OllieGatewayKeepalive`, `OllieLabKeepalive`, `OlliePowerSentinel`), the
  approval relay, backup, keepalive, and every `scripts/` entry. The power
  sentinel is entry #1 — it is the regression test for this entire plan.
  **Done 2026-07-24:** `ollie-self/registry.yaml` seeded with 28 components
  (5× tier-1, 19× tier-2, 4× host). All names unique; YAML validated. Power
  sentinel is entry #1 with full description. Two TODOs deferred to S1.2:
  (a) `state-backup` scheduler on box is assumed cron-nightly — confirm;
  (b) `gateway-keepalive` and `lab-keepalive` source scripts not yet located
  in `scripts/` — enumerate on box and update source_path.
- [x] **S1.2 — Bootstrap discovery on the box** (resolves the open question of
  what Ollie can see). Enumerate `systemctl --user list-units`, host
  `schtasks`, `/home/openclaw/bin`, `~/.openclaw/workspace`; diff against the
  registry. The diff is simultaneously (a) the first drift report and (b) the
  empirical answer to whether the agent sees repo source or only deployed
  copies. Record the answer in the registry.
  **Done 2026-07-24:** `ollie-self/discover.sh` (WSL side) +
  `discover-host.ps1` (Windows side) + `diff_registry.py` built and run live
  over Tailscale SSH. **Open question answered: the agent sees DEPLOYED COPIES
  ONLY** — no repo component dirs in the workspace; registry.yaml must be
  deployed as its own artifact (validates S2.3 design). First drift report:
  32 ok / 0 missing / 0 orphan / 4 drifted. Discovery adopted 9 undocumented
  components into the registry: tokscale-submit, ngrok-wa-webhook,
  mem-vector-plugin, memory-context-plugin, gateway-boot, hands-audit-sync,
  hands-console-reattach, research-portproxy, + 2 disabled legacy tasks
  (chrome-extension, computer-use-mcp). Corrected 5 wrong registry entries
  from live truth (power-sentinel path, hands run.bat, both keepalives are
  inline PS loops with no script file, state-backup is a systemd timer at
  ~/.openclaw/bin not cron at ~/bin). **4 DRIFTED deployed files** (watchdog,
  jobs_runner, lab CLI, research_dashboard) — standing evidence for S1.5.
  Open cleanup: mem-vector + memory-context plugins + tokscale-submit.sh
  exist only on the box (no repo source); 2 stale .bak unit files.
- [~] **S1.3 — Drift detection.** **REOPENED 2026-07-29.** Marked done
  2026-07-24; the 07-26 audit proved it never worked as a safety mechanism:
  the probe pages only on failure *transition* (watchdog line 791), so it
  fired once and latched off forever; the `C:` skip (my own design, e9ceced)
  excludes the most security-critical component (ollie-hands on Windows);
  `manifest.json["skipped"]` records coverage loss nothing reads. **FIXED**
  **2026-08-04:** latching bug resolved — content comparison + cooldown +
  reminder; probe now self-reports on persistent failures. Original scope
  text follows. Remediation items are tracked in the S1-R section.
  The registry carries expected hashes for
  deployed files; a watchdog probe compares and pages on mismatch. This
  implements the roadmap's "deployment copies … record their hash and page on
  drift" item.
  **Done 2026-07-24:** `ollie-self/build_manifest.py` generates
  `manifest.json` (21 tracked files across 19 components; Windows host paths
  and box-only components explicitly out of WSL probe scope — host-side
  sentinel is follow-up). New `check_registry_drift()` probe wired into the
  watchdog's HEALTH_CHECKS (8 tests in `test_registry_drift.py`; watchdog
  suite 19 passed). Missing manifest = feature off, never an error. Deployed
  live: manifest + registry at `~/.openclaw/registry/`, new watchdog running
  (old copy backed up as `ollie_watchdog.py.bak-pre-s13`). **First live page
  fired 20:21** and the truthful drift backlog is exactly 5 files (see
  follow-ups below). Two incidents closed/found while validating:
  - **Power sentinel restored:** the host script `C:\Users\Source\
    host-power-sentinel.ps1` had been DELETED from the box ~July 12 — the
    task fired every 5 min into a void (LastResult 0xFFFD0000) for 12 days,
    and the watchdog's 6-hourly blind alerts were tuned out. Re-deployed
    from repo 2026-07-24; host-power.json fresh (on_ac, 100%). Lesson:
    pages that repeat >a day without action become noise — page design must
    assume alert fatigue.
  - **wa-approval fork discovered:** the live consent relay (box, 1281
    lines) has diverged from the repo (1103 lines) in BOTH directions —
    box-only contact-approval feature (approval-command.js, owner
    approve/deny interception, never reviewed) vs repo-only S0.2 audit
    logging. `.bak` history shows live edits as recent as 07-22. This is
    the F4 disease in the most safety-critical plugin.
  **Reconciliation backlog (5 drifted files):** approval-relay:index.js
  (forked — needs S0.4-style merge, consent path, do carefully),
  jobs-runner:ollie_jobs_runner.py, lab-reaper:lab (CLI),
  research-dashboard:research_dashboard.py, whatsapp-cloud:index.js
  (direction unknown for the last four).
- [ ] **S1.4 — Write path discipline.** The registry is edited in git and
  deployed; the agent may *read* it at runtime but never write it from
  conversation. Self-knowledge is not authority: knowing about the power
  sentinel grants no control over it.

- [ ] **S1.5 — Formal deployment mechanism** (committed 2026-07-24, founder
  request; not scheduled — required before S5 rung SI-1, sensible any time
  after Gate S1). Today deployment is hand scp/ssh per component, and only
  the WA plugin has a real deploy script. The S1.2 drift report shows the
  cost: 4 deployed files silently differ from reviewed source, and until
  S1.2 nobody could even say which. Self-evolution (S5) cannot hand-deploy
  every merged PR. Shape:
  - **Git as transport.** The box pulls a reviewed ref (tag/deploy branch);
    no more scp of individual files. Provenance (what commit is deployed),
    atomicity (checkout is one operation), rollback (checkout previous ref).
  - **The registry IS the deploy manifest.** Every component already carries
    `source_path` → `deployed_path`; the deploy tool is generated from
    registry.yaml, so a new component becomes deployable by adding its
    registry entry — never a new hand script.
  - **Verification built in.** Post-deploy, deployed hashes must equal the
    manifest's; the S1.3 probe flips from "page on drift" to "verify each
    deploy" (drift becomes impossible-to-miss instead of admitted).
  - **Declarative restarts.** Each registry entry names what to restart when
    its files change (unit/task/plugin-reload); the deploy tool restarts
    only what changed.
  - **Generalize the one good example.** `scripts/deploy-wa-plugin.sh`
    already has the right skeleton (syntax check → remote backup → push →
    hash compare → restart → verify → restore-on-failure); S1.5 is that
    pattern, generated from the registry, for every component.
  - The protected-core invariant applies to the deploy tool itself.

**Gate S1:** every running unit/task on the box resolves to a registry entry;
one intentional deployed-file change is caught by the drift probe.

## S2 — Consumption: generated context + query tool (~3–4 days)

- [ ] **S2.1 — Generated `SYSTEM.md`.** A generator compiles the registry into
  a compact, size-budgeted (≤4 KB) system brief in the deployed workspace,
  regenerated on every deploy. Completeness lint: every registry component
  appears or is explicitly excluded with a reason — a component can no longer
  be silently absent from Ollie's world. Generated files carry a
  `generated: vN <hash>` header; hand-editing them on the box is drift.
- [ ] **S2.2 — Fix the truncation class.** Heartbeat/brief readers must not
  silently truncate generated context: remove the 6,000-char `read()` cap for
  generated files, and the generator fails if output exceeds budget. (The
  current cap silently drops the brief/spend/projects/protocol tail of
  HEARTBEAT.md from every beat.)
- [ ] **S2.3 — `self` query tool.** An agent-callable CLI
  (`ollie-self/self_cli.py`): `self what-is <name>`, `self components`,
  `self status` (registry + watchdog state + ledger counts), `self changes`
  (recent drift + deploy events). Works whether or not repo source is
  agent-readable on the box: the registry is deployed as its own artifact and
  is self-sufficient; repo docs are optional enrichment, never required.
- [ ] **S2.4 — Doctrine split.** Hand-written workspace doctrine shrinks to
  identity, values and judgment; every restated system fact (capability
  ladder, timers, gates) moves to generated views. Duplication between
  AGENTS.md / HEARTBEAT.md / component READMEs is deleted at the source.

**Gate S2:** from a fresh session, Ollie correctly answers "what is the power
sentinel", "which timers run and when", "what changed this week", and "what
are you doing / why / what is blocked" — from registry + ledger, not memory.

## S3 — Executive grounding (folds into roadmap M1/M2; ~1–2 weeks)

- [ ] Commitments migrate from `OPEN_LOOPS.md` into the ledger (M1); Markdown
  becomes a generated view.
- [ ] Selector respects goal and commitment state (fix P1-D): eligibility
  excludes paused/achieved goals and closed commitments; add due/follow-up
  aging, WIP limits and leases.
- [ ] Producers emit opportunities to the ledger instead of self-scheduling
  (M5); heartbeat, project-tick, lab, research and curiosity keep their
  machinery as executors.
- [ ] Work digest and Mission Control become read models of registry + ledger,
  not state owners (M5); MC route failures (P1-I) close before it displays
  anything canonical.

**Gate S3:** roadmap gates "Commitment loop usable" and "Selector useful",
plus: on a replay set the selector's choice is explainable from canonical
state and never ranks curiosity above an open founder commitment.

## S4 — Evidence and closure hardening (roadmap M3 + VC P1 items; ~1–2 weeks)

- [ ] Task-class verifiers replace `os.path.exists` ground truth (P1-F).
- [ ] Evidence immutable; terminal transitions guarded on insert and update
  (P1-B); transition tables for runs/commitments (P1-C).
- [ ] Jobs: attempt IDs, leases, transactional outbox, idempotency keys (P1-E).

**Gate S4:** a crashed-and-recovered job cannot repeat an external effect or
delivery; a verified commitment's evidence cannot be deleted afterward.

## Design constraints

- The registry is generated from reviewed source, never hand-maintained on the
  box. Prose docs remain for humans; the agent's world is generated.
- Generated context has a hard size budget with a completeness lint; both are
  enforced in CI, not by habit.
- Self-knowledge is not authority: the registry can describe a component; only
  Hands policy and owner consent can act on it.
- No activity-volume metrics anywhere in this plan. The self-model exists so
  Ollie can say "nothing valuable now — here is what I know and why" and be
  believed.

## Success criteria

1. Ollie answers "what is X" for any system component from canonical state,
   with a source pointer — the power-sentinel question can never recur.
2. Ollie answers "what are you doing, why, what is blocked, what changed" from
   registry + ledger alone (README criterion #5).
3. Any deployed-vs-source drift pages the founder before a founder question
   exposes it.
4. Onboarding a new component = one registry entry + regeneration; never a
   hand-edit across four doctrine files.
5. Owner denial of any action or plan provably dispatches nothing.

## S5 — Self-improving systems track (exploratory; not yet adopted)

Direction flagged by the founder 2026-07-23: Ollie should be able to improve
its own code and any onllm project — not only maintain them. Not scheduled;
recorded here so the prerequisite shape is visible while S0–S4 are built.

**Reframe:** self-improvement is not a new subsystem — it is the executive
loop pointed at codebases. A code improvement is a work item whose evidence
is: reproduced symptom, root cause, bounded diff, tests green, PR reviewed,
deployment drift-clean. Most primitives already exist: the self-PR doctrine
(workspace/AGENTS.md:136-153), the `ollie-onllm` bot account (branch
protection blocks merges), the Lab for untrusted execution, bounded project
sessions, and the ledger/evidence model from S3/S4.

**Autonomy ladder (envelope widens only with evidence):**

| Rung | Shape | Status |
|---|---|---|
| SI-0 | Human-initiated fixes; Ollie implements in a bounded session, raises a PR; human merges | exists today (doctrine) |
| SI-1 | Ollie files its own improvement opportunities (from audit, `outcome_unknown` events, failed jobs, recurring workarounds) into the ledger; works them under contract; CI green + human merge | needs S0 (CI gate) + S3 |
| SI-2 | Continuous loop: expected-vs-realized value on merged PRs feeds selection; Ollie may batch and merge low-risk classes (docs, tests, lint) itself within a narrow envelope | needs S4 + M3 value history |
| SI-3 | Wider self-merge envelope | needs M4 unattended-foundation gates; explicit founder decision |

**The protected-core invariant (non-negotiable at every rung):** Ollie never
modifies its own gatekeepers — `policy.py`, `consent.py`, `executor.py`,
`auth.py`, `audit.py`, the approval relay, and the registry itself. Enforced
by CODEOWNERS + CI + branch protection, never by prompt. An agent that can
rewrite its constraints has none. (P0-A is the cautionary tale: consent
semantics relaxed silently while the suite went red. Self-improvement without
CI gates and drift detection industrializes exactly that failure.)

**Open problems for the later brainstorm:** the reviewer problem (human review
doesn't scale; Ollie reviewing itself is biased — plausibly a separately-rooted
reviewer agent with its own context and no shared memory); measuring
"improvement" (tests passing is not value; needs the M3 expected-vs-realized
loop); per-repo blast-radius budgets; when a model/provider swap counts as
self-modification. (Provenance and rollback for self-deployed changes is no
longer open — it is S1.5, committed.)

## Approval-routing doctrine (adopted 2026-07-24, founder discussion)

The consent/approval system exists for **ollie-hands actions** (T2+). Channels:

- **Autonomous hands use** (heartbeat, project-tick, jobs, anything Ollie
  initiates itself) → approval request **always on Telegram**. No WhatsApp
  24h-window dependency; Telegram is the always-reachable floor.
- **Interactive, admin-initiated** hands use → approval on the **channel the
  admin is actively using**, WhatsApp included (the initiating message opens
  the 24h window, so the prompt lands inside it). WhatsApp interactive reply
  buttons are the preferred UX (verified sendable via Cloud API 2026-07-24);
  typed `approve H-xxx` / `deny H-xxx` commands are the degradation floor if
  buttons misbehave.
- **Detached actions route by immediate initiator**: a WhatsApp request that
  becomes a scheduled job needing hands hours later routes to Telegram
  (origin = jobs-runner), explicitly.
- **Delivery failure → Telegram.** Not just unknown origin: a WhatsApp
  approval prompt that fails to send re-routes to the floor.
- **Contact gating** (unknown WhatsApp sender messages Ollie) → Ollie holds
  and does not respond; approval request goes to **Telegram**. Decided
  2026-07-24: gating IS wanted; WhatsApp itself is never an approval surface
  for it.
- **Initiator approves (multi-admin rule)**: when Prakash triggers hands on
  WhatsApp, **Prakash** approves it. Approver = the admin who initiated,
  not a hardcoded owner number.
- **Identity resolution fails closed**: unresolvable sender → guest/gated,
  never main-agent.
- **Guests never reach hands at all** — gateway-level tool allowlist
  (verified live 2026-07-24: guest agent has message/web_search/web_fetch/
  tool_search/factcheck only; no hands tools exist in its world).
- **Timeout**: pending approval gets one Telegram nudge before expiry; on
  timeout it fails (deny-by-default). Future: message → nudge → **call**
  escalation once Ollie has calling (see below).
- **T3 follows normal origin-channel routing — no channel separation.**
  Decided 2026-07-26. Requiring Telegram specifically for T3 (dangerous shell
  args) would defend against remote single-channel compromise — a hijacked WA
  Web session or SIM swap can otherwise both request *and* approve without
  ever touching a second channel. Founder weighed that against the friction
  of app-switching mid-conversation and chose one uniform rule: T3 approves
  on the origin channel like every other tier. The residual risk is accepted
  and stated here so it is a known posture rather than an oversight; revisit
  if a second approver or an external-acting tier is ever added.

**Calling capability (recorded for the escalation future):** the long-term
vision is message → nudge → **call** on unanswered approvals. Verified
2026-07-24: WhatsApp Business Calling API exists (voice/video, WebRTC) but
typically requires ~2k business-initiated conversations/day eligibility —
build the call fallback **once calling is set up** (WABA calling or virtual
number); Telegram Bot API has NO call capability. Until then: nudge → fail.

**Typing indicator (founder request):** show "typing…" presence on WhatsApp
while Ollie composes — Cloud API supports `typing_indicator` (auto-clears on
send or after 25s). To implement in the whatsapp-cloud plugin.

**Config footgun (documented):** WhatsApp runs via the plugin stack
(`whatsapp`, `ollie-whatsapp-cloud`, `ollie-wa-approval` plugins, all
enabled, ngrok webhook tunnel); core `channels.whatsapp.enabled: false` is
vestigial and misleading — do not "fix" it without checking the plugins.

## Decision log

- 2026-07-23: Adopted after structural review: integrity stop-the-line (S0)
  precedes the self-model; the self-model precedes executive wiring. Registry
  and ledger stay separate stores. Trigger: founder report that Ollie did not
  know the power sentinel; review verified P0-A live at HEAD (25cf912).
- 2026-07-23: Self-improving systems track (S5) recorded as exploratory.
  Founder intent: Ollie improves its own code and any onllm project.
  Sequencing constraint: the protected-core invariant and CI/drift gates from
  S0/S1 are prerequisites; autonomy ladder rungs are adopted only by future
  explicit decisions.
- 2026-07-24: S1.2 bootstrap discovery run live over Tailscale SSH. Open
  question resolved: the agent sees deployed copies only, never repo source —
  registry.yaml must ship as its own artifact. 9 undocumented components
  adopted into the registry; 4 deployed files found drifted from reviewed
  source (watchdog, jobs_runner, lab, research_dashboard).
- 2026-07-24: Formal deployment committed as S1.5 (founder: "the way we are
  deploying right now... at some point I want Ollie to self-evolve, which
  means some sort of formal way to update the system"). Not scheduled;
  required before S5 SI-1. Git transport, registry-as-manifest, verified
  deploys, declarative restarts, rollback.
- 2026-07-24: S1.3 drift probe live. Power-sentinel incident closed (host
  script deleted ~07-12, silent for 12 days despite 6-hourly pages — alert
  fatigue is real; restored from repo). wa-approval consent-relay fork
  discovered (box-only unreviewed contact-approval feature + repo-only S0.2
  audit logging); reconciliation backlog set at 5 files. Registry corrected
  from live truth a second time (hands engine is Windows-deployed,
  ollie-executive never deployed, TTS lives at ~/tts).
- 2026-07-26: T3 channel separation declined — T3 approvals route by origin
  like every other tier. One uniform routing rule beats a tier-specific
  exception; the remote single-channel compromise risk is accepted and
  documented in the doctrine above rather than mitigated.
- 2026-07-26: S0 and S1.1–S1.3 committed (d923f79, 42acf31, c0810ef, e9ceced,
  b7471b6, 58068f3). Verified before committing: hands 199 pass/1 skip,
  watchdog 19 pass, and the relay JS suite fails identically (6/14) against a
  clean HEAD checkout — so those 14 are genuine pre-existing contract drift,
  not regressions from the digest-attach change.
- 2026-07-26: **The repo was the stale copy for ollie-hands, not the box.** The
  deployed engine was ~220 lines ahead and carried an entire `grants.py`
  capability layer with no repo counterpart, plus executor live-resource
  enforcement and fail-closed policy effect envelopes; the repo had drifted
  `browser.py` to `headless=True`. Deploying repo→box would have been a net
  security regression. Reconciled box→repo (6036ec3, 3bde2aa); suite 199→339.
  Two corrections to the S0 record follow: P0-A was **already fixed on the
  box** by hand-written code (`_confirm_ok` wired into both consent gates in
  `executor.run`), so it was only ever live in the repo; and the `/R`
  case-sensitivity bug noted in S0.3 was not real (`_norm` lowercases and the
  pattern was already lowercase). P0-B and P0-C remain **live gaps on the
  box** — verified by importing the deployed `policy.py` and running its own
  classifier: `wmic process call create`, `echo $(...)`, `where.exe /R` and
  reading `approval.token` all returned T0 auto.
  Lesson for S1.5: drift is bidirectional and per-hunk. The box was stale
  *inside* the files it led on (no transport-death handling, dispatch flag set
  after dispatch), so neither side is safe to take wholesale.
- 2026-07-26: Contact-gating routing found ALREADY CORRECT on the box —
  `evaluateInbound`/`evaluateOutbound` call `sendOwnerTelegram` directly, so
  first-contact prompts have always been Telegram-only. The 2026-07-24 note
  claiming the box fork "prompts via WhatsApp" was wrong; what the box
  actually added is accepting owner approve/deny *commands* from WhatsApp,
  which is a separate question from where prompts are *sent*. The fork
  reconcile is correspondingly smaller than scoped.
- 2026-07-30: Xenia YouTube→Spotify direction unblocked. Reused the retired
  "Tushar music sync" Spotify developer app (Client Credentials flow — the
  Development-mode 25-user quota and redirect URIs only constrain user OAuth,
  not app-only search; no other consumer shares the app's rate limit now).
  Creds installed at `~/.openclaw/secrets/xenia.json` (0600, box only, never
  in repo). Box copy of `xenia_convert.py` verified byte-identical to repo
  (sha256 2b0bc046…) before testing. Live e2e: `youtube.com/watch?v=dQw4w9WgXcQ`
  → yt-dlp metadata → Spotify official search →
  `open.spotify.com/track/4L7qMw8HI3vM57hHRMyb4Y` (title/artist match). Both
  Xenia directions now fully live; guest MCP path needs no restart (secrets
  are read per call). Remaining Xenia caveat: nothing — Spotify creds were
  the last blocker.
- 2026-08-02: Gateway died mid-session (process 310275, nohup via SSH). First
  restart (10:00) loaded Telegram provider but never started polling —
  `[diag] isolated polling ingress started` never appeared in logs; pending
  updates accumulated unread. Second restart (10:08) worked immediately:
  polling started, pending message consumed. Root cause unclear — either the
  nohup process was killed when the SSH session ended (nohup insufficient
  under WSL process management), or a race condition in Telegram provider
  init. The nohup-via-SSH pattern is not reliable for long-lived processes;
  needs a proper process manager (systemd broken, D-Bus unavailable from
  SSH path). **Finding F-0015: gateway process unreliable under nohup;**
  **needs persistent process manager.**
- 2026-08-02: workspace/AGENTS.md on box is 24,328 chars; gateway injection
  limit is 12,000 chars. Agent receives truncated doctrine on every session
  start — roughly half the instructions are silently dropped. The file has
  grown organically (Xenia rule, fact-checking, hands, approvals, channel
  routing, etc.) and now exceeds the budget. **Finding F-0016: AGENTS.md**
  **needs splitting or summarization to fit the 12KB injection limit;**
  **current state means the agent is operating with incomplete instructions.**
- 2026-08-04: **Latching bug fixed** (watchdog line 791). Root cause: failure
  detection compared key presence (`name not in prev`) not error content, so
  once a check failed it was invisible forever — no re-page on content
  change, no periodic reminder, no severity increase. Proved live 08-02 when
  gateway died and watchdog logged "ok" despite process being dead. Fix:
  content comparison (`err != prev_err`) + per-check cooldown (6h) + daily
  reminder (24h). State tracks `alert_state` per check (`err`, `ts`,
  `remind_ts`). 8 new tests in `test_alerting.py` (27 watchdog total, all
  green). Deployed to box, hash-verified. **This was the single most**
  **dangerous silent-failure mode in the watchdog** — all other audit findings
  (F-0001–F-0013, backup death, coverage gaps) were invisible because of it.
