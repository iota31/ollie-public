# Spend Governance — how Ollie manages his own resource use

## Context

Ollie now has multiple autonomous spenders hitting one finite model plan
(MiMo 3B credits / month, rotating): the **heartbeat** (every 30 min,
always on), **lab research jobs**, **lab POCs** (container + harness),
and **project work sessions**. Trigger incident (2026-06-11): the lab
produced ~15 research notes in a night against a prompt-stated budget of
"2/day" — proving prompt-level budgets do not hold. Tushar wants spend
governed by **usefulness-to-him**, with **himself as the grader**.

## Critical reframe (where I disagree with the original brief)

The brief was "tie tokens to a productivity function." I think building
that now is premature and partly misdiagnoses the problem:

1. **The urgent risk is RUNAWAY, not inefficiency.** With 3B credits the
   problem isn't that Ollie spent "too much" on 15 useful-ish notes — it's
   that *nothing mechanical stops a bug / prompt-injection / confused model
   from burning the whole plan in hours*. That risk is solved by a boring
   **hard ceiling**, not a clever value function. Ship the boring thing
   first; it fully addresses the actual danger.

2. **The value signal is too sparse and laggy to drive a controller.**
   Tushar is a busy founder; realistically he engages with 1–2 lab outputs
   a week. A budget controller fed near-zero feedback most days will simply
   sit at its floor — i.e. an elaborate machine that approximates a flat
   cap. We'd be over-engineering.

3. **Engagement ≠ usefulness (Goodhart on the proxy).** If budget keys off
   "what Tushar drills into," Ollie drifts toward *attention-grabbing*
   output (novelty, sensational findings) over boring-but-valuable work.
   The proxy diverges from the target. The earlier claim "Tushar can't be
   gamed" was wrong: the *gradee can game the grader's attention*.

4. **You can't manage what you don't measure — and we don't measure.**
   Jobs/sessions don't record token (or even cost-proxy) usage today. Every
   "productivity/ROI function" is fiction until spend is instrumented.
   Measurement is the real unlock and is independently valuable.

5. **The likely #1 spender isn't the lab at all — it's the heartbeat.**
   48 beats/day, each a reasoning-model call on a large context (HEARTBEAT
   + OPEN_LOOPS + jobs + lab inbox + beat log). That steady drip probably
   dwarfs a handful of research notes. Capping the lab while ignoring the
   always-on loop would be optimizing the wrong thing. **Measure before
   throttling.**

**Correct sequencing:** (0) hard ceilings now → (1) measurement now →
(2) manual, data-informed tuning as the real v1 of "grade by usefulness" →
(3) automated earned-trust ONLY if accumulated data later proves the signal
supports it. The sexy controller is Phase 3, gated, maybe never.

## Architecture: one spend ledger, tiered governance

A single source of truth all spenders read/write, so governance is holistic
(one plan, all lanes) rather than siloed per feature.

```
budget.py  (shared module on the box, imported by every spender)
  ├─ check(lane) -> (ok|deny, reason)   # mechanical gate, pre-spend
  ├─ record(lane, item, cost, model)    # append to spend-ledger.jsonl
  └─ reads  budget-config.json (the dials Tushar/▒ tune)
            spend-state.json   (today's per-lane counters)

Spenders that call it: ollie_heartbeat.py, job-submit.sh (research+POC),
ollie_project_tick.py.  Lanes: heartbeat | research | poc | project.
```

- `spend-ledger.jsonl` — append-only: `{ts, lane, item, cost, model, outcome?}`.
  `outcome` backfilled later when a value event references the item.
- `budget-config.json` — the tunable dials: per-lane daily ceilings, global
  daily ceiling, floor. Edited by Tushar (via chat-Ollie) — these ARE the
  grades.
- `spend-state.json` — rolling daily counters; reset on date change.

## Phase 0 — Hard ceilings (mechanical, build first)

The safety net. Enforced in the dispatch path, immune to model state.

- `ollie-jobs/budget.py`: `check(lane)` returns deny when today's lane count
  ≥ ceiling OR global units ≥ global ceiling; `record(...)` increments +
  appends to the ledger. Pure stdlib, atomic writes (the `state.json` /
  `epochs.json` pattern already in the repo).
- Wire `check`/`record` into: `job-submit.sh` (refuse research/POC over cap,
  with a logged reason — the submitter sees "budget: research cap reached"),
  `ollie_project_tick.py` (already has per-project/day; add the shared
  record so it shows in the unified ledger).
- Default ceilings — sized so that **even every lane maxed can't burn >~5%
  of the monthly plan in a day** (compute from the measured per-action cost
  in Phase 1; until then use conservative counts): research ≤6/day,
  poc ≤2/day, project sessions ≤6/day total, global self-directed units/day
  bounded. Floor (always-allowed) baked in so Ollie is never fully starved.
- Heartbeat is timer-bounded (48/day fixed) so it needs no count cap — but
  see Phase 1 for trimming its cost.

## Phase 1 — Measurement + visibility (build with Phase 0)

You cannot tie spend to value without seeing spend.

1. **Discover the cost signal.** Investigate whether the openclaw CLI agent
   exposes token usage (trajectory `.jsonl` / session `.json` / gateway
   log). If yes → record real tokens. If not → record a proxy: model tier
   used × wall-clock duration × a per-tier weight. Be honest in the ledger
   which it is.
2. **Heartbeat audit (do this first — likely the biggest cost).** Measure a
   week of beat cost. Cheap wins to evaluate: back off cadence in quiet
   hours (e.g. 30→60 min overnight), trim the beat prompt, or have trivial
   "nothing changed" beats short-circuit before the full context load.
3. **Weekly spend digest** (rides the morning brief, one section): spend by
   lane, what each produced, and — paired with the value events below —
   what Tushar engaged with. Pure visibility, no auto-control. This alone
   lets Tushar govern by adjusting dials.
4. **Value-event capture** (the grading substrate, low-friction): chat-Ollie
   logs a `value:` line to the ledger when Tushar's natural behavior signals
   usefulness — drilled in, acted on, "useful/junk", greenlit a POC. NO new
   ritual required of Tushar; silence is neutral, never punished.

## Phase 2 — Manual, data-informed tuning (the real v1 of "I grade by usefulness")

Human-in-the-loop, robust against sparse signal and Goodhart.

- Weekly, Tushar reads the digest and adjusts `budget-config.json` dials
  ("more lab, cap research at 3, projects get more") — via chat-Ollie, who
  edits the config. **The dials are the grade.** Low-friction, occasional,
  and the controller is a human, so it can't be gamed or destabilized by
  lag/sparsity.
- **Auto-bonus for TERMINAL, unfakeable signals only:** a POC graduated into
  a project, or a project PR Tushar merges → a small automatic ceiling bump
  for that lane. These events are unambiguous and dense enough to trust;
  fuzzy engagement is left to the human dial.

## Phase 3 — DEFERRED: automated earned-trust controller

The credit-balance design (floor + balance that accrues on value events,
decays on silence, clamps to ceiling). **Do NOT build until ALL gates pass:**
- ≥8 weeks of spend + value-event data exist.
- Measured engagement density is high enough to drive a controller (Tushar
  consistently generates ≥N value events/week — TBD from real data).
- Manual tuning (Phase 2) has proven too coarse or too annoying in practice.
- We can show, on historical data, the controller would have beaten the
  manual dials. If it wouldn't have, don't build it.

Documented now so the intent survives; built only if earned.

## Files

| Action | Path |
|---|---|
| new | `ollie-jobs/budget.py` (gate + ledger + config reader) |
| new (box) | `workspace/budget-config.json`, `~/.openclaw/logs/spend-ledger.jsonl`, `spend-state.json` |
| edit | `ollie-jobs/job-submit.sh` (check/record on research+poc), `ollie-jobs/ollie_project_tick.py` (record + shared gate), `ollie-jobs/ollie_heartbeat.py` (record beat cost; cadence/prompt trim per audit), `workspace/HEARTBEAT.md` (weekly spend digest in brief; value-event logging duty in AGENTS.md), `workspace/AGENTS.md` (chat-Ollie: log value events, edit dials on request), `ollie-watchdog/ollie_watchdog.py` (alert if a single day's spend > catastrophe threshold — the backstop's backstop) |

Reuse: atomic-write state pattern (`epochs.json`, lab `state.json`),
mechanical-gate pattern (`ollie-lab/lab` spawn checks), silent-job +
`deliver()` (jobs runner), brief-section pattern (HEARTBEAT.md).

## Verification

**Phase 0**
1. Set research ceiling to 2, submit 3 research jobs → 3rd refused with a
   logged budget reason; ledger shows 2 recorded.
2. Watchdog catastrophe alert: simulate a day-spend over threshold → Telegram
   alert fires.

**Phase 1**
3. Confirm the ledger records real tokens (or honest proxy) per action across
   all four lanes after a normal day.
4. Heartbeat audit produces a real per-beat cost number; quiet-hours back-off
   measurably cuts daily beat spend with no missed real events.
5. Morning brief shows the spend digest; numbers reconcile with the ledger.

**Phase 2**
6. Tushar says "cap research at 3" in chat → config updated → next day's gate
   enforces 3.
7. Merge a project PR → that lane's ceiling auto-bumps by the configured step;
   ledger shows the terminal-signal event.

## Failure modes

| Failure | Behavior |
|---|---|
| Runaway loop / injection | hard ceiling + watchdog day-spend alert cap the blast radius |
| Token usage not exposed by CLI | fall back to documented proxy (tier×duration); ledger marks which |
| Sparse grading | Phase 2 is human-tuned + neutral-on-silence; never starves below floor |
| Goodhart / attention-chasing | engagement is NOT an auto-budget input in v1; only terminal signals are |
| Plan rotation (new key/limits) | ceilings are config; re-sized when the plan changes (open-loop already tracks renewal) |
| Heartbeat dominates cost silently | Phase-1 audit surfaces it before we mis-optimize the lab |
```
