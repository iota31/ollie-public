# Mapping: natural-language browser goals -> Hands tools

Read-only audit. Goals: (1) trace how an LLM user message becomes a
`hands__act` / `hands__plan_submit` tool call, (2) identify what is
enforceable in code vs. model prompt policy, (3) flag tests needed to
prevent `act` fallback and unsupported infrastructure claims.

Scope: `ollie-hands/` Python engine, the unified `openclaw-ollie-wa-approval`
plugin (retired `openclaw-ollie-hands-approval` is a tombstone), and live
workspace artefacts under `workspace/`. The `ollie-guest/` and
`ollie-executive/` trees were inspected but do not host hands.

---

## 1. Tool surface the brain actually sees

The MCP server is a FastMCP app at `/mcp` on port 3200 with bearer auth
on every request and an inert-boot kill switch.

Tool definitions (the docstrings are what the LLM model reads):

| Tool | File:line | Purpose |
| --- | --- | --- |
| `session_info` | `ollie-hands/ollie_hands/server.py:58-75` | Read-only engine state (always available) |
| `observe` | `ollie-hands/ollie_hands/server.py:78-103` | Screenshot + window list + UIA + monitor info; never actuates |
| `act` | `ollie-hands/ollie_hands/server.py:106-167` | ONE policy-gated, consent-enforced step. Param grid 24-wide: `kind` + (kind-specific) + `commit`, `secret_ref`, `x/y/x2/y2`, `selector`, `url`, `value`, etc. |
| `solve_captcha` | `ollie-hands/ollie_hands/server.py:170-188` | Convenience wrapper that dispatches `kind=captcha` via `act_step` |
| `plan_submit` | `ollie-hands/ollie_hands/server.py:191-236` | Multi-step `Script` parsed from typed input; consent decided ONCE per script hash |
| `task_status` | `ollie-hands/ollie_hands/server.py:239-244` | Returns live `_TASKS` entry for a running/finished plan |
| `task_abort` | `ollie-hands/ollie_hands/server.py:247-253` | Cooperative abort by setting the per-task `threading.Event` |

What the LLM sees for `plan_submit` is the typed input:

- `steps`: `list[PlanStepInput]` (`actscript.py:83-95`) with strict `id`,
  `kind` literal, `args`, optional `preconditions`/`postcondition`/`on_fail`/
  `checkpoint`/`timeout`.
- `authorization`: `AuthorizationInput | None` (`actscript.py:98-107`),
  intentionally permissive at the schema layer (`total=False`); the engine
  re-parses via `Scope.parse` (`grants.py:54-102`) which enforces the real
  rules (origin exactness, effect category allowlist, TTL 30..1800, etc.).
- `title`: free-form string.

Because `AuthorizationInput` is `total=False`, `None` is cleanly
representable (`test_plan_tool_schema.py:90-107`). That fixes the live
`hands__plan_submit` null/required-properties regression captured at
`test_plan_tool_schema.py:38-87`.

---

## 2. Routing natural language into hands

The brain never sees the message "do X on this site" translated into a
plan. The actual chain is:

1. **Prompt build.** Workspace `AGENTS.md` (lines 220-274) instructs the
   brain how to choose between `observe`, `act`, `plan_submit`, and the
   capability ladder (L0 shell > L1 UIA > L2 Camoufox > L3 pixels).
   `WORK_DIGEST` injection happens in `before_prompt_build` in
   `openclaw-ollie-wa-approval/index.js:771-784` (mtime-cached read).
2. **Channel gating.** Owner-only on Telegram and WhatsApp via
   `before_agent_run` in `openclaw-ollie-wa-approval/index.js:869-939`.
   This is the ONLY pre-LLM gate; the brain is never in the approval path.
3. **Tool dispatch.** MCP client (`mcp.hands__*`) → streamable HTTP →
   bearer-auth middleware (`ollie_hands.auth.BearerMiddleware`,
   imported at `server.py:29`) → tool implementation.
4. **Engine gate.** `_gate()` (`server.py:51-55`) re-reads the kill
   switch and `DISABLED` flag-file per request.
5. **Single-action path.** `act_step()` (`ollie_hands/engine.py:274-352`)
   classifies via `policy.classify_*`, audits, dispatches, audits.
6. **Plan path.** `actscript.parse()` (`actscript.py:202-304`) classifies
   every step, then `executor.run()` (`executor.py:393-576`) confirms ONCE
   for the script's max tier, then runs steps locally.

The AGENTS.md "Consent is the engine's call, not yours" rule
(`workspace/AGENTS.md:259-262`) is reinforced by code: every actuation
goes through `engine.act_step` or `executor._run_step`, both of which
gate the same way.

---

## 3. plan_submit contract — what is mechanically enforced

`actscript.parse()` (`actscript.py:202-304`) is the strict gate. Hard
checks that cannot be widened by a planner:

- `kind` ∈ `{shell, uia, window, clipboard, browser, pixels, captcha}`
  (`actscript.py:36`, `233-235`).
- `on_fail` ∈ `{retry, repair, escalate, abort}` (`actscript.py:37`,
  `236-238`).
- Step ids unique; `args` is an object (`actscript.py:226-242`).
- Condition `type` ∈ the 11-element `SUPPORTED_CONDITION_TYPES`
  frozenset (`actscript.py:38-42`, `258-262`). `uia_text_contains`,
  `browser_url`, `selector_exists` are rejected, as called out in the
  `plan_submit` docstring at `server.py:213-214`.
- Write steps MUST declare a `postcondition`
  (`actscript.py:280-285`). BLOCKED steps skip this rule so the block
  reason wins.
- Script-wide `consent` is the max tier (`actscript.py:287-289`,
  `_TIER_ORDER` at `:199`).
- `authorization` only valid for browser steps
  (`actscript.py:269-271`); URLs must be inside
  `authorization.resources` (`:296-297`); effects must be inside
  `authorization.effects` (`:298-299`).

Mechanically derived effects are computed in
`browser_step_effect_and_resource` (`actscript.py:48-70`) — this is the
crucial piece that stops a planner from picking a benign-looking
"navigation" effect for an `external_commit` step. The runtime gate at
`executor._runtime_browser_decision` (`executor.py:29-60`) re-runs
`classify_browser` with the LIVE `target_text` resolved from the DOM via
`Eng.L2.element_text` so a "Submit"/"Sign up" button on the page escalates
to CONFIRM even when the plan said `progress`.

---

## 4. Hard policy gate (the part the LLM cannot widen)

`policy.py` is in-code and LLM-uneditable by design (docstring `:1-18`).

Hard deny sets:

- `_BLOCKED` (`policy.py:110-126`): Defender/firewall/BitLocker tamper,
  audit-trail edits, config/policy/token edits, vault/DPAPI access.
  Triggers T4/BLOCKED outright.
- `_CONFIRM` (`policy.py:129-140`): recursive force-delete,
  format-volume, shutdown/restart-computer, HKLM reg delete, diskpart,
  bcdedit, broad process kill.

The narrow engine-owned effect gate is the load-bearing defense for
browser objectives:

- `classify_browser` (`policy.py:267-316`) maps each op to a tier.
  - `goto/extract/links/screenshot/get_attr/property_matches/status/element_text`
    = read = NOTIFY (`:281-282`).
  - `click/select/press/fill/type_text` without an effect envelope = T3
    CONFIRM, reason `"browser {op} with undeclared consequence"`
    (`:294-297`).
  - With an effect envelope, the allowlist per op is narrow
    (`policy.py:300-306`): `click` admits only
    `navigation/session_preference/progress`; `fill/type_text` admit only
    `draft`; `select/press` admit
    `navigation/session_preference/draft/progress`. An incompatible
    envelope escalates to CONFIRM (`:308-310`).
- COMMIT_WORDS regex (`policy.py:259-264`) covers `send/post/buy/pay/
  sign up/log in/...` and is matched against `target_text` resolved at
  runtime, so a hijacked planner cannot omit the `commit` flag to slip a
  commit button past (`:286-288`).
- `submit` is unconditionally T3 (`policy.py:314-315`).
- `classify_action` (`policy.py:319-363`) covers shell/uia/window/
  clipboard/pixels/captcha; shell uses the regex sets plus
  `_EXTERNAL_SHELL_COMMIT` (`policy.py:162-166`) for mail/git push/curl
  POST/PUT/PATCH/DELETE/Invoke-RestMethod.
- `_effect()` envelope parser (`policy.py:169-205`) is fail-closed for
  unknown keys/categories/scopes: a mixed legacy/current envelope, an
  unknown category, or a non-bool `commit` returns `valid=False` →
  CONFIRM. This is what stops the "set `commit=False` on an external
  push" laundering attempt.

`effect_categories` (`policy.py:34-50`) are the canonical seven:
`observe, navigation, session_preference, draft, progress,
external_commit, identity_commit, destructive`. `COMMIT_EFFECTS`
(`policy.py:46-50`) is the last three.

Cross-modality parity is enforced — `classify_action` rejects a
`commit=False` caller override for any mutation without an effect, so
planner labels cannot downgrade unknown shell/UIA/pixels mutations to
NOTIFY. Tested in `test_effect_policy.py:17-49` and
`test_executor_safety.py`.

---

## 5. Single-use grant + scoped authorization

`grants.GrantStore` (`grants.py:130-227`) is the engine's
owner-approved authorization lease. It complements (never replaces)
exact-digest approvals.

- `Scope.parse` (`grants.py:54-102`) is strict: origins must parse to
  exactly `scheme://host[:port]` with no path/query/fragment
  (`parse_declared_resource`, `:38-43`); effects must be in
  `EFFECT_CATEGORIES`; TTL 30..1800.
- `GrantStore.authorize` (`grants.py:149-175`) re-validates a reused
  grant against family/resources/effects AND the plan's
  `required_resources`/`required_effects`. Returns `family_mismatch`,
  `resource_scope_widened`, `effect_scope_widened`,
  `required_resource_out_of_scope`, `required_effect_out_of_scope`,
  `commit_already_consumed`, or `unknown_or_expired`.
- `reserve_commit` (`grants.py:188-219`) atomically claims the
  single-use commit allowance with a `holder` token; the same holder
  re-reserving is idempotent; a competing holder loses with
  `commit_already_consumed`. Invoked from
  `executor.GrantContext.reserve_commit` (`executor.py:124-125`) at the
  last safe moment — see Inv3 below.

---

## 6. Per-step runtime contract

`executor._run_step` (`executor.py:227-390`) is the structural cure for
"acted on ambient state":

1. Preconditions via `conditions.check_all` (`conditions.py:144-149`).
   On fail: retry (3x with 0.6s sleep), repair (1.0s), or escalate
   (`executor.py:255-276`). Each retry/repair re-checks the kill gate
   via `_ensure_runnable` (`:128-141`).
2. Runtime browser re-classification (`executor.py:284-291`) using live
   `target_text`. If the runtime tier is CONFIRM and the plan-time tier
   wasn't, the step escalates with `runtime_effect_escalated` and
   `action_dispatched=False` — so the dispatch has NOT fired.
3. Scoped-authorization live guards (when `grant_ctx` is present):
   - **Inv4a** (`:301-310`): every non-`goto` browser step reads the
     live browser URL and fails closed if outside the approved origins.
   - **Inv3** (`:311-323`): atomic commit reservation immediately
     before the first commit dispatch, so a second/concurrent task
     never fires its commit.
4. `Eng._dispatch` runs the action; `action_dispatched=True` after.
5. **Inv4b** (`:332-347`): for a `goto`, the LANDED URL is enforced
   against approved origins. If the navigation escaped scope, returns
   `outcome_unknown` — never re-dispatches.
6. Postcondition: one observe, then a settle+re-observe on failure
   (`executor.py:359-384`); never repeats the action.
7. Human collision auto-pause (`executor.py:500-510`): `obs.last_input_tick()`
   compared against the baseline; L3's own `pixels.last_injected_tick()`
   is excluded so Ollie doesn't self-collision.

The task registry is in-memory (`executor._TASKS`, `:83-84`); persistence
is via audit chain only.

---

## 7. Durable task state / resume

The repo does NOT persist plan state across engine restarts:

- `_TASKS` (`executor.py:83-84`) is a process-local dict guarded by
  `_TLOCK`. `task_status(task_id)` (`:144-149`) returns `None` for
  unknown ids — there is no on-disk resume store.
- The audit chain (`audit.Audit`, `audit.py:52-110`) is the durable
  reconstruction path: each event carries `prev` + `hash` so the trail
  itself is tamper-evident and verifiable off-box by
  `scripts/audit-verify.py` (`audit.py:116-181`).
- On boot, `Audit._load_last_hash` (`audit.py:64-85`) re-reads the most
  recent file and continues the chain. There is no executor-state
  recovery beyond the audit log.

Implication: a planned restart of the engine mid-plan loses the task
record; the brain sees a `task_status` 404. AGENTS.md lines 273-275
already call out `task_abort` for a running task and the
`C:\ProgramData\ollie-hands\DISABLED` kill switch; neither is a
durable-resume path. This is an infrastructure claim worth flagging (see
section 10).

---

## 8. Diagnosis / health reporting

Engine-level:

- `session_info` (`server.py:58-75`) returns `hands_enabled`, platform,
  session lock state, monitors; audited.
- `observe` (`server.py:78-103`) returns full situational awareness
  with explicit `screenshot_status` when pixels are missing
  (`observe.py:226-268`).
- Audit event `boot` (`server.py:284-289`) on every start; `refused`
  events when hands are disabled (`:54`).
- Health from outside: `ollie-watchdog` (`ollie-hands/scripts/ollie_watchdog.py`)
  pings the gateway webhook, ngrok domain, jobs runner, 4dpocket,
  watchdog itself, stale jobs, disk, LLM/search provider quotas.
- `ollie-hands/scripts/supervisor.py` is the host-side single-instance
  supervisor using an exclusive TCP sentinel at :3201; logs to its own
  `supervisor.log` so a crash trace is never overwritten by the engine.
- Restart: `scripts/restart-host.ps1` is the documented restart path;
  `RUNBOOK.md` is its source of truth. The user-memory note
  `project_ollie_hands_restart` warns that `Stop/Start-ScheduledTask`
  leaves the :3200 holder orphaned and runs stale code; only
  `restart-host.ps1` is safe.

Plan-level:

- `task_status(task_id)` (`server.py:239-244`) returns a structured
  dict: `task_id`, `status`, `title`, `step`, `total`, `results[]`,
  `consent`, `hash`, `started`.
- `task_abort(task_id)` (`server.py:247-253`) cooperatively cancels —
  the abort is checked at every `_ensure_runnable` boundary and
  between steps in `executor.run` (`:489-493`, `:528-534`).
- Failure modes from `executor.run`: `ok`, `blocked`, `denied`,
  `escalated`, `outcome_unknown`, `aborted`, `timeout`,
  `paused_collision`, `checkpoint`, `grant_rejected`.
- Error shape from `act_step`: `{action, preview, policy:{tier,consent,
  reason}, status: ok|blocked|denied|error, result?, error?}`
  (`engine.py:283-352`).
- Plan-level failure shape from `server.py:223-232` returns
  `{status:"invalid", error}` when `parse()` raises `ScriptError`, so
  the brain gets a typed error string instead of an exception.

---

## 9. What is enforceable in code vs. model prompt policy

Enforceable in code (the brain cannot widen these):

- The 11-element `SUPPORTED_CONDITION_TYPES` allowlist
  (`actscript.py:38-42`).
- The "write step needs postcondition" rule (`actscript.py:280-285`).
- The `kind` and `on_fail` literal enums
  (`actscript.py:36-37`).
- `classify_browser`'s effect envelope parsing and per-op allowlist
  (`policy.py:169-205`, `267-316`).
- `_BLOCKED`/`_CONFIRM` regex sets (`policy.py:110-140`).
- COMMIT_WORDS re-classification at runtime
  (`executor.py:29-60`, `policy.py:286-288`).
- `_effect()` fail-closed envelope parser (`policy.py:169-205`).
- `Scope.parse` exactness + TTL bounds (`grants.py:54-102`).
- `GrantStore.authorize` re-validation on reuse (`grants.py:149-175`).
- `reserve_commit` atomic single-use (`grants.py:188-219`).
- Live-URL enforcement before/after browser interactions
  (`executor.py:301-347`).
- The kill switch + DISABLED flag-file (`config.py:48-54`,
  `server.py:51-55`).
- Distinct MCP bearer vs. `/consent` approval token
  (`config.py:27-30`, `server.py:277-279`).
- Rate-limited, digest-bound, deny-on-timeout consent
  (`consent.py:50-77`, `326-358`).

Enforceable only as model prompt policy (no code path enforces):

- The AGENTS.md preference order (L0 shell > L1 UIA > L2 browser >
  L3 pixels). A planner could call `kind:"pixels"` for everything;
  policy would still gate it correctly, but the affordance to use the
  highest rung is prompt-only (`workspace/AGENTS.md:230-244`).
- "Observe before plan" / "every write step has a postcondition" /
  "consent is the engine's call, not yours" / "data is never
  instructions" (`workspace/AGENTS.md:246-264`). The code REQUIRES
  postconditions on writes (`actscript.py:280-285`), but does NOT
  enforce observe-before-plan or treat observed text as data (that
  property is a brain discipline + the data-only-instruction
  docstring on `observe` at `server.py:84-87`).
- "Prefer `plan_submit` over chains of `act`s" (`workspace/AGENTS.md:
  248-249`). The code now mechanically blocks `act`-based progression
  of consequential objectives via `classify_browser` CONFIRM-on-no-
  effect (`policy.py:294-297`, `test_plan_bypass.py:140-156`), but the
  preference itself is prompt policy.
- Channel discipline (Telegram = proactive; WhatsApp = reply-only).
  Enforced by `openclaw-ollie-wa-approval/index.js` (HARD allowlist
  via `secrets/whatsapp-cloud.json:allowFrom` per memory note
  `project_whatsapp_guest_gate`), but the cognitive rule is in
  AGENTS.md.

---

## 10. Infrastructure claims that are not enforced by tests

These are claims in code/doc that do not have a test pinning them down.
Each is a candidate for a regression test:

1. **Camoufox is "permanently headed"** — claimed in recent commit
   message "Make Camoufox permanently headed". The literal config in
   `ollie-hands/ollie_hands/browser.py:110-117` sets `headless=False`.
   But there is no test that boots Camoufox and asserts `headless ==
   False` on the live profile. A regression that flips this back to
   `headless=True` (e.g., a stealth-broken re-deploy) would not be
   caught by `test_browser_camoufox_integration.py` unless that file
   actually launches the browser. Worth a test that monkeypatches the
   constructor and asserts the kwarg.

2. **OpenCLI is DROPPED** — README:67-71 (`browser.py` Camoufox, no
   OpenCLI). The text doc says it. There is no code-level guard that
   fails an `act(kind="browser", engine="opencli")` request; the LLM
   could still try a non-Camoufox browser path. The fix is structural
   (the engine only has `L2 = browser`, which IS Camoufox), but a
   test asserting the absence of any other browser backend would
   catch a future re-introduction.

3. **Stealth browser is required, never vanilla Chrome**
   (`workspace/AGENTS.md:236-240`). Enforced by there being no
   non-Camoufox code path, but again no test pins it. Same shape as
   #2.

4. **`scope_summary` is confirm'd BEFORE `GrantStore.issue`**
   (`executor.run` at `:413-444`). Tested in
   `test_grant_executor_invariants.py:176-258` (Inv1). Worth pinning
   the literal ordering in a single labelled test so a future refactor
   that moves `grant_store.issue` above `consent.confirm` is caught.

5. **`task_status` returns `None` for unknown task_ids** — see
   `executor.get_status` (`:144-149`). The 404 contract is documented
   in the docstring at `server.py:241-244` ("error: unknown task_id"),
   but there's no test asserting that an unknown id never returns a
   partial record or a phantom ok. Easy to add.

6. **Durable plan resume across engine restart** — there is no
   durable store for `_TASKS`. If anyone in the future adds an
   "auto-resume on restart" claim, there is no test for it. Worth
   adding a test that asserts a task spawned in process A is invisible
   to `task_status` after a clean restart, and that recovery relies on
   the audit chain (which IS tested in `test_audit_chain.py`).

7. **`session_info` works even while hands are disabled** — claimed
   in `server.py:62-65`. Not pinned by a test that toggles
   `enabled=false` and verifies `session_info` still returns
   metadata. Easy add.

8. **MCP bearer is rejected on `/consent`** — claimed in README:23-24
   and enforced by `BearerMiddleware`. Not pinned by a test that POSTs
   `/consent` with the bearer (vs. approval token) and asserts 401.
   Worth adding so a future refactor that relaxes the middleware
   order is caught.

9. **Telegram `sendMessage` with `inline_keyboard` falling back to
   plain text only on definitive markup rejection, never on
   ambiguous failure** — captured in `consent.py:267-307` and tested
   in `test_inline_approval.py:80-127`. The distinction between
   `definitive_rejection` and `ambiguous_failure` is the load-bearing
   invariant. Worth a test that simulates a network timeout (URLError)
   and asserts NO plain-text retry.

10. **Rate-limited `/consent` resolver** — claimed in
    `consent.py:329-340`. Tested indirectly via existing tests, but
    not pinned by a test that fires 13 requests in <60s and asserts
    the 13th returns `429`. Easy add.

11. **MCP `Authorization` bearer mismatch returns 401** — the
    middleware is referenced (`server.py:283`) but never tested in
    isolation.

12. **Channel gating `senderIsOwner===true` is required** — claimed
    in `openclaw-ollie-wa-approval/index.js:886-893`. Tested in the
    JS unit tests via `isAuthorizedOwnerCallback`, but the field
    name `senderIsOwner` is taken from upstream gateway typings and
    could drift. Worth a test that confirms the exact field name.

13. **Camoufox async-loop integrity** — `_loop_main`/`_browser_loop`
    (`browser.py:32-52`) start a dedicated daemon thread. There is no
    test that asserts the engine rejects calls from outside the loop
    or that a second engine process in the same profile doesn't
    collide. `test_browser_lifecycle.py` likely covers the happy path;
    worth checking whether it covers profile lock collision.

14. **`patch-playwright-driver.py` — what does it patch?** — file
    exists (`ollie-hands/scripts/`). Not inspected per scope, but the
    existence of a runtime patch to a vendor driver is exactly the
    kind of unsupported infrastructure claim worth documenting and
    pinning.

---

## 11. Tests needed to prevent `act` fallback (already present vs. missing)

Present and green (per the task list and the test files I read):

- `test_plan_bypass.py` — proves `fill/type_text/click/select/press`
  without effect are CONFIRM in `classify_browser`; the Reddit sequence
  is locked down at every mutating step.
- `test_effect_policy.py` — cross-modality consequence parity
  (Enter-as-Commit, `git push`, `Send-MailMessage`, `Set-Content`).
- `test_plan_tool_schema.py` — exact wire-format regressions from
  live MiniMax session (`authorization:null`, `$text` wrapping).
- `test_grant_executor_invariants.py` — Inv1..Inv7 covering scope
  consent ordering, grant reuse rejection, atomic commit reservation,
  live-origin enforcement, runtime target-text escalation,
  arbitrary-click laundering, partial-auth strictness.
- `test_executor_safety.py` — failed postcondition never re-dispatches;
  closed-gate failures cancel cleanly; abort checked at dispatch.
- `test_inline_approval.py` — exact payload without digest; fallback
  classification of `_send_with_result` for HTTP 400/401/403/429; W
  expiry under lock.
- `test_policy.py`, `test_grants.py`, `test_consent_route.py`,
  `test_approval_auth.py` — verified by the task list as green.

Missing or worth adding (flagged for the next round):

- A test that proves a model that ONLY has `act` cannot progress a
  Reddit-style signup (currently `test_plan_bypass.py:140-156` covers
  classification, not end-to-end sequence with no plan_submit). A
  higher-fidelity test would call `engine.act_step` 5 times with the
  Reddit sequence and assert that every mutating step returns
  `{status:"denied"}` or `{status:"blocked"}`.
- A test that pins the `effect` envelope fail-closed rule across
  every kind (shell, uia, window, clipboard, browser, pixels,
  captcha) and asserts a missing envelope is always CONFIRM. Partial
  coverage exists; full coverage by parameterisation would close the
  gap.
- A test that pins `policy.classify_browser("submit") == CONFIRM` even
  with `commit=false` and even with `effect={category:"navigation"}`.
  This is the obvious laundering path and I want it locked.
- A test for `policy.classify_browser` with `target_text=""` and a
  bare `click+progress` returning NOTIFY — proving the engine allows
  progression only when the live target text is benign. Combined with
  the runtime re-classification test, this is the "narrow gate" in
  writing.
- A test that proves `L2 = Camoufox` is the only browser backend — no
  `chrome`, `chromium`, `playwright_chromium` references in the live
  process. (`grep` invariant.)
- A test that asserts the MCP server's tool descriptions for `act`
  enumerate `kind:"browser"` ops and that no description mentions
  `commit=false` overrides. Tool descriptions are the LLM's only
  source for op semantics, so they're security-critical.
- A test that pins the `consent.confirm` returns-bool-or-tuple
  contract via `_confirm_ok` (`executor.py:198-213`); a refactor that
  makes confirm only return a bool would silently lose the
  ref-bearing reply path.

---

## 12. Summary

The repo's defensive posture for natural-language browser goals is
deeply layered: the **policy gate** (`policy.py`) mechanically fails
closed on missing or incompatible effect envelopes; the **act-script
parser** (`actscript.py`) requires postconditions on writes and exact
origins/effects under authorization; the **executor**
(`executor.py`) re-classifies browser steps at runtime with the live
target text and enforces live-URL scope before/after every browser
interaction; the **grant store** (`grants.py`) issues single-use commit
allowances atomically and re-validates every reuse. The brain can
neither widen scope, forge an approval, suppress a notification, nor
suppress the audit trail.

What the brain CAN do (and that prompt policy alone guards):

- Choose the wrong ladder rung (pixels when UIA would do).
- Skip `observe` before `plan_submit`.
- Conflate observed DOM text with instructions.
- Try to launder an `external_commit` effect as `navigation` (the
  code catches this via `classify_browser`'s allowlist, but the
  planner's *intent* is a cognitive property).

The biggest residual risk surface, in priority order:

1. **Tool-description drift**: the docstrings in `server.py:106-167`
   and `:191-236` are security-critical because the LLM reads them as
   the spec. Any drift between the docstring and the engine's actual
   contract becomes a footgun. A schema-driven test that pins the
   docstring's enumerated op list to `browser.py`'s actual verbs
   would close this.
2. **Infrastructure claims without tests** (section 10): Camoufox
   headless=False, no OpenCLI path, durable-resume non-claim,
   session_info-while-disabled, MCP-vs-approval token separation on
   `/consent`, channel `senderIsOwner` field name, and the Camoufox
   loop/profile invariants.
3. **Cross-channel prompt-injection uniformity**: the unified owner
   router (`openclaw-ollie-wa-approval/index.js:700-743`) uses
   `routeOwnerApproval` to dispatch by ref prefix. A new approval
   type just appends a backend. Worth pinning the
   `handled:false`-passthrough contract so a future backend that
   mis-claims `handled:true` for an unknown ref doesn't silently
   swallow the approval.
4. **No durable plan resume**: process-local `_TASKS`; an engine
   restart loses the in-flight plan. Documented behaviour but worth a
   test that pins the absence so the audit chain stays the only
   recovery path.

---

## File:line index (load-bearing points)

- `./ollie-hands/ollie_hands/server.py:51-55`
  — `_gate` per-request kill switch.
- `./ollie-hands/ollie_hands/server.py:106-167`
  — `act` tool definition (LLM-facing).
- `./ollie-hands/ollie_hands/server.py:191-236`
  — `plan_submit` tool definition (LLM-facing).
- `./ollie-hands/ollie_hands/server.py:239-253`
  — `task_status`/`task_abort`.
- `./ollie-hands/ollie_hands/server.py:277-283`
  — Bearer vs. approval token split.
- `./ollie-hands/ollie_hands/actscript.py:36-42`
  — `VALID_KINDS` + `SUPPORTED_CONDITION_TYPES`.
- `./ollie-hands/ollie_hands/actscript.py:48-70`
  — mechanically derived browser effect + origin.
- `./ollie-hands/ollie_hands/actscript.py:202-304`
  — `parse()` strict gate.
- `./ollie-hands/ollie_hands/policy.py:110-140`
  — `_BLOCKED`/`_CONFIRM` regex sets.
- `./ollie-hands/ollie_hands/policy.py:162-166`
  — `_EXTERNAL_SHELL_COMMIT` regex.
- `./ollie-hands/ollie_hands/policy.py:169-205`
  — `_effect()` fail-closed envelope.
- `./ollie-hands/ollie_hands/policy.py:259-264`
  — COMMIT_WORDS regex.
- `./ollie-hands/ollie_hands/policy.py:267-316`
  — `classify_browser` (the load-bearing narrow gate).
- `./ollie-hands/ollie_hands/policy.py:319-363`
  — `classify_action` cross-modality.
- `./ollie-hands/ollie_hands/engine.py:84-111`
  — per-action classifier with live target_text.
- `./ollie-hands/ollie_hands/engine.py:274-352`
  — `act_step` policy+consent+dispatch+audit.
- `./ollie-hands/ollie_hands/executor.py:29-60`
  — runtime browser re-classification.
- `./ollie-hands/ollie_hands/executor.py:83-84`
  — `_TASKS` registry (process-local).
- `./ollie-hands/ollie_hands/executor.py:95-126`
  — `GrantContext` per-run lease.
- `./ollie-hands/ollie_hands/executor.py:227-390`
  — `_run_step` lifecycle (pre/post + reservation).
- `./ollie-hands/ollie_hands/executor.py:393-576`
  — `run()` script-level consent + execution loop.
- `./ollie-hands/ollie_hands/conditions.py:85-141`
  — supported condition types check.
- `./ollie-hands/ollie_hands/consent.py:267-324`
  — `deliver_pending` / `confirm` (inline keyboard with typed fallback).
- `./ollie-hands/ollie_hands/consent.py:326-358`
  — `resolve` digest-bound, rate-limited, single-use.
- `./ollie-hands/ollie_hands/grants.py:54-102`
  — `Scope.parse` strict origin/effect/TTL rules.
- `./ollie-hands/ollie_hands/grants.py:149-219`
  — `authorize` reuse validation + atomic `reserve_commit`.
- `./ollie-hands/ollie_hands/audit.py:52-110`
  — chain-of-hash tamper-evident append-only log.
- `./ollie-hands/ollie_hands/browser.py:104-141`
  — Camoufox async-loop integrity.
- `./ollie-hands/ollie_hands/observe.py:226-268`
  — observe never-acts, screen-capture-failure degrades gracefully.
- `./openclaw-ollie-wa-approval/index.js:44-77`
  — `WA_PLUGIN_ID` + config defaults.
- `./openclaw-ollie-wa-approval/index.js:308-326`
  — `sendOwnerTelegram` adapter.
- `./openclaw-ollie-wa-approval/index.js:585-606`
  — `parseOwnerCommand` approve/deny regex.
- `./openclaw-ollie-wa-approval/index.js:654-694`
  — unified owner-approval router backends.
- `./openclaw-ollie-wa-approval/index.js:771-784`
  — `before_prompt_build` WORK_DIGEST injection.
- `./openclaw-ollie-wa-approval/index.js:804-851`
  — inline approval callback registration.
- `./openclaw-ollie-wa-approval/index.js:869-939`
  — `before_agent_run` HARD gate.
- `./openclaw-ollie-hands-approval/index.js:99-114`
  — retired tombstone plugin (no hooks).
- `./workspace/AGENTS.md:220-274`
  — brain-facing Hands doctrine (capability ladder + discipline).
- `./workspace/IDENTITY.md:24-30`
  — legacy `desktop`/`computer-use-mcp` retired; Hands is THE path.
- `./ollie-hands/README.md:73-88`
  — policy/consent table.
- `./ollie-hands/README.md:119-131`
  — approval-auth migration checklist (do not deploy partially).
- `./ollie-hands/scripts/supervisor.py:40-72`
  — single-instance sentinel + crash logging.
- `./ollie-hands/scripts/ollie_watchdog.py:71-88`
  — proactive-brain liveness thresholds.
- `./ollie-hands/tests/test_plan_bypass.py:140-156`
  — Reddit sequence reproduction via bare `act()`.
- `./ollie-hands/tests/test_grant_executor_invariants.py:176-732`
  — Inv1..Inv7 explicit coverage.
- `./ollie-hands/tests/test_inline_approval.py:250-319`
  — `_send_with_result` classification of HTTP 400/401/403/429.
- `./ollie-hands/tests/test_plan_tool_schema.py:38-107`
  — live MiniMax wire-format regressions.