# Harden Ollie Hands browser execution and recovery

## Context

The Reddit signup failure exposed trusted-boundary defects rather than a prompting problem: direct `act` can mis-handle tuple-shaped approval results, mutating plan steps can be described as observe-only, semantic click inspection fails open, the browser has no real wait primitive, and stale MCP sessions surface as generic failures. Keep the user instruction natural and strengthen the existing Hands `act`/`plan_submit` architecture instead of adding another workflow engine.

Close locally provable safety gaps first, add bounded browser synchronization and typed health/failure reporting, and treat production OpenClaw MCP reconnection as a separate client-boundary change if that client is not present in this repository.

## Critical files

Modify only files required from this set:

- `ollie-hands/ollie_hands/consent.py`
- `ollie-hands/ollie_hands/engine.py`
- `ollie-hands/ollie_hands/actscript.py`
- `ollie-hands/ollie_hands/policy.py`
- `ollie-hands/ollie_hands/executor.py`
- `ollie-hands/ollie_hands/browser.py`
- `ollie-hands/ollie_hands/server.py`
- `ollie-hands/ollie_hands/grants.py` only if commit semantics change
- focused tests under `ollie-hands/tests/`

Reuse `_confirm_ok()` behavior from `executor.py`, exact-origin utilities from `grants.py`, transport classification from `browser.py`, runtime policy checks from `executor.py`, and existing task/error envelopes rather than introducing parallel abstractions.

## Implementation

### 1. Close the direct-action approval bypass

- Move tuple/bool approval normalization into a shared consent-layer function, then use it from `engine.act_step()` and `executor.run()`.
- Interpret tuple approval solely from its first element; `(False, ref)` must deny execution.
- Preserve approval references for auditing without allowing tuple truthiness to authorize an action.
- Add `act_step()` regressions proving denied and timed-out tuple results never reach `_dispatch`, while approved tuple and legacy bool results retain current behavior.

### 2. Make scoped mutation authorization fail closed

- In `actscript.py`, stop deriving `observe` for mutating browser operations such as `click`, `fill`, `type_text`, `press`, `select`, and `submit`.
- Require every supported mutation to declare a valid effect compatible with its operation and policy classification before requesting or using a grant.
- Keep read operations eligible for `observe` and HTTP(S) navigation eligible for `navigation`.
- Ensure the approval summary states the effects actually enforced at dispatch.
- Test missing, malformed, and incompatible effects, plus a positive Reddit-style plan whose final account-creation action is `identity_commit`, not `progress`.

### 3. Add a real bounded wait and safe navigation validation

- Add one canonical `wait` browser operation with a bounded timeout and narrowly supported conditions needed by current flows, using existing Playwright/Camoufox primitives.
- Wire it consistently through plan parsing, policy, dispatch, public exposure where applicable, and tests.
- At the trusted runtime boundary, accept only canonical HTTP(S) navigation and reject `javascript:`, `data:`, `file:`, and other unsupported schemes.
- Preserve the rule that uncertain consequential actions are never replayed.
- Test that `javascript:void(0)` cannot serve as a wait and timeout failures are bounded and machine-readable.

### 4. Fail closed when semantic mutation inspection is unavailable

- Change `_runtime_browser_decision()` so inspection exceptions for mutating controls do not degrade to empty text or no decision.
- Return a typed policy/inspection failure or require fresh confirmation before dispatch; never execute under a weaker inferred effect.
- Keep inspection failures distinct from transport death and ordinary action timeout.
- Test that clicks, especially commit-capable controls, are not dispatched when target semantics cannot be inspected.

### 5. Establish one supported browser-operation contract

- Define one canonical supported-operation table/schema consumed by parsing, policy validation, and dispatch.
- Reconcile drift around `select`, `submit`, `property_matches`, and operations recognized by policy but not implemented by the runtime.
- Remove unsupported public claims rather than adding speculative functionality; implement only the existing contract and the new wait primitive.
- Add a contract test proving every exposed/parseable operation has policy classification and dispatch, while unknown operations fail before approval.

### 6. Return typed boundary failures and authoritative health

- Add stable codes to existing response envelopes for authorization denial, policy rejection, invalid action, action timeout, browser unavailable/dead transport, stale MCP session, and outcome unknown.
- Retain human-readable details, but make orchestration depend on codes rather than prose or guessed-port probes.
- Strengthen `session_info`/health to report Hands process state and an authoritative browser/page liveness result from the owned Camoufox loop without starting another browser.
- Do not label a timeout as dead infrastructure; reset browser state only when transport classification or an explicit liveness probe proves it unusable.
- Test code preservation through server responses.

### 7. Repair stale-session recovery at the real client boundary

- Patch the production OpenClaw streamable-HTTP client only if its deployed/runtime source is available: on a typed/session-level `Session not found`, discard the session, initialize once, and retry only retry-safe reads or requests proven not dispatched.
- Never automatically replay mutations or consequential actions after an uncertain response; return `outcome_unknown` and require observation/reconciliation.
- If the OpenClaw client remains external to this repository, do not patch `research_social.py` and claim production is fixed. Complete the Hands-side typed contract and record the external client patch as a deployment blocker before retrying Reddit.
- Test an expired session with a safe read and prove mutations are never replayed.

### 8. Align consequential-commit wording with enforcement

- Treat “single-use” as one consequential dispatch, not merely one task-holder reservation, unless current owner-approved behavior explicitly says otherwise.
- If changed now, atomically consume the allowance immediately before dispatch and reject a second commit-effect step under the same grant.
- Otherwise, change approval wording to describe task-scoped allowance accurately and leave behavior unchanged; do not preserve misleading copy.
- Add concurrency and repeated-dispatch tests for the selected semantics.

## Verification

1. Run focused Hands tests for consent, direct actions, policy, plan parsing, grants, executor behavior, browser recovery, and server envelopes.
2. Run the full `ollie-hands` suite, including existing effect-policy and vault tests, without folding unrelated failures into this change.
3. Run a local MCP integration proving:
   - tuple denial blocks direct `act` dispatch;
   - an observe-only grant cannot authorize mutation;
   - a valid plan can navigate, wait, inspect, and proceed;
   - unsupported URL schemes are rejected;
   - inspection failure blocks mutation;
   - stale-session recovery retries a safe read at most once and never replays mutation;
   - health distinguishes a live browser, action timeout, stale session, and dead transport.
4. Before Windows deployment, diff every target against the live box because deployed code may be newer than the repository; preserve live-only changes.
5. Deploy only reviewed files, restart Hands exclusively with `ollie-hands/scripts/restart-host.ps1`, and verify port `3200`, MCP initialization, authoritative health, and headed Camoufox state.
6. Send one real Telegram integration approval for owner resolution; never synthesize approval.
7. Retry the natural request “Create a Reddit account for yourself” only after structural gates pass. Confirm the final identity commit consumes its allowance once and audit records contain typed outcomes.

## Scope boundaries

- Do not use prompt instructions as the primary fix.
- Do not create a second workflow engine or durable task system in this increment.
- Do not patch repository-owned test/research clients and claim production OpenClaw recovery is fixed.
- Do not deploy, restart shared services, or retry Reddit until implementation and local verification are complete and deployment is separately authorized.
- Keep the unrelated `test_live_localhost_grant_flow_end_to_end` concurrency issue out of scope unless it blocks verification.