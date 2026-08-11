# Scoped browser authorization grants plan

## Current state summary
- `ollie_hands/grants.py` already has a narrow `Scope` parser, canonical origin normalization, in-memory `GrantStore`, subset checks, TTL bounds, and pre-dispatch single-use commit consumption.
- `ollie_hands/actscript.py` already parses top-level `authorization` and optional `grant_id`, derives `required_resources` from browser step `url`, derives `required_effects` from browser `effect`, and enforces exact-plan `hash` binding.
- `ollie_hands/executor.py` already issues/reuses grants, binds non-`goto` browser steps to the live page origin, consumes commit before dispatch, and never repeats uncertain commits.
- `ollie_hands/server.py` does not yet instantiate/pass a `GrantStore`, does not expose authorization in the `plan_submit` tool schema/docs, and only exposes POST `/consent`.
- `ollie_hands/consent.py` already keeps approval auth independent, hides the digest from inventory, and has `pending_inventory()` but no route exposes it.
- Policy anti-laundering is split: runtime browser commit detection uses `target_text`, but plan-time browser classification does not have access to the same resolved text and instead relies on op/effect compatibility.

## Goals to preserve
- One concise owner approval per new scope.
- Repaired plans inside the approved envelope do not reprompt.
- Exact binding on origin + effects + family + TTL.
- Fail closed on malformed/missing scope metadata.
- Single-use consequential commit; no auto-replay after uncertain outcome.
- Legacy exact-plan confirm flow still works when no `authorization` is supplied.
- Approval route stays independently authenticated from MCP.
- Digest never shown in owner inventory/API.

## File-by-file implementation plan

### 1) `./ollie-hands/ollie_hands/server.py`
Minimal orchestration changes only.

Planned changes:
- Instantiate one process-wide `GrantStore` next to `Consent`:
  - `grant_store = GrantStore(audit)`
- Pass `grant_store` into `executor_mod.run(...)` from `plan_submit`.
- Expand `plan_submit` signature/docstring so the MCP schema visibly supports:
  - top-level `authorization: dict | None = None`
  - nested fields `family`, `resources`, `effects`, `ttl_seconds`, optional `grant_id`
- Keep legacy callers valid by making `authorization` optional and defaulting to `None`.
- Add owner-only GET endpoint for pending approvals/grants inventory:
  - `GET /consent`
  - returns `consent.pending_inventory()` only, not grants
  - protected by the same approval bearer as POST `/consent`
- Optionally add a second owner-only endpoint for active grants inventory if product wants visibility into reusable scopes; otherwise do not add it now.

Exact API contract to expose in `plan_submit` docs:
```json
{
  "title": "continue reddit triage",
  "authorization": {
    "family": "reddit-triage",
    "resources": ["https://www.reddit.com"],
    "effects": ["navigation", "draft", "progress"],
    "ttl_seconds": 600,
    "grant_id": "optional-existing-grant-id"
  },
  "steps": [ ... ]
}
```

Exact HTTP schemas/return values:
- `POST /consent` request remains:
```json
{"code":"abc","approve":true,"script_hash":"<digest>"}
```
- `POST /consent` success remains:
```json
{"ok":true,"code":"abc","approved":true}
```
- `POST /consent` failures remain one of:
```json
{"ok":false,"error":"...","error_code":"digest_required|digest_mismatch|unknown_or_expired|rate_limited"}
```
- New `GET /consent` success:
```json
{
  "ok": true,
  "pending": [
    {"code":"abc","preview":"Authorize task family 'reddit-triage'...","expires_in":512}
  ]
}
```
- New `GET /consent` empty:
```json
{"ok": true, "pending": []}
```

Why this is surgical:
- No new subsystem; just wire the already-existing grant/consent pieces into the running server and expose the missing inventory route.

### 2) `./ollie-hands/ollie_hands/actscript.py`
Tighten parse-time envelope derivation and legacy fallback boundaries.

Planned changes:
- Keep `authorization` optional; if absent, preserve legacy exact-plan behavior unchanged.
- Centralize browser-step scope derivation into one helper, e.g.:
  - `_browser_step_scope_requirements(step: Step) -> tuple[set[str], set[str]]`
- Use that helper in `parse()` to derive:
  - required origins from browser `goto` URLs
  - required effects from explicit browser `effect`, with `goto -> navigation`, browser reads -> observe fallback only where no effect is required
- Add explicit parse-time rejection for any authorization-bearing browser write step whose effect is absent or invalid; keep existing exact error semantics where possible.
- Add parse-time final-goto guard: if the final browser `goto` URL canonical origin is outside `authorization.resources`, reject the plan before execution.
  - This closes the “final goto redirect origin is unchecked” hole for planned navigations.
- Keep runtime live-origin enforcement as defense in depth for server-side redirects and current-page repair flows.
- For legacy non-authorization plans, do not add new schema requirements beyond what already exists.

Reusable helper(s):
- `_browser_step_scope_requirements(step)`
- Possibly `_browser_step_effect(step)` if shared with executor/grants logic.

Exact return/behavior expectations:
- On invalid authorization object: raise `ScriptError` with current GrantError message.
- On plan widening beyond scope: keep current `ScriptError("plan contains a browser URL outside authorization.resources")` / `ScriptError("plan effects exceed authorization.effects")` style.
- On unsupported scoped modality: keep `ScriptError("scoped authorization currently supports browser steps only")`.

### 3) `./ollie-hands/ollie_hands/executor.py`
Finish runtime enforcement around origin drift and grant reuse.

Planned changes:
- Leave the existing control flow intact.
- Extract current grant-related checks into reusable helpers so plan-time and runtime semantics stay aligned:
  - `_step_effect(step)` already exists; keep and reuse.
  - Add `_expected_browser_resource(step, live_url: str | None = None) -> str | None`
  - Add `_enforce_live_browser_scope(step, grant)` replacing `_enforce_live_resource`.
- Strengthen origin checks for browser navigation:
  - after any browser `goto`, inspect `browser.status().url`, canonicalize final origin, and require membership in `grant.scope.resources`
  - if redirect lands outside scope, return `outcome_unknown` if dispatch already happened, with `grant_boundary`/post-dispatch detail indicating redirected origin escaped scope
- Keep pre-dispatch commit consumption exactly as-is for `COMMIT_EFFECTS`.
- Do not consume commits for non-commit steps.
- Do not auto-replay grant-backed commits on any failure path.
- Include `grant_id` in the task summary when a new grant is issued; preserve current behavior.

Exact failure shapes to preserve/extend:
- Missing store with scoped auth: task summary `{status:"grant_rejected", error:"grant store unavailable"}`.
- Reuse denial: `{status:"grant_rejected", error:"<GrantError reason>"}`.
- Post-dispatch scope escape (redirect/live origin mismatch): step outcome `{"status":"outcome_unknown", "stage":"grant_boundary", ...}` and task status `outcome_unknown`.

Why this is surgical:
- No redesign of executor loop; just close the unchecked final-origin gap and make helpers reusable.

### 4) `./ollie-hands/ollie_hands/grants.py`
Small hardening and optional inventory support.

Planned changes:
- Keep the current in-memory implementation for now; do not introduce persistence in this scope.
- Add a read-only inventory method only if the owner needs grant visibility now:
  - `list_active() -> list[dict]`
  - returns sanitized grant metadata without any digest-like values:
```json
[
  {
    "grant_id": "...",
    "family": "reddit-triage",
    "resources": ["https://www.reddit.com"],
    "effects": ["draft","navigation","progress"],
    "expires_in": 412,
    "commit_consumed": false
  }
]
```
- Purge expired entries opportunistically inside `list_active()` and existing lookup paths.
- Keep `GrantStore.authorize(...)` as the single reuse gate; do not duplicate subset logic elsewhere.

Important product note:
- Grant persistence is currently in-memory only. Restarting `ollie-hands` will drop reusable scopes. That is acceptable for this scoped change unless owner explicitly wants cross-restart reuse.

### 5) `./ollie-hands/ollie_hands/consent.py`
No structural redesign; just formalize inventory response usage.

Planned changes:
- Reuse `pending_inventory()` exactly for the new GET `/consent` route.
- Keep `confirm()` summary-based prompts for new scope approvals so the owner sees concise scope text, not selectors or digests.
- Keep `resolve()` behavior requiring the independent approval token and consuming the challenge atomically.
- Optional tiny refinement: document that `preview` may be either exact-plan preview or scope summary; no logic change needed.

Exact inventory response content:
- code
- preview
- expires_in
- never `action_digest`
- never `script_hash`

### 6) `./ollie-hands/ollie_hands/policy.py`
Unify anti-laundering semantics between plan and runtime without broad refactor.

Problem to fix:
- Runtime `act()` can escalate based on resolved `target_text`.
- `plan_submit` parse/classification cannot currently use the same text-based signal, so anti-laundering differs between direct act and planned execution.

Minimal plan:
- Introduce one reusable classifier helper for browser commit suspicion, e.g.:
  - `browser_commit_signal(*, op, key="", commit=False, target_text="") -> str | None`
- `classify_browser()` calls that helper.
- `actscript._validate_browser_effect()` continues parse-time compatibility checks using `classify_browser()` without `target_text`.
- In executor, immediately before dispatching any browser interaction step, resolve live target text when feasible from the browser engine and re-run the same helper/classifier with that text.
  - If the live target text now implies commit but the approved effect envelope is only reversible (`navigation/session_preference/draft/progress`), fail closed before dispatch.
  - If the grant/effect already allows a commit effect and the step is within scope, proceed with normal commit-consume behavior.

Reusable function(s):
- `browser_commit_signal(...)` or `_browser_commit_reason(...)`
- optional `classify_browser_runtime(...)` wrapper if needed, but avoid a new abstraction layer unless executor genuinely needs it.

Important implementation constraint:
- Do not force full selector-resolution into parse time. The point is to align the runtime guard with the same policy primitive, not to make plan parsing browser-aware.

### 7) `./ollie-hands/ollie_hands/browser.py` or engine-side browser adapter
Only if needed to support runtime target-text parity.

Planned changes:
- Add the smallest possible read helper to obtain human-visible text for a target selector before a browser interaction step, for example:
  - `element_text(selector: str) -> {"ok": true, "text": "Submit"}`
  - or reuse an existing browser op if already present (`element_text` appears policy-aware but confirm actual implementation before editing)
- Executor uses that helper only for interactive browser ops where target text matters (`click`, maybe `press` if focused element text is available).
- If text cannot be resolved reliably, keep fail-closed behavior based on declared effect / explicit commit / Enter key, but do not invent a heavy DOM-inspection subsystem.

Decision note:
- If no cheap selector->text primitive exists, owner may need to choose between:
  1. shipping without plan/runtime text parity in this pass, or
  2. adding one tiny browser read primitive now.
- My recommendation is option 2 if the primitive is already nearly present.

### 8) Tests under `./ollie-hands/tests/`
Add focused coverage only around the missing seams.

#### Extend `./ollie-hands/tests/test_approval_auth.py`
Add:
- GET `/consent` with approval token returns pending inventory.
- GET `/consent` with MCP bearer returns 401.
- Inventory payload never contains `action_digest` / `script_hash`.

#### Extend `./ollie-hands/tests/test_plan_tool_schema.py`
Add:
- `plan_submit`/Pydantic schema includes optional top-level `authorization` object.
- Nested `authorization.effects/resources` serialize as arrays, not wrapped objects.

#### Extend `./ollie-hands/tests/test_effect_policy.py`
Add:
- parse/runtime parity tests around commit-like target text once runtime helper exists.
- browser `goto` with scoped auth accepts same-origin URL and rejects out-of-scope origin.
- reversible effect envelope cannot bless a click whose resolved text is commit-like.

#### New or extend grant-focused tests
Likely best in `./ollie-hands/tests/test_executor_safety.py` or a new `test_grants.py` if the existing file gets too mixed.
Add:
- scoped auth without `grant_store` -> `grant_rejected`.
- new scope approval issues a grant and returns `grant_id`.
- same-scope repair with `grant_id` reuses without reprompt.
- widened resources/effects/family reject reuse.
- expired grant rejects reuse.
- post-redirect final origin outside scope yields `outcome_unknown` and no replay.
- commit step consumes grant before dispatch; second commit under same grant rejects.
- non-commit steps under same grant remain reusable until TTL expiry.

## Exact API and data-shape recommendations

### MCP `plan_submit` input
Keep top-level shape:
```json
{
  "title": "string",
  "authorization": {
    "family": "string 1..128",
    "resources": ["https://origin", "https://other-origin"],
    "effects": [
      "observe",
      "navigation",
      "session_preference",
      "draft",
      "progress",
      "external_commit",
      "identity_commit",
      "destructive"
    ],
    "ttl_seconds": 30,
    "grant_id": "optional"
  },
  "steps": [PlanStepInput, ...]
}
```
Notes:
- `authorization` omitted => legacy exact-plan approval path.
- `grant_id` present => reuse attempt; absent => request one owner approval for the new scope.
- `ttl_seconds` remains optional in input and defaults to current parser default.

### MCP `plan_submit` output
No new envelope required; extend existing task summary when applicable:
```json
{
  "task_id": "...",
  "status": "ok|denied|grant_rejected|escalated|outcome_unknown|...",
  "title": "...",
  "step": 2,
  "total": 3,
  "results": [...],
  "consent": "notify|confirm|auto|blocked",
  "hash": "...",
  "grant_id": "optional when a new or reused scoped grant is active",
  "error": "optional"
}
```
Recommendation:
- include `grant_id` both on newly issued and successfully reused grants so the caller can carry it forward without branching.

### GET `/consent`
Recommended because the audit explicitly calls out missing inventory and `Consent` already supports pending inventory.
```json
{"ok": true, "pending": [{"code":"...","preview":"...","expires_in":123}]}
```
No digest fields.

## Migration / implementation sequence
1. Wire `GrantStore` into `server.py` and pass it into `executor.run`.
2. Expose optional top-level `authorization` on `plan_submit` signature/docstring/schema.
3. Add GET `/consent` using the approval bearer and `pending_inventory()`.
4. Extract scope-derivation helpers in `actscript.py`; keep legacy no-authorization path unchanged.
5. Strengthen executor post-`goto` final-origin enforcement using live URL after navigation.
6. Unify browser commit-signal logic in `policy.py`; then use it in executor runtime checks for browser interactions.
7. Add/extend tests in the order above, covering wiring first, then runtime redirect/commit semantics.
8. Only after tests are green, consider whether owner wants active-grant inventory and/or persistence; do not block the main fix on those.

## Test matrix

### A. Legacy exact-plan fallback
- No `authorization`, confirm-tier plan -> owner confirm prompt uses exact-plan preview.
- No `authorization`, notify-tier plan -> no grant issuance, existing narration unchanged.
- No `authorization`, blocked plan -> blocked before execution.

### B. New-scope approval issuance
- `authorization` without `grant_id`, browser-only steps, in-scope effects/resources -> one owner approval prompt using scope summary.
- Approval success -> task proceeds, summary includes `grant_id`.
- Approval deny/timeout -> status `denied`.

### C. Reuse without reprompt
- Same `family`, subset/equal `resources`, subset/equal `effects`, unexpired TTL -> no confirm call, run proceeds.
- Repaired plan drops steps / narrows URLs/effects -> allowed.

### D. Fail-closed widening / malformed scope
- Family mismatch -> `grant_rejected`.
- Resource widening -> `grant_rejected`.
- Effect widening -> `grant_rejected`.
- Invalid authorization field/effect/TTL -> `invalid` parse response or `ScriptError` in unit tests.
- Scoped auth on non-browser step -> reject at parse.

### E. Origin binding
- Planned `goto` URL out of scope -> parse rejects.
- `goto` starts in-scope but redirects to other origin -> runtime `outcome_unknown`, no silent continuation.
- Non-`goto` browser step on current live page outside scope -> `grant_boundary` failure before dispatch.

### F. Commit anti-replay
- Scoped commit effect step consumes grant before dispatch.
- Same grant reused for second commit effect -> `grant_rejected` / `commit_already_consumed`.
- Commit dispatched but postcondition uncertain -> outcome unknown, never retried.
- Non-commit steps before/after a commit are governed by whether the grant is still otherwise valid; second consequential commit is denied.

### G. Anti-laundering parity
- Direct `act(browser click)` with resolved target text `Submit` => confirm.
- `plan_submit` step declared as reversible but runtime-resolved target text `Submit` => fail closed before dispatch unless commit effect is authorized.
- `press Enter` remains confirm regardless of reversible effect declaration.

### H. Approval auth and inventory
- POST `/consent` requires approval token, not MCP bearer.
- GET `/consent` requires approval token, not MCP bearer.
- GET `/consent` payload exposes code/preview/expires_in only.
- Concurrent approvals remain isolated and single-use.

## Product decisions needing owner input
1. Active grant inventory exposure: do you want only pending approvals visible at `GET /consent`, or also a separate owner-only active-grants listing endpoint? The audit requires pending inventory; active-grant visibility is optional.
2. Cross-restart reuse: current grants are in-memory. Is losing reusable scopes on `ollie-hands` restart acceptable for this pass? My default recommendation is yes, keep it in-memory for now.
3. Runtime target-text parity dependency: if the browser adapter lacks a tiny `element_text(selector)` primitive, should this pass add that one minimal helper now? I recommend yes if missing; otherwise parity remains imperfect.
