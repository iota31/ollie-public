# Design: Maintainable fix for OpenClaw Telegram/Hands approval deadlock (lane metadata)

**Mode:** read-only design/research (plan-mode)
**Date:** 2026-07-16
**Status:** Design complete; implementation blocked on upstream OpenClaw source acquisition
**Scope:** Smallest correct source-level extension to allow `ollie_approval:v1:*` callbacks to be dispatched on a lane independent of the chat turn that originated a synchronous Hands request.

---

## 1. Executive summary

**Deadlock:** An Ollie Telegram turn calls Hands synchronously and occupies the gateway's isolated polling ingress lane `telegram:<chatId>`. The custom `ollie_approval:v1:a|d:H-*` callback is dispatched through the same lane selector, so it cannot reach the registered plugin handler until the Hands turn times out.

**Built-in behavior (reference):** Execution approval callbacks are assigned lane `telegram:<chatId>:approval` (distinct from plain `telegram:<chatId>`).

**Root cause location:** The lane key `telegram:<chatId>` is produced by the OpenClaw gateway's isolated polling ingress (not by any plugin). The plugin registers via `registerInteractiveHandler({channel:"telegram", namespace:"ollie_approval", handler})`; the gateway currently ignores any per-handler metadata and always uses the chat id for Telegram.

**Maintainable source status:**
- **Gateway (TypeScript source):** NOT present in this repository. The installed package on box is `openclaw 2026.5.28` at `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/` (minified, content-hashed `.js` only). The local stub at `openclaw-ollie-wa-approval/node_modules/openclaw/` exports only `definePluginEntry` — a one-line identity function. No `.ts`, no `.d.ts`, no repository/homepage metadata.
- **Plugin (maintainable):** YES — `openclaw-ollie-wa-approval/index.js` (lines 943–988 for registration; 592–615 for parser; 617–623 for owner auth; 644–724 for handler).
- **Upstream discoverability:** GitHub search and local package metadata returned no discoverable public repository for OpenClaw 2026.5.28. No submodule, no lockfile entry, no `repository` field.

**Conclusion:** The smallest correct fix is a **gateway-side change** to honor optional per-handler lane metadata. The plugin can be extended to emit that metadata, but without the gateway change the metadata is a no-op. A workaround that lives entirely in this repo is possible (engine out-of-band resolution) but is larger and changes the trust/timeout model.

**Blocker:** Upstream OpenClaw source acquisition (or a targeted patch from the OpenClaw maintainers) is required before a gateway change can be written, reviewed, and shipped.

---

## 2. Current dispatch path (exact symbols from live 2026.5.28 dist)

All paths are under `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/` on the box. None have companion `.ts` in this clone.

| Symbol | File | Line (approx) | Role |
|---|---|---|---|
| `bot.on("callback_query", ...)` | `bot-iSDqdz0Y.js` | 2799 | Top-level Telegram callback entry. First line: `if (shouldSkipUpdate(ctx)) return;`. Then `answerCallbackQuery`, scope checks, `authorizeTelegramEventSender`, then `dispatchTelegramPluginInteractiveHandler`. |
| `shouldSkipUpdate` | `bot-iSDqdz0Y.js` | 7452 | `updateTracker.shouldSkipHandlerDispatch(ctx)` — in-process de-dupe guard. |
| `dispatchTelegramPluginInteractiveHandler` | `bot-iSDqdz0Y.js` | 1496 | Builds `ctx.callback = {data, namespace, payload, messageId, chatId, messageText}` and calls `dispatchPluginInteractiveHandler({channel:"telegram", data, dedupeId:callbackId, onMatched, invoke})`. |
| `dispatchPluginInteractiveHandler` | `plugin-runtime-DBz1g2if.js` | 46 | Calls `resolvePluginInteractiveNamespaceMatch(channel, data)` (splits on first `:`), then `claimPluginInteractiveCallbackDedupe(dedupeId)` (5 min ttl, key `openclaw.pluginInteractiveCallbackDedupe`, maxSize 4096). On duplicate: returns `{matched:true, handled:true, duplicate:true}` without invoking plugin. |
| `registerPluginInteractiveHandler` | `command-registration-BWsYSY_K.js` | 179 | Stores `Map<channel:ns, registration>`. Rejects empty ns and ns not matching `^[A-Za-z0-9._-]+$`. |
| `resolvePluginInteractiveNamespaceMatch` | `command-registration-BWsYSY_K.js` | 121 | Splits on first `:`; first token matched against registered namespaces. |
| `registerInteractiveHandler` (plugin API) | `loader-wybWjJVr.js` | 4571 | Wraps `registerPluginInteractiveHandler`; emits warn on failure. This is what the plugin probes with `typeof api.registerInteractiveHandler === "function"`. |
| `getTelegramSequentialKey` (inferred) | (ingress) | — | Produces the lane key `telegram:<chatId>` for the isolated polling ingress spool. Evidence: spool failure message names `lane telegram:<OWNER_TELEGRAM_CHAT_ID>`. No conditional on namespace or handler metadata in the observed path. |

**Lane selector is chat-id only.** The ingress serializes every Telegram update for a given bot+chat onto `telegram:<chatId>`. There is no namespace-based override today. Built-in approvals achieve separation by using a different key (`telegram:<chatId>:approval`), which implies the ingress has a special case for its own approval surface — not a general plugin extension point.

---

## 3. Plugin registration contract (maintainable source)

File: `./openclaw-ollie-wa-approval/index.js`

**Registration (lines 943–988):**
```js
if (typeof api.registerInteractiveHandler === "function") {
  api.registerInteractiveHandler({
    channel: "telegram",
    namespace: APPROVAL_CALLBACK_NS,  // "ollie_approval"
    handler: async (ctx) => {
      // normalize, call handleApprovalCallback, always return { handled: true }
      return { handled: true };
    },
  });
}
```

**Namespace and version (lines 592–593):**
```js
const APPROVAL_CALLBACK_NS = "ollie_approval";
const APPROVAL_CALLBACK_VERSION = "v1";
```

**Parser (lines 595–615):** splits `data` on `:`, requires 4 parts, `[0]`=`"ollie_approval"`, `[1]`=`"v1"`, `[2]`∈{`a`,`d`}, `[3]` matches `/^H-[A-Za-z0-9_-]{1,61}$/`.

**Owner authorization (lines 617–623):**
```js
function isAuthorizedOwnerCallback(cfg, ctxLike) {
  const ownerId = String(cfg?.ownerTelegramChatId ?? "");
  return !!ownerId &&
    ctxLike?.senderId === ownerId &&
    ctxLike?.chatId === ownerId &&
    ctxLike?.auth?.isAuthorizedSender === true;
}
```

**Handler always claims handled (line 983):** `return { handled: true };`. This is the value the dispatcher passes to `onMatched`.

**Current `__testHooks` export (lines 992–1006):** does not include lane-related helpers; adding `lane` metadata would be a new export or an extension of the registration object shape in tests.

---

## 4. Smallest correct source-level extension (design)

### 4.1 Goal

Allow a registered interactive handler to declare that its callbacks should be dispatched on a lane other than the default chat lane, without changing authorization, dedupe, H-ref opacity, or fail-closed semantics.

### 4.2 Proposed registration envelope (plugin → gateway)

Add an optional `lane` field to the object passed to `registerInteractiveHandler`:

```ts
// Proposed (plugin emits; gateway consumes if present)
api.registerInteractiveHandler({
  channel: "telegram",
  namespace: "ollie_approval",
  lane: "approval",                    // NEW, OPTIONAL
  handler: async (ctx) => { ... },
});
```

Semantics:
- `lane` is advisory. The gateway resolves a final lane key from `(channel, namespace, lane?, chatId?)`.
- If `lane` is omitted or unrecognized, behavior is unchanged (current default for that channel).
- For Telegram, a canonical mapping might be:
  - `lane: undefined` → `telegram:<chatId>` (current default)
  - `lane: "approval"` → `telegram:<chatId>:approval` (matches built-in execution approvals)
  - `lane: "free"` or `lane: "global"` → a non-sequential lane (if the ingress supports it)
- The gateway must perform this resolution **before** enqueueing onto the sequential spool, so the callback is never placed behind the originating chat turn.

### 4.3 Gateway ingress changes required (NOT in this repo)

Exact files (box paths; symbols from 2026.5.28 dist):

1. `command-registration-BWsYSY_K.js`
   - Extend `registerPluginInteractiveHandler(channel, namespace, handler, meta?)` to accept and store `meta.lane` (or a fourth parameter).
   - Store alongside the handler: `{ handler, lane }`.

2. `bot-iSDqdz0Y.js` (or the isolated polling ingress module that owns `getTelegramSequentialKey`)
   - In the `callback_query` path (around line 2799), after namespace resolution but **before** determining the spool lane, call a resolver:
     ```js
     const reg = getRegisteredInteractiveHandler("telegram", namespace);
     const laneKey = resolveTelegramLaneForInteractive({ chatId, lane: reg?.lane, namespace });
     ```
   - `resolveTelegramLaneForInteractive` would implement:
     - `lane === "approval"` → `telegram:${chatId}:approval`
     - `lane === undefined` → `telegram:${chatId}`
     - `lane === "free"` → a non-sequential/global lane (implementation-defined)
   - The spool/enqueue must use `laneKey` rather than unconditionally `telegram:${chatId}`.

3. `plugin-runtime-DBz1g2if.js`
   - `dispatchPluginInteractiveHandler` already receives `channel` and `data`. It should also receive or derive `lane` from registration so that any lane-aware scheduling (if centralized) is consistent.
   - Dedupe key (`dedupeId: callbackId`) must remain stable regardless of lane; do not change the 5-min TTL or claim logic.

4. Loader surface (`loader-wybWjJVr.js`)
   - `registerInteractiveHandler` wrapper should forward `lane` (if present) to `registerPluginInteractiveHandler`.
   - No behavior change if `lane` is absent.

### 4.4 Namespace-to-lane resolution (ingress point)

The ingress must resolve **before queueing**. The order is:

1. Receive `callback_query` Update from Telegram.
2. `shouldSkipUpdate(ctx)` — early de-dupe guard (unchanged).
3. `authorizeTelegramEventSender(...)` — owner attribution (unchanged; feeds `ctx.auth.isAuthorizedSender`).
4. `dispatchTelegramPluginInteractiveHandler({data, callbackId, ...})`:
   - Parse namespace via `resolvePluginInteractiveNamespaceMatch("telegram", data)` → `"ollie_approval"`.
   - Look up registration → `{ handler, lane? }`.
   - Compute lane key:
     - If `lane` present and recognized for Telegram → `telegram:${chatId}:${lane}` or a documented variant.
     - Else → `telegram:${chatId}`.
5. Enqueue the normalized callback context onto the resolved lane's sequential spool.
6. Worker dequeues and invokes the plugin handler via the dispatcher's `invoke`.

If step 4 selects a different lane than the originating chat turn, the callback is no longer blocked by a synchronous Hands reply on `telegram:<chatId>`.

### 4.5 Minimal plugin-side diff (illustrative)

In `openclaw-ollie-wa-approval/index.js`, change the registration site:

```js
// BEFORE
api.registerInteractiveHandler({
  channel: "telegram",
  namespace: APPROVAL_CALLBACK_NS,
  handler: async (ctx) => { ... return { handled: true }; },
});

// AFTER (lane is OPTIONAL; omitting it preserves current behavior)
api.registerInteractiveHandler({
  channel: "telegram",
  namespace: APPROVAL_CALLBACK_NS,
  lane: "approval",                 // NEW
  handler: async (ctx) => { ... return { handled: true }; },
});
```

Add to `__testHooks` for unit tests if desired:
```js
export const __testHooks = {
  ...existing,
  APPROVAL_CALLBACK_NS,
  APPROVAL_CALLBACK_VERSION,
};
```

No change to `parseApprovalCallback`, `isAuthorizedOwnerCallback`, `handleApprovalCallback`, or `postHandsConsent`. The lane is a dispatch concern, not an authorization or backend concern.

### 4.6 Config surface (optional, not required)

Do **not** put `lane` in `openclaw.plugin.json` config schema. The lane is an intrinsic property of this handler's dispatch contract, not a runtime toggle. If future handlers need configurability, that can be added later; for `ollie_approval` it is a constant.

---

## 5. Invariants that must be preserved (non-negotiable)

1. **Owner-only authorization remains in the plugin.** `isAuthorizedOwnerCallback` (lines 617–623) still requires `senderId === ownerTelegramChatId && chatId === ownerTelegramChatId && ctx.auth.isAuthorizedSender === true`. Moving the callback to a different lane does not relax or relocate this check. `authorizeTelegramEventSender` (gateway) is the source of the `auth` signal; it must still run.

2. **H-ref opacity and shape check.** The parser regex `/^H-[A-Za-z0-9_-]{1,61}$/` is a **shape** guard, not an authorization. The actual authorization is the owner chat-id match plus the engine's live `_pending` membership. Lane changes must not collapse these two checks.

3. **Bearer-token separation.** The plugin uses `handsApprovalToken` (config-only, never logged) for `POST /consent`. This token is distinct from the `hands` MCP bearer. Lane metadata must not cause the bearer to be transmitted on a different surface or logged.

4. **Dedupe via Telegram callback.id.** `claimPluginInteractiveCallbackDedupe(dedupeId)` keys on `callbackId` (Telegram-assigned). A lane re-route must keep `dedupeId` stable. An attacker who replays a callback id within the 5-min TTL must still be deduped, regardless of lane.

5. **Fail-closed in `before_agent_run`.** The originating Ollie turn is held by the engine's `await consent_post_response`, gated by `approval.token`. The plugin's `before_agent_run` returns `{outcome:"block", reason:"owner-approval-handled"}` for the owner's text reply so the brain never sees it. The lane fix must NOT route an approval callback's resolved decision (`a`/`d`) into the brain's chat turn.

6. **No user-text leak in approval metadata.** `logApprovalEvent` (lines 110–122) whitelists `APPROVAL_LOG_FIELDS`. Any new lane field added to logging must be appended to that set and remain ≤80 chars.

7. **No new public network surfaces.** The plugin's outbound surfaces are `api.runtime.channel.outbound.loadAdapter("telegram")` and `fetch` to the local `/consent`. Lane metadata must not introduce a third outbound path.

8. **Idempotency under restart.** The live engine's `_pending` is in-memory on `C:\ollie-hands`. A callback that arrives after an engine restart cannot be answered because the entry is gone. The plugin must continue to surface `unknown_or_expired` as a terminal UI (per existing handler). Lane changes must not alter this contract.

---

## 6. Exact upstream paths / symbols / tests (as discovered)

**Gateway (box-only, dist, no companion source in this clone):**
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/bot-iSDqdz0Y.js`
  - `bot.on("callback_query", ...)` at ~2799
  - `dispatchTelegramPluginInteractiveHandler` at ~1496
  - `shouldSkipUpdate` at ~7452
  - `authorizeTelegramEventSender` at ~2373 (and ~2506, ~2880 in related paths)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/plugin-runtime-DBz1g2if.js`
  - `dispatchPluginInteractiveHandler` at ~46
  - Dedupe via `claimPluginInteractiveCallbackDedupe` (in-process, 5 min ttl, key `openclaw.pluginInteractiveCallbackDedupe`, maxSize 4096)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/command-registration-BWsYSY_K.js`
  - `registerPluginInteractiveHandler` at ~179
  - `resolvePluginInteractiveNamespaceMatch` at ~121 (splits on first `:`)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/loader-wybWjJVr.js`
  - `registerInteractiveHandler` plugin API surface at ~4571
- Inferred ingress symbol: `getTelegramSequentialKey` (produces `telegram:<chatId>`); observed via spool failure naming `lane telegram:<OWNER_TELEGRAM_CHAT_ID>`.

**Plugin (maintainable, in this repo):**
- `./openclaw-ollie-wa-approval/index.js`
  - Registration at 943–988
  - Namespace/version at 592–593
  - Parser at 595–615
  - Owner auth at 617–623
  - Handler at 644–724
  - Always returns `{ handled: true }` at 983
- `./openclaw-ollie-wa-approval/openclaw.plugin.json`
  - Config schema (no `lane` today; none needed for this handler)
- Tests (must remain green):
  - `openclaw-ollie-wa-approval/test/inline_handler_registration.test.js`
  - `openclaw-ollie-wa-approval/test/callback_correlation.test.js`
  - `openclaw-ollie-wa-approval/test/router.test.js` (referenced by comments)
- Hands engine tests (must remain green):
  - `ollie-hands/tests/test_inline_approval.py`
  - `ollie-hands/tests/test_consent_route.py`
  - `ollie-hands/tests/test_grant_executor_invariants.py`

**Local SDK stub (editor only, not runtime):**
- `openclaw-ollie-wa-approval/node_modules/openclaw/package.json` → `{"name":"openclaw","type":"module","exports":{"./plugin-sdk/plugin-entry":"./plugin-sdk/plugin-entry.js"}}`
- `openclaw-ollie-wa-approval/node_modules/openclaw/plugin-sdk/plugin-entry.js` → `export function definePluginEntry(entry) { return entry; }`
- No `.d.ts`, no `PluginApi` surface, no `registerInteractiveHandler` declaration.

**Package metadata (plugin declares peer):**
- `openclaw-ollie-wa-approval/package.json` → `"peerDependencies": { "openclaw": ">=2026.5.28" }`

**Upstream discoverability:** No `repository`, `homepage`, or `bugs` fields observed in the local stub. GitHub/web searches for OpenClaw 2026 telegram handler symbols returned no usable public source location. This repo's git remote is only `https://github.com/onllm-dev/ollie.git`; no submodules or OpenClaw references.

---

## 7. Verification obligations (read-only until upstream source is available)

Until the gateway TypeScript source is available and a patch can be written:

1. **Document the contract in the plugin README.** Add a section "Lane contract" that states: "This handler registers with `lane:"approval"` (when supported by the gateway). The gateway is responsible for mapping that to a non-chat lane before enqueue. Without gateway support, callbacks remain on `telegram:<chatId>`."

2. **Add a structural test that the registration object includes `lane` when the field is supported by the test harness.** The test can assert shape without asserting runtime lane selection (since the harness's `registerInteractiveHandler` is a stub).

3. **Do not claim lane separation is live until:**
   - A patched gateway (with the resolver change) is deployed and its version string or build id is recorded.
   - A live callback for an in-flight Hands turn is observed to be processed while the originating turn is still blocked (end-to-end timing evidence).
   - The spool does not emit a handler-timeout on `telegram:<chatId>` for the callback update.

4. **Preserve existing test coverage.** All current tests for parser, owner auth, terminal vs transient outcomes, dedupe, and logging must continue to pass. Lane metadata is additive and must not alter those paths.

---

## 8. Deployment considerations (after upstream source is obtained)

1. **Gateway change first.** The plugin can be updated to emit `lane:"approval"` immediately (no behavior change until gateway honors it). The gateway patch must be merged and deployed before any claim of separation.

2. **Version the contract.** If the gateway adds `lane` support in a release after 2026.5.28, the plugin should either:
   - Declare a newer peer range (`>=2026.5.28-with-lane-support` or a semver bump), or
   - Probe at runtime (if the gateway exposes a capability flag) and fall back gracefully.
   In the absence of a capability flag, the safest stance is "emit `lane` and document that separation requires gateway X."

3. **Do not change the engine's `/consent` contract.** The lane change is a dispatch/routing concern inside the gateway. The engine still receives `POST /consent {ref, approve}` with `Authorization: Bearer <approval.token>` and returns `{ok, ...}` or error codes. No change to Hands is required for the lane fix.

4. **Do not alter `before_agent_run` text-command path.** The owner's plain-text `approve <ref>` / `deny <ref>` replies are still routed via `before_agent_run` and return `{outcome:"block"}`. That path is intentionally chat-turn and is separate from the callback path.

5. **Do not relax `isAuthorizedOwnerCallback`.** Even if the lane changes, the plugin must still validate `senderId/chatId/auth.isAuthorizedSender`.

---

## 9. Workaround that does not require gateway change (larger, deferred)

If a gateway patch is not immediately available, an alternative is to change the originating turn's wait from "await a chat message on `telegram:<chatId>`" to "await an out-of-band signal from the engine."

- The engine can emit a server-sent event (SSE) or a short-lived webhook to the originating turn when consent resolves.
- The plugin still receives the callback on the (blocked) lane, POSTs to `/consent`, and the engine resolves the pending waiter out of band.
- This removes the round-trip dependency on the chat lane without changing how the gateway dispatches the callback.

**Trade-offs:**
- Larger change (engine + server + originating turn glue).
- Alters timeout and cancellation semantics (the turn is no longer "chat blocked").
- Still requires the callback to be delivered to the plugin (so lane starvation can still bite if the gateway is wedged).

This is documented as a workaround path, not the preferred fix. The preferred fix is gateway lane metadata.

---

## 10. Note on deployed Hands path (verified claim)

The user states: treat claims that `C:\ollie-hands\consent.py` is live as unverified/wrong-path. The deployed package was previously `C:\ollie-hands\ollie_hands\consent.py`.

**Observation from prior incident (plan `immutable-baking-neumann-agent-a9f296541a0249c9b.md`):**
- Live `consent.py` mtime on `C:\ollie-hands\consent.py` was `2026-06-10` (legacy 6-digit numeric, no keyboard).
- The repo working tree has a newer `ollie_hands/consent.py` with `H-` keys and inline keyboards, but it was **not deployed** at the time of that investigation.
- The server entry point observed was `C:\ollie-hands\venv\Scripts\python.exe -m ollie_hands.server`.

For this plan, the exact deployed module path is **not material** to the lane-metadata design; the design is about gateway dispatch before the plugin is invoked. However, any end-to-end verification must confirm which `consent.py` is actually serving `/consent` at test time and that it emits `ollie_approval:v1:*` callback data (otherwise callbacks will never be produced to exercise the lane).

---

## 11. Precise implementation / verification / deployment plan

### Phase 0 — Prerequisites (read-only until complete)

- [ ] Acquire the OpenClaw gateway TypeScript source for version 2026.5.28 (or the nearest tag/branch that matches the box `dist/` symbols). Record the repo URL, commit, and build instructions.
- [ ] Confirm that the source contains the ingress module that owns `getTelegramSequentialKey` (or equivalent) and the registration surfaces listed in §6.
- [ ] Confirm that a build from source produces a `dist/` layout compatible with the installed package on box (filenames, loader entry points).
- [ ] Record the current gateway version string and the exact `lastUpdateId` / spool state before any changes (baseline for later verification).

**Blocker:** If the upstream source cannot be obtained (no public repo, no vendor drop, no CLA/access), this plan cannot proceed past documentation. The plugin can still emit `lane:"approval"` as a forward-compatible annotation, but separation will not be live.

### Phase 1 — Design finalization (in this repo, no gateway change yet)

- [ ] Add a "Lane contract" section to `openclaw-ollie-wa-approval/README.md` documenting:
  - Current behavior (callbacks on `telegram:<chatId>`).
  - Intended behavior with `lane:"approval"`.
  - That the gateway is responsible for the mapping.
  - That omitting `lane` preserves legacy behavior.
- [ ] Extend the plugin registration to include `lane: "approval"` (additive; no behavior change until gateway honors it).
- [ ] Add a structural test in `inline_handler_registration.test.js` that asserts the registration object carries `lane:"approval"` when the field is present. The test harness stub should capture the object; the test does not simulate lane selection.
- [ ] Ensure all existing tests remain green (parser, auth, terminal/transient/expired outcomes, dedupe, logging, no secret leakage).
- [ ] Update plugin `package.json` peer range comment or README to indicate "lane support expected in gateway >= X" once the gateway change lands (do not change the declared peer yet if it would break installs against 2026.5.28).

**Deliverable:** A PR against this repo that:
- Adds `lane:"approval"` to the registration object.
- Documents the contract.
- Adds the structural test.
- Does not claim lane separation is live.

### Phase 2 — Gateway patch (requires upstream source)

- [ ] In the gateway source, extend `registerPluginInteractiveHandler` to accept and store optional lane metadata.
- [ ] Implement `resolveTelegramLaneForInteractive({ chatId, lane, namespace })` with documented mapping (see §4.4).
- [ ] Modify the `callback_query` path (post-namespace match, pre-enqueue) to compute and use the resolved lane key.
- [ ] Ensure `dispatchPluginInteractiveHandler` and dedupe paths remain unchanged (dedupe key is still `callbackId`).
- [ ] Add/adapt gateway unit tests:
  - Registration with `lane:"approval"` stores the lane.
  - Dispatch for namespace `ollie_approval` on Telegram yields lane `telegram:<chatId>:approval` (or documented equivalent).
  - Dispatch without lane (or unknown lane) yields the default `telegram:<chatId>`.
  - Dedupe still keys on callback id and is lane-independent.
- [ ] Build and smoke-test the gateway locally against a harness that can observe lane selection (even if the harness is a thin shim).

**Deliverable:** A patch or PR against the OpenClaw upstream with:
- Exact file/line references to the changed ingress and registration modules.
- Passing unit tests for lane resolution.
- No changes to authorization, dedupe, or plugin handler contracts.

### Phase 3 — Integration verification (sandbox, not live)

- [ ] Install the patched gateway in a sandbox WSL or container.
- [ ] Install the updated plugin (with `lane:"approval"`).
- [ ] Use a test bot and a synthetic originating turn that blocks on a simulated long-running action.
- [ ] Emit an `ollie_approval:v1:*` callback while the turn is blocked.
- [ ] Assert:
  - The callback is delivered to the plugin handler without waiting for the originating turn to unblock.
  - The originating turn remains blocked until the action resolves (or times out).
  - Owner authorization, H-ref parsing, and dedupe continue to function.
  - Logs contain the correlation fields (message_id, ref, decision, auth, edit_result) without secrets.
- [ ] Exercise the fallback paths (transient 5xx, 404/unknown_or_expired, edit failure → reply fallback).
- [ ] Confirm that omitting `lane` from registration reverts to default behavior (for backward compatibility).

**Deliverable:** A short integration report with timestamps, log excerpts (sanitized), and pass/fail for each assertion. Do not run against the production bot or production Hands.

### Phase 4 — Staged deployment (after owner approval)

- [ ] Stage the patched gateway on the box (do not enable the plugin's lane behavior yet if it is conditional; the plugin can already emit `lane`).
- [ ] Restart the gateway; verify boot logs show successful load and handler registration (no `registerInteractiveHandler not available`).
- [ ] Capture baseline: current `lastUpdateId`, no in-flight approvals.
- [ ] Flip the plugin to produce a real approval keyboard from a controlled Hands action (or a synthetic test action if available).
- [ ] While the action is in-flight (originating turn blocked), press the button on Telegram.
- [ ] Observe:
  - The callback is processed (plugin logs `callback received` / `cb ...` lines).
  - The originating turn remains blocked (or proceeds only when the action itself resolves via `/consent`).
  - No handler-timeout on `telegram:<chatId>` for the callback update.
  - UI reflects the decision (editMessage or fallback reply).
- [ ] Run the text-command path (`approve <ref>`) in parallel to ensure it still blocks via `before_agent_run`.
- [ ] Revert or disable if anomalies appear; otherwise leave enabled.

**Deliverable:** Deployment log with:
- Gateway version/build id before and after.
- Plugin sha256 before and after.
- Sanitized excerpts showing callback delivery while a turn is blocked.
- Confirmation that all invariants (auth, dedupe, fail-closed, no secret leak) were preserved.

### Phase 5 — Post-deploy hardening (optional but recommended)

- [ ] Add a gateway capability probe (if the gateway exposes one) so the plugin can detect lane support and log a one-time notice if running against an older gateway.
- [ ] Add a metric or structured log line in the gateway (or plugin) that records the lane key chosen for each interactive dispatch (sanitized; no PII).
- [ ] Update the plugin README with the actual gateway version that first honored `lane:"approval"`.
- [ ] If a future gateway changes the lane key format, treat it as a contract change and update the plugin accordingly (with a peer range bump).

---

## 12. Risk register (for the implementer)

- **R1 — Upstream source unavailable.** Highest risk. Without the gateway TypeScript, the lane resolver cannot be written. Mitigation: document the contract; emit `lane` as a forward marker; pursue OpenClaw maintainers for a patch or source drop.
- **R2 — Lane key format drift.** If the gateway uses a different suffix than `:approval` for built-in approvals, the mapping must match exactly. Mitigation: inspect the gateway source for how built-in approvals compute their lane key and replicate that for the plugin path.
- **R3 — Dedupe TTL interaction.** If a long-running Hands turn exceeds the 5-min dedupe TTL, a second tap on the same button could be treated as a new event. This is pre-existing (not introduced by lane changes) but worth noting.
- **R4 — Spool restart side effects.** The isolated polling ingress can restart on handler-timeout (observed with 1500 s). If a callback is enqueued on a different lane and that lane's worker is unhealthy, the symptom moves rather than disappears. Mitigation: verify the approval lane has a healthy worker; consider a global/free lane if sequential behavior is not required.
- **R5 — Observability gap.** If the gateway does not log the chosen lane for interactive dispatches, verification relies on timing and absence of timeout. Mitigation: add a one-line structured log in the ingress when selecting a lane for an interactive callback.

---

## 13. Out of scope (explicitly)

- Changing the engine's `/consent` contract or adding new HTTP surfaces.
- Altering `before_agent_run` text-command routing or the unified owner-approval router.
- Touching secrets (approval.token, MCP bearer, bot token) in any plan step.
- Running live approvals against production Hands without explicit owner approval and a rollback plan.
- Refactoring Hands-side consent delivery or the originating turn's wait strategy (those are separate concerns; see the workaround in §9).

---

## 14. References (files and symbols)

**This repo (maintainable):**
- `openclaw-ollie-wa-approval/index.js` — registration (943), parser (595), owner auth (617), handler (644), always `{ handled: true }` (983)
- `openclaw-ollie-wa-approval/openclaw.plugin.json` — config schema (no lane)
- `openclaw-ollie-wa-approval/test/inline_handler_registration.test.js`
- `openclaw-ollie-wa-approval/test/callback_correlation.test.js`
- `openclaw-ollie-wa-approval/test/router.test.js`
- `ollie-hands/tests/test_inline_approval.py`, `test_consent_route.py`, `test_grant_executor_invariants.py`

**Box (gateway dist, not in this clone):**
- `.../openclaw/dist/bot-iSDqdz0Y.js` — callback_query (2799), dispatchTelegram... (1496), shouldSkipUpdate (7452), authorize... (2373)
- `.../openclaw/dist/plugin-runtime-DBz1g2if.js` — dispatchPluginInteractiveHandler (46)
- `.../openclaw/dist/command-registration-BWsYSY_K.js` — register... (179), resolve... (121)
- `.../openclaw/dist/loader-wybWjJVr.js` — registerInteractiveHandler (4571)
- Inferred: `getTelegramSequentialKey` (ingress lane selector → `telegram:<chatId>`)

**Local SDK stub (editor only):**
- `openclaw-ollie-wa-approval/node_modules/openclaw/package.json`
- `openclaw-ollie-wa-approval/node_modules/openclaw/plugin-sdk/plugin-entry.js`

**Prior investigation (context):**
- `Plans/immutable-baking-neumann-agent-a9f296541a0249c9b.md` — first failing boundary for a specific callback
- `Plans/immutable-baking-neumann-agent-af1fca9b74052c555.md` — source mapping for lane and dispatch symbols
- `Plans/curried-wiggling-eclipse-agent-a4828504b7d40e01c.md` — tap-flash diagnostic (button path, no lane change)

---

## 15. Bottom line

- The maintainable source for the **plugin contract** is in this repo; the maintainable source for the **lane selector and dispatch** is not.
- The smallest correct fix is an **optional `lane` field on `registerInteractiveHandler`** plus a **gateway resolver** that maps `lane:"approval"` to a non-chat lane **before enqueue**.
- The plugin change can be written today; the gateway change cannot without upstream source.
- All security, dedupe, authorization, and fail-closed invariants are preserved by keeping those checks in the plugin and keeping dedupe keyed on `callbackId`.
- The blocker is explicit: obtain the OpenClaw gateway source (or a patch from its maintainers) before claiming lane separation is achievable.

When the upstream source is available, Phase 2 can begin with the exact file/line targets listed in §6 and the resolver semantics in §4.4.
