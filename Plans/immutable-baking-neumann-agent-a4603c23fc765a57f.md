# Plan: Fix Telegram callback-query sequential-key deadlock for `ollie_approval` (and any future custom plugin namespace)

Repo under inspection (read-only): https://github.com/openclaw/openclaw @ `v2026.5.28` (commit `e93216080aa1f425d3ab127014603eba8e365b2d`).

This plan is design-only. Nothing in the openclaw tree will be touched in this session.

---

## 1. Confirmed deadlock

**Symbol chain (file paths are repo-relative to the upstream repo):**

1. `extensions/telegram/src/sequential-key.ts`
   - `getTelegramSequentialKey(ctx)` is the sole key function consumed by `sequentialize`.
   - For callback_query updates it currently branches **only** on `parseExecApprovalCommandText(ctx.update.callback_query.data)`. If that parser returns non-null it returns `telegram:<chatId>:approval`; otherwise it falls through to `telegram:<chatId>` (or `telegram:<chatId>:topic:<threadId>`), the same lane as ordinary chat text.
   - That means a non-`/approve` callback_query (plugin interactive button, model picker, pagination, multi-select, plugin binding approval, anything that goes through `dispatchPluginInteractiveHandler`) collides on the chat lane with any in-flight prompt.

2. `extensions/telegram/src/bot-core.ts`
   - `bot.use(botRuntime.sequentialize(getTelegramSequentialKey))` is the single wiring point. Lane isolation is entirely decided by the string returned by `getTelegramSequentialKey`.
   - `processMessage` and the `bot.on("callback_query", ...)` handler both run **behind** `sequentialize`. They serialize on whatever key `getTelegramSequentialKey` returns.
   - No other lane-routing hook is exposed by the SDK; every Telegram-side isolation decision goes through this one function.

3. `extensions/telegram/src/polling-session.ts` — isolated ingress
   - `#spooledUpdateLaneKey(update)` calls the same `getTelegramSequentialKey(...)`.
   - `activeSpooledUpdateHandlersByLane` is keyed by `buildSpooledUpdateHandlerKey({ spoolDir, laneKey })` where `laneKey` is exactly what `getTelegramSequentialKey` returns. **One in-flight handler per lane** (claim via `claimTelegramSpooledUpdate`, see `extensions/telegram/src/telegram-ingress-spool.ts`).
   - `#drainSpooledUpdates` skips any spooled update whose `laneKey` already has an active handler — and `claimedLaneKeys.has(laneKey)` will also block re-entry. The deadlock is exactly this: an active long-running text turn holds the chat lane; subsequent plugin callback_query updates spool on disk behind it.

**Where plugin callbacks are registered/dispatched (same tag):**

- Registry: `src/plugins/interactive-registry.ts` — `registerPluginInteractiveHandler(pluginId, registration, opts?)`. Registration is keyed on `toPluginInteractiveRegistryKey(channel, namespace)` = `"<channel>:<namespace>"`. Namespace validator in `src/plugins/interactive-shared.ts`: `^[A-Za-z0-9._-]+$`.
- Dispatcher (channel-agnostic core): `src/plugins/interactive.ts` — `dispatchPluginInteractiveHandler(...)`. Resolves namespace match via `resolvePluginInteractiveNamespaceMatch`, then **dedupes by `dedupeId`** using `claimPluginInteractiveCallbackDedupe` from `src/plugins/interactive-state.ts`.
- Telegram-side adapter: `extensions/telegram/src/interactive-dispatch.ts` — `dispatchTelegramPluginInteractiveHandler({ data, callbackId, ctx, respond, onMatched })`. This is what `bot-handlers.runtime.ts` calls inside `bot.on("callback_query", ...)` (see `bot-handlers.runtime.ts` line ~2143). It strips `data` into `{namespace, payload}` and passes `dedupeId = callbackId` (the Telegram `callback_query.id`).
- Callback_query handler block: `extensions/telegram/src/bot-handlers.runtime.ts` lines ~1953 onward. Order of attempts: `parsePluginBindingApprovalCustomId` → `dispatchTelegramPluginInteractiveHandler` → `parseTelegramManagedSelectCallback` → `parseExecApprovalCommandText` (legacy `/approve` buttons) → `commands_page_*` pagination → `parseModelCallbackData` → `parseTelegramNativeCommandCallbackData` fallback.

**Root cause in one sentence:** every non-`/approve` callback_query is routed onto the same chat lane as text, so any prompt in progress starves plugin interactive callbacks (and vice versa) both inline and on the spool. The `dispatchPluginInteractiveHandler` dedupe uses `callback.id`, which only protects against Telegram-driven redelivery — it does nothing about the upstream queue.

---

## 2. Constraints to preserve

These are non-negotiable and each constrains the fix shape:

- **Auth.** Sender authorization (`authorizeTelegramEventSender`, the `inlineButtonsScope` check, the explicit approval-allowlist check at `bot-handlers.runtime.ts` ~line 2289) must run **before** any lane decision. Lanes are a scheduling concern; they cannot widen the set of authorized senders.
- **Callback-id dedupe.** `dispatchPluginInteractiveHandler` already dedupes by `dedupeId` (= `callback.id`). The lane fix must not break the dedupe contract; ideally it should not even touch the dedupe path.
- **H-ref opacity.** Approval ids (`plugin:<uuid>`) must not leak into the lane key. The namespace string is fine — it is already public through `dispatchPluginInteractiveHandler` — but the approval payload (`payload` after `namespace:`) must stay opaque to lane logic. The current `/approve` branch satisfies this (it only checks the `data` prefix). The fix must keep the same posture for plugin interactive callbacks.
- **Hands-only resolution.** Live approval resolution must remain owner-only (i.e. the same `pluginApprovalAuthorizedSender` / `execApprovalAuthorizedSender` gate at `bot-handlers.runtime.ts` ~line 2289). The lane key is a scheduling primitive; it cannot encode authorization.
- **Fail-closed.** If `getTelegramSequentialKey` cannot derive a stable key for a callback_query (missing message, no chat id, unknown namespace), it must default to a chat-scoped lane, not a global one. This is already true today; the fix must not regress it.

---

## 3. Can callback-query updates use `telegram:<chatId>:approval` based on namespace before enqueueing?

**Yes — but only for namespaces whose registration declares it.** The check has to be made at the same logical layer that already understands the namespace, not at the lane layer alone. Concretely:

- `extensions/telegram/src/sequential-key.ts` currently calls `parseExecApprovalCommandText(ctx.update.callback_query.data)` to detect `/approve …` payloads. That is a prefix-only check that runs *before* the namespace registry is consulted. We can add a parallel, equally cheap prefix check for known interactive namespaces without touching the SDK.
- The dispatch order in `bot-handlers.runtime.ts` already establishes `dispatchTelegramPluginInteractiveHandler` as the canonical entry point. By the time the lane key is needed, grammy middleware has not yet run; we cannot call `resolvePluginInteractiveNamespaceMatch` directly from `getTelegramSequentialKey` because that would introduce an SDK import into a low-level hot path (and would also make the function async if any registration store access is async). The cheap, safe alternative is a **namespace-set check on the callback_data prefix**, gated by the same registry so we don't bake in a string.
- Namespaces match the validator `^[A-Za-z0-9._-]+$` and the resolver slices on the first `:` (see `resolvePluginInteractiveMatch` in `src/plugins/interactive-shared.ts`). So a `data.startsWith(namespace + ":")` check is exactly the prefix the resolver will use — no false positives at the registry level.

So the answer is: yes, **on the same terms as `/approve` today**, by recognizing the namespace prefix in `sequential-key.ts` and routing it to `telegram:<chatId>:approval`. The existing per-chat `:approval` lane is the right scope: there is exactly one approval owner per chat at a time, and the `/approve` branch already proved it serializes correctly.

---

## 4. Decision: smallest safe fix

**Recommendation: narrowly scoped custom `ollie_approval` callback lane, with the wiring designed so the same hook can later host additional plugin interactive namespaces without re-touching `sequential-key.ts`.**

Why not the fully generic option:

- A "generic plugin lane metadata API" would require (a) a new field on `PluginInteractiveHandlerRegistration` (e.g. `lane?: "chat" | "approval" | string`), (b) a new optional `getLaneKey(ctx, namespace, payload)` registration, and (c) a runtime that **reads the registry synchronously from inside `getTelegramSequentialKey`**. The synchronous constraint is the real problem: the SDK currently treats the registry as a module-level `Map` (`getPluginInteractiveHandlersState()`), so reads are sync, but introducing a metadata field is a permanent public API surface for one owner-only flow. It's premature surface area.
- The narrow option (a) reuses the existing `:approval` lane, (b) namespaces are already a first-class concept in the registry, (c) the prefix check is O(1) string ops, and (d) the registry lookup is the gate — unknown namespaces fall through to the chat lane unchanged. We can grow it into the generic option later if a second plugin asks for a custom lane; today the only namespace that needs out-of-chat ordering is the approval callback.

Concretely, the narrow option consists of:

### 4.1 Add a new symbol exported from the Telegram extension

`extensions/telegram/src/approval-callback-lane.ts` (new file):

```ts
import { listPluginInteractiveHandlers } from "openclaw/plugin-sdk/plugin-runtime";

const TELEGRAM_APPROVAL_NAMESPACE_PREFIXES = new Set<string>([
  "ollie_approval",          // owner-only Hands approval button lane
  // future owner-only lanes can be added here explicitly
]);

let cachedTelegramApprovalNamespaces: ReadonlySet<string> | undefined;

/**
 * Synchronous, test-overridable accessor that returns the set of
 * Telegram-channel interactive-handler namespaces that must be routed
 * onto the per-chat approval lane instead of the chat text lane.
 *
 * The set is intentionally a closure-managed snapshot so that
 * getTelegramSequentialKey can stay synchronous. Callers that mutate
 * the registry should re-snapshot via {@link refreshTelegramApprovalNamespaces}.
 */
export function getTelegramApprovalNamespaces(): ReadonlySet<string> {
  if (cachedTelegramApprovalNamespaces) {
    return cachedTelegramApprovalNamespaces;
  }
  const next = new Set<string>();
  for (const handler of listPluginInteractiveHandlers()) {
    if (handler.channel !== "telegram") continue;
    if (TELEGRAM_APPROVAL_NAMESPACE_PREFIXES.has(handler.namespace)) {
      next.add(handler.namespace);
    }
  }
  cachedTelegramApprovalNamespaces = next;
  return cachedTelegramApprovalNamespaces;
}

export function refreshTelegramApprovalNamespaces(): void {
  cachedTelegramApprovalNamespaces = undefined;
}

export function resetTelegramApprovalNamespacesForTests(): void {
  cachedTelegramApprovalNamespaces = undefined;
}
```

Notes:
- The hardcoded allowlist is the **owner-only** fix surface. Adding a namespace requires editing this file; that is the audit gate that satisfies "Hands-only resolution" and "fail-closed".
- `listPluginInteractiveHandlers` is already exported by the SDK (`src/plugins/interactive-registry.ts`). The snapshot is fine because plugin registration happens at startup, not on a hot path.

### 4.2 Extend `getTelegramSequentialKey` to use the snapshot

`extensions/telegram/src/sequential-key.ts`:

- Import `getTelegramApprovalNamespaces` from `./approval-callback-lane.js`.
- After the existing `parseExecApprovalCommandText(callbackData)` branch, add a parallel branch that, **only when `callbackData` is a non-empty string and `msg?.chat?.id` is a number**, calls `isTelegramApprovalNamespaceCallback(callbackData)`. If true, return `telegram:<chatId>:approval` (matching the existing `/approve` shape). Otherwise fall through unchanged.
- `isTelegramApprovalNamespaceCallback(data)` is implemented as: take `data.indexOf(":")`, take the prefix, and `getTelegramApprovalNamespaces().has(prefix)`. No allocation on the hot path beyond a single substring.

Pseudocode:

```ts
const callbackData = ctx.update?.callback_query?.data;
if (callbackData) {
  if (parseExecApprovalCommandText(callbackData) !== null) {
    if (typeof chatId === "number") return `telegram:${chatId}:approval`;
    return "telegram:approval";
  }
  if (isTelegramApprovalNamespaceCallback(callbackData)) {
    if (typeof chatId === "number") return `telegram:${chatId}:approval`;
    return "telegram:approval";
  }
}
```

This preserves the existing per-chat `:approval` lane semantics, including the timing/reply-fence behavior already tested in `extensions/telegram/src/telegram-reply-fence.test.ts` and the spool timing in `polling-session.ts` (`ISOLATED_INGRESS_BACKLOG_STALL_MS`, `#waitForSpooledUpdateHandlers`).

### 4.3 Hook the cache invalidation

`extensions/telegram/src/bot.ts` (or wherever the plugin registry is wired into the Telegram extension at boot — the test harness uses `clearPluginInteractiveHandlers` + `registerPluginInteractiveHandler`): call `refreshTelegramApprovalNamespaces()` immediately after the plugin registration loop. This guarantees the lane snapshot reflects the active registry before the first callback_query lands.

If no upstream entry point exists yet, the registry's `restorePluginInteractiveHandlers` (also exported by `src/plugins/interactive-registry.ts`) is the natural seam.

### 4.4 Register the Ollie namespace at boot

In the existing openclaw-ollie-wa-approval plugin (separate repo, see `./openclaw-ollie-wa-approval/approval-command.js`), add:

```ts
import { registerPluginInteractiveHandler } from "openclaw/plugin-sdk/plugin-runtime";

registerPluginInteractiveHandler(
  "openclaw-ollie-wa-approval",
  {
    channel: "telegram",
    namespace: "ollie_approval",
    handler: async (ctx) => {
      // existing owner-only resolution
      // ctx.callback.payload is the opaque approval payload (H-ref-free)
    },
  },
  { pluginName: "openclaw-ollie-wa-approval", pluginRoot: import.meta.dirname },
);
```

This guarantees `dispatchTelegramPluginInteractiveHandler` finds a handler, so the lane assignment at `sequential-key.ts` is honored end-to-end.

---

## 5. Where the tests go

All under `extensions/telegram/src/`:

1. `approval-callback-lane.test.ts` (new) — exercises `getTelegramApprovalNamespaces`:
   - empty registry → empty set
   - unknown namespace → not in set
   - `ollie_approval` registered for `telegram` → in set
   - `ollie_approval` registered for non-telegram channel → not in set
   - `refreshTelegramApprovalNamespaces()` re-reads after registration changes

2. Extend `extensions/telegram/src/sequential-key.test.ts` (already a parameterized `it.each` table — just add new rows):
   - `data: "ollie_approval:<opaque>"` → `"telegram:<chatId>:approval"`
   - `data: "ollie_approval:"` → `"telegram:<chatId>:approval"` (defensive; matches resolver behavior)
   - `data: "ollie_approval"` (no colon) → still routes to `:approval` because `resolvePluginInteractiveMatch` treats the whole string as the namespace; mirror that with the same `indexOf(":") >= 0` check
   - `data: "unknown_namespace:foo"` → unchanged: `"telegram:<chatId>"`
   - existing `/approve` row remains `"telegram:<chatId>:approval"`

3. Extend `extensions/telegram/src/polling-session.test.ts`:
   - Drive two synthetic `TelegramSpooledUpdate`s, one a text message, one an `ollie_approval` callback_query, both on the same chat.
   - Assert that the second one is **not** blocked by the first one's handler key (i.e. they get separate `laneKey`s).
   - With both as plain chat, assert the second one **is** blocked (current behavior preserved).

4. Extend `extensions/telegram/src/approval-handler.runtime.test.ts` only if the registration call moves through this file; otherwise skip to keep blast radius minimal.

---

## 6. Verification plan

Local (openclaw tree, before opening a PR upstream):

1. `pnpm --filter @openclaw/extension-telegram test -- sequential-key` — green, including new rows.
2. `pnpm --filter @openclaw/extension-telegram test -- approval-callback-lane` — green.
3. `pnpm --filter @openclaw/extension-telegram test -- polling-session` — green, including the lane-isolation row.
4. `pnpm --filter @openclaw/extension-telegram test` — full extension green; in particular `bot-handlers.runtime.test.ts` and `bot.create-telegram-bot.test.ts` must not regress (the lane key change only adds new branches, no existing branches are altered).
5. `pnpm -r test` — full repo green.

Manual reproducer to confirm the original deadlock is gone (do **not** run during this plan-mode session; included as the post-merge verification step):

1. Bring up the openclaw gateway with `ollie-wa-approval` loaded and `ollie_approval` registered.
2. From an authorized Telegram DM, send a long-running agent prompt (e.g. "research X"). Wait until you see the prompt reach the model.
3. From the same chat, tap the inline Approve button on an outstanding `ollie_approval` request.
4. Expect: the button resolves in well under the prompt's run time, the prompt continues uninterrupted, and no spool file is left behind for the callback.

Live box:

- Reproduce on `ollie@onllm.dev` after a non-prod deploy; confirm `journalctl --user`-free log inspection (per memory `feedback_no_systemctl_user.md`) shows the callback handler entered and exited, not "blocked by lane" and not "spooled".
- Owner-only check: a non-owner tap of the button still hits the existing `authorizeTelegramEventSender` denial at `bot-handlers.runtime.ts` ~line 2289, never reaches the lane, and is logged as "Blocked telegram approval callback from …". This proves the auth gate is upstream of the lane fix.

---

## 7. Patch outline (single PR into openclaw upstream)

Files added:

- `extensions/telegram/src/approval-callback-lane.ts`
- `extensions/telegram/src/approval-callback-lane.test.ts`

Files modified:

- `extensions/telegram/src/sequential-key.ts` — add one import, one new branch inside the `callbackData` block (sketch in §4.2).
- `extensions/telegram/src/sequential-key.test.ts` — new `it.each` rows.
- `extensions/telegram/src/polling-session.test.ts` — lane-isolation row (§5.3).
- `extensions/telegram/src/bot.ts` (or wherever plugin registration is wired) — call `refreshTelegramApprovalNamespaces()` after the registration loop.

Files NOT modified:

- `extensions/telegram/src/bot-handlers.runtime.ts` — auth, dedupe, and resolution paths untouched.
- `src/plugins/interactive*.ts` — no SDK changes; the generic metadata API can be added later when a second owner-only namespace justifies it.
- `extensions/telegram/src/approval-handler.runtime.ts` — no changes; `ollie_approval` is not an exec approval, so it correctly bypasses this file.

---

## 8. Risks and mitigations

- **Snapshot staleness if a plugin registers after boot.** Mitigation: explicit `refreshTelegramApprovalNamespaces()` call from the registration seam; covered by test.
- **Two plugins claiming the same namespace.** Already blocked by the registry at `registerPluginInteractiveHandler` (returns `{ ok: false, error: "…already registered…" }`). No new failure mode.
- **Approval payload leaking into lane key.** The lane check slices on the first `:`; everything after is ignored. Verified by the existing `resolvePluginInteractiveMatch` slicing rule and tested explicitly in §5.2.
- **Sync-vs-async in `getTelegramSequentialKey`.** The snapshot accessor is sync; the registry `Map` is sync; no `await` introduced on the hot path. The function signature stays synchronous.
- **Telegram's own namespace prefixes changing.** `ollie_approval` is a stable, plugin-owned string — there is no upstream registry it can collide with. The allowlist is explicit so any future addition is code-reviewed.
- **Spool starvation under heavy load.** The per-lane `ISOLATED_INGRESS_BACKLOG_STALL_MS` watchdog (polling-session.ts) already covers this for any lane; introducing a new namespace on `:approval` does not change the watchdog math.

---

## 9. Summary

- Confirmed the deadlock is in `extensions/telegram/src/sequential-key.ts` + `extensions/telegram/src/bot-core.ts` (inline) and `extensions/telegram/src/polling-session.ts` (isolated ingress), all routed by the single `getTelegramSequentialKey` function.
- Plugin interactive callbacks are registered via `registerPluginInteractiveHandler` (in the openclaw-ollie-wa-approval plugin), dispatched via `dispatchTelegramPluginInteractiveHandler` in `extensions/telegram/src/interactive-dispatch.ts`, and ultimately fan out from `bot.on("callback_query", ...)` in `extensions/telegram/src/bot-handlers.runtime.ts`.
- Callback_query updates **can** use `telegram:<chatId>:approval` based on namespace before enqueueing — by adding a sync, registry-driven prefix check inside `getTelegramSequentialKey`, mirroring the existing `/approve` branch.
- The smallest safe fix is a narrow custom `ollie_approval` lane (with explicit allowlist + snapshot cache), not a generic plugin-lane metadata API. The narrow fix is fully reversible into the generic shape later without changing the public lane contract.
- Auth, dedupe, H-ref opacity, Hands-only resolution, and fail-closed behavior are all preserved: lane routing is downstream of authorization, the dispatch dedupe still keys on `callback.id`, the lane key only consumes the opaque namespace prefix, the namespace must be in an explicit allowlist, and any unknown namespace or missing chat id falls back to the chat lane exactly as today.
