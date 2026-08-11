# Project Tier — Ollie handles projects end-to-end

## Context

Today Ollie has three work granularities: chat turns (seconds), background
jobs (≤25 min, one-shot), and lab POCs (one night, disposable). The Jarvis
vision needs a fourth: **projects** — multi-day efforts with planning,
implementation, verification, and delivery, owned end-to-end ("build the
link-shortener", "migrate the watchdog to X", "ship feature Y on 4DPocket").

The core constraint shaping everything: **LLM sessions are bounded and
forgetful; projects are long and stateful.** So a project NEVER lives inside
a session. A project lives **on disk**; sessions are interchangeable workers
that pick it up, advance it one increment, and write everything back. (Same
durable-state pattern that already works for heartbeat + lab + open-loops.)

Harness decision feeding in: aider stays the in-session code grinder; Codex
CLI is blocked on OpenAI's Responses-only lock-in (revisit when ongateway
ships a Responses→chat bridge — also a product opportunity).

## The shape: PM loop, not a long session

```
            ┌──────────────────────────────────────────────┐
            │ workspace/projects/<slug>/   (durable truth)  │
            │  PROJECT.md  PLAN.md  JOURNAL.md  state.json  │
            │  inbox.md    repo → git branch ollie/<slug>   │
            └──────────────────────────────────────────────┘
                 ▲ read at session start   │ written at session end
                 │                         ▼
  tick timer ─► project_tick.py ─► silent job: ONE work session (≤45 min)
  (mechanical: which project,        agent = worker: follow PLAN, do next
   budget left, box healthy?)        increment, verify, journal, protocol out
                 │
                 ▼ protocol
   CONTINUE | MILESTONE: <msg> | BLOCKED: <question> | DONE | FAILED: <why>
                 │
                 ▼
   Telegram (milestones/blockers/done) · morning brief (status lines)
   Tushar replies in chat → chat-Ollie updates inbox.md/state → unblocked
```

Ollie-as-PM is split across THREE roles, each already proven elsewhere:
- **Scheduler** (mechanical, no LLM): `project_tick.py` — like `lab_watcher`.
- **Worker** (LLM, bounded): one session per tick — like a lab POC job.
- **Reviewer/owner** (LLM): heartbeat flags stale/blocked projects; chat-Ollie
  handles Tushar's answers and scope changes — like open-loops duty.

## Project anatomy (`workspace/projects/<slug>/`)

| File | Role | Written by |
|---|---|---|
| `PROJECT.md` | Charter: goal, scope, constraints, **definition of done**, stakeholder decisions log | chartering session; chat-Ollie on scope change |
| `PLAN.md` | Living plan: phases → tasks with `[ ]/[x]/[~]` + notes | worker sessions |
| `JOURNAL.md` | Append-only session log: did / verified / next / blockers — the project's memory | every worker session |
| `inbox.md` | Messages INTO the project (Tushar's answers, new asks) — consumed at session start | chat-Ollie |
| `state.json` | Machine state: `status` (chartered/active/blocked/review/done/archived), sessions_used today/total, last_session, priority | runner + chat-Ollie |
| `repo` | The actual work: git repo/worktree, branch `ollie/<slug>` (self-PR doctrine: PRs only, never main) | worker sessions |

Trusted, Ollie-authored code runs on the gateway (he has shell + gh).
Anything incorporating third-party untrusted code goes through `lab`
containers (existing doctrine) — projects don't relax the sandbox rule.

## Lifecycle

```
proposed → chartered → active ⇄ blocked → review → done → archived
```

- **proposed**: from Tushar in chat ("Ollie, project: …") or lab graduation
  (🌟 POC → pitch in brief). Chat-Ollie scaffolds the dir + draft charter.
- **chartered**: Tushar approves the charter (scope + definition-of-done) in
  chat. No charter, no ticks — prevents runaway scope. Cheap approval: he
  replies "approved" on Telegram/WhatsApp.
- **active**: tick-eligible. Worker sessions advance it.
- **blocked**: a session ended `BLOCKED: <question>` → one Telegram ping,
  no more ticks until inbox.md gets an answer (chat-Ollie writes it, flips
  status back to active). No re-ping spam; heartbeat brief lists blocked
  projects daily.
- **review**: definition-of-done met → `DONE` protocol → Telegram with the
  deliverable (PR link / artifact) → Tushar verdict in chat: done or back
  to active with feedback in inbox.md.
- **archived**: dir moves to `projects/_archive/`; JOURNAL is the record.

## Session protocol (strict, fail-closed — heartbeat pattern)

Worker prompt = charter + plan + journal tail (~last 3 sessions) + inbox +
"advance this project by ONE meaningful increment, verify it, write back".
Final output line must be one of:
`CONTINUE` · `MILESTONE: <1-line>` · `BLOCKED: <question>` · `DONE` ·
`FAILED: <why>`. Malformed → treated as `FAILED(protocol)`; two consecutive
protocol failures → project auto-blocked + flagged (a confused model must
not grind a project into mush).

**Session contract (in the prompt):**
1. Start: read inbox.md (consume + clear), re-verify last session's claim
   cheaply (run the tests yourself — trust-but-verify across sessions).
2. Work ONE increment from PLAN.md. Use aider for grindy code subtasks.
3. Every increment ends verified: tests/run output quoted in JOURNAL.md.
   "It should work" is not a journal entry.
4. End: update PLAN checkboxes, append JOURNAL entry (did/verified/next),
   commit granularly on `ollie/<slug>` (no co-author lines), emit protocol.

## Scheduling & budgets (mechanical, in `project_tick.py`)

- Tick timer: every 2h, 07:00–23:00 box time (projects work daytime;
  nights belong to lab POCs and Dreaming — keeps load + spend legible).
- Per tick: at most ONE session, for the highest-priority `active` project
  with budget left. Defaults: **4 sessions/project/day, 2 active projects
  max** (configurable in state.json / a lab-style consts block).
- Gates before submitting (reuse lab CLI patterns): jobs queue idle?
  box memory headroom >3GB? disk <85%? MiMo reachable? else skip tick + log.
- Session = silent background job (existing runner) with per-job timeout
  override `"timeout_s": 2700` — ONE small jobs-runner change (currently
  hard 1500s).
- Staleness: heartbeat flags any active project with no session in 48h.

## Reporting

- **Telegram (proactive)**: MILESTONE / BLOCKED / DONE only — the events
  worth a ping. Never per-session noise.
- **Morning brief**: `— projects —` one line each: `slug: status, last
  increment, next step, sessions used`. Same headline-only contract as lab.
- **Drill-down**: "how's <project>?" in chat → chat-Ollie reads JOURNAL/PLAN
  and answers (doctrine addition).

## Build phases

**Phase 1 — single-project MVP**
1. `ollie-jobs/ollie_project_tick.py` + `ollie-project-tick.{service,timer}`
   (clone lab-watcher unit pattern): pick project, gates, budget, submit
   session job, parse protocol from job result, update state.json, Telegram
   on MILESTONE/BLOCKED/DONE (reuse `deliver()`).
2. Jobs runner: honor optional per-job `timeout_s` (one-line change).
3. `workspace/PROJECT_DOCTRINE.md`: session contract + protocol (worker
   prompt template lives here, like HEARTBEAT.md does for beats).
4. `workspace/AGENTS.md`: project duties for chat-Ollie (scaffold proposals,
   write Tushar's answers to inbox.md, flip blocked→active, handle "pause/
   kill/status" commands). `HEARTBEAT.md`: brief section + staleness flag.
5. Scaffold `projects/` tree + `_archive/`.

**Phase 2 — the loop hardening**
6. Lab graduation path (🌟 POC → proposed project with the lab note as
   charter seed). Priority ordering between 2 active projects.
7. Review-tier polish: DONE delivers PR links; Tushar feedback → inbox.md
   round-trip verified.
8. Watchdog: project-tick freshness check (lab-watcher pattern).

**Phase 3 — later**
9. Parallel worker sessions (needs per-project lock; not before MVP proves
   the loop). ongateway Responses bridge → Codex CLI as optional heavy
   harness. Spend-aware scheduling off provider usage APIs.

## Files

| Action | Path |
|---|---|
| new | `ollie-jobs/ollie_project_tick.py`, `ollie-jobs/ollie-project-tick.{service,timer}` |
| new | `workspace/PROJECT_DOCTRINE.md` |
| edit | `ollie-jobs/ollie_jobs_runner.py` (per-job `timeout_s`), `workspace/AGENTS.md`, `workspace/HEARTBEAT.md`, `ollie-watchdog/ollie_watchdog.py` (P2) |
| box | `workspace/projects/{,_archive/}` |

Reuse: silent jobs + `deliver()` (`ollie_jobs_runner.py`), protocol-parse +
fail-closed pattern (`ollie_heartbeat.py:parse_outcome`), unit pattern
(`ollie-lab-watcher.*`), health gates (`ollie-lab/lab` spawn checks),
self-PR + lab doctrines (AGENTS.md), aider-in-container (lab).

## Verification (pilot project)

1. Charter a small REAL pilot with a crisp definition-of-done (Tushar picks;
   candidate: a small onllm utility with tests + README, delivered as a PR).
2. Watch ≥3 worker sessions advance it: PLAN checkboxes move, JOURNAL grows,
   commits land on `ollie/<slug>`, each increment's verification quoted.
3. Force a BLOCKED round-trip: charter omits one decision on purpose →
   Ollie asks on Telegram → answer in chat → inbox.md → next session uses it.
4. MILESTONE ping arrives; per-session noise does NOT.
5. DONE → PR link on Telegram → review verdict round-trip.
6. Kill-switch: "pause project <slug>" in chat stops ticks within one cycle.
7. Budget: verify no more than 4 sessions/day; tick skips when box busy.

## Failure modes

| Failure | Behavior |
|---|---|
| Worker session times out / crashes | job FAILED → tick marks protocol-failure; 2 consecutive → auto-block + flag |
| Model lies about progress | next session re-verifies before building on it; journal requires quoted output |
| Scope creep | charter is the contract; scope changes only via Tushar → PROJECT.md decisions log |
| Runaway spend | session budget + daytime window + box gates; MiMo credit check in tick (P2) |
| Project rot | 48h staleness flag in brief; archived if Tushar says kill |
| Two projects fight for attention | priority field; 2-active cap; tick is serial |
