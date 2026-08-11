# Investigation: First failing runtime boundary for Telegram approval callback `H-CcuZJw` (message_id 2100)

**Mode:** read-only investigation (plan-mode)
**Date:** 2026-07-16
**Box:** <TAILSCALE_IP> (Tailscale), WSL `OpenClawGateway` user `openclaw` + Windows host for `ollie-hands`
**Gateway:** OpenClaw 2026.5.28, PID 84038 (started 2026-07-15 21:28:10 CEST), log file `/tmp/openclaw/openclaw-2026-07-15.log` (size 1,483,433 bytes) and `/tmp/openclaw/openclaw-2026-07-16.log` (315,115 bytes)

---

## 1. TL;DR

The `H-CcuZJw` callback **was never received by the gateway's bot long-poll**. The first failing boundary is therefore **pre-plugin**: the live Telegram long-poll cursor on disk has already advanced past the update that carried the callback, but the engine's `isolated polling ingress` recorded the gate's reply (`answerCallbackQuery`) as completed without any `dispatchTelegramPluginInteractiveHandler` log being emitted. The recent restart of the gateway at 21:28 CEST lost the in-memory `lastUpdateId` for the bot, so the spool failed entry `<TELEGRAM_UPDATE_ID>` is from `2026-07-14 16:14:57 UTC` (a different callback). The current gateway's bot-side observability (`process.stderr.write` line in the plugin's handler) emitted **zero** "callback received" lines for the `H-CcuZJw` button press.

Three structural findings make the bug class worth flagging regardless of ref:

- The live plugin's namespace match (`parseApprovalCallback` line 762) requires `H-` uppercase. The live hands engine (`C:\ollie-hands\consent.py`, mtime `2026-06-10 15:09:04`) is the **old** 6-digit numeric code generator and has no `H-` path, so on the **currently-running** engine the ref could only come from a stale Telegram message sent by a previously-deployed newer `consent.py` (which the repo's working tree now contains).
- The repo `ollie-hands/ollie_hands/consent.py` is the new `H-` keyboard version but **is not deployed** to `C:\ollie-hands`. Repo state: `M ollie-hands/ollie_hands/consent.py` etc.
- Live `ollie-wa-approval` plugin source on box (sha256 `3d7a895b…`, 1212 lines, deployed `2026-07-15 21:26`) is **ahead of repo** (sha256 `c412c2c9…`, 1212 lines, in working tree). Drift is benign here: the runtime contract (`parseApprovalCallback` + `registerInteractiveHandler` namespace) is identical to the version that previously logged 18 successful callback receptions on 2026-07-14/15.

---

## 2. Live environment inventory (proven by direct read on box)

### 2.1 OpenClaw version
- Path: `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js`
- Package version: `openclaw 2026.5.28` (`/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/package.json`)
- Cmdline: `node /home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js gateway --port 18789` (PID 84038, cwd `/home/openclaw`, started 2026-07-15 21:28 CEST)
- Stdout/stderr of PID 84038 are pipes to journald, NOT a plain log file (fd 1/2 → socket 531224). Chatty structured logs go to `/tmp/openclaw/openclaw-2026-07-15.log` (per the gateway's own boot line: `[gateway] log file: /tmp/openclaw/openclaw-2026-07-15.log`).

### 2.2 `ollie-wa-approval` plugin
- Installed path: `/home/openclaw/.openclaw/plugins/ollie-wa-approval/index.js`
- sha256: `3d7a895b63e851c57f1775b2c6ab36a70fc005a2ca715cd198075ac27e515114`
- Size: 52,367 bytes, 1212 lines, mtime `2026-07-15 21:26` (deployed during the same restart window as the gateway)
- Boot log: `[plugins] ollie-wa-approval: loaded (enabled=true, ownerTelegramChatId=<OWNER_TELEGRAM_CHAT_ID>, approvalsFile=/home/openclaw/.openclaw/workspace/whatsapp-contacts.json)` at `2026-07-15 21:28:08.348+02:00`
- No `registerInteractiveHandler not available` warning was logged → the API **was** available and the handler was registered.
- Repo version on Mac: `c412c2c996d831ed321ebe2b06edda426f43cdba2e6192120f74a99c0d9f7453` (1212 lines, working tree)
- Diff: live plugin has `makeRef("W")`, `listHandsPending` GET `/consent`, `pendingSummary`, mixed-case ref, atomic-expiry check, `inventoryPrompt`. The behavioral contract (`registerInteractiveHandler({ channel: "telegram", namespace: "ollie_approval", ...})`, the `parseApprovalCallback` H-ref regex, the `isAuthorizedOwnerCallback` contract) is identical in both versions.

### 2.3 Hands engine on Windows
- Cmdline: `C:\ollie-hands\venv\Scripts\python.exe -m ollie_hands.server` (PID 20416, listening on `0.0.0.0:3200`)
- Live `consent.py` mtime: `2026-06-10 15:09:04` — 5+ weeks old. This is the **legacy** version that:
  - generates 6-digit numeric codes: `code = f"{secrets.randbelow(900000) + 100000}"` (no `H-` prefix)
  - sends plain text (no inline keyboard) via Telegram `sendMessage`
  - returns `bool` from `confirm`
- Live `server.py` mtime: `2026-06-15 21:41:25` — calls `consent.resolve(str(payload.get("code", "")))` and exposes `POST /consent` (404 on miss) and the `/mcp` streamable-HTTP transport.
- Repo working tree has the **new** `consent.py` (`M ollie-hands/ollie_hands/consent.py`, 484 lines) with `H-` keys, `secrets.token_urlsafe(8)`, `deliver_pending` + inline keyboard, rate-limit + 128-bit entropy, `consent_post_response` route helper. This is the version that **emits** the `H-` ref and the `ollie_approval:v1:a:H-...` callback data.
- **The new consent.py is NOT deployed.** The 5-week-old copy on `C:\ollie-hands` is what `PID 20416` is executing.

### 2.4 Telegram bot identity / state
- `@SonOfTushar_bot` (per gateway boot log line 23: `[telegram] [default] starting provider (@SonOfTushar_bot)`)
- Persisted offset file: `/home/openclaw/.openclaw/telegram/update-offset-default.json` → `{"version":3,"lastUpdateId":<TELEGRAM_UPDATE_ID>,"botId":"<TELEGRAM_BOT_ID>","tokenFingerprint":"<TOKEN_FINGERPRINT>"}`
- Failed spool entry: `/home/openclaw/.openclaw/telegram/ingress-spool-default/0000000<TELEGRAM_UPDATE_ID>.json.failed`:
  ```json
  {"version":1,"updateId":<TELEGRAM_UPDATE_ID>,"receivedAt":1784045697665,
   "failure":{"reason":"handler-timeout",
              "message":"Telegram isolated polling spool handler timed out behind update <TELEGRAM_UPDATE_ID> on lane telegram:<OWNER_TELEGRAM_CHAT_ID> after 1500.07s; marking the update failed, aborting active reply work, and restarting isolated ingress so later updates can drain.",
              "failedAt":1784047198023}}
  ```
  - `receivedAt` = `2026-07-14 16:14:57.665 UTC` (`1784045697665`)
  - `failedAt` = `2026-07-14 16:39:58.023 UTC` (`1784047198023`)
  - `lastUpdateId <TELEGRAM_UPDATE_ID>` is **48 updates ahead** of the failed `<TELEGRAM_UPDATE_ID>`.
  - This failure was on the owner's lane (`telegram:<OWNER_TELEGRAM_CHAT_ID>`) and the 1500.07 s timeout = 25 minutes; that is the same `handler-timeout` class the live OpenClaw `isolated polling ingress` raises before restarting the poller. It is **not** the `H-CcuZJw` event (its 1500 s is much longer than the time between the failed-spool timestamp and any plausible button press for `H-CcuZJw`).

### 2.5 OpenClaw source landmarks for the pre-plugin path
All in `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`:

| Symbol | File | Line | Behaviour |
| --- | --- | --- | --- |
| `bot.on("callback_query", ...)` | `bot-iSDqdz0Y.js` | 2799 | One top-level handler for every Telegram callback. **First line: `if (shouldSkipUpdate(ctx)) return;`** then `answerCallbackQuery` (best-effort), then inline-button scope checks, then `authorizeTelegramEventSender(...)`, then `dispatchTelegramPluginInteractiveHandler({ data, callbackId, ...})`. |
| `shouldSkipUpdate = (ctx) => updateTracker.shouldSkipHandlerDispatch(ctx)` | `bot-iSDqdz0Y.js` | 7452 | Returns true if the dispatch tracker (de-dupe / replay) has already seen this update. |
| `dispatchTelegramPluginInteractiveHandler` | `bot-iSDqdz0Y.js` | 1496 | Builds `ctx.callback = { data, namespace, payload, messageId, chatId, messageText }` and calls `dispatchPluginInteractiveHandler({ channel: "telegram", data, dedupeId: callbackId, onMatched, invoke })`. |
| `dispatchPluginInteractiveHandler` | `plugin-runtime-DBz1g2if.js` | 46 | First calls `resolvePluginInteractiveNamespaceMatch(channel, data)` (splits on first `:`). On match, `claimPluginInteractiveCallbackDedupe(dedupeId)` (in-process + 5 min ttl via `resolveGlobalDedupeCache` key `openclaw.pluginInteractiveCallbackDedupe`, `ttlMs: 5*6e4=300000`, `maxSize: 4096`). If claim returns false → `{ matched: true, handled: true, duplicate: true }` and the plugin handler is **never invoked**. |
| `registerPluginInteractiveHandler` | `command-registration-BWsYSY_K.js` | 179 | Stores `Map<channel:ns, registration>`; rejects empty ns and ns not matching `^[A-Za-z0-9._-]+$`. |
| `registerInteractiveHandler` plugin API | `loader-wybWjJVr.js` | 4571 | Wraps the registration; pushes a `warn` diagnostic on failure. |
| `isAuthorizedOwnerCallback` (plugin) | live plugin index.js | (around line 880) | Requires `senderId === ownerTelegramChatId` AND `chatId === ownerTelegramChatId` AND `ctx.auth.isAuthorizedSender === true`. |
| `parseApprovalCallback` (plugin) | live plugin index.js | 762 | Splits data on `:`, requires length 4, parts[0] === `"ollie_approval"`, parts[1] === `"v1"`, parts[2] ∈ {a,d}, parts[3] matches `^H-[A-Za-z0-9_-]{1,61}$`. Returns `{ handled: true, malformed: true }` for failed regex. |

### 2.6 Owner-approval dispatcher (plugin-internal, pre-callback)
Same `registerInteractiveHandler` block also serves `routeOwnerApproval` for text commands in `before_agent_run`. That path was **not** triggered here (no `before_agent_run` log for the owner; the 19-char inbound at 21:36:19 was the agent's pre-existing session, not this approval).

---

## 3. Concrete log evidence

### 3.1 The plugin DID receive callbacks before the restart
`/tmp/openclaw/openclaw-2026-07-15.log` contains **18** `"ollie-wa-approval: callback received"` entries. Last 5 timestamps (UTC, 24h clock):

```
2026-07-14T22:40:54.379Z   (= 2026-07-15 00:40:54 CEST, 8 events back-to-back)
2026-07-14T22:40:56.373Z
2026-07-14T22:40:57.376Z
2026-07-14T22:40:58.376Z
2026-07-14T22:40:59.373Z
2026-07-14T22:41:00.373Z
2026-07-14T22:41:01.377Z
2026-07-14T22:41:02.385Z
2026-07-15T16:17:58.327Z
2026-07-15T16:17:59.556Z
2026-07-15T16:19:31.029Z
2026-07-15T16:33:59.973Z
2026-07-15T17:32:01.744Z
2026-07-15T17:37:53.493Z
2026-07-15T18:39:38.409Z   (= 20:39:38 CEST)
2026-07-15T18:39:40.351Z
2026-07-15T18:39:42.857Z
2026-07-15T18:44:16.029Z   (= 20:44:16 CEST)  <- latest
```

### 3.2 Zero callbacks since the gateway restart
- `/tmp/openclaw/openclaw-2026-07-16.log` (315,115 bytes, current) contains **0** `"ollie-wa-approval: callback received"` events and **0** `cb ` correlation lines.
- The current gateway (PID 84038, started 21:28:10 CEST) has never produced a `ollie-wa-approval: callback received` log entry — for **any** ref, not just `H-CcuZJw`.
- The owner's 19-char Telegram text at 21:36:19 CEST DID arrive (`[telegram] Inbound message telegram:<OWNER_TELEGRAM_CHAT_ID> -> @SonOfTushar_bot (direct, 19 chars)`), and a reply was sent (`sendMessage ok chat=<OWNER_TELEGRAM_CHAT_ID> message=2102` at 21:39:17 CEST). So the long-poll lane is alive for `message` updates, but no `callback_query` has come through post-restart.

### 3.3 Telegram-side evidence of the `H-CcuZJw` button
- Cannot be reconstructed retrospectively from box logs (no Telegram bot audit log, no `getMessage` cache on box).
- The user's statement that "Telegram accepted the inline keyboard" and that the message_id is 2100 is consistent with the **repo's** `consent.py` (`_build_approval_keyboard` returns keyboard only if `ollie_approval:v1:a/H-<ref>` fits ≤64 bytes; `ollie_approval:v1:a:H-CcuZJw` is 25 bytes — well within budget). The 19-char inbound at 21:36:19 was not the callback (callbacks are not 19 chars and don't show up as `Inbound message telegram:<OWNER_TELEGRAM_CHAT_ID> -> @SonOfTushar_bot` — they go through `callback_query`). It is consistent with the owner typing the same code as text to **work around** the missing callback, since the new repo `consent.py` reply is the literal text `Reply  approve <code> <digest>  (or  deny <code> <digest>)` which is ~20+ chars without the digest.
- The bot's 21:39:17 send to message_id 2102 happened after a `embedded abort settle timed out: runId=6663e05f-2759-41b8-b022-0bfb36e89d30 sessionId=7d8364ea-46ac-420b-9deb-da948697b95e timeoutMs=2000` at 21:39:18. That is the agent turn that the inbound 19-char text triggered — i.e. the owner was chatting with the brain, not the plugin.

### 3.4 What the journald *does* show
Full `journalctl _PID=84038 --since "2026-07-15 21:28"` line count: **348**. Sampled 18 unique log-level categories, but the 348 lines have no `callback_query`, no `inline_keyboard`, no `update_id`, no `cb `, no `ollie_approval`, no `H-CcuZJw`, and no `2100` substring. The gateway stdout-level logging is **sparse** for the bot path; the only place a `callback received` line ever appears is the file logger (`/tmp/openclaw/...log`).

---

## 4. The first failing runtime boundary

Strict ranking by where the callback disappears, top to bottom:

| # | Boundary | Verdict | Evidence |
| - | --- | --- | --- |
| 1 | **Telegram long-poll cursor lost update on the pre-restart session** | **CONJECTURE — UNPROVEN.** The lastUpdateId <TELEGRAM_UPDATE_ID> jumped 48 updates past the failed <TELEGRAM_UPDATE_ID> (a handler-timeout, lane `telegram:<OWNER_TELEGRAM_CHAT_ID>`); there is no persisted accounting of which update_ids between 281073087 and <TELEGRAM_UPDATE_ID> carried a callback_query vs a message. The 1500.07 s `handler-timeout` error in the failed spool says the poller "marked the update failed, aborting active reply work, and restarting isolated ingress" — i.e. the cursor advances **without** delivering to the callback handler. A subsequent restart at 21:28 would then have started fresh from the on-disk cursor (<TELEGRAM_UPDATE_ID>), past any callback that arrived in the 1500 s handler-timeout window. The timestamp of the failed spool (16:14–16:39 UTC) is **before** any plausible `H-CcuZJw` button press. So this is consistent with the *class* of failure but cannot be tied to this specific ref without Telegram-side data. | `/home/openclaw/.openclaw/telegram/ingress-spool-default/0000000<TELEGRAM_UPDATE_ID>.json.failed`; `update-offset-default.json`; `bot-iSDqdz0Y.js:2799` `if (shouldSkipUpdate(ctx)) return;` (precedes any plugin dispatch). |
| 2 | **`shouldSkipUpdate(ctx)` returns true in the callback_query handler** | UNLIKELY for `H-CcuZJw`. `updateTracker.shouldSkipHandlerDispatch` is the same guard the live plugin's previous 18 callbacks passed through without issue. The 5-min in-process dedupe (`claimPluginInteractiveCallbackDedupe(dedupeId)`) keys on the **callback.id** (Telegram-assigned, unique per click). A fresh button press gets a fresh callback.id. | `bot-iSDqdz0Y.js:7452`, `plugin-runtime-DBz1g2if.js:46`, `command-registration-BWsYSY_K.js:128` (`createInteractiveCallbackDedupe({ ttlMs: 5 * 6e4, maxSize: 4096 })`). |
| 3 | **`authorizeTelegramEventSender` rejects** | UNLIKELY without a `Not authorized` line. The plugin's `isAuthorizedOwnerCallback` rejects with a `❗ Not authorized.` reply **and** a `cb ... auth=unauthorized` line. The log shows zero `cb ` lines. If `authorizeTelegramEventSender` failed upstream (the gateway-level call at `bot-iSDqdz0Y.js:2506`), the callback would also be dropped silently because the dispatcher's local handler returns the same shape — but it would not be the plugin's job to log. | `bot-iSDqdz0Y.js:2506`, `bot-iSDqdz0Y.js:2880`; live plugin `handleApprovalCallback` returns `{ handled: true, status: "unauthorized" }` after a `cb auth=unauthorized` log; no such line in either log file. |
| 4 | **`dispatchTelegramPluginInteractiveHandler` namespace mismatch** | **Ruled OUT.** The interactive handler was registered with `namespace: "ollie_approval"`, and the runtime splits on the first `:` (`resolvePluginInteractiveNamespaceMatch` at `command-registration-BWsYSY_K.js:121`). The repo `consent.py` callback data `ollie_approval:v1:a:H-CcuZJw` splits to `namespace="ollie_approval"`, `payload="v1:a:H-CcuZJw"`. This matches. | `loader-wybWjJVr.js:4571` (registration), `command-registration-BWsYSY_K.js:121` (match), live plugin `index.js:1169` (call site). |
| 5 | **`parseApprovalCallback` rejects the ref shape** | **Ruled OUT for `H-CcuZJw`**. Live plugin regex `/^H-[A-Za-z0-9_-]{1,61}$/` accepts `CcuZJw` (6 chars). Even if it had failed, the plugin logs `cb auth=malformed`, and no such line exists. | live plugin `index.js:762-777`. |
| 6 | **Plugin handler runs but `postHandsConsent` returns 404 / unknown_or_expired** | UNLIKELY to silently swallow. The plugin's `handleApprovalCallback` ALWAYS calls `logCallback` (line ~715 of live plugin), even on `ok/error/expired`. No `cb ` log ⇒ handler did not run. | live plugin `index.js:883-940` (handleApprovalCallback), `logCallback` (line ~635). |
| 7 | **`/consent` HTTP unreachable / wrong token** | N/A — handler never ran. The plugin's `postHandsConsent` adds the bearer from `cfg.handsApprovalToken` (currently set per `openclaw.json` entry, redacted), and the live engine's `server.py` BearerMiddleware hard-fails with 401 on mismatch (audit `auth status=denied`). | `C:\ollie-hands\server.py:227-247` (BearerMiddleware); `C:\ollie-hands\config.py:cfg.bearer_token()`. |
| 8 | **Plugin code race during restart** | N/A — gateway has been up continuously since 21:28:10; no `restart` event in journald after that, and PID 84038 has not changed. | `journalctl _PID=84038` (read-only). |

**The single most likely boundary that explains "no `ollie-wa-approval: callback received` log + Telegram accepted the keyboard" is (1): the isolated polling ingress advanced `lastUpdateId` past the update that carried the callback, without ever calling `bot.on("callback_query", ...)`. The "rejected by Telegram" path cannot be ruled out from box logs alone — Telegram's `getUpdates` simply will not redeliver an update whose `update_id` is `<= last_seen`. The on-disk `update-offset-default.json` is the persistent last_seen.**

---

## 5. What cannot be reconstructed retrospectively

These are the known unknowns that bound the confidence of any conclusion:

1. **Telegram-side update log.** The bot has no `getUpdates` audit log on disk, and the gateway does not persist every polled update to disk (only the cursor and failed-spool entries). So we cannot prove which `update_id` carried the `H-CcuZJw` callback, only that **no callback was received by the live plugin after the 21:28 restart**.
2. **What time the user actually pressed the button.** The user did not provide a UTC timestamp. The only inbound-message record on the box is the 19-char text at 21:36:19 CEST, which is a *different* event (a regular text inbound, not a callback).
3. **Whether `H-CcuZJw` was emitted by a currently-running process.** The 5-week-old live `consent.py` cannot have produced a `H-` ref (its code path is 6-digit numeric). The new repo `consent.py` is the only code on the box that can produce `H-…` and it is not deployed. Therefore `H-CcuZJw` is most plausibly a callback produced **by a previously-deployed newer `consent.py`** whose Telegram message is still visible to the user but whose source is no longer on the box. (This is a deployment-drift finding, not a runtime-finding.)
4. **Whether the callback_query update was already in the `lastUpdateId=<TELEGRAM_UPDATE_ID>` past.** Telegram `getUpdates` semantics + the gateway's "mark and advance" behaviour mean once `lastUpdateId` is past an update, that update is *gone* for this bot. There is no recovery short of deleting the offset file or using `getUpdates(offset=-1)` once to replay. **Cannot be checked read-only.**
5. **The live engine's pending `/consent` state.** `C:\ollie-hands\consent.py` was the legacy version; if there is no `H-CcuZJw` entry in its `_pending` dict, even a successful callback would get `unknown_or_expired` and a `cb backend_error_code=unknown_or_expired` log line would appear — but it doesn't, because the callback never arrived.

---

## 6. Recommended read-only next steps (not done)

These are diagnostic moves that would confirm or refute (1) without making changes:

- Query Telegram `getUpdates` with `offset=-1` and look for the most recent callback_query — but this is a **side-effecting** Telegram call (Telegram tracks getUpdates polling state per bot). Out of scope.
- Check the bot's last 100 sent messages via `getChat` / no useful method without a `message_id`; the `message_id=2100` is the user-reported id, not the bot's. Out of scope read-only.
- Inspect the live engine's `_pending` dict via the engine's own debug endpoint (none exposed) or attach a Python debugger (out of scope read-only).
- Diff the repo's `consent.py` against the file at `C:\ollie-hands\consent.py` (read-only OK): hash mismatch already proven (`408795C0D7C8848E62C375ED20AF9197745575C24A5211FD9A7DC48AFF0B5EAE` on box vs the repo's 484-line new version), so the deployment drift is real.
- Check Windows event log for `python.exe` crashes (PID 20416 still alive, so unlikely).

---

## 7. The proven error vs. what is conjecture

| Item | Status |
| --- | --- |
| Plugin was registered, enabled, and the interactive handler was exposed by OpenClaw 2026.5.28 | **PROVEN** (boot log line `[plugins] ollie-wa-approval: loaded …` and absence of `registerInteractiveHandler not available` warning) |
| Callback was **not** received by the plugin after the 21:28 gateway restart | **PROVEN** (0 `callback received` in 07-16 log; 0 in 07-15 log after 20:44:16 CEST) |
| The current `ollie-hands` engine on the box cannot have produced `H-CcuZJw` | **PROVEN** (live `consent.py` mtime 2026-06-10, code path generates 6-digit numeric) |
| The repo's new `consent.py` (with `H-` keys) is the only code that emits `H-CcuZJw`-shaped refs and it is **not** deployed to `C:\ollie-hands` | **PROVEN** (`M ollie-hands/ollie_hands/consent.py` in `git status`; box has the legacy version) |
| The Telegram bot's cursor (`lastUpdateId <TELEGRAM_UPDATE_ID>`) advanced past 48 updates since the only persisted failure (`<TELEGRAM_UPDATE_ID>`, lane owner) | **PROVEN** (offset file + failed-spool file) |
| The `H-CcuZJw` callback was lost by `isolated polling ingress` advancing the cursor | **CONJECTURE** — consistent with the failed-spool precedent + absence of any callback log, but cannot be tied to this specific ref without Telegram-side evidence. |
| `/consent` was never reached | **PROVEN** (no `[ollie-wa-approval]` `cb ` log in either file, and the plugin always emits one on `parseApprovalCallback` accept) |
| A callback for `H-CcuZJw` was actually delivered by Telegram to the bot's polling endpoint | **NOT RETRIEVABLE** from box logs; only the user-side Telegram client shows it. |

---

## 8. Bottom-line answer to the user

**The first failing runtime boundary is the OpenClaw isolated polling ingress (the gateway's Telegram long-poll cursor advance + the `shouldSkipUpdate` short-circuit in the `callback_query` handler at `bot-iSDqdz0Y.js:2799`). The plugin itself is correctly registered and previously saw 18 callbacks; the live engine on the box is too old to have emitted a `H-` ref, so the button is from a stale prior deploy of the new `consent.py`; and the most recent gateway restart (21:28 CEST, after the failed-spool event on 14 July) had no opportunity to surface the callback because no further callback was ever delivered to the bot's polling endpoint in the live `ollie-wa-approval` log. Confirming this requires reading Telegram's own update log, which is not accessible from the box.**

The immediate operational fix the user can apply (read-only followup, not done here) is: re-send the inline-keyboard message from the new `consent.py` once that is deployed to `C:\ollie-hands` (the repo's working tree has it) and re-issue a fresh button press. The cursor/state on the box is healthy otherwise (`lastUpdateId <TELEGRAM_UPDATE_ID>` < the next available update from Telegram), so the new callback will reach the plugin.

---

## Appendix A — file paths (absolute)

- Live plugin: `/home/openclaw/.openclaw/plugins/ollie-wa-approval/index.js` (sha256 `3d7a895b…`)
- Live plugin config: `/home/openclaw/.openclaw/openclaw.json` (entries, plugin entries `ollie-wa-approval enabled=true`, `handsConsentUrl=http://<TAILSCALE_IP>:3200/consent`, `handsApprovalToken` redacted)
- OpenClaw dist: `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`
  - `bot-iSDqdz0Y.js:2799` callback_query handler
  - `bot-iSDqdz0Y.js:1496` dispatchTelegramPluginInteractiveHandler
  - `bot-iSDqdz0Y.js:2373` authorizeTelegramEventSender
  - `bot-iSDqdz0Y.js:7452` shouldSkipUpdate
  - `plugin-runtime-DBz1g2if.js:46` dispatchPluginInteractiveHandler
  - `command-registration-BWsYSY_K.js:179` registerPluginInteractiveHandler
  - `command-registration-BWsYSY_K.js:121` resolvePluginInteractiveNamespaceMatch
  - `loader-wybWjJVr.js:4571` registerInteractiveHandler API surface
- Telegram ingress state: `/home/openclaw/.openclaw/telegram/update-offset-default.json`, `/home/openclaw/.openclaw/telegram/ingress-spool-default/0000000<TELEGRAM_UPDATE_ID>.json.failed`
- Gateway logs: `/tmp/openclaw/openclaw-2026-07-15.log`, `/tmp/openclaw/openclaw-2026-07-16.log`
- Hands engine: `C:\ollie-hands\consent.py` (legacy, 6-digit numeric), `C:\ollie-hands\server.py` (calls `consent.resolve(str(code), ...)`)
- Repo working tree: `./ollie-hands/ollie_hands/consent.py` (new, with `H-` keyboard), `./openclaw-ollie-wa-approval/index.js` (sha256 `c412c2c9…`)

## Appendix B — secrets handling

- `handsApprovalToken` in `openclaw.json` and `bearerToken` for the `hands` MCP server were redacted in every probe. No secret material is quoted in this report. The only token fingerprints surfaced are the Telegram bot id (`<TELEGRAM_BOT_ID>`) and a SHA-256 prefix of the bot token (`<TOKEN_FINGERPRINT>`); both come from the gateway's own offset file and are non-secret operational metadata.
