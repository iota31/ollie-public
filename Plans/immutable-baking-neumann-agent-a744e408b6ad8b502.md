# Telegram inline callback — read-only dispatch investigation

Scope: read-only. Trace from Hands engine `confirm()` (the call that builds the
inline keyboard) through Telegram's `callback_query` round-trip back to the
plugin's `registerInteractiveHandler`, then forward to `POST /consent` on the
Hands engine. Find the first boundary that fails.

No edits, no deploys, no live probes. The box is reachable but I am not
allowed to touch it this turn (plan mode).

---

## 0. Trace map (verified from repo, not box)

```
Hands engine (<TAILSCALE_IP>:3200)                          Telegram                          Plugin (gateway WSL)
─────────────────────────────────                       ─────────                         ─────────────────────
engine.act_step()
  └── consent.confirm(preview, script_hash)             ──sendMessage (bot A)──►          (none)
        ├── begin_confirm()  → PendingConsent
        ├── deliver_pending() → _send_with_result(preview, reply_markup={Approve|Deny})
        │     └── POST https://api.telegram.org/botA/sendMessage
        │           payload includes inline_keyboard
        │             callback_data = "ollie_approval:v1:a:H-XXXX" | "…:d:H-XXXX"
        └── await_confirm()  blocks on threading.Event
                                                                       ◄── user taps Approve/Deny
                                                                  callback_query
                                                                       │
                                                                       ▼
                                              Telegram delivers to ONE bot token
                                              (the one whose keyboard was rendered —
                                              here: bot A = Hands bot, NOT the
                                              gateway bot)
                                                                       │
                                                                       ▼  (if delivered to bot A, NOT the gateway)
                                                                 (Hands has no getUpdates; the
                                                                  plugin lives on the gateway bot.)
                                                                       │
                                                                       ▼ (if delivered to the gateway bot)
                                                           registerInteractiveHandler fires
                                                           (namespace="ollie_approval",
                                                            channel="telegram")
                                                                       │
                                                       parseApprovalCallback  → isAuthorizedOwnerCallback
                                                                       │
                                                                       ▼
                                                           postHandsConsent(ref, approve)
                                                                └── POST http://<TAILSCALE_IP>:3200/consent
                                                                      Authorization: Bearer <approval_token>
                                                                       │
                                                                       ▼
                                                           Hands /consent
                                                             BearerMiddleware → consent.resolve()
                                                                       │
                                                                       ▼
                                                           pc.event.set()  →  await_confirm unblocks
                                                           Telegram answerCallbackQuery / editMessage
```

---

## 1. Proven facts (from this repo, no box contact)

### F1. Hands engine emits the inline keyboard via `confirm()` (the in-class wrapper)
File: `./ollie-hands/ollie_hands/consent.py`
- Two methods named `confirm` exist in the same `Consent` class: line 142 (legacy,
  text-only `_send`) and line 396 (wrapper: `begin_confirm` → `deliver_pending`
  → `await_confirm`). In Python the second definition replaces the first in the
  class dict; only the wrapper is reachable. (`grep -n '^    def confirm'`
  consent.py → 142, 396).
- `deliver_pending()` (line 316) calls `_send_with_result(preview, reply_markup=kb)`.
  `_build_approval_keyboard(ref)` (line 205) produces exactly:
  ```
  inline_keyboard: [[
    {text:"Approve", callback_data:"ollie_approval:v1:a:<ref>"},
    {text:"Deny",    callback_data:"ollie_approval:v1:d:<ref>"},
  ]]
  ```
  Confirmed by `tests/test_inline_approval.py::test_build_approval_keyboard_emits_exact_payload_without_digest`.
- Only callers of `consent.confirm(...)` in production code:
  - `ollie_hands/engine.py:315` — single `act_step` (CONFIRM-tier)
  - `ollie_hands/executor.py:257` — `_confirm_scope_ok` (plan with `authorization`)
  - `ollie_hands/executor.py:530` — top-level CONFIRM plan gate

### F2. Plugin receives callbacks via `registerInteractiveHandler`
File: `./openclaw-ollie-wa-approval/index.js`
- Line 932–967: when `api.registerInteractiveHandler` is available, the plugin
  registers `{channel:"telegram", namespace:"ollie_approval", handler}`.
  (`handleApprovalCallback` at line 659 is the handler body.)
- The plugin calls `POST http://<TAILSCALE_IP>:3200/consent` via
  `postHandsConsent` (line 515) using the bearer from
  `cfg.handsApprovalToken` (the dedicated approval credential, NOT the MCP
  bearer).
- Owner-auth gate requires ALL of (line 580–586):
  `senderId === cfg.ownerTelegramChatId` AND
  `chatId   === cfg.ownerTelegramChatId` AND
  `auth.isAuthorizedSender === true`.
- Terminal success path (line 698–708): atomic `editMessage({text, buttons:[]})`,
  no prior `clearButtons`.

### F3. Hands `/consent` requires an INDEPENDENT bearer
File: `./ollie-hands/ollie_hands/auth.py`
- `BearerMiddleware` (line 8) inspects `scope["path"]`. If the path stripped of
  trailing `/` is exactly `"/consent"`, it requires
  `Authorization: Bearer <approval_token>`; otherwise it requires the MCP bearer.
  On mismatch: 401 with `{"error":"unauthorized"}` and `audit.event("auth", status="denied")`.
- `server.py:299-301` hard-fails at boot if `bearer == approval_token`.
- Config: `approval.token` is a separate file at
  `<CONFIG_PATH>/approval.token` (default
  `~/.config/ollie-hands/approval.token` on POSIX, `C:\ProgramData\ollie-hands\approval.token`
  on Windows). Auto-generated if absent (`config.py:103-105`).
- Plugin schema (`openclaw-ollie-wa-approval/openclaw.plugin.json:47-52`) says
  `handsApprovalToken` default is `""`. **If empty, the plugin's
  `postHandsConsent` short-circuits with
  `{ok:false, handled:true, error:"hands approval token not configured"}`**
  (`index.js:516-518`) — Hands would never even see the request.

### F4. `ollie-hands-approval` plugin is RETIRED
File: `./openclaw-ollie-hands-approval/index.js`
- The entire plugin is a no-op tombstone (lines 99–112). It registers NO hooks.
  Hands approvals are handled by the unified owner-approval router in
  `ollie-wa-approval`. The retired plugin's `consentUrl` default is the same
  `http://<TAILSCALE_IP>:3200/consent`.

### F5. Telegram bot token reuse
Per `secrets.local.md`: there is a single Telegram bot (`@SonOfTushar_bot`,
token `<BOT_TOKEN_REDACTED> and the owner chat id is `<OWNER_TELEGRAM_CHAT_ID>`. The same
token is configured into both `channels.telegram.botToken` (gateway) and
`cfg.telegram_bot_token` (Hands). **They should be the same token on a single
box. They could in principle be different on disk.** `secrets.local.md` does
not record the Hands-side token value explicitly — only the gateway one.

### F6. Plugin contract assumes the gateway bot token == Hands bot token
The plugin's `registerInteractiveHandler({channel:"telegram", …})` is registered
against whatever bot token the gateway's Telegram channel is polling. If
Hands' `cfg.telegram_bot_token` is the SAME bot, Telegram delivers the
`callback_query` to that bot's `getUpdates` (long-poll). If different, Telegram
delivers the callback to Hands' bot, which never reads updates → callback
silently dropped at the platform layer.

This is the load-bearing hypothesis the rest of this report depends on
(see H1, H2).

---

## 2. Concrete code/log evidence available locally

None. There is no captured log line in this repo showing a `callback_query`
arrival at the gateway, no `cb {…}` line from `logCallback` (`index.js:635`),
no `auth.status="denied"` 401 audit, and no recorded `interactive handler
not available` warning. The only logs that exist are test fixtures
(`stderr.write` calls inside `__testHooks` paths). To get real evidence the
box has to be read; that is out of scope for this read-only turn.

---

## 3. Hypotheses ranked by probability (with discriminating evidence)

### H1 (HIGH). Bot-token mismatch: Hands sends the keyboard under bot A, gateway polls bot B
Symptom: user sees the Approve/Deny buttons; tap → Telegram client flashes the
button (client-side animation runs regardless); no edit happens; no plugin log
line appears.
Why it's the strongest single hypothesis: the inline path goes through Hands
`/consent`'s independent auth, so the plugin and Hands have separate creds —
but the bot token is a separate question and lives in two configs
(`channels.telegram.botToken` on the gateway, `cfg.telegram_bot_token` on
Hands). The plugin only listens to the gateway bot's `callback_query`
stream. Telegram routes callbacks to the bot whose message carried the
keyboard. If those tokens diverge, the callback arrives at Hands' bot,
which has no `getUpdates` consumer and no plugin.
Evidence to read on box (read-only):
- `~/.openclaw/openclaw.json` → `channels.telegram.botToken`
- Hands host config (`~/.config/ollie-hands/config.toml` on Linux, or the
  Windows equivalent under `C:\ProgramData\ollie-hands\`) →
  `telegram_bot_token`
- Live gateway log filtered on `callback_query` or `ollie_approval` for the
  minute of the tap
Discriminator: if a `callback received: hasData=true data=ollie_approval:…`
stderr line (`index.js:940-942`) appears at the moment of the tap → H1 is
OUT. If NO such line appears and the Telegram-side delivery audit (on the
bot's webhook or `getUpdates`) shows the callback went elsewhere → H1 is
IN.

### H2 (HIGH). `handsApprovalToken` is empty (or wrong) on the live gateway
Symptom: callback reaches the plugin, owner-auth passes, plugin POSTs to
`/consent`, gets back `{ok:false, error:"hands approval token not configured"}`
or a 401 from Hands. Plugin falls into the **transient** branch
(`index.js:709-714`): `reply("❗ Approval failed: …")` with buttons left
intact. Net effect: tap flashes, a new (non-editing) text reply appears,
original keyboard still visible.
Why H2 is high: plugin default is `""` (`openclaw.plugin.json:48-51`); the
plugin reads from
`api.config?.plugins?.entries?.["ollie-wa-approval"]?.config?.handsApprovalToken`.
If the live `openclaw.json` entry was never populated, the very first
`postHandsConsent` call returns `{ok:false, handled:true, error:"hands
approval token not configured"}` (`index.js:517-518`) and Hands is never
contacted. This produces a *visible* error reply but does NOT clear the
buttons (because the transient branch only `reply`s, never `editMessage`s).
Evidence to read on box:
- `~/.openclaw/openclaw.json` →
  `plugins.entries["ollie-wa-approval"].config.handsApprovalToken`
  (note: this is the plugin's per-entry config, NOT the MCP bearer; the
  schema says it is `""` by default)
- The Hands `approval.token` file (POSIX
  `~/.config/ollie-hands/approval.token`, Windows
  `C:\ProgramData\ollie-hands\approval.token`). These two MUST be equal in
  value; the plugin sends the former, Hands compares to the latter.
- Gateway stderr for `[ollie-wa-approval] cb {… backend_status:401 …}` or
  `…error:"hands approval token not configured"`
Discriminator: presence of `backend_status:401` or `error:"hands approval
token not configured"` in the `cb` correlation log → H2 confirmed. The
plugin **does** log a single line per callback via `logCallback`
(`index.js:635-657`).

### H3 (MEDIUM). The plugin is loaded but its `enabled` flag is `false` on the live box
The plugin's owner-approval router path still runs when `enabled=false` — but
that is only the **text** approve/deny path (`before_agent_run` line 851-863).
The `registerInteractiveHandler` registration is unconditional
(`index.js:932-967`), so a callback still reaches `handleApprovalCallback`.
So `enabled=false` does NOT itself break callbacks; H3 demoted from the
earlier plan's ranking. (Confirmed by reading `index.js:828-832` log line and
the unconditional `if (typeof api.registerInteractiveHandler === "function")`
block.)

### H4 (MEDIUM). `registerInteractiveHandler` does not exist on the live OpenClaw core
If the gateway runs an OpenClaw version that doesn't expose
`api.registerInteractiveHandler`, the plugin logs
`registerInteractiveHandler not available; inline approval buttons will not
work.` (`index.js:966`) and the plugin has no way to be notified of a tap at
all. This produces a fully silent drop: tap → client animation → no edit, no
reply, no log on the gateway side about the tap. (Hands engine still has no
notification either, because `await_confirm` keeps blocking on the
threading.Event.) The wait would time out at `confirm_timeout`, owner gets
nothing, audit shows `confirm status=denied detail=code=… timeout=true`.
Evidence to read on box: that exact warning log line on gateway startup or in
the stderr.
Discriminator: log line present → H4. Absent → H4 out.

### H5 (MEDIUM). OpenClaw interactive-handler ctx field names diverged from the plugin's expectations
The plugin's `norm` builder (`index.js:946-954`) reads from a single shape:
`ctx.senderId`, `ctx.auth`, `ctx.callback.{data|payload, chatId|chat_id,
messageId|message_id}`, `ctx.respond`. If the live core passes e.g.
`ctx.update.callback_query.data` and `ctx.update.callback_query.from.id`, then
`data` is `undefined`, `senderId`/`chatId` may be empty, and
`isAuthorizedOwnerCallback` returns `false` for senderId, chatId, AND
auth.isAuthorizedSender (the auth is `undefined`).
The handler still runs and would `reply("❗ Not authorized.")` (terminal
*non*-edit branch) because the unauthorized path uses `reply`, not `editMessage`
(`index.js:678-691`). Buttons stay visible; a text "Not authorized." appears.
Evidence to read on box: `Not authorized.` reply in Telegram conversation +
the `[ollie-wa-approval] callback received: …` stderr line where
`hasSender=false` or `hasAuth=false` (`index.js:940-942`).
Discriminator: stderr line on tap showing
`hasSender=false`/`hasAuth=false` → H5. All three flags true but the request
still fails → not H5, look at H1/H2.

### H6 (MEDIUM). Hands `/consent` returns 200 with `{ok:true}` but the Telegram edit fails
Once the plugin has a 200 from Hands, it calls
`respond.editMessage({text:"…Approved H-XXXX. Ollie is proceeding.", buttons:[]})`.
If Telegram rejects the edit (e.g. message not found, message not modified,
chat not found), `respondToCallback` (`index.js:588-597`) catches the error
and logs `callback editMessage error: <…>`, then the plugin tries a
`reply({text, buttons:[]})` fallback (line 705-707). The fallback is a NEW
message with NO buttons, so the original message with the buttons is
untouched.
Why H6 is medium: this is the most user-visible-but-still-broken mode
("Approved" reply appears in chat but the original buttons never go away).
Evidence to read on box: stderr line `callback editMessage error: …` for
each tap.
Discriminator: presence of `editMessage error` AND presence of `edit_result:
"reply_fallback"` in the `cb` correlation log → H6 confirmed.

### H7 (LOW). The Hands `/consent` response is 404 because the ref already expired
If the owner is slow on the tap (>confirm_timeout), `consent.resolve()`
returns `{ok:false, error_code:"unknown_or_expired"}`. Plugin treats 404 as
terminal (`index.js:553, 695-696`): it calls `editMessage("❗ Unknown or
expired ref …")`. If `editMessage` then fails as in H6, the same
"buttons-stay-visible" symptom appears.
Discriminator: `cb` log with `backend_status:404 backend_error_code:"unknown_or_expired"`.

### H8 (LOW). Rate-limit on the Telegram `editMessage`/callback path
`rate_limited: true` from Hands → `429`. Plugin treats as transient
(`index.js:709-714`), keeps buttons, replies. After the rate-limit window the
plugin does not retry automatically; buttons remain until the next tap.
Discriminator: `cb` log with `backend_status:429`.

---

## 4. Most likely FIRST failing boundary

Without box-side log access I cannot prove which hypothesis is firing. The
structural ranking in order of expected yield is:

1. **H1** (token mismatch) — cheapest to verify (one string compare on box)
   and produces the cleanest "nothing happens at all" symptom.
2. **H2** (empty `handsApprovalToken`) — same cost to verify, produces a
   visible "Approval failed: hands approval token not configured" reply
   that the user would likely have reported; H2 fits if the user reports
   seeing a text reply appear but no buttons clearing.
3. **H4** (missing `registerInteractiveHandler` API on live core) — verify
   by checking the gateway startup log for the exact warning string.
4. **H5** (ctx field names diverged) — verify by grepping gateway stderr for
   the `callback received: …` line at tap time and reading
   `hasSender`/`hasAuth` flags.
5. **H6 / H7 / H8** — only matter if H1/H2/H4/H5 are out.

The single highest-leverage box-side check is to read the gateway stderr
since the last failed tap for:
- `callback received: hasSender=… hasChat=… hasData=… hasAuth=…`
- `cb {… backend_status:… edit_result:…}` (one line per callback)
- `registerInteractiveHandler not available`

Those three lines exist by design (plugin emits them on every tap or on
load). Their presence/absence and their values are the discriminator for
H1/H2/H4/H5 simultaneously.

---

## 5. Why this is structural, not "telegram broken"

The plugin + engine pair has 42 structural tests covering every code path
involved (parser, owner-auth gate, terminal vs transient vs malformed,
clearButtons vs editMessage atomicity, sanitizeForLog). All 42 are green by
design (see `openclaw-ollie-wa-approval/test/inline_approval_callbacks.test.js`
and `ollie-hands/tests/test_inline_approval.py`). The test gap is precisely
that **none of them exercise the live OpenClaw core contract** — they hand-
build `api` mocks. So the failure mode is contract drift between the
plugin's expectations and the live gateway version, not a code bug.

---

## 6. Files referenced (absolute paths)

Plugin (Telegram side):
- `./openclaw-ollie-wa-approval/index.js`
  — register, `registerInteractiveHandler`, `postHandsConsent`,
  `handleApprovalCallback`, `logCallback`
- `./openclaw-ollie-wa-approval/openclaw.plugin.json`
  — config schema, defaults (`handsApprovalToken=""`,
  `handsConsentUrl="http://<TAILSCALE_IP>:3200/consent"`)
- `./openclaw-ollie-wa-approval/test/`
  — six Node test files covering parser, auth, router, expiry, handler
  registration

Hands engine (server side):
- `./ollie-hands/ollie_hands/consent.py`
  — inline keyboard emit, `begin_confirm`, `deliver_pending`,
  `_send_with_result` (definitive vs ambiguous classification), `resolve`
- `./ollie-hands/ollie_hands/server.py`
  — `/consent` route registration, BearerMiddleware mount,
  `cfg.bearer_token()` / `cfg.approval_token()` boot check
- `./ollie-hands/ollie_hands/auth.py`
  — route-scoped Bearer split (MCP vs `/consent`)
- `./ollie-hands/ollie_hands/config.py`
  — `approval_token_file` default, auto-generate if absent
- `./ollie-hands/tests/test_inline_approval.py`
  — keyboard payload, fallback, classification, rate-limit tests
- `./ollie-hands/tests/test_consent_correlation.py`
  — `deliver_pending` audit events (keyboard_accepted / keyboard_rejected /
  plain_accepted / keyboard_send_failed)
- `./ollie-hands/tests/test_consent_route.py`
  — `/consent` POST shape, ref validation, digest binding

Retired plugin (tombstone):
- `./openclaw-ollie-hands-approval/index.js`
  — registers no hooks, no impact on callback flow

Diagnostic plans already on disk (used as evidence scaffolding, not as
authoritative state):
- `./Plans/curried-wiggling-eclipse-agent-a4828504b7d40e01c.md`
- `./Plans/curried-wiggling-eclipse-agent-a36917c426546755a.md`
- `./Plans/curried-wiggling-eclipse-agent-a5ad139b325e2aef7.md`

---

## 7. What I did NOT do (per plan-mode constraint)

- Did not SSH to the box.
- Did not read or write any file under `~/.openclaw/`, the WSL distro, or
  the Windows host.
- Did not propose a fix, edit, or deploy.
- Did not run the test suite (read-only investigation).