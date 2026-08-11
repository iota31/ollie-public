# `@ollie/openclaw-wa-approval`

OpenClaw plugin: HARD first-contact WhatsApp approval gate for **Ollie**.

The owner's Telegram chat is the approval surface. When a WhatsApp number that
is not on the approved list tries to reach Ollie (or Ollie tries to reach an
unapproved number), the plugin BLOCKS the message pre-LLM, sends a one-line
Telegram approval request, and waits for the owner to reply `approve <ref>` or
`deny <ref>`.

This package is **local source only**. It is not installed on the gateway yet.
See **STAGE 2 DEPLOY** at the bottom for the exact install/enable/test steps
the operator should run.

---

## What it does

| Trigger | Behavior |
|---|---|
| Inbound WA from approved number | Pass (Ollie processes normally) |
| Inbound WA from blocked number | Silent block (no Telegram push) |
| Inbound WA from unknown number | Block pre-LLM, push `📩 New WhatsApp from +X '<preview>'. Reply: approve <ref> / deny <ref>`, dedup on existing pending |
| Outbound WA to approved number | Pass (Ollie sends) |
| Outbound WA to blocked/unknown number | Cancel the send, push the same approval request |
| Owner Telegram reply `approve <ref>` | Move number to `approved[]`, delete pending, send `✅ Approved +X` confirmation |
| Owner Telegram reply `deny <ref>` | Move number to `blocked[]`, delete pending, send `⛔ Denied +X` confirmation |
| Owner reply with unknown ref | Send `❓ No pending approval request with ref "<ref>"` |

Decisions are persisted to a JSON state file (default:
`~/.openclaw/workspace/whatsapp-contacts.json` — the same file the openclaw
installer already creates) so approvals survive a gateway restart.

## Hooks used (with source references)

All hook contracts were verified against the live gateway source on
box `<TAILSCALE_IP>` (WSL `OpenClawGateway`) at
`/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/`.

| Hook | Source ref | What we do |
|---|---|---|
| `before_agent_run` | `dist/plugin-sdk/hook-types-B_5108I1.d.ts:868-877` (event shape) and `dist/hook-runner-global-BdHeqZIb.js:676-707` (gate runner) | Return `{outcome:"block",reason,message?}` to STOP the model call entirely. Returns `undefined` (== pass) for non-WA or approved senders. **This is the HARD pre-LLM gate.** |
| `message_sending` | `dist/plugin-sdk/hook-types-B_5108I1.d.ts:190-200` (event + result shape) and `dist/deliver-B_snf0tE.js:729-748` (runtime sets `ctx.channelId = provider name`) | Return `{cancel: true, cancelReason}` to stop outbound delivery. `cancel:true` is terminal per `hooks.md`. |
| `message_received` | `dist/plugin-sdk/hook-types-B_5108I1.d.ts:175-188` (event shape) | Fire-and-forget observer. We match `ctx.channelId === "telegram"` AND `event.senderId === ownerTelegramChatId`, parse `approve <ref>` / `deny <ref>`, and update state. |

### Provider identification
- For `before_agent_run` we use `ctx.messageProvider` (typed on
  `PluginHookAgentContext` at `hook-types-B_5108I1.d.ts:284`).
- For `message_sending` and `message_received` we use `ctx.channelId` because
  the runtime sets it to the provider name (verified in
  `deliver-B_snf0tE.js:738`: `channelId: params.channel`). The d.ts comment
  says "conversation target identifier" but the actual runtime value is the
  provider name (`"whatsapp"`, `"telegram"`, ...).

### Telegram send (canonical pattern)
From `dist/extensions/device-pair/index.js:556-560`:
```js
const send = (await api.runtime.channel.outbound.loadAdapter("telegram"))?.sendText;
await send({ cfg: api.config, to, text, ...accountId ? { accountId } : {} });
```
This is exactly what this plugin does in `sendOwnerTelegram()`.

### Plugin entry
- Imported as `import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry"`.
  This is the canonical non-channel entry helper per
  `dist/plugin-sdk/plugin-entry-Cufh5MG3.js:6-25`.
- OpenClaw auto-discovers the entry from
  `DEFAULT_PLUGIN_ENTRY_CANDIDATES` (`manifest-DaiqPlf0.js:833-838`:
  `index.ts`, `index.js`, `index.mjs`, `index.cjs`). The `index.js` at the
  package root is what gets loaded. The `src/index.ts` is a typed mirror for
  documentation and editor support — do not point the manifest at it.

## Config

| Key | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | boolean | `false` | **Master switch.** When false, the plugin loads but every hook is a no-op. Ship inert, flip on during STAGE 2. |
| `ownerTelegramChatId` | string | `"<OWNER_TELEGRAM_CHAT_ID>"` | Telegram chat id of the owner. Approval requests go here; replies are correlated from here. |
| `approvalsFile` | string | `"~/.openclaw/workspace/whatsapp-contacts.json"` | Path to the JSON state file. `~` is expanded. Reuses the existing file the openclaw installer already creates. |
| `requestTimeoutMinutes` | number | `60` | How long a pending entry stays "active" before being flagged `expired` in the state file. Expired entries do **not** auto-approve — they just tell the owner the request is stale. |
| `hookTimeoutMs` | number | `5000` | Per-hook budget. Overrides the OpenClaw default of 15s for `before_agent_run` so a slow Telegram push does not stall the model. |
| `handsConsentUrl` | string | `"http://<TAILSCALE_IP>:3200/consent"` | Hands owner-consent endpoint. |
| `handsApprovalToken` | string | `""` | Approval-only credential from the Hands host's `approval.token`; the MCP bearer is never reused. |

## State file format

The state file is the same `whatsapp-contacts.json` the openclaw installer
creates. The plugin reads/writes only the runtime fields
(`approved`, `blocked`, `pending`, `updated`, `updatedBy`) and **preserves**
sibling metadata fields (`_schema`, `_purpose`, `owner`, `notes`) so we are a
polite co-author of the file.

```json
{
  "_schema": "whatsapp-contacts.v1",
  "_purpose": "Source-of-truth allow/block list for Ollie's WhatsApp channel ...",
  "owner": "+<OWNER_PHONE>",
  "approved": ["+<OWNER_PHONE>"],
  "blocked": [],
  "pending": {
    "a1b2c3": {
      "from": "+15551234567",
      "preview": "hi is this ollie",
      "kind": "inbound",
      "requestedAt": "2026-06-04T20:00:00.000Z",
      "expiresAt": "2026-06-04T21:00:00.000Z"
    }
  },
  "updated": "2026-06-04T20:00:00.000Z",
  "updatedBy": "ollie-wa-approval@0.1.0",
  "notes": ["..."]
}
```

Writes are atomic: write to `<file>.tmp.<pid>.<rand>` then `rename(2)` to the
final path. This is the same pattern the `phone-control` bundled plugin uses
(`dist/extensions/phone-control/index.js`).

## Defensive behavior

- **State file unreadable on startup** → log a warning, fail safe: deny
  unknowns and cancel outbounds. **Never silently approve** when the file is
  missing.
- **Telegram send fails** → log a warning, gate stays closed. We never throw
  out of a hook and break the host.
- **Hook errors** → `before_agent_run` returns `{outcome:"block"}` (fail-closed,
  per the host's invalid-decision handling in
  `hook-runner-global-BdHeqZIb.js:683-695`); `message_sending` returns
  `{cancel:true}`. The host never sees an exception.
- **Concurrent writers** → a single in-process Promise-based mutex serializes
  read-modify-write cycles. The plugin is single-process; cross-process safety
  comes from atomic-rename.

---

## STAGE 2 DEPLOY

> **STOP.** The user has not approved going live. Do not run any of these
> commands until they do. This section exists so the operator has the exact
> sequence in hand.

### 1. Copy the package to the box
```bash
# from the operator's Mac
scp -i ~/.ssh/id_ed25519 -r ~/PycharmProjects/ollie/openclaw-ollie-wa-approval \
    source@<TAILSCALE_IP>:/tmp/openclaw-ollie-wa-approval
```
Then on the box:
```bash
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP>
wsl -d OpenClawGateway
sudo cp -r /tmp/openclaw-ollie-wa-approval /home/openclaw/.openclaw/plugins/ollie-wa-approval
sudo chown -R openclaw:openclaw /home/openclaw/.openclaw/plugins/ollie-wa-approval
```
(Pick whatever install directory the existing external plugins use; the
default for non-bundled is `~/.openclaw/plugins/<id>/`.)

### 2. Edit `openclaw.json` to register the plugin
```jsonc
{
  "plugins": {
    "entries": {
      "ollie-wa-approval": {
        "enabled": true,                 // turn the OpenClaw loader on
        "config": {
          "enabled": false,              // ← LEAVE FALSE FOR FIRST BOOT. Flip to true after a clean start.
          "ownerTelegramChatId": "<OWNER_TELEGRAM_CHAT_ID>",
          "approvalsFile": "/home/openclaw/.openclaw/workspace/whatsapp-contacts.json",
          "requestTimeoutMinutes": 60,
          "hookTimeoutMs": 5000,
          "handsConsentUrl": "http://<TAILSCALE_IP>:3200/consent",
          "handsApprovalToken": "<approval.token supplied through secret config>"
        },
        "hooks": {
          "allowConversationAccess": true   // ← REQUIRED. before_agent_run is a conversation hook.
        }
      }
    }
  }
}
```

> ⚠️ **`hooks.allowConversationAccess: true` is mandatory.** Per
> `docs/plugins/hooks.md:191-198`, non-bundled plugins must opt in to
> conversation hooks (`before_model_resolve`, `before_agent_reply`,
> `llm_input`, `llm_output`, `before_agent_finalize`, `agent_end`, **and
> `before_agent_run`**). Without it, the hook is silently skipped and Ollie
> will be exposed to unknown senders.

### 3. Open the WhatsApp gate
Edit `channels.whatsapp.dmPolicy` so unknown numbers can actually reach the
hook. Two options:

**Option A (recommended): open + let the plugin gate.**
```jsonc
"channels": {
  "whatsapp": {
    "dmPolicy": "open",
    "allowFrom": ["+<OWNER_PHONE>"]   // belt-and-suspenders; WA channel will also pass anyone through, but our hook blocks unknowns pre-LLM
  }
}
```

**Option B (stricter): keep allowlist, expand it via a one-time script.**
Leave `dmPolicy: "allowlist"`, but pre-populate `allowFrom` with the numbers
you actually want. Unknowns never reach the hook in this mode, so the plugin
is a defense-in-depth layer. **This defeats the purpose of the gate** unless
you also have a way to add new numbers quickly.

→ Use **Option A**.

### 4. Restart the gateway
```bash
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> "wsl -d OpenClawGateway -- openclaw gateway restart"
```
(or however the supervisor is set up — see the live `RUNBOOK.md`)

### 5. Verify inert boot
After restart, **before flipping `config.enabled` to `true`**, check the
plugin loaded with no errors. The boot log should show:
```
[ollie-wa-approval] ollie-wa-approval: loaded (enabled=false, ownerTelegramChatId=<OWNER_TELEGRAM_CHAT_ID>, approvalsFile=/home/openclaw/.openclaw/workspace/whatsapp-contacts.json)
[ollie-wa-approval] ollie-wa-approval: config.enabled=false -> registering inert no-op hooks. Flip config.enabled=true to activate.
```

### 6. Flip `config.enabled` to `true` (re-edit openclaw.json)
```jsonc
"ollie-wa-approval": {
  "enabled": true,
  "config": { "enabled": true, ... }
}
```
Then `openclaw gateway restart` again.

### 7. Test
**Inbound test (unknown sender):**
1. From a WhatsApp number that is **not** in `approved[]`, send Ollie a
   message like `hello`.
2. Ollie should NOT reply.
3. The owner's Telegram should receive:
   ```
   📩 New WhatsApp from +1XXX... '<hello>'. Reply: approve <ref> / deny <ref>
   ```
4. On Telegram, reply `approve <ref>`. You should get `✅ Approved +1XXX...
   (ref <ref>). Next message from this number will reach Ollie.`
5. Send another WhatsApp from the same number. Ollie should reply.

**Inbound test (deny):**
1. From another unknown number, send Ollie `hey`.
2. Owner gets a new push with a different ref.
3. Reply `deny <ref>`. You should get `⛔ Denied +1XXX... (ref <ref>).
   Future messages from this number will be blocked.`
4. Send another WhatsApp from the same number. Ollie should NOT reply, and
   no new Telegram push should fire (deduped against `blocked[]`).

**Outbound test (Ollie tries to send first):**
1. Tell Ollie on Telegram: "Send a WhatsApp to +1YYY... saying hello."
2. The send should be cancelled. Owner should get a Telegram push with
   `kind:"outbound"`.
3. Approve. The send does **NOT** auto-replay. The cancelled message is
   lost. **The owner must re-ask Ollie to send.** This is documented
   behavior (PRD risk R3).

**Belt-and-suspenders test:**
1. Flip `config.enabled` back to `false`, restart.
2. Try the inbound test from another unknown number. Ollie SHOULD reply
   (the gate is off, so the WA channel's `dmPolicy:open` lets it through).
3. Flip back to `true`, restart. Gate is back on.

### 8. Revert (disable)
To turn the gate off without uninstalling:
```jsonc
"ollie-wa-approval": {
  "enabled": true,
  "config": { "enabled": false, ... }   // ← just flip this
}
```
Then `openclaw gateway restart`. The plugin stays loaded and the manifest
stays in `openclaw.json`, but every hook becomes a no-op. This is the
safest way to debug "is the gate causing this?" — flip one boolean, restart.

To fully uninstall: delete the package directory, remove the
`ollie-wa-approval` entry from `openclaw.json`, restart.

---

## File tree

```
openclaw-ollie-wa-approval/
├── README.md                  # this file
├── package.json               # @ollie/openclaw-wa-approval@0.1.0 (peer: openclaw >= 2026.5.28)
├── openclaw.plugin.json       # manifest: id, hooks, configSchema, uiHints
├── index.js                   # the loadable entry (DEFAULT_PLUGIN_ENTRY_CANDIDATES picks this)
└── src/
    └── index.ts               # typed mirror of index.js for reference and editor support
```

## Honest notes / risks

- **(R1) `allowConversationAccess` is mandatory.** Forgetting it silently
  disables the gate. Verified against `docs/plugins/hooks.md:191-198`.
- **(R2) `ctx.channelId` semantics.** The d.ts comment for
  `PluginHookMessageContext.channelId` says "conversation target identifier",
  but the runtime sets it to the provider name (verified in
  `deliver-B_snf0tE.js:738`). We rely on the runtime behavior.
- **(R3) Outbound send lost on cancel.** When `message_sending` cancels a
  send, the agent's intended reply is gone. The owner must re-ask Ollie to
  send. This is fundamental to the hook contract — a cancelled payload is
  not retried.
- **(R4) Telegram push must be < 5s.** The hook times out at 5s
  (configurable). If Telegram is degraded, the push will be dropped but the
  gate stays closed (fail-closed). The owner will not see the request and
  the sender will not reach Ollie. Consider adding a longer
  `hookTimeoutMs` (e.g. 10000) if Telegram is slow on the wire.
- **(R5) State file is shared with the openclaw installer.** The installer
  can overwrite `approved[]`/`blocked[]` during a future install. The plugin
  preserves sibling fields (`_schema`, `notes`, etc.) but its runtime fields
  are last-write-wins. Concurrent writes from the installer are rare and
  the atomic-rename pattern keeps them safe.
- **(R6) Pending requests have no auto-approval path.** A `requestTimeoutMinutes`
  timeout only flags the entry as `expired` in the file; the next message
  from that number creates a **new** pending entry (with a new ref). This is
  intentional — we never want a timeout to silently approve an unknown.
- **(R7) `ctx.messageProvider` for `before_agent_run`** is verified by the
  PRD's source citation (`dist/hooks.md`); the d.ts is the type-level
  guarantee. Both line up.

## Source verification log

All API shapes were read from the live gateway (`source@<TAILSCALE_IP>` → WSL
`OpenClawGateway` → `/home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/`)
on 2026-06-04. No API signatures were invented.

| What | File | Lines |
|---|---|---|
| `PluginHookName` enum | `dist/plugin-sdk/hook-types-B_5108I1.d.ts` | 265-266 |
| `before_agent_run` event shape | same file | 868-877 |
| `before_agent_run` runner (fail-closed on invalid) | `dist/hook-runner-global-BdHeqKff.js` / `hook-runner-global-BdHeqZIb.js` | 676-707 |
| `message_sending` event + result | `dist/plugin-sdk/hook-types-B_5108I1.d.ts` | 190-200 |
| `message_received` event | same file | 175-188 |
| `PluginHookMessageContext` (ctx for message hooks) | same file | 94-170 |
| `PluginHookAgentContext` (ctx for before_agent_run) | same file | 274-293 |
| `definePluginEntry` (canonical entry) | `dist/plugin-sdk/plugin-entry-Cufh5MG3.js` | 6-25 |
| `api.on(name, handler, {priority, timeoutMs})` | `docs/plugins/hooks.md` | 31-50 |
| Telegram send pattern | `dist/extensions/device-pair/index.js` | 556-560 |
| WA channel `allowFrom` resolved at ingress | `dist/access-control-*.js` | 51-60 (per PRD) |
| `openclaw.plugin.json` manifest shape | `dist/extensions/phone-control/openclaw.plugin.json` | (canonical template) |
| `DEFAULT_PLUGIN_ENTRY_CANDIDATES` | `dist/manifest-DaiqPlf0.js` | 833-838 |
| Atomic file replace pattern | `dist/extensions/phone-control/index.js` | (uses `replaceFileAtomic`) |
| Conversation hook access opt-in | `docs/plugins/hooks.md` | 191-198 |
| `ctx.messageProvider` for channel-originated runs | `docs/plugins/hooks.md` | 240-247 |

## License

UNLICENSED / private.
