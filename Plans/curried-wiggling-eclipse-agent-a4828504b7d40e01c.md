# Approval callback: tap flashes but does not clear buttons / resolve consent

Read-only diagnostic plan. No edits, deploys, or live consent invocation will be
performed from this plan; the user reviews and explicitly approves any future
fix/test/deploy work.

## 1. Scope and goals

**Surface observed (per user):** A Telegram tap on an inline approval button
emitted by the Hands consent engine produces a brief visual "flash" on the
client (the standard Telegram "you tapped a button" affordance), but the
buttons remain visible, the prompt text is unchanged, and the Hands
`POST /consent` either does not run or runs but has no effect on the pending
state. The owner therefore cannot resolve the prompt from the button and must
fall back to the text grammar.

**What this plan covers:**

- Map the full inline-button contract from engine through plugin handler to
  backend, with exact file references.
- Identify the precise structural points where the live flow can fail silent
  (owner-auth gating, callback-data shape, ordering of `clearButtons` vs
  `editMessage` vs `POST /consent`, transient detection, timeout on the core
  interactive-handler loop).
- Enumerate root-cause candidates ranked by probability, with the exact
  evidence the candidate produces in the live system (which logs to read).
- Propose a minimal instrumented fix + test plan that adds an end-to-end
  callback integration test (the current suite has no live OpenClaw core
  loader test) and a short set of focused instrumentation points so the next
  live attempt produces enough evidence to discriminate candidates.

**What this plan does NOT cover (deferred):**

- Editing any plugin, engine, or test file.
- Deploying to the box.
- Invoking any approval flow against the live Hands engine.
- Touching any secret material (approval.token, MCP bearer).

## 2. Files and contracts touched

| Layer | File | Role |
|---|---|---|
| Approval router (live) | `./openclaw-ollie-wa-approval/index.js` | Registers `before_agent_run`, `message_sending`, `message_received`, and `registerInteractiveHandler({channel:"telegram", namespace:"ollie_approval"})`; owns `parseApprovalCallback`, `isAuthorizedOwnerCallback`, `handleApprovalCallback`, `postHandsConsent`. |
| Approval router | `./openclaw-ollie-wa-approval/approval-command.js` | Strict text grammar `^(approve\|deny)(?:\s+((?:H\|W)-[A-Za-z0-9_-]{3,32}))?\s*$`. |
| Approval router tests | `./openclaw-ollie-wa-approval/test/inline_approval_callbacks.test.js` | 42 structural tests (parser, owner-auth, success/expired/transient). |
| Plugin manifest | `./openclaw-ollie-wa-approval/openclaw.plugin.json` | Declares `handsConsentUrl` default `http://<TAILSCALE_IP>:3200/consent`. |
| Engine keyboard emitter | `./ollie-hands/ollie_hands/consent.py` | `_build_approval_callback`, `_build_approval_keyboard`, `deliver_pending` (sends the keyboard; falls back to plain text on definitive Telegram 4xx indicating markup rejection). |
| Engine HTTP surface | `./ollie-hands/ollie_hands/server.py` lines 257-282 | `consent_endpoint` mounted at `/consent` (GET inventory, POST resolve). |
| Engine auth boundary | `./ollie-hands/ollie_hands/auth.py` | `BearerMiddleware` rejects `/consent` calls without `Bearer <approval.token>`. |
| Engine tests | `./ollie-hands/tests/test_inline_approval.py` | Python coverage of keyboard emission and fallback. |

The plugin comment (lines 1063-1074) claims a live OpenClaw 2026.5.28
contract for `registerInteractiveHandler`. That comment is the single piece of
live-evidence evidence we have for handler ctx shape; there is no source listing
in the checked tree beyond `node_modules/openclaw/plugin-sdk/plugin-entry.js`,
which only exports the trivial `definePluginEntry` stub.

## 3. Code map of the callback path

The full request lifecycle is:

```
Telegram (tap)
  -> Telegram Bot API delivers Update to gateway
  -> OpenClaw core matches update against registered interactive handlers
     (channel=telegram, namespace=ollie_approval)
  -> Core constructs ctx and invokes our handler(ctx)
       ctx = {
         senderId:        <string>,
         callback:        { data|callback_data|payload, chatId|chat_id, messageId|message_id, ... },
         auth:            { isAuthorizedSender: <boolean> },
         respond:         { reply({text}), editMessage({text}), editButtons(...), clearButtons() }
       }
  -> Plugin wraps ctx into `norm` and calls handleApprovalCallback(api, norm)
  -> handleApprovalCallback:
        parsed = parseApprovalCallback(norm)        // strict 4-part split, /H-[A-Za-z0-9_-]{1,61}/
        if !parsed.handled -> {handled:false}        // not ours; core falls through
        if parsed.malformed -> clearButtons + editMessage "Invalid approval button."
                                 (claim handled:true so no double-fire)
        if !isAuthorizedOwnerCallback -> clearButtons + editMessage "Not authorized."
                                         (claim handled:true)
        r = await postHandsConsent(api, parsed.ref, parsed.approve)
           POST http://<TAILSCALE_IP>:3200/consent
             headers: Authorization: Bearer <approval_token>
             body:    {"ref": <ref>, "approve": <bool>}
        branch on r.status/r.body for clear+edit (terminal) vs reply (transient)
        ALWAYS return { handled: true } for our namespace.
```

### 3.1 Contract-violation hotspots (live vs plugin code)

The plugin's parser/auth gates reject most callback payloads before the POST.
The user-reported symptom — "tap flashes" — implies the callback is reaching
the gateway (or at least Telegram's tap acknowledgement reaches the client)
but the plugin either:

1. Never receives the callback (so the core does not invoke the handler), or
2. Receives the callback but the handler returns `{handled:false}` and the
   core falls through to its default behavior, or
3. Receives the callback, the handler hits a different branch that does not
   clear/edit (e.g. recognized as transient, or `respond` is missing), or
4. Posts to `/consent`, gets success, but the editMessage call fails silently
   inside the swallowed `try {} catch {}`.

The "tap flashes but stays visible" symptom rules out (3) only partially:
Telegram client-side animation fires on any tap, regardless of whether the
bot ever replies.

## 4. Root-cause candidates (ranked by probability)

Each candidate is paired with the specific evidence you would see in the
live logs and the precise file:line to read.

### R1 (HIGH): namespace or channel mismatch on the core registration side

The plugin registers:

```
api.registerInteractiveHandler({
  channel: "telegram",
  namespace: APPROVAL_CALLBACK_NS,    // "ollie_approval"
  handler: async (ctx) => ...
});
```

(`./openclaw-ollie-wa-approval/index.js` lines 1075-1095)

The Hands engine emits callback_data starting with `ollie_approval:v1:...`
(`./ollie-hands/ollie_hands/consent.py`
line 181). The names match.

But: the core's interactive-handler matching is per-message-incoming-update;
the bot account that emits the keyboard and the bot account that owns the
core's update ingestion may be different (the gateway may be polling a
different token than the one Hands uses for `sendMessage`).

Evidence to read on box:

- Gateway debug log for the tap event. Look for lines containing
  `callback_query`, `ollie_approval`, or `interactive`.
- Hands `cfg.telegram_bot_token` / gateway bot token mismatch. If they
  differ, the core never sees the `callback_query` at all — only Hands
  sees a `callback_query` against its own token, and our plugin's
  `registerInteractiveHandler` is on the gateway. Telegram forwards the
  tap to whichever bot token the originating keyboard came from.

How to discriminate: if no plugin log line appears on tap, candidate R1 is
in play. If a "Telegram callback received" style log appears, R1 is out.

### R2 (HIGH): handler ctx field-name mismatch

The plugin reads from `ctx.callback.data` and `ctx.callback.chatId`
(`index.js` lines 1084-1089). The live OpenClaw 2026.5.28 contract may use
`ctx.callback_query.data` or top-level `ctx.data`. The plugin's wrapper
explicitly ORs `data | payload` and `chatId | chat_id`, which is defensive
for some but not all possible shapes.

If the live core uses e.g. `ctx.update.callback_query.data` and `ctx.senderId`
lives at `ctx.update.callback_query.from.id`, our `norm` ends up with
`data: undefined`, `senderId: ctx.senderId` (still present if it exists at
that path), and the parser returns `{handled:false}`.

Evidence to read: the gateway's interactive-handler dump / inspection script.
If a payload-shaped object exists in `ctx`, R2 is in play. Compare to the
plugin's normalization or to a candidate `ctx.callback_query` shape.

### R3 (MEDIUM): handler returns `{handled:false}` for malformed pay to claim

`parseApprovalCallback` returns `{handled:false}` when:

- channel !== "telegram" (defensive branch at line 754).
- data does not exist or is not a string (lines 756-757).
- split(":") does not yield exactly 4 parts (line 759).
- parts[0]/parts[1] don't match namespace/version (line 760).
- dec/ref are well-formed but our regex `/^H-[A-Za-z0-9_-]{1,61}$/` rejects
  (line 767). Refs are `H-` plus 6 url-safe base64 chars, ≤ 62 bytes total;
  refs longer than 63 bytes never reach here because the engine refuses to
  emit a keyboard if the callback exceeds 64 bytes
  (`consent.py:182 _build_approval_callback`).

If any of these fire, the handler returns `{handled:false}` and the core
proceeds with whatever its default behavior is — typically a no-op or a
generic acknowledgment — which leaves the buttons visible.

The single most likely R3 sub-cause is **channel mismatch**: the parser
inspects `ctxLike.channel` (line 753) and bail-outs when it's not exactly
`telegram`. The wrapping `norm` sets `channel: "telegram"` unconditionally
(line 1083), so this is only a risk if the calling convention passes
something other than the `norm` we built — which would only happen if R2 is
also wrong.

Discriminator: instrument the parser (next section) so the first five lines
of `parseApprovalCallback` log the raw ctx keys + the data string length.
A `data: undefined` line confirms R2/R3.

### R4 (MEDIUM): owner-auth gate rejects without telling the user

`isAuthorizedOwnerCallback` (lines 782-799) requires ALL of:

- `senderId` exact-equals `cfg.ownerTelegramChatId` (string "<OWNER_TELEGRAM_CHAT_ID>").
- `chatId` exact-equals the same.
- `auth.isAuthorizedSender === true` (strict boolean, not truthy).

The plugin's wrapper code currently sets only `senderId`, `chatId`, `data`,
`auth`, `respond`, `messageId` from ctx (lines 1082-1090). It is plausible
that `ctx.auth` is at a different path (e.g. `ctx.callback_query.from` already
checked at the core level, and `auth` is a top-level wrapper), so
`norm.auth` is `undefined` and the gate fails closed.

The handler DOES clear buttons on rejection (lines 817-820), which would
manifest as "tap flashes, then text changes to 'Not authorized.'".
A persistent flash with no text change implies the gate is NOT the failure
point (R4 is out).

Discriminator: instrument the auth gate with a one-line log of
`senderId`, `chatId`, and `auth?.isAuthorizedSender`. If the tap event
reaches the plugin and the log shows `auth?.isAuthorizedSender === false`,
R4 is in play — and the fix is to widen the owner-auth contract (e.g.
also accept `ctx.update?.callback_query?.from?.is_bot === false` plus
sender-id match).

### R5 (MEDIUM): throw inside the handler kills `clearButtons`/`editMessage`

The terminal/transient UI branch is wrapped in `try {} catch {}`
(lines 825-854). Any throw from `respond.clearButtons()` or
`respond.editMessage()` is silently swallowed, leaving the buttons
unchanged. The plugin unit tests (`inline_approval_callbacks.test.js`
lines 132-174) exercise only mock `clearButtons`/`editMessage` that never
throw. Real OpenClaw core methods could throw under several realistic
conditions:

- The core implements `editMessage` as a thin wrapper around `fetch` to
  Telegram Bot API; a 4xx (e.g. `message is not modified`, `chat not
  found`, `message can't be edited`) throws a rejected promise.
- Telegram 400 with `message is not modified` is the canonical
  client-side-no-change response; if the plugin posts to `/consent` AFTER
  the core's auto-acknowledgement timeout, the editMessage would fail
  silently.

Discriminator: wrap the `try {} catch {}` so any throw logs the error
(MEMORY warns never to use `try{}catch{}` for swallow-and-continue without
a log — current code violates this). A log line like
`editMessage threw: <message_is_not_modified>` is the smoking gun.

### R6 (LOW): Hands `/consent` returns 404 on the first POST because the ref
arrived after expiry, then `editMessage` with "Unknown or expired" fails
because the original task already moved on

The engine sets `pending[ref].expires_at` to `time.monotonic() + confirm_timeout`
(`consent.py:253`). For L2 browser steps under load, the `_send_with_result`
fallback dispatch delays the keyboard delivery; by the time the owner taps,
the ref may be expired.

If `/consent` returns 404 with `error_code: "unknown_or_expired"`, the
plugin correctly calls `clearButtons` + `editMessage("Unknown or expired ref H-XXX")`.
But if the original Hands task already produced a terminal message via
`task_finished`, Telegram rejects the edit with `message is not modified`
or `message to edit not found`. Buttons stay visible.

Discriminator: log the `/consent` response body and the `editMessage`
outcome for every callback. If `/consent` says `unknown_or_expired` AND
editMessage throws, R6 is in play.

### R7 (LOW): Hook-timeout / core interactive-handler deadline

The plugin's interactive handler has no explicit timeout — OpenClaw
defaults are unknown. If the handler is bounded by the gateway's
interactive-loop deadline (e.g. 3s), and `postHandsConsent` takes >3s
(network or Hands startup), the core aborts the handler — possibly with a
silent cancel — leaving the buttons visible. The plugin's `await
postHandsConsent` is not wrapped in a Promise.race timeout.

Discriminator: add a wall-clock instrumentation on `handleApprovalCallback`
returning. If the handler is killed before reaching the UI branch, R7.

### R8 (LOW): Concurrent in-flight taps coalesce

If the owner double-taps (or replays the same `callback_id`),
OpenClaw 2026.5.28 is documented as "callback-id dedupe are core-provided".
The plugin returns `{handled:true}` regardless. Two simultaneous taps race:
tap #1 succeeds; tap #2 hits a `popup` auto-ack but the plugin never sees
it (core dedupes). This should not manifest as "buttons stay", only as
"first tap clears, second tap nothing".

Unlikely to be primary; included for completeness.

## 5. Pre-existing test gap (root cause hidden by green tests)

The 42 structural tests in
`./openclaw-ollie-wa-approval/test/`
all run with a hand-built mock of `api`, `runtime`, `registerInteractiveHandler`,
and `respond`. They verify:

- `parseApprovalCallback` happy + malformed paths.
- `isAuthorizedOwnerCallback` strict 4-field gate.
- `handleApprovalCallback` happy/unauthorized/malformed/expired/transient
  branches.
- `routeOwnerApproval` inventory selection, dedupe, bare command.
- `enabled=false` still registers the handler.
- `applyOwnerReply` rejects expired W ref under lock.

What they do NOT cover:

1. The actual normalization at the registration site (lines 1082-1090):
   no test asserts the wrapper correctly extracts `data`, `senderId`,
   `chatId`, `auth`, and `respond` from a real OpenClaw ctx shape. Any
   field-name mismatch (R2) is invisible to this suite.
2. The exact `respond` method shape. Tests pass `clearButtons`/`editMessage`
   as async functions; the live contract may return a Promise that rejects
   on `message is not modified`. Test mocks never reject.
3. The Hands `POST /consent` interaction. Tests use `globalThis.fetch`
   stubs; live uses uvicorn-starlette with `BearerMiddleware`. Mismatch is
   only visible end-to-end.

A minimal instrumented fix must close test gap (1) at minimum.

## 6. Live-evidence gaps right now

We have no logs to read because no live tap has been observed in this
session. The next-tap evidence must include:

1. The gateway's interactive-handler log line (whether the handler was
   invoked at all).
2. Inside the plugin handler: raw ctx keys present, raw data string,
   parsed `dec` and `ref` bytes, `auth.isAuthorizedSender`, the
   `respond` keys present.
3. The Hands `/consent` response status + body for the callback's POST.
4. Whether `respond.clearButtons` was called and whether
   `respond.editMessage` resolved or threw.

Without (2)-(4), any fix is a guess. The fix plan therefore starts with
instrumentation, not a code change.

## 7. Minimal instrumented fix + test plan

### Phase 0: instrumentation-only (no behavioral change)

Insert ONE log line at each boundary. Use existing `api.logger.info/warn`
so no new log infra is needed.

- `parseApprovalCallback` (line 752 area): `info` with raw ctx keys,
  `channel`, `data` string, and the length. Drop on the test path so
  the existing 42 tests stay green (gate the log behind a `process.env`
  flag, or print only when the input data starts with `ollie_approval`
  so production-only data triggers it).
- `isAuthorizedOwnerCallback` (line 798 area): `info` with the four
  required fields and the boolean result, only on reject.
- `handleApprovalCallback` (line 824 area): `info` with `ref`,
  `approve`, and the final `r.status`/`r.body` summary.
- Around the `clearButtons`/`editMessage` try-catch (lines 836-853): wrap
  any thrown error and `warn` it (this fixes the silent-swallow footgun
  regardless of which root cause is active — it's a pure observability
  improvement, not a behavior change).

These four lines turn "tap flashes, nothing happens" into a 5-line stack
trace in the gateway log on the next tap.

### Phase 1: targeted fix (only after Phase 0 disambiguates)

Based on which R-root evidence Phase 0 surfaces:

- If R1 or R2 surfaces, the fix is a wrapper-only change at lines
  1082-1090 to read the correct ctx path. Tests added in Phase 2.
- If R4 surfaces (auth.isAuthorizedSender false), the fix is in
  `isAuthorizedOwnerCallback` to additionally accept the runtime's
  in-band owner signal (e.g. `ctx.senderIsOwner`) for callback-side
  matching; tests for that path go in Phase 2.
- If R5 surfaces (editMessage throws swallowed), the fix is to convert
  `try{}catch{}` into a `try{...}catch(err){api.logger.warn(...)}` —
  also observability, no behavior change beyond the next-tap UI update.
- If R6 surfaces (expired + edit fails on a moved-on task), the fix is
  to add an outer `Promise.race` against `clearButtons` so even when
  the edit fails the buttons are gone. Tests added in Phase 2.
- If R7 surfaces (handler killed by deadline), add a Promise.race
  inside `handleApprovalCallback` between `postHandsConsent` and a
  `setTimeout(clearButtons+editMessage("Still working..."))`. Tests
  added in Phase 2.

### Phase 2: tests that lock in the fix

Add to
`./openclaw-ollie-wa-approval/test/`
a new file `inline_callback_contract.test.js` that:

1. Mocks an OpenClaw-shaped interactive handler ctx using the ACTUAL key
   names the plugin's wrapper reads (`callback.data`,
   `callback.chatId`, top-level `senderId`, top-level `auth`,
   top-level `respond`). Asserts that the wrapper produces a `norm`
   whose `data`, `chatId`, `senderId`, `auth`, `respond` match what
   `parseApprovalCallback`/`isAuthorizedOwnerCallback` expect. (Closes
   test gap (1); R2 prevention.)
2. Forces `clearButtons` and `editMessage` to throw with a Telegram
   `400 message is not modified` shape; asserts the plugin still returns
   `{handled:true}` and does not crash. (Closes test gap (2); R5/R6
   prevention.)
3. Forges a ctx whose `auth.isAuthorizedSender === false` but
   `senderId === chatId === ownerTelegramChatId` and `senderIsOwner` is
   undefined; asserts the handler returns `{handled:true, status:"unauthorized"}`
   and clears UI. (R4 lockdown.)
4. End-to-end callback: spin up the plugin, invoke the registered
   handler with a fully-formed mock ctx, mock `globalThis.fetch` to
   return a 200 `{ok:true}`, assert `respond.clearButtons` was called
   and `respond.editMessage` was called with the terminal text. The
   existing tests already do this in `inline_approval_callbacks.test.js`
   lines 132-174; the new test wires the SAME flow through the
   registered handler (the wrapper) to expose R2-class regressions.

For Python engine coverage, add to
`./ollie-hands/tests/test_inline_approval.py`
one assertion that `_send_with_result`'s `definitive_rejection`
classification does NOT fire on an overlong `callback_data` (we
already validate at build time, but locking in the invariant against
future edits is cheap).

### Phase 3: smoke test plan against the live system (read-only)

Before declaring the fix live, the operator-facing smoke is:

1. On the box, restart the gateway.
2. From the owner's Telegram, send a Hands-bound action that triggers
   `confirm()` in `consent.py` (e.g. an L2 browser step that requires
   approval).
3. Tap "Approve" on the inline keyboard.
4. Within ~10s the plugin log (with Phase 0 instrumentation) should show:
   - handler invoked
   - parsed ref + decision
   - owner-auth OK
   - POST `/consent` 200 + `{ok:true}`
   - `respond.clearButtons()` and `respond.editMessage({text})` resolved
     without throw
5. If any of those four lines are missing or report an unexpected
   value, the fix target is the bullet above.

The user should NOT press the inline button until they have reviewed
and approved the fix plan; this is the "live approvals must be
owner-resolved" MEMORY rule (a one-tap test costs a real pending ref
that may auto-deny on timeout).

## 8. Risks and exclusions

- Risk: instrumentation logs leak the approval token or ref into the
  gateway log. Mitigation: explicitly do NOT log the Authorization header
  and redact any string that matches `/Bearer\s+\S+/i`. The current
  code does not log it; Phase 0 must preserve that.
- Risk: editing `consent.py` is out of scope for this plan per the user's
  read-only constraint. The instrumented fix is in the JS plugin only.
- Risk: the live `registerInteractiveHandler` contract may differ from
  the comment-derived shape; the Phase 2 tests must be written against
  the freshly-discovered shape, not the comment.
- Exclusion: nothing in this plan touches secrets, deploys files to the
  box, or invokes a live approval.

## 9. Decision record

The repo shows the JS plugin and the Python engine both have unstaged
edits as of 2026-07-12 (git status `M` on
`openclaw-ollie-wa-approval/index.js`,
`ollie-hands/ollie_hands/consent.py`, and the test files). The deployed
copies on the box may drift from repo HEAD. As per MEMORY
`feedback_repo_box_drift.md`, the next step before any commit must be a
fresh `git status` and a `diff box-vs-repo` for both files. The user's
explicit "Plan-mode read-only" instruction overrides that: do not run a
diff during planning; just note it for the next phase.

This plan does not propose changes to the consent engine, to the
authentication boundary, or to the gateway config. It proposes only
minimal plugin-side instrumentation gated by env, with test coverage
that closes the wrapper-shape gap. The user reviews and approves the
plan before any code touches the filesystem.
