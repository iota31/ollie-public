# Ollie Cofounder Roadmap

This is the durable implementation roadmap following the July 2026 holistic
review of the repository and the available Telegram history. Its purpose is to
keep the immediate Hands repair connected to the larger product objective.

## Product objective

Ollie should increase the effective capacity of the founding team: understand
the team's goals, select valuable work proactively, execute safely, verify the
result, remember commitments, and report outcomes without requiring Tushar to
act as its scheduler, verifier, or recovery system.

## Holistic course-correction assessment

**Verdict:** the project is pointed in the right direction, but implementation
has grown outward faster than the product's executive core. This is not a
rewrite situation. The useful primitives are real; the correction is to put one
small, legible decision-and-closure loop above them and stop treating new
mechanisms as progress by themselves.

### Keep

- The WSL brain / Windows Hands separation. An untrusted model should not have
  ambient host access, and host policy should remain independent of the model.
- Hands as a standalone actuator with inert boot, in-code policy, audit,
  postconditions and an explicit capability ladder.
- Disk-backed project state and bounded, replaceable worker sessions. Long work
  should survive model/session boundaries.
- The lab sandbox for third-party code.
- Heartbeat, jobs, projects, briefs, monitors and memory as execution/reporting
  primitives beneath a future executive core.
- Evidence-based completion and fail-closed protocol parsing.

### Consolidate or simplify

- **Autonomous work origination:** heartbeat, curiosity, dream promotion,
  research feeds, lab and project tick each make local scheduling decisions.
  Convert them into producers feeding one opportunity queue; only one selector
  should decide what receives time and budget.
- **Commitment state:** `OPEN_LOOPS.md`, project state, job records, session
  memory and briefs overlap. Keep their useful views, but back promises and
  outcomes with one canonical commitment/outcome schema.
- **Channel logic:** Telegram and WhatsApp should be adapters into the same
  control path. Remove policy, approval and session behavior that exists only
  in one channel unless the transport truly requires it.
- **Deployment copies:** stop treating manually copied scripts as independent
  sources. Build/deploy from canonical repository revisions, record their hash,
  and page on drift.
- **Operating doctrine:** split durable invariants from changeable prompts.
  Avoid repeating the same rule across workspace instructions, plans, runtime
  copies and old status documents.
- **Status protocols:** jobs, heartbeat, projects, lab and research use adjacent
  but different outcome vocabularies. Standardize the envelope (`accepted`,
  `running`, `blocked`, `verified`, `failed`, `cancelled`) while retaining
  task-specific evidence.

### Correct now

- Timers and queue volume have been allowed to stand in for executive
  judgment. They should trigger evaluation, not automatically create work.
- Research notes and POCs too often terminate at “artifact produced” instead
  of a decision or shipped change. Require a graduation or stop decision.
- Promises can still live only in conversation. Every promise needs durable
  ownership and follow-up immediately, not eventual memory curation.
- Some documentation described intended safety properties as deployed facts.
  `ARCHITECTURE.md` now records the actual trust boundary and open gaps.
- Hands consent is not fully independent while `/consent` shares the ordinary
  caller bearer; consequence policy is inconsistent across browser/UIA/pixels;
  running plans do not recheck the global disabled state before every step; and
  retries may duplicate consequential actions. These are autonomy blockers,
  not backlog polish.
- Health checks and tests need to exercise real entry paths. A reachable
  process, a unit test, or a green watchdog is not sufficient evidence of a
  working founder-facing loop.

### Defer

- Native MXC integration unless a concrete requirement cannot be met by the
  present two-tier boundary.
- New grounding models, additional channels, broader CAPTCHA automation and
  more Hands verbs until existing rungs are reliable and policy-complete.
- Mission Control/dashboard polish until there is canonical state worth
  displaying.
- Learned reward models, reinforcement learning and automatic weight tuning
  until the team has a clean history of expected and realized outcomes.
- More general research/curiosity throughput. The system has enough ways to
  start work; it needs better selection and closure.

### Remove when encountered

- Claims that prompt instructions are hard security controls.
- Parallel deployed copies with no source/hash provenance.
- Dead prototype paths after their replacement is verified and rollback has
  expired.
- Metrics whose numerator is activity (jobs, notes, tokens, heartbeats) without
  a verified downstream outcome.

## Target topology

The target is one executive loop using the existing specialist machinery:

```text
founder goals + conversations + monitors + saved items + system health
                                  |
                                  v
                    opportunity / commitment inbox
                                  |
                                  v
          canonical goals -> priority selector -> work contract
                                  |
                 +----------------+----------------+
                 v                v                v
              quick job        project         lab/research
                 +----------------+----------------+
                                  v
                    tools / 4DPocket / Hands
                                  v
             task evidence -> outcome ledger -> founder brief
                                  |
                                  +--> expected vs realized value
```

The **work contract** is the handoff boundary between executive judgment and
execution. It contains: goal/commitment, expected value, bounded scope,
dependencies, cost/risk ceiling, consent requirement, stop condition, evidence
required, and the next follow-up time. Workers may decide how to execute inside
that contract; they may not silently widen it.

The executive selector should be deterministic and inspectable first. A simple
precedence/ranking rule plus a small exploration budget is preferable to an
opaque “reward function.” Telegram feedback and weekly founder labels calibrate
the score, while safety and truthfulness remain non-tradeable constraints.

## Current sequence

### M0 — Restore trustworthy Hands browser execution (in progress)

- [x] Reconstruct the July 1 Reddit incident from Telegram and host audit.
- [x] Establish that the failure is site-independent: the first browser `goto`
  succeeded, then browser operations failed with Playwright's sync-API/
  asyncio-loop error, including on `example.com`.
- [x] Establish that deployed `browser.py` and `server.py` match repository
  source; this is not a stale-deployment explanation.
- [x] Replace the incompatible synchronous Playwright/Camoufox lifecycle with
  a persistent async browser lifecycle (or isolated worker process).
- [x] Add a lifecycle regression:
  `goto -> screenshot -> extract -> second goto` through an async caller.
- [x] Deploy from the canonical source and verify hashes.
- [x] Run the live adversarial harness, including successive browser
  `goto -> extract` operations against an inert data page. All six safety traps
  held; the harness's final summary hit an unrelated Windows cp1252/emoji
  printing error after reporting the individual passes.
- [x] Run a longer neutral live-browser soak: repeated navigation/extraction,
  screenshot observation, multiple dummy form fills, submit inspection without
  submission, and recovery navigation all succeeded. Invalid selectors and an
  invalid radio-button `fill` escalated safely with precise errors.
- [ ] Retry Reddit only after neutral verification; distinguish Hands failure,
  CAPTCHA, verification, consent, and Reddit rejection as separate blockers.

2026-07-11 live retry root cause: Reddit loaded and Hands remained alive, but
the Playwright 1.60.0 Node driver exited while handling a Camoufox/Firefox
`PageError`. Its bundled handler unconditionally dereferenced
`pageError.location.url`; Reddit produced an error event without `location`,
causing a fatal `TypeError` and the subsequent Python-side message
`Connection closed while reading from the driver`. Audit evidence disproves a
generic one-action ceiling: `goto` and `screenshot` both succeeded before the
driver exited. Live Camoufox was 0.4.11 and declares an unversioned Playwright
dependency, while this repository used open-ended lower bounds.

Recovery checklist:

- [x] Select and lock a tested Camoufox/Playwright pair; do not rely on loose
  minimum versions.
- [x] Defensively patch the driver page-error handler so a missing
  location cannot terminate the transport.
- [x] Add a real Windows Camoufox deploy gate using a loopback form:
  `goto -> extract -> fill -> verify -> second fill -> verify -> status`.
- [x] Verify neutral navigation/extraction/fill and a Reddit pre-submit probe;
  cover deliberately terminated-driver recovery in deterministic unit tests.
- [ ] Rotate the exposed email credential at its provider.
- [x] Remove the accidental WSL `reddit_pw`; retain only the Windows DPAPI
  `reddit_pw` ref.
- [ ] Retry signup only after the above checks, stopping before final submit.

2026-07-11 qualification result: Camoufox 0.4.11 + Playwright 1.60.0 are now
exactly pinned and the installed Playwright bundle is patched fail-fast and
idempotently. The real Windows loopback gate passed. A real Reddit pre-submit
smoke then loaded the complete signup UI, found `input[name='email']`, filled a
reserved `.invalid` address, verified the live DOM value without returning it,
and kept the same driver/context alive. It did not click Continue, submit,
solve a CAPTCHA, or use credentials. Browser operations are serialized; dead
transports reset atomically; safe operations retry once, while clicks, typed
keystrokes, and presses never replay after uncertain dispatch.

Hands plan narration now keeps a narrated multi-step plan in one Telegram
status bubble by editing start to terminal state (and sends a fallback if edit
fails). This preserves the pre-action security notification and makes failure,
uncertainty, pause, checkpoint, and completion visible without two bubbles per
plan. Tool documentation explicitly lists the supported host and browser
conditions and rejects invented condition names before dispatch.

2026-07-11 retry finding: Reddit was never reached. MiniMax/OpenClaw serialized
the untyped nested `plan.steps` collection as `{item: ...}`; Hands correctly
rejected it before execution. `plan_submit` now exposes top-level, strongly
typed `steps: array[PlanStepInput]`. A direct live plan and a full
MiniMax→OpenClaw→Hands transport probe both completed successfully. The Reddit
password that entered trajectory logs was invalidated and replaced with the
host-side DPAPI vault reference `reddit_pw`; the affected email credential must
also be rotated at its provider before retry.

Incident conclusion: Reddit/OTP/network explanations were not established.
Ollie subsequently admitted inventing several of them. Browser-driving sites
were also explicitly marked deferred, so this was both a real implementation
defect and a readiness/scope failure.

### M0.5 — Close immediate Hands trust gaps before broad autonomy

- [x] Give owner approval a credential/path independent of the ordinary Hands
  caller bearer; bind approval to the exact action/plan hash, owner, expiry and
  one-time nonce; rate-limit failures. Current P0 separates authority from the
  MCP bearer, but the approval token still lives in Tier 1 plugin configuration;
  strong isolation from a fully compromised Tier 1 requires a separate relay
  principal or OS-backed secret boundary.
- [x] Fail closed consistently across browser clicks/submit keys, mutating UIA,
  raw pixel input, and arbitrary shell mutations. Caller-supplied effect labels
  may escalate consent but cannot downgrade an ambiguous action. Continue
  evolving this into richer engine-derived consequence classification.
  shell and CAPTCHA paths. “Acts as Tushar” must always confirm regardless of
  actuator syntax.
- [x] Recheck both the `DISABLED` flag and task abort immediately before every
  step, especially before a consequential dispatch.
- [x] Stop automatic redispatch when a postcondition is inconclusive. The
  executor now re-observes without repeating the action and returns the
  first-class terminal state `outcome_unknown`. Domain idempotency keys and
  reconciliation remain future work before selective retries are introduced.
- [ ] Add end-to-end regression tests through MCP/auth/policy/consent, not only
  direct Python calls.
- [ ] Define browser-profile secret handling and incident rotation.

Target: monitored external actions cannot approve themselves, route around
consent through another modality, or be duplicated by a verification retry.

### M0.7 — Integrity stop-the-line and canonical self-model (prerequisite)

Inserted 2026-07-23; full detail in `Plans/ollie-self-model-plan.md`. Trigger:
Ollie could not describe its own system (did not know the power sentinel), and
review verified P0-A live at HEAD — owner denial of a CONFIRM-tier plan
executes anyway (executor.py:228 truthiness on the consent tuple).

- [ ] S0: fix P0-A/P0-B/P0-C with tests; hands suite green in CI; one
  canonical watchdog; clean deployable HEAD.
- [ ] S1: `ollie-self/registry.yaml` — one machine-readable model of every
  component, unit, task, deployed path and probe; bootstrap box discovery;
  deployed-hash drift detection.
- [ ] S2: generated `SYSTEM.md` context (size-budgeted, completeness-linted,
  truncation class fixed) plus an agent-callable `self` query tool. Doctrine
  keeps identity/judgment; system facts become generated views.

Gate: Ollie answers "what is X", "what are you doing/why/blocked" and "what
changed" from canonical state; drift pages before a founder question does.
M1–M3 then build on state the agent can actually see.

### M1 — Canonical goals and commitments

- [ ] Define the founding team's active outcome portfolio.
- [x] Add a shadow-safe SQLite/WAL goal model with transactional migrations.
- [x] Add a commitment ledger: every promise has an owner, next action, due or
  follow-up time, success evidence, and explicit terminal state.
- [ ] Migrate active project and founder-directed commitments.

Target: Ollie never silently loses a commitment.

### M2 — Proactive work selector

- [x] Implement the deterministic selector in shadow mode; wiring it ahead of
  heartbeat, projects, jobs, maintenance, and curiosity remains pending.
- [ ] Use precedence: founder commitments, promised follow-ups, active-goal
  work, maintenance, then curiosity.
- [ ] Require goal, expected value, dependency check, evidence, cost/risk, and
  stop condition for autonomous initiatives.
- [ ] Limit simultaneous bets and give curiosity a fixed exploration budget.
- [ ] Allow "no valuable autonomous work now" as a correct outcome.

Target: proactivity means goal ownership, not continuous activity.

### M3 — Evidence-based closure and value learning

- [ ] Define evidence requirements by task class.
- [ ] Capture natural value events: verified outcome, promise kept, useful
  discovery, founder intervention, repeated instruction, dropped commitment,
  unsupported claim, and unnecessary work.
- [ ] Add expected-versus-realized value review.
- [ ] Add a small weekly founder calibration sample to the brief.
- [ ] Keep hard safety/trust constraints outside the reward score.

Target: verified progress per unit of founder attention.

### M4 — Safe unattended-operation hardening

- [x] Make spend/budget mutation atomic.
- [ ] Make consequential actions idempotent across retries.
- [ ] Recheck the kill switch before every plan step.
- [ ] Apply consequence-based policy consistently across browser, UIA, and
  pixel modalities.
- [ ] Strengthen approval authentication and rate limiting.
- [ ] Add source/deployment drift detection, CI, restore verification, and
  behavioral regression tests.

Target: a trustworthy unattended foundation, not merely a capable demo.

### M5 — Consolidation and product surface

- [x] Retire the legacy soft-gated `desktop`/`computer-use-mcp` path: remove
  its MCP entry, stop/disable its WSL proxy and Windows task, and disable the
  obsolete Chrome-extension tasks. Task XML and config rollback copies were
  retained. Hands is now the only configured host actuator.
- [x] Pause autonomous curiosity dispatch while keeping ingestion, gating,
  deduplication and candidate ranking live. A runtime marker makes this
  fail-safe independent of timer/unit reloads; the executive selector will
  later consume these candidates explicitly.
- [ ] Feed heartbeat, research, lab, dream/curiosity and project candidates into
  the canonical opportunity queue; remove their independent priority choices.
- [ ] Route Telegram and WhatsApp into one commitment and policy path while
  retaining transport-specific delivery behavior.
- [ ] Standardize outcome envelopes and evidence references across jobs,
  projects and monitors.
- [ ] Make Mission Control a read model of canonical goals, commitments,
  outcomes, spend and system health—not another state owner.
- [ ] Archive superseded runtime copies and documentation after deployment
  provenance is enforced.

Target: one product with specialist workers, rather than several loosely
coordinated autonomous subsystems.

## Scope estimate

These are engineering ranges, not promises; the live-box and external-site
verification windows can add elapsed time.

| Outcome | Likely effort for one engineer |
|---|---:|
| Finish Hands neutral qualification and incident closure | 1–3 days |
| Close M0.5 trust blockers for monitored external actions | 4–8 days |
| Canonical goals, commitments and selector MVP (M1–M2) | 2–4 weeks |
| Evidence/value loop and producer consolidation (M3/M5 core) | 2–4 additional weeks |
| Trustworthy unattended foundation including the rest of M4 | roughly 6–10 weeks total from this course correction |

This scope is moderate because most execution components remain. A rewrite of
OpenClaw, Hands, projects, jobs or the lab is not justified. The largest change
is semantic and stateful: introduce a canonical executive model, migrate each
producer to it, and remove duplicate sources of priority and truth.

## Phase gates

Do not advance because a phase's code exists; advance only when its behavior is
demonstrated:

| Gate | Required evidence |
|---|---|
| Hands qualified | neutral soak survives navigation, extraction, screenshot, form interaction without submit, restart/recovery and correct audit |
| Hands externally safe | independent approval, cross-modality consequence tests, per-step kill and non-duplicate retry tests pass end to end |
| Commitment loop usable | Telegram promises appear durably, due follow-ups fire, and no representative eval commitment is silently lost |
| Selector useful | on a replay set, active commitments outrank curiosity and the chosen work can be explained from canonical state |
| Outcome learning useful | founder can inspect expected/realized value and evidence; weekly calibration changes later selection |
| Unattended foundation | sustained soak with fault injection, restore rehearsal, deployment-drift detection and bounded spend |

## Design constraints

- Reuse the existing heartbeat, jobs, project sessions, briefs, monitors,
  spend controls, notifications, memory, and Hands primitives.
- Do not start with reinforcement learning or an opaque learned reward model.
  Begin with an inspectable scorecard and founder calibration.
- Never accept activity volume, notes produced, or jobs launched as a proxy for
  realized value.
- Never claim completion without task-appropriate evidence.

## Decision log

- 2026-07-10: Start with Hands because the executive layer must not delegate to
  an unreliable actuator. Return to M1 immediately after Hands is verified.
- 2026-07-23: Insert M0.7 (integrity stop-the-line + canonical self-model)
  before M1, per `Plans/ollie-self-model-plan.md`. Verified driver: P0-A
  (denied plan executes) live at HEAD; Ollie cannot describe its own system.
  Self-improving systems track recorded there as exploratory (S5).
