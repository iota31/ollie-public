# OpenClaw / Windows Node — Architecture & Source Map (read-only investigation)

Authoritative starting points given:
- https://github.com/openclaw/openclaw-windows-node  (Windows Hub: tray, MCP server, node, PowerToys plugin)
- https://docs.openclaw.ai/platforms/windows         (docs page for the Windows platform)

Goal: identify the upstream core gateway source for version `2026.5.28`, map it to the installed artifacts, and locate the maintainable source paths for the requested symbols.

All findings below are read-only research; no edits, deploys, restarts, or commits were performed.

---

## 1. Two repos, two roles

| Repo | Role | Stack | Default branch | Latest tag visible |
|---|---|---|---|---|
| `openclaw/openclaw-windows-node` | **Windows companion / tray** (WinUI 3, .NET 10) — connects TO the gateway, manages a WSL distro that runs the gateway, exposes local MCP server mode | C# / .NET (WinUI 3), PowerShell, Inno Setup, npm for WinUI assets | `main` (HEAD `bf0fa8a9bde433db9f6758cc89723bf9bfdd06c0`) | n/a (independent release cadence; `OpenClawCompanion-Setup-{x64,arm64}.exe`) |
| `openclaw/openclaw` | **Core gateway + every channel plugin** (Telegram/WhatsApp/Slack/Discord/Signal/etc.) + Mac/iOS/Linux/Android client apps + Control UI | TypeScript / Node 22.22.3+ / pnpm workspaces | `main` | `v2026.7.2-beta.1` (2026-07-15); older stable `v2026.7.1` (2026-07-13); many dated tags back through May 2026 |

Key proof points:

- `openclaw/openclaw-windows-node/build.ps1` does **no** gateway download — only `dotnet build`, `git rev-parse`, `npm ci` for `@microsoft/mxc-sdk` (just to copy `wxc-exec.exe`). Source: verbatim fetch of `build.ps1` from `openclaw/openclaw-windows-node` main.
- The Windows Hub docs (`https://docs.openclaw.ai/platforms/windows`) explicitly say: "Windows Hub publishes independently from the OpenClaw CLI and Gateway" and "Windows Hub does not mutate the user's existing Ubuntu distro" — it provisions an app-owned `OpenClawGateway` WSL distro and **installs the Gateway inside it from the standard install script** (`https://openclaw.ai/install.ps1`).
- The PowerShell installer (`https://openclaw.ai/install.ps1`) and shell installer (`https://openclaw.ai/install.sh`) both pin to the **same** upstream source repo: `https://github.com/openclaw/openclaw.git` (no separate "core" or "gateway" subrepo; no submodules).

Conclusion: there is **no separate "core" or "gateway" package** beyond `openclaw/openclaw` itself. The Windows tray never compiles gateway code.

---

## 2. Mapping installed gateway `2026.5.28` to upstream

Verified via the GitHub refs/tags API.

| Item | Value |
|---|---|
| npm package | `openclaw` (global, `npm install -g openclaw@latest`) |
| npm dist-tag channel | `latest`, `next`, `beta`, `dev` |
| Tag name (stable) | **`v2026.5.28`** |
| Tag object SHA (annotated) | `9c580cbc3c10f1b3f0e405bafe2b9c4da3bc251f` |
| Underlying commit SHA | **`e93216080aa1f425d3ab127014603eba8e365b2d`** |
| Tag date / tagger | `2026-05-30T19:46:03Z` / Peter Steinberger `<<OSS_AUTHOR_EMAIL>>` |
| Signature | verified (GPG/SSH) |
| Root `package.json` `version` field | `"2026.5.28"` (no `v` prefix) |
| Root `package.json` `main` | `"dist/index.js"` |
| Root `package.json` `bin` | `"openclaw": "openclaw.mjs"` |
| Root `package.json` `files` (published) | `CHANGELOG.md`, `LICENSE`, `npm-shrinkwrap.json`, `openclaw.mjs`, `pnpm-workspace.yaml`, `README.md`, `THIRD_PARTY_NOTICES.md`, `dist/` |
| Repo default branch for moving-head | `main` |
| Also exists (pre-release) | `v2026.5.28-beta.1` … `v2026.5.28-beta.4` |

Source verification URLs:
- https://github.com/openclaw/openclaw/tree/v2026.5.28
- https://github.com/openclaw/openclaw/commit/e93216080aa1f425d3ab127014603eba8e365b2d
- https://api.github.com/repos/openclaw/openclaw/git/refs/tags/v2026.5.28
- https://api.github.com/repos/openclaw/openclaw/git/tags/9c580cbc3c10f1b3f0e405bafe2b9c4da3bc251f

The Windows EXE installer (`OpenClawCompanion-Setup-x64.exe`) ships only the WinUI tray and the SetupEngine; it then calls the standard gateway installer (`install.ps1`) inside the app-owned `OpenClawGateway` WSL distro to provision the gateway. So on a Windows machine "installed from the EXE," the gateway package is still `openclaw@2026.5.28` (or whatever the EXE's pinned dist-tag resolves to) running inside WSL.

---

## 3. Repo layout at `v2026.5.28` (commit `e932160`)

Top-level (from repo root listing):

- `apps/` — 8 subdirs: `.i18n`, `android`, `ios`, `linux`, `macos`, `macos-mlx-tts`, `shared`, `swabble`
- `extensions/` — **158 subdirs**, one per channel/integration (`telegram`, `discord`, `slack`, `whatsapp`, `signal`, `imessage`, `matrix`, `irc`, `line`, `msteams`, `mxc`, `nextcloud-talk`, `mattermost`, `synology-chat`, `feishu`, `qqbot`, `zalo`, `zalouser`, `nostr`, `sms`, `voice-call`, `talk-voice`, etc.)
- `packages/` — 23 subdirs (notable: `gateway-client`, `gateway-protocol`, `plugin-sdk`, `plugin-package-contract`, `agent-core`, `llm-core`, `acp-core`, `memory-host-sdk`, `sdk`, `normalization-core`, etc.)
- `src/` — gateway runtime (`plugin-sdk`, `entry.ts`, `warning-filter.ts`, etc.)
- `ui/` — Control UI (Vite-built web bundle)
- `skills/`, `docs/`, `scripts/`, `config/`, `deploy/`, `examples/`, `qa/`, `test/`, `security/`, `patches/`, `git-hooks/`
- Root: `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `openclaw.mjs`, `tsconfig*.json`, `Dockerfile`

Telegram extension is a private workspace package `@openclaw/telegram` rooted at `extensions/telegram/` with `openclaw.extensions: ["./index.ts"]` and a `setupEntry: "./setup-entry.ts"`. It depends on grammY 1.43.0 and `@grammyjs/runner` 2.0.3.

---

## 4. Symbols the user asked about — exact paths and references at `v2026.5.28` (commit `e932160`)

### 4.1 `getTelegramSequentialKey` (Telegram sequential-key / lane selector)

**File:** `extensions/telegram/src/sequential-key.ts`

Verified verbatim at `e93216080aa1f425d3ab127014603eba8e365b2d`:

```ts
export function getTelegramSequentialKey(ctx: TelegramSequentialKeyContext): string {
  const reaction = ctx.update?.message_reaction;
  if (reaction?.chat?.id) {
    return `telegram:${reaction.chat.id}`;
  }
  const msg =
    ctx.message ??
    ctx.channelPost ??
    ctx.editedMessage ??
    ctx.editedChannelPost ??
    ctx.update?.message ??
    ctx.update?.edited_message ??
    ctx.update?.channel_post ??
    ctx.update?.edited_channel_post ??
    ctx.update?.callback_query?.message;
  const chatId = msg?.chat?.id ?? ctx.chat?.id;
  const rawText = msg?.text ?? msg?.caption;
  const botUsername = ctx.me?.username;
  if (isTelegramControlLaneText({ rawText, botUsername })) {
    if (typeof chatId === "number") {
      return `telegram:${chatId}:control`;
    }
    return "telegram:control";
  }
  if (isBtwRequestText(rawText, botUsername ? { botUsername } : undefined)) {
    const messageId = msg?.message_id;
    if (typeof chatId === "number" && typeof messageId === "number") {
      return `telegram:${chatId}:btw:${messageId}`;
    }
    if (typeof chatId === "number") {
      return `telegram:${chatId}:btw`;
    }
    return "telegram:btw";
  }
  const callbackData = ctx.update?.callback_query?.data;
  if (callbackData && parseExecApprovalCommandText(callbackData) !== null) {
    if (typeof chatId === "number") {
      return `telegram:${chatId}:approval`;
    }
    return "telegram:approval";
  }
  const isGroup = msg?.chat?.type === "group" || msg?.chat?.type === "supergroup";
  const messageThreadId = msg?.message_thread_id;
  const isForum = resolveTelegramMessageForumFlagHint({
    chatType: msg?.chat?.type,
    isForum: msg?.chat?.is_forum,
    isTopicMessage: msg?.is_topic_message,
  });
  const threadId = isGroup
    ? resolveTelegramForumThreadId({ isForum, messageThreadId })
    : messageThreadId;
  if (typeof chatId === "number") {
    return threadId != null ? `telegram:${chatId}:topic:${threadId}` : `telegram:${chatId}`;
  }
  return "telegram:unknown";
}
```

Lane families returned: per-chat (default), per-topic (forum threads), per-message `btw`, per-chat `control` (read-only status commands + abort/stop), per-chat `approval` (exec-approval callbacks), per-reaction, and a fallback `"telegram:unknown"` / `"telegram:control"`.

Helpers in the same file: `isTelegramReadOnlyControlLaneText`, `isTelegramControlLaneText`, `isTelegramTargetedStopCommand`. Constants: `TELEGRAM_READ_ONLY_STATUS_COMMAND_KEYS`, `TELEGRAM_ACTIVE_RUN_CONTROL_COMMAND_KEYS`.

Wired into grammY as a `bot.use(botRuntime.sequentialize(getTelegramSequentialKey))` middleware in `extensions/telegram/src/bot-core.ts`.

### 4.2 Isolated polling ingress

**Primary files (all under `extensions/telegram/src/`):**

- `extensions/telegram/src/monitor-polling.runtime.ts` — barrel: re-exports `TelegramPollingSession` from `./polling-session.js` and `deleteTelegramUpdateOffset`, `readTelegramUpdateOffset`, `writeTelegramUpdateOffset` from `./update-offset-store.js`.
- `extensions/telegram/src/polling-session.ts` — the class `TelegramPollingSession`; encapsulates both the classic and isolated polling cycles. Key surface:
  - `runUntilAbort()` — main loop until `abortSignal.aborted`.
  - `markForceRestarted()`, `markTransportDirty()`, `abortActiveFetch()`.
  - `activeRunner` getter.
  - `#runPollingCycle` — classic cycle: `run(bot, runnerOptions)` from `@grammyjs/runner`, wraps `bot.api.getUpdates` via a `bot.api.config.use` middleware that records `noteGetUpdatesStarted/Success/Error/Finished` on a `TelegramPollingLivenessTracker`.
  - `#runIsolatedIngressCycle` — selected when `isolatedIngress.enabled`; spawns a worker (`createTelegramIngressWorker` or `isolatedIngress.createWorker`), writes updates to a filesystem spool (`resolveTelegramIngressSpoolDir({ accountId })`), then drains/replays them through `bot.handleUpdate`.
  - `#ensureWebhookCleanup` — calls `bot.api.deleteWebhook({ drop_pending_updates: false })` once per session; retries on 409.
  - 409 (`isGetUpdatesConflict`) detection forces a transport rebuild + re-run of webhook cleanup; surfaces "Another OpenClaw gateway, script, or Telegram poller may be using this bot token; stop the duplicate poller or switch this account to webhook mode."
  - `#waitBeforeRestart` — backoff policy `TELEGRAM_POLL_RESTART_POLICY = { initialMs: 2000, maxMs: 30_000, factor: 1.8, jitter: 0.25 }`.
  - Stall watchdog: `DEFAULT_POLL_STALL_THRESHOLD_MS = 120_000` (clamped 30_000–600_000), `POLL_WATCHDOG_INTERVAL_MS = 30_000`, `POLL_STOP_GRACE_MS = 15_000` force-restart timer.
  - Spool drain: `TELEGRAM_SPOOLED_DRAIN_START_LIMIT = 100`, `TELEGRAM_SPOOLED_DRAIN_SCAN_LIMIT = 1_000`, `TELEGRAM_SPOOLED_CLAIM_REFRESH_INTERVAL_MS = 5 * 60_000`, `TELEGRAM_SPOOLED_CLAIM_HEALTH_GRACE_MS = 10 * 60_000`, `ISOLATED_INGRESS_BACKLOG_STALL_MS = 25 * 60_000`, `ISOLATED_INGRESS_ADOPTION_STALL_MS = 5 * 60_000`, `TELEGRAM_SPOOLED_HANDLER_ABORT_GRACE_MS = 5_000`, env override `OPENCLAW_TELEGRAM_SPOOLED_HANDLER_TIMEOUT_MS`.
  - Lane keys fed to the spool = `getTelegramSequentialKey({ update, me })` per update; handler keys are `${spoolDir}\0${laneKey}` in the module-global `activeSpooledUpdateHandlersByLane` map.
  - Lease refresh: `claimNextTelegramSpooledUpdate` respects `blockedLaneKeys` (active handlers, existing claims, retry-delayed lanes); on claim-refresh failure it calls `supersedeTelegramReplyFenceLane` to abort in-flight reply work (pre-adoption only).
  - Stale-claim recovery on each drain: `recoverStaleTelegramSpooledUpdateClaims`, `isTelegramSpooledUpdateClaimOwnedByOtherLiveProcess`, `isTelegramSpooledCorruptClaimOwnedByOtherLiveProcess`.
  - Retry policy: `spooled-update-retry-policy.ts` — `resolveNonRetryableSpooledUpdateFailure`, `resolveSpooledUpdateAttemptNumber`, `shouldDeadLetterRetryableSpooledUpdate`, `resolveSpooledUpdateRetryDelayMs`.
  - Shutdown finally-block: `await this.#transportState.dispose()` to release undici keep-alive sockets (referenced as `openclaw#68128`).
- `extensions/telegram/src/telegram-ingress-worker.ts` / `.runtime.ts` — worker thread that runs the `getUpdates` call and posts `poll-start` / `poll-success` / `poll-error` / `update` / `spooled` IPC events.
- `extensions/telegram/src/telegram-ingress-spool.ts` / `.types.ts` — filesystem spool writer/reader/claimer, with `telegram-ingress-claim-owner.ts` owning the per-lane claim state.
- `extensions/telegram/src/update-offset-store.ts` / `.runtime.ts` — `readTelegramUpdateOffset`, `writeTelegramUpdateOffset`, `deleteTelegramUpdateOffset` — durable cursor (offset is **forced to `null`** for the worker when isolated ingress is enabled, since the worker owns its own cursor).
- `extensions/telegram/src/polling-lease.ts`, `polling-liveness.ts`, `polling-transport-state.ts`, `polling-status.ts`, `polling-session-restart-policy.ts` — supporting types/utilities.
- `extensions/telegram/src/bot-update-tracker.ts`, `bot-updates.ts`, `webhook.ts`, `allowed-updates.ts` — webhook-mode counterparts and update accounting.
- `extensions/telegram/src/ingress.ts` — does **not** contain the polling loop; only exposes ingress **authorization** helpers (`createTelegramIngressSubject`, `createTelegramIngressResolver`, `telegramAllowEntries`, `resolveTelegramCommandIngressAuthorization`, `resolveTelegramEventIngressAuthorization`) that consume pre-built `ChannelIngressEventInput` values.

The key toggle for "isolated vs classic" ingress lives on the per-account Telegram config under `isolatedIngress.enabled` (consumed inside `TelegramPollingSession`'s constructor; default is class-controlled).

### 4.3 Interactive handler registration / registry

**File:** `extensions/telegram/src/bot-handlers.runtime.ts` (note the `.runtime.ts` suffix marks it as the compiled/imported variant; the `.ts` source is the same module).

Verified export at commit `e932160`:

```ts
export const registerTelegramHandlers = ({
  cfg,
  accountId,
  bot,
  opts,
  telegramTransport,
  runtime,
  mediaMaxBytes,
  telegramCfg,
  allowFrom,
  groupAllowFrom,
  resolveGroupPolicy,
  resolveTelegramGroupConfig,
  shouldSkipUpdate,
  processMessage,
  logger,
  telegramDeps,
  resolveGroupActivation,
  resolveGroupRequireMention,
}: RegisterTelegramHandlerParams) => {
  // ~1000 lines:
  //   bot.on("message_reaction", ...)
  //   bot.on("callback_query", ...)
  //   bot.on("message:migrate_to_chat_id", ...)
  //   handleInboundMessageLike(...)
}
```

Composed from three runtime objects: `messageRuntime`, `authorizationRuntime`, `inboundRuntime`. Delegates registration to: reaction handler, callback-query handler, migration handler, message handler. Configures text-fragment buffering constants (`TELEGRAM_TEXT_FRAGMENT_*`, `FORWARD_BURST_DEBOUNCE_MS`) and an `inboundDebouncer` via `createInboundDebouncer`.

**Caller (`bot-core.ts`)** wires it together with grammY:

```ts
// from extensions/telegram/src/bot-core.ts
bot.catch((err) => { runtime.error?.(`telegram bot error: ${formatUncaughtError(err)}`); });
bot.use(async (ctx, next) => { const begin = updateTracker.beginUpdate(ctx); ... }); // update accounting
bot.use(async (ctx, next) => { /* pre-sequentialize answerCallbackQuery */ });
bot.use(botRuntime.sequentialize(getTelegramSequentialKey));              // <-- lane serialization
bot.use(async (ctx, next) => { /* verbose raw-update log */ });
registerTelegramNativeCommands({ ... });
registerTelegramHandlers({ ... });
```

Sibling handler files (each handling one slice of the dispatch tree):
`bot-handlers.inbound.runtime.ts`, `bot-handlers.inbound-text.runtime.ts`, `bot-handlers.inbound-debounce.runtime.ts`, `bot-handlers.inbound-media-group.runtime.ts`, `bot-handlers.message.runtime.ts`, `bot-handlers.message-context.runtime.ts`, `bot-handlers.message-events.runtime.ts`, `bot-handlers.message-lifecycle.runtime.ts`, `bot-handlers.message-session.runtime.ts`, `bot-handlers.callback.runtime.ts`, `bot-handlers.callback-actions.runtime.ts`, `bot-handlers.callback-approvals.runtime.ts`, `bot-handlers.callback-errors.runtime.ts`, `bot-handlers.callback-interactions.runtime.ts`, `bot-handlers.callback-model.runtime.ts`, `bot-handlers.authorization.runtime.ts`, `bot-handlers.authorization-groups.runtime.ts`, `bot-handlers.agent.runtime.ts`, `bot-handlers.reaction.runtime.ts`, `bot-handlers.migration.runtime.ts`, `bot-handlers.media.ts`, `bot-handlers.debounce-key.ts`.

`bot.ts` is a thin wrapper exporting `createTelegramBot(opts)` that delegates to `createTelegramBotCore(opts)` in `bot-core.ts`.

### 4.4 Plugin callback dispatch

The plugin model in this repo is **not** a free-form callback dispatcher — it's a typed, contract-driven loader. Two related surfaces:

**Channel plugin contract** — `extensions/telegram/src/channel.ts` (built via `createChatChannelPlugin({ base: createTelegramPluginBase(...), pairing, security, threading, outbound })`). The `base` shape: `{ allowlist, bindings, conversationBindings, groups, agentPrompt, messaging, resolver, lifecycle, heartbeat, approvalCapability, directory, actions, message, status, gateway }`. Lifecycle hooks include `onAccountConfigChanged`, `onAccountRemoved`, `logoutAccount`. The gateway start hook:

```ts
startAccount: async (ctx) => {
  return resolveTelegramMonitor()({
    token, accountId: ctx.account.accountId, config: ctx.cfg,
    runtime: ctx.runtime, channelRuntime: ctx.channelRuntime,
    abortSignal: ctx.abortSignal, useWebhook: ...
  });
}
```

**Generic (non-channel) plugin entry** — `src/plugin-sdk/plugin-entry.ts` (also surfaced through `packages/plugin-sdk/src/plugin-entry.ts` re-exporting `../../../src/plugin-sdk/plugin-entry.js`):

```ts
export function definePluginEntry({
  id, name, description, kind,
  configSchema = emptyPluginConfigSchema,
  reload, nodeHostCommands, securityAuditCollectors,
  register,
}: DefinePluginEntryOptions): DefinedPluginEntry
```

Where `register: (api: OpenClawPluginApi) => void` is the **single dispatch entry**. `api` is typed against the full plugin surface re-exported from `../plugins/types.js`, including: providers (discovery/catalog/normalize/resolve/prepare/failover/replay/thinking-policy/cache-ttl/websocket-policy), tools (`OpenClawPluginToolFactory`, `OpenClawPluginHttpRouteHandler`), commands (`OpenClawPluginCommandDefinition`), services (`OpenClawPluginService`, lifecycle hooks), agent turns (`PluginAgentTurnPrepareEvent`, `PluginAgentEventEmitParams`, `PluginHeartbeatPromptContributionEvent`, `PluginNextTurnInjection*`), session extensions, conversation bindings, node-host commands, security audit collectors, migrations, and gateway-discovery advertise hooks.

`kind` on `definePluginEntry` is marked **deprecated**; the manifest `kind` field in `openclaw.plugin.json` is now authoritative. Channel plugins use a separate helper `defineChannelPluginEntry(...)` from `openclaw/plugin-sdk/core` (the `channel.ts` registration path).

**Plugin external-contract validation** — `packages/plugin-package-contract/src/index.ts` exports `normalizeExternalPluginCompatibility(packageJson)` and `validateExternalCodePluginPackageJson(packageJson)`, with required manifest fields `["openclaw.compat.pluginApi", "openclaw.build.openclawVersion"]`. (This is the runtime check that ensures third-party plugins carry the right metadata before dispatch loads them.)

### 4.5 Source-to-artifact mapping (how source becomes what runs)

Root `package.json` (`"version": "2026.5.28"`) build scripts:

- `build`: `node scripts/build-all.mjs`
- `build:docker` (the production-publish chain): `node scripts/tsdown-build.mjs && node scripts/check-cli-bootstrap-imports.mjs && node scripts/runtime-postbuild.mjs && node scripts/build-stamp.mjs && node scripts/runtime-postbuild-stamp.mjs && pnpm plugins:assets:build && pnpm plugins:assets:copy && node --experimental-strip-types scripts/copy-hook-metadata.ts && node --experimental-strip-types scripts/copy-export-html-templates.ts && node --experimental-strip-types scripts/write-build-info.ts && node --experimental-strip-types scripts/write-cli-startup-metadata.ts && node --experimental-strip-types scripts/write-cli-compat.ts`
- `build:plugin-sdk:dts`: `node scripts/run-tsgo.mjs -p tsconfig.plugin-sdk.dts.json --declaration true`

`scripts/build-all.mjs` orchestrator — ordered steps (with input/output cache hashing):

1. `plugins:assets:build`
2. `tsdown` (`scripts/tsdown-build.mjs`)
3. `check-cli-bootstrap-imports`
4. `runtime-postbuild` (`scripts/runtime-postbuild.mjs`)
5. `build-stamp`
6. `runtime-postbuild-stamp`
7. `build:plugin-sdk:dts` (Windows: `--max-old-space-size=8192`)
8. `write-plugin-sdk-entry-dts`
9. `check-plugin-sdk-exports`
10. `plugins:assets:copy`
11. `copy-hook-metadata`
12. `copy-export-html-templates`
13. `ui:build` (caching explicitly disabled — Control UI build ID derives from package.json + git HEAD + `OPENCLAW_CONTROL_UI_BUILD_ID` env)
14. `write-build-info`
15. `write-cli-startup-metadata`
16. `write-cli-compat`

`scripts/tsdown-build.mjs` — wraps `tsdown` itself:
- Pre-cleans `dist/` and `dist-runtime/` (`TSDOWN_OUTPUT_ROOTS = ["dist", "dist-runtime"]`)
- Prunes stale symlinks under `extensions/*/node_modules` overlays inside dist trees
- Invokes tsdown with `--no-clean`, `--config-loader unrun`, plus memory budgeting (cgroup / `/proc/meminfo` → `DEFAULT_TSDOWN_MAX_OLD_SPACE_MB = 8192`, floor `2048`, headroom `768`)
- Scans output for `[UNRESOLVED_IMPORT]` and `[INEFFECTIVE_DYNAMIC_IMPORT]` sentinels — fatal outside extensions
- Timeout = `SIGTERM` → 5s → `SIGKILL`

`scripts/runtime-postbuild.mjs` — phase order:
1. plugin SDK root alias (`dist/plugin-sdk/root-alias.cjs`)
2. bundled plugin metadata
3. official channel catalog (`dist/channel-catalog.json`)
4. bundled plugin runtime overlay
5. **static extension assets** (where Telegram source files get staged into `dist/` via `copyStaticExtensionAssets` + `copyStaticExtensionAssetsToRuntimeOverlay` from `scripts/lib/static-extension-assets.mjs`)
6. stable root runtime imports → stable aliases
7. stable root runtime aliases
8. legacy root runtime compat aliases
9. legacy CLI exit compat chunks (`dist/memory-state-CcqRgDZU.js` and `dist/memory-state-DwGdReW4.js`)

Note that the bot-handlers / sequential-key / polling-session source files are bundled **into** `dist/` via tsdown (ESM) and then re-exported through hashed chunks with stable alias files rewritten on top.

The launcher (`openclaw.mjs`) loads the gateway by trying `./dist/entry.js` then `./dist/entry.mjs`, with precomputed help fast paths from `dist/cli-startup-metadata.json` and version from `dist/build-info.json`. Verified at commit `e932160`.

---

## 5. Build a source-level gateway change safely (recommended workflow)

For a change confined to the gateway/plugin code (e.g. editing `getTelegramSequentialKey` in `extensions/telegram/src/sequential-key.ts`, or anything in `src/plugin-sdk/`, or another channel under `extensions/`):

1. **Branch from the installed tag, not main.** Check out `v2026.5.28` (commit `e93216080aa1f425d3ab127014603eba8e365b2d`) or whatever the box actually has. Avoid touching `main`, which is now past `v2026.7.2-beta.1` and may carry breaking refactors (the lane/delivery files were split heavily between May and July 2026).

   ```bash
   git clone https://github.com/openclaw/openclaw.git openclaw-src
   cd openclaw-src
   git checkout v2026.5.28          # commit e93216080aa1f425d3ab127014603eba8e365b2d
   git switch -c my-fix
   ```

2. **Confirm Node + pnpm + memory budget.** The install scripts require Node ≥ 22.22.3 / 24.15.0 / 25.9.0, and `tsdown-build.mjs` enforces `--max-old-space-size=8192` (with a 2048 MB floor). Set:

   ```bash
   export NODE_OPTIONS="--max-old-space-size=8192"
   corepack enable
   corepack prepare pnpm@11 --activate
   ```

   `pnpm` version comes from the repo's `package.json` `packageManager` field — check that first; the installer will too.

3. **Install deps (with the same flags the Windows installer uses):**

   ```bash
   pnpm install \
     --prefer-offline \
     --config.node-linker=hoisted \
     --config.engine-strict=false \
     --config.enable-pre-post-scripts=true \
     --config.side-effects-cache=false \
     --no-frozen-lockfile
   ```

   `CI=true` is helpful to skip the UI build's TTY prompts.

4. **Make the source edit.** For the user's specific symbols:
   - `extensions/telegram/src/sequential-key.ts` for `getTelegramSequentialKey` and lane selection.
   - `extensions/telegram/src/polling-session.ts` (and the surrounding `*.runtime.ts` siblings) for isolated polling ingress.
   - `extensions/telegram/src/bot-handlers.runtime.ts` (+ the `bot-handlers.*.runtime.ts` siblings) for handler registration.
   - `extensions/telegram/src/channel.ts` for the channel-plugin dispatch contract.
   - `src/plugin-sdk/plugin-entry.ts` (re-exported via `packages/plugin-sdk/src/plugin-entry.ts`) for the generic plugin callback dispatch.

5. **Build:**

   ```bash
   pnpm build           # runs scripts/build-all.mjs
   # or for a focused gateway-only build that still does tsdown + runtime-postbuild:
   node scripts/tsdown-build.mjs && node scripts/runtime-postbuild.mjs
   ```

   Verify the dist artifacts: `dist/entry.js`, `dist/index.js`, `dist/extensionAPI.js`, `dist/plugin-sdk/index.{js,d.ts}`, `dist/plugin-sdk/root-alias.cjs`, `dist/channel-catalog.json`, plus the bundled Telegram extension (under `dist/extensions/...` after the static-extension-assets phase).

6. **Smoke-test the bundle** before shipping:

   ```bash
   node openclaw.mjs --version     # should print "OpenClaw 2026.5.28 (<short-sha>)"
   node openclaw.mjs doctor --non-interactive
   node openclaw.mjs gateway status --json
   ```

7. **Ship.** Two routes depending on install layout:

   - **WSL on Windows (Windows-Hub-installed):** the Hub provisions an app-owned `OpenClawGateway` WSL distro that runs the gateway from `~/.openclaw/` inside that distro. To replace the installed `dist/` with the rebuilt one, the safe path is: rebuild in your source checkout, then `tar` the `dist/` + the launcher (`openclaw.mjs`) + root `package.json` and overlay them onto the WSL distro's gateway install (e.g. into `/usr/lib/node_modules/openclaw/` or wherever the global prefix landed). Then restart the gateway via the Hub tray or `wsl -d OpenClawGateway openclaw gateway restart`.

   - **Linux/macOS native install:** rebuild, then `npm install -g ./openclaw-src` (with `pnpm` pack if you want a tarball).

   The Windows tray EXE itself **does not need to be rebuilt** for gateway-source changes — it consumes the running gateway over its WebSocket (`ws://...`) and only reads node capability manifests. Confirm by reading `openclaw-windows-node/build.ps1`: it only `dotnet build`s the tray/MCP/CLI/SetupEngine projects and `npm ci`s `@microsoft/mxc-sdk`. No reference to "gateway" or "core" appears.

8. **Verify after restart.** Use the same checks the user's memory recommends for delegated box work:

   ```bash
   node openclaw.mjs --version      # confirm build SHA + version
   openclaw doctor --non-interactive
   openclaw daemon status --json
   openclaw gateway status --json   # data.service.loaded === true
   ```

   Then exercise the affected Telegram lane by sending a real message and observing lane logs (look for `telegram:<chatId>`, `:topic:<id>`, `:control`, `:btw`, `:approval` lane keys).

---

## 6. Rebuilt-tray-app — when it's actually needed

**Only rebuild the Windows tray when you change Windows-side code**, i.e. anything under `openclaw-windows-node/src/` (the `OpenClaw.Tray.WinUI`, `OpenClaw.Connection`, `OpenClaw.Shared`, `OpenClaw.Chat`, `OpenClaw.Cli`, `OpenClaw.WinNode.Cli`, `OpenClaw.SetupEngine`, `OpenClaw.SetupEngine.UI`, `OpenClawTray.FunctionalUI` projects) or its installer manifest `installer.iss`. Triggers include:

- Changing the gateway WebSocket protocol in `OpenClaw.Connection`/`OpenClaw.Shared` (affects how the tray talks to the gateway).
- Adding/removing a Command Center diagnostics panel.
- Changing the local MCP server capabilities (`OpenClaw.WinNode.Cli`).
- Updating tray UI, chat window, or settings schema.
- Updating Inno Setup / WiX packaging.

**Do NOT rebuild the tray for** gateway runtime changes (Telegram sequential-key, polling ingress, plugin dispatch, channel plugins, model providers, Control UI, etc.). The tray only consumes the gateway's published WebSocket protocol and its declared node capability surface; both are stable across `v2026.5.28` → future patches.

---

## 7. Key URLs (evidence index)

Upstream gateway:
- Repo root: https://github.com/openclaw/openclaw
- `v2026.5.28` tag tree: https://github.com/openclaw/openclaw/tree/v2026.5.28
- Commit at v2026.5.28: https://github.com/openclaw/openclaw/commit/e93216080aa1f425d3ab127014603eba8e365b2d
- Tags list: https://github.com/openclaw/openclaw/tags (paginated)
- Telegram extension tree (main): https://github.com/openclaw/openclaw/tree/main/extensions/telegram
- Telegram `src/` listing: https://github.com/openclaw/openclaw/tree/main/extensions/telegram/src
- `packages/` listing: https://github.com/openclaw/openclaw/tree/main/packages
- `apps/` listing: https://github.com/openclaw/openclaw/tree/main/apps
- `scripts/` listing: https://github.com/openclaw/openclaw/tree/main/scripts

Symbols (raw at `e93216080aa1f425d3ab127014603eba8e365b2d`):
- `getTelegramSequentialKey`: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/src/sequential-key.ts
- `TelegramPollingSession` (polling ingress): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/src/polling-session.ts
- `registerTelegramHandlers`: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/src/bot-handlers.runtime.ts
- `bot-core.ts` (middleware wiring): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/src/bot-core.ts
- `channel.ts` (plugin dispatch surface): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/src/channel.ts
- `plugin-entry.ts` (generic dispatch): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/src/plugin-sdk/plugin-entry.ts
- `plugin-package-contract` validation: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/packages/plugin-package-contract/src/index.ts
- Root `package.json` (version, main, bin, scripts): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/package.json
- Telegram `package.json` (grammy deps, manifest): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/extensions/telegram/package.json
- `openclaw.mjs` (launcher): https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/openclaw.mjs
- Build scripts: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/scripts/build-all.mjs
- tsdown wrapper: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/scripts/tsdown-build.mjs
- runtime-postbuild: https://raw.githubusercontent.com/openclaw/openclaw/e93216080aa1f425d3ab127014603eba8e365b2d/scripts/runtime-postbuild.mjs

Windows Hub:
- Repo: https://github.com/openclaw/openclaw-windows-node
- Releases: https://github.com/openclaw/openclaw-node/releases (direct download base: `/releases/latest/download/`)
- Default-branch HEAD: `bf0fa8a9bde433db9f6758cc89723bf9bfdd06c0`
- `build.ps1`: https://raw.githubusercontent.com/openclaw/openclaw-windows-node/main/build.ps1 (verbatim search showed: no "gateway" or "core" references; only `dotnet build`, `git rev-parse`, `npm ci` for `@microsoft/mxc-sdk`)
- Docs page: https://docs.openclaw.ai/platforms/windows
- Install scripts (gateway): https://openclaw.ai/install.ps1 and https://openclaw.ai/install.sh (both pinned to `https://github.com/openclaw/openclaw.git`)

GitHub API refs:
- Tag ref: https://api.github.com/repos/openclaw/openclaw/git/refs/tags/v2026.5.28
- Tag object: https://api.github.com/repos/openclaw/openclaw/git/tags/9c580cbc3c10f1b3f0e405bafe2b9c4da3bc251f (resolves to commit `e93216080aa1f425d3ab127014603eba8e365b2d`, verified)

---

## 8. Caveats / notes for whoever picks this up

- **Two `openclaw/openclaw` repos** exist in search index snippets — one is a 1997 Captain Claw game reimplementation. The gateway is unambiguously the TypeScript monorepo at `openclaw/openclaw` (383k stars, MIT, TS, pnpm workspaces, README first line "OpenClaw — Personal AI Assistant"). The Windows install script (`install.ps1`) hardcodes `https://github.com/openclaw/openclaw.git` for `Install-OpenClawFromGit`. If a search ever surfaces a different `openclaw/openclaw` result, ignore it.
- **Tags page paginates newest-first.** A direct hit for `v2026.5.28` is `https://api.github.com/repos/openclaw/openclaw/git/refs/tags/v2026.5.28` rather than scraping the HTML tag list.
- **The `2026.5.28-beta.1` … `-beta.4` tags also exist** alongside the stable `v2026.5.28`; only the stable was tagged by Peter Steinberger on 2026-05-30. If the installed gateway reports `2026.5.28-beta.<n>`, the underlying commit is one of `829e71d108bf4d81d26f666658cd3750c512f49e` / `c0f2eea32ec97adebe32502d079e7f752274f6e4` / `93b52fadfbfd28236d85001b2d52e315e069b7d8` / `327ebee5220abfe2c06a937ac36e483caef3e302`.
- **`extensions/telegram/src/*.runtime.ts` is the import surface.** The plain `.ts` files (no `.runtime`) are the source-of-truth variants in most cases (the WebFetch tool saw the `.ts` variants at HEAD but the symbols are identical because the `.runtime.ts` siblings re-export from them — verified on `sequential-key.ts`). When in doubt, treat the `.runtime.ts` path as the runtime-visible name.
- **No separate "core" or "gateway" subrepo or submodule exists.** The Windows install copies the WSL distro gateway from the standard `install.ps1` flow which clones `openclaw/openclaw` directly. There is no plugin-loader-only artifact shipped separately from the gateway.
- **`build` is reproducible from a clean checkout**, but `ui:build` is explicitly cache-disabled because the Control UI bundle ID derives from `package.json`, git HEAD, and `OPENCLAW_CONTROL_UI_BUILD_ID`. Expect every rebuild to re-emit UI assets; that's intentional.
- **No secrets are exposed** by any of the paths above. All scripts that read environment values (installers, runtime-postbuild) treat them as opaque; no tokens or API keys are baked into source.