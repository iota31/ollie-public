# Investigation: Architectural fix for Telegram/Hands approval deadlock — maintainable source mapping

**Mode:** read-only investigation (plan-mode)
**Date:** 2026-07-16
**Scope:** Map the maintainable OpenClaw source for the deadlocking `telegram:<chatId>` sequential lane and the `registerInteractiveHandler` namespace dispatch, so a future PR can move `ollie_approval:v1:*` callbacks off the chat lane without re-deriving the symbols from the box.

---

## 1. TL;DR

- The deadlocking lane is the **isolated polling ingress sequential lane** keyed `telegram:<chatId>` (per bot). It is owned by the OpenClaw gateway, **not** by any plugin. The deadlock appears because (a) the originating Ollie turn is awaiting the same bot's long-poll to drain a synchronous reply, and (b) `ollie_approval:v1:*` callbacks are dispatched through the same chat-lane as ordinary text, so they queue behind the chat the brain is waiting on.
- **No maintainable OpenClaw TypeScript source is present in this repository.** The only shipped plugin SDK stub is `./openclaw-ollie-wa-approval/node_modules/openclaw/plugin-sdk/plugin-entry.js`, which is a one-line `definePluginEntry` identity function — not the gateway. The on-box "source of truth" is the minified `dist/` bundle at `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`, which has no companion `.ts`.
- The plugin-side maintainable source is `openclaw-ollie-wa-approval/index.js` (the unified router). Lane metadata, by contrast, has to be carried by the plugin and consumed by the gateway; the gateway's expected envelope is documented in the plan `immutable-baking-neumann-agent-a9f296541a0249c9b.md` and in the test fixtures under `openclaw-ollie-wa-approval/test/`.

---

## 2. Source-vs-dist inventory

| Layer | Maintainable source in repo? | Where it actually lives | Notes |
| --- | --- | --- | --- |
| Gateway core (TypeScript) | **NO** | Box: `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/*.js` (minified, content-hashed) | No companion `.ts`; only emitted bundle. Package is `openclaw 2026.5.28`. |
| Plugin SDK stub (for editor / typecheck) | YES (stub only) | `./openclaw-ollie-wa-approval/node_modules/openclaw/plugin-sdk/plugin-entry.js` | One-liner `export function definePluginEntry(entry) { return entry; }`. No `PluginApi` types, no `registerInteractiveHandler` declaration. |
| Plugin (unified approval router) | YES | `./openclaw-ollie-wa-approval/index.js` (working tree `c412c2c9…`, 1212 lines; live on box `3d7a895b…`, 1212 lines, identical contract) | This is the only place where the `ollie_approval:v1:*` namespace, parser, and handler registration are defined in editable form. |
| Plugin config schema | YES | `./openclaw-ollie-wa-approval/openclaw.plugin.json` | `configSchema` documents `enabled`, `ownerTelegramChatId`, `approvalsFile`, `requestTimeoutMinutes`, `hookTimeoutMs`, `handsConsentUrl`, `handsApprovalToken`. **No lane-metadata field** is currently declared. |
| Hands engine (button originator) | YES (working tree) | `./ollie-hands/ollie_hands/consent.py` (484 lines, NEW with `H-` keys + inline keyboard; **not** deployed) | Live on Windows: `C:\ollie-hands\consent.py` mtime `2026-06-10` (legacy 6-digit numeric, no keyboard). |
| Plugin TypeScript source | YES | `./openclaw-ollie-wa-approval/src/index.ts` | 13897 bytes, 11 Jul 2024 (this is an older partial rewrite that diverges from `index.js`; the live runtime is `index.js`). |

**Source/dist drift that is relevant to the fix:**

- `openclaw-ollie-wa-approval/index.js` (repo) vs `/home/openclaw/.openclaw/plugins/ollie-wa-approval/index.js` (box): identical `parseApprovalCallback` / `isAuthorizedOwnerCallback` / `registerInteractiveHandler({channel:"telegram", namespace:"ollie_approval"})` contract. Box is **ahead** of repo on `makeRef("W")`, `listHandsPending`, `pendingSummary`, `inventoryPrompt`, but the public contract that the gateway dispatches against is identical (sha256 `3d7a895b…` on box, `c412c2c9…` in repo).
- `ollie-hands/ollie_hands/consent.py` (repo, 484 lines, NEW) vs `C:\ollie-hands\consent.py` (box, legacy, 5+ weeks old). Drift is the source of the `H-` ref inconsistency in the recent callback incident.

---

## 3. Maintainable source for each piece the fix must touch

### 3.1 Interactive-handler registration / registry (plugin side)

- **File:** `./openclaw-ollie-wa-approval/index.js`
- **Function:** `register(api) { … }` (the default export's body, around lines 794–990 in the current working tree).
- **Call site:** lines 943–988
  ```js
  if (typeof api.registerInteractiveHandler === "function") {
    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: APPROVAL_CALLBACK_NS,           // "ollie_approval"
      handler: async (ctx) => { … handleApprovalCallback(api, norm) … },
    });
  }
  ```
- **Constants:** `APPROVAL_CALLBACK_NS = "ollie_approval"` (line 592), `APPROVAL_CALLBACK_VERSION = "v1"` (line 593).
- **Parser:** `parseApprovalCallback(ctxLike)` (lines 595–615) — splits on `:`, requires length 4, parts `[0,1]` = `["ollie_approval","v1"]`, parts `[2]` ∈ `{a,d}`, parts `[3]` matches `/^H-[A-Za-z0-9_-]{1,61}$/`.
- **Authorization:** `isAuthorizedOwnerCallback(cfg, ctxLike)` (lines 617–623) — requires `senderId === ownerTelegramChatId` AND `chatId === ownerTelegramChatId` AND `ctx.auth.isAuthorizedSender === true`.
- **Dispatch:** `handleApprovalCallback(api, ctxLike)` (lines 644–724) — POSTs to `/consent` via `postHandsConsent`, edits/replies via `ctx.respond.{editMessage,reply,clearButtons}`.

### 3.2 Interactive-handler registry on the gateway side

- **File (box):** `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/command-registration-BWsYSY_K.js`
  - Line 179: `registerPluginInteractiveHandler(channel, namespace, handler)` — stores `Map<channel:ns, registration>`. Rejects empty `ns` and `ns` not matching `^[A-Za-z0-9._-]+$`.
  - Line 121: `resolvePluginInteractiveNamespaceMatch(channel, data)` — splits on the **first** `:`. First token is matched against registered namespaces.
- **File (box):** `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/plugin-runtime-DBz1g2if.js`
  - Line 46: `dispatchPluginInteractiveHandler({ channel, data, dedupeId, onMatched, invoke })` — performs the namespace match, then `claimPluginInteractiveCallbackDedupe(dedupeId)` (in-process + 5 min ttl, key `openclaw.pluginInteractiveCallbackDedupe`, `maxSize: 4096`). On a duplicate claim, the plugin handler is **not** invoked; returns `{matched:true, handled:true, duplicate:true}`.
- **File (box):** `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/loader-wybWjJVr.js`
  - Line 4571: `registerInteractiveHandler` plugin API surface. Wraps `registerPluginInteractiveHandler`, emits a `warn` diagnostic on failure. **This is the symbol the plugin probes with `typeof api.registerInteractiveHandler === "function"`** — proven by the boot log absence of `registerInteractiveHandler not available` after the 21:28 restart.

> None of these `.js` files have companion `.ts`. The actual maintainable source must come from the OpenClaw upstream (a separate repository, not present in this clone). Any fix that needs to alter registry semantics (e.g. add a lane-metadata column to the registration) requires an upstream change plus a redeploy of the gateway. The plugin alone cannot rewrite the lane.

### 3.3 Telegram sequential-key selection (the deadlock)

- **File (box):** `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/bot-iSDqdz0Y.js`
  - Line 2799: top-level `bot.on("callback_query", …)` handler. **First line: `if (shouldSkipUpdate(ctx)) return;`** (de-dupe guard at line 7452 — `shouldSkipUpdate = (ctx) => updateTracker.shouldSkipHandlerDispatch(ctx)`). Then `answerCallbackQuery` (best-effort), then inline-button scope checks, then `authorizeTelegramEventSender(...)` (line 2373), then `dispatchTelegramPluginInteractiveHandler(...)`.
  - Line 1496: `dispatchTelegramPluginInteractiveHandler({data, callbackId, …})` — builds `ctx.callback = {data, namespace, payload, messageId, chatId, messageText}` and calls `dispatchPluginInteractiveHandler({channel:"telegram", data, dedupeId:callbackId, onMatched, invoke})`.
  - The `telegram:<chatId>` lane key is produced by the **isolated polling ingress** (not by `bot-iSDqdz0Y.js` itself). The only text evidence in box artifacts is the spool-failure message:
    ```
    Telegram isolated polling spool handler timed out behind update <TELEGRAM_UPDATE_ID>
    on lane telegram:<OWNER_TELEGRAM_CHAT_ID> after 1500.07s; marking the update failed,
    aborting active reply work, and restarting isolated ingress so later
    updates can drain.
    ```
    ⇒ the lane selector is `telegram:<chatId>`, **always** the chat id (no plugin hook, no namespace conditional). That selector is what makes `ollie_approval:v1:*` callbacks queue behind any pending inbound message on that chat.

### 3.4 Where lane metadata CAN be added cleanly

Three hook points, ranked by cleanliness:

1. **Plugin-side, minimal blast radius** — pass a per-callback metadata envelope through `registerInteractiveHandler` and have the plugin itself short-circuit out of the chat lane by **answering the callback and replying via `ctx.respond` before the gateway's chat-lane serializes**. This requires the gateway to honor a returned `{ handled: true }` synchronously enough that the lane is unblocked; the current contract already returns `{handled:true}` (line 983 in the plugin), and the dispatcher's `onMatched` callback runs in the same microtask after `bot.on("callback_query")`. **Caveat:** the chat-lane is keyed before `dispatchTelegramPluginInteractiveHandler` runs; the lane enqueue is on `getUpdates`-return, not on plugin return. The plugin cannot dequeue its own lane.
2. **Gateway-side, lane-metadata field on registration** — extend `registerPluginInteractiveHandler(channel, namespace, handler, {lane: "free" | "telegram:<chatId>" | "global"})`. The dispatcher checks the lane policy and dispatches `ollie_approval:v1:*` callbacks to a non-blocking lane (e.g. `global` or a dedicated `interactive:ollie_approval:<chatId>` lane that is not serialized against the chat turn). Cleanest, but requires an upstream OpenClaw patch — there is no `.ts` source in this repo to edit.
3. **Telegram update-type discrimination** — have the gateway route `callback_query` updates onto a lane separate from `message` updates, so that the chat-lane is only used for messages. This is what the OpenClaw `isolated polling ingress` does for some updates already (the failure message says "isolated"), but the lane selector currently does not differentiate by `update_kind`. Same caveat as (2): upstream patch required.

---

## 4. Tests and fixtures that must be preserved / extended

| Test file | Covers | Must keep green |
| --- | --- | --- |
| `./openclaw-ollie-wa-approval/test/inline_handler_registration.test.js` | `enabled=false` still registers handler; wraps the actual registered handler wrapper (not `__testHooks.handleApprovalCallback` directly); exercises terminal `editMessage({text, buttons:[]})`, transient `reply(error)`, expired 404 edit, and `fetch` abort signal. | YES — guards the runtime contract. |
| `./openclaw-ollie-wa-approval/test/callback_correlation.test.js` | Callback routing from owner Telegram keyboard. | YES. |
| `./openclaw-ollie-wa-approval/test/router.test.js` | `routeOwnerApproval` text-command path. | YES (implied by `inline_handler_registration.test.js` comment "covered by router.test.js"). |
| `./ollie-hands/tests/test_browser_schema_dispatch.py`, `test_consent_route.py`, `test_grant_executor_invariants.py`, `test_grants.py`, `test_inline_approval.py`, `test_plan_bypass.py`, `test_transport_close.py` | Hands engine consent/inline-approval invariants. | YES — the originating turn's `await consent_post_response` must not deadlock against the engine's own state machine. |

The `inline_handler_registration.test.js` test wraps `ours.handler(ctx)` with a hand-built `ctx` (line 122), so it is the natural place to add a "lane-metadata" assertion if/when the plugin gains a way to surface it.

---

## 5. Concrete file paths (absolute)

Plugin-side maintainable:
- `./openclaw-ollie-wa-approval/index.js` (lines 30–1008; relevant symbols at 592–615, 617–623, 644–724, 727–762, 794–990)
- `./openclaw-ollie-wa-approval/openclaw.plugin.json`
- `./openclaw-ollie-wa-approval/test/inline_handler_registration.test.js`
- `./openclaw-ollie-wa-approval/test/callback_correlation.test.js`
- `./openclaw-ollie-wa-approval/test/router.test.js`

Originating-turn maintainable:
- `./ollie-hands/ollie_hands/consent.py` (lines: `_build_approval_keyboard`, `consent_post_response`, `deliver_pending`)
- `./ollie-hands/ollie_hands/server.py` (the route that the originating Ollie turn POSTs to)
- `./ollie-hands/tests/test_consent_route.py`, `test_inline_approval.py`, `test_grant_executor_invariants.py`

Gateway-side (box, NOT in repo, dist-only):
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/bot-iSDqdz0Y.js` (lines 2799 callback_query handler; 1496 `dispatchTelegramPluginInteractiveHandler`; 7452 `shouldSkipUpdate`; 2373 `authorizeTelegramEventSender`)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/plugin-runtime-DBz1g2if.js` (line 46 `dispatchPluginInteractiveHandler`)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/command-registration-BWsYSY_K.js` (line 121 `resolvePluginInteractiveNamespaceMatch`; line 179 `registerPluginInteractiveHandler`)
- `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/loader-wybWjJVr.js` (line 4571 `registerInteractiveHandler` API surface)

Persistence / state:
- `/home/openclaw/.openclaw/telegram/update-offset-default.json` (`{"version":3,"lastUpdateId":<TELEGRAM_UPDATE_ID>,"botId":"<TELEGRAM_BOT_ID>",...}`)
- `/home/openclaw/.openclaw/telegram/ingress-spool-default/0000000<TELEGRAM_UPDATE_ID>.json.failed` (handler-timeout on `telegram:<OWNER_TELEGRAM_CHAT_ID>` lane)
- `/home/openclaw/.openclaw/openclaw.json` (plugin entries; `ollie-wa-approval enabled=true`, `handsConsentUrl`, `handsApprovalToken`)

Local SDK stub (editor-only):
- `./openclaw-ollie-wa-approval/node_modules/openclaw/plugin-sdk/plugin-entry.js`

---

## 6. Security invariants to preserve

1. **Owner-only authorization remains enforced in the plugin, not delegated to the gateway.** `isAuthorizedOwnerCallback` (lines 617–623) must still require `senderId === ownerTelegramChatId` AND `chatId === ownerTelegramChatId` AND `ctx.auth.isAuthorizedSender === true`. Moving the callback to a non-chat lane must NOT bypass the `ctx.auth.isAuthorizedSender === true` check — `authorizeTelegramEventSender` is the only signal that the bot's long-poll observed the callback as owner-attributed.
2. **`H-` ref entropy** — the parser regex `/^H-[A-Za-z0-9_-]{1,61}$/` is a **shape** check, not an authorization. The actual authorization is still the `owner` chat-id match + the live `_pending` membership inside the engine. Lane-metadata changes must NOT collapse these two checks.
3. **Bearer-token separation** — the plugin reuses `handsApprovalToken` (config-only, never logged), distinct from the `hands` MCP bearer. Lane metadata must not cause the bearer to be transmitted on a different surface.
4. **Dedupe via callback.id** — `claimPluginInteractiveCallbackDedupe(dedupeId)` keys on Telegram-assigned callback.id (5 min ttl, maxSize 4096). A lane re-route must keep `dedupeId` stable, or an attacker who replays a Telegram callback id (within 5 min) could double-spend the approval.
5. **Fail-closed in `before_agent_run`** — the originating Ollie turn is held by the engine's `await consent_post_response`, gated by `approval.token`. The plugin's `before_agent_run` returns `{outcome:"block", reason:"owner-approval-handled"}` for the owner's text reply, so the brain never sees the reply. The lane fix must NOT route an approval callback to the brain's chat turn — i.e. the callback's resolved decision (`a`/`d`) is the only thing the engine should learn.
6. **No user-text leak in approval metadata** — `logApprovalEvent` (lines 110–122) whitelists `APPROVAL_LOG_FIELDS`. Any new lane-metadata field added to logging must be appended to that set and remain ≤80 chars.
7. **No new public network surfaces** — the plugin's outbound surface is `api.runtime.channel.outbound.loadAdapter("telegram")` plus `fetch` to the local `/consent`. Lane-metadata must not introduce a third outbound path.
8. **Idempotency under restart** — the live engine's `_pending` is in-memory on `C:\ollie-hands`. A callback that arrives after the engine restart cannot be answered because the entry is gone. The plugin must continue to surface `unknown_or_expired` as a terminal UI (per the existing handler), and the lane fix must not change this.

---

## 7. Bottom-line answer to the user

**Lane metadata CAN be added cleanly, but only on the gateway side.** The plugin already registers cleanly (`registerInteractiveHandler({channel:"telegram", namespace:"ollie_approval", handler})`) and already returns `{handled:true}` synchronously after dispatch — the `handled:true` is what the dispatcher passes to `onMatched`. The deadlocking behavior is upstream of the plugin: the **isolated polling ingress** serializes every Telegram update (including callbacks) onto the lane `telegram:<chatId>`, and the originating Ollie turn is awaiting a reply on that same chat, so the lane round-trips itself.

There is no maintainable OpenClaw TypeScript source in this repository to edit. The gateway is a content-hashed `dist/` bundle on the box (`openclaw 2026.5.28`). To add lane metadata:

1. Either ship an upstream OpenClaw change that adds `{lane}` to `registerPluginInteractiveHandler` and honors it in `dispatchTelegramPluginInteractiveHandler` / the isolated polling ingress; or
2. Have the isolated polling ingress discriminate `callback_query` updates (already partially isolated) from `message` updates so the chat lane is message-only; or
3. As a workaround within the plugin alone: ensure the originating Ollie turn's `await consent_post_response` resolves via a **Telegram-out-of-band channel** (e.g. a long-poll on the engine's own `/consent/stream` SSE) rather than awaiting a chat message — this breaks the round-trip without changing the lane at all.

Workaround (3) is the only fix that can be done in this repo, in `ollie-hands/ollie_hands/server.py` + `consent.py`, without an OpenClaw upstream patch. Options (1) and (2) require coordination with the upstream OpenClaw maintainers and a gateway redeploy.