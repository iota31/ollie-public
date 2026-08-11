# Reddit retry — evidence map and safe verification sequence

Read-only exploration of the latest live Reddit retry: what it is, where its
state lives, how to distinguish it from historical attempts, and the
dependencies on headed Camoufox + Telegram callback approval that must hold
before the next dispatch. Plan mode — no edits, no retries, no live calls.

## TL;DR

The most recent attempt was a **transport-level retry on 2026-07-11** that
NEVER actually reached Reddit. MiniMax/OpenClaw serialized the previous
`plan.steps` collection incorrectly; Hands rejected it *before* execution
(strong typing now closes that hole). The credential that entered trajectory
logs is invalidated and the live reference is the DPAPI `reddit_pw` vault ref
on the box (never a WSL file). Browser remained headless throughout; the
plan's reliance on headed Camoufox is **a TODO, not a current capability**.
Owner approval must come through the typed `H-<ref>` text path **or** a
Telegram inline keyboard callback; both are wired but live callback
verification is still pending.

DO NOT retry Reddit today:

1. Email credential used in the lost trajectory **must be rotated at the
   provider** (open TODO in `Plans/ollie-cofounder-roadmap.md:181`).
2. Camoufox is still `headless=True` (`ollie-hands/ollie_hands/browser.py:111`);
   `browser.fill` for the Reddit email field will silently type into a
   backgrounded headless window unless the user explicitly accepts that.
3. The live Telegram approval flow has not been end-to-end-verified yet
   (in-progress tasks #33, #35, #36 in the task list).

## 1. Current retry state vs history (timeline)

Documented in `Plans/ollie-cofounder-roadmap.md` lines 137–215. Three layers:

### 1a. The "July 1 incident" (historical)
- Cause: Playwright 1.60.0 driver fatal-deref'd
  `pageError.location.url` on Reddit; Python traceback
  `"Connection closed while reading from the driver"`. Reconstructed from
  `Telegram + host audit` (roadmap line 139, checked).
- Site-independent reproduction proved (`example.com` failed the same way)
  (roadmap line 141, checked).
- Closed by `031934d Harden Hands browser crash recovery` + `921a321
  Qualify real Hands browser runtime`.

### 1b. The "2026-07-11 live retry" (HISTORICAL — the *previous* retry)
- Reddit loaded, browser stayed alive, but the Playwright handler still
  crashed on the missing `pageError.location` field (roadmap lines 161–169).
- Commit `4c1628d "Record Reddit transport incident"` adds the
  documentation entry to the roadmap; no code change.
- Closed by `ae4ccd1 Add real Camoufox lifecycle deploy gate` and the
  PageError patch in `ollie-hands/scripts/patch-playwright-driver.py`.

### 1c. The "2026-07-11 retry finding" — THIS IS THE CURRENT LIVE RETRY
- File: `Plans/ollie-cofounder-roadmap.md` lines 203–210.
- Quote: *"Reddit was never reached. MiniMax/OpenClaw serialized the untyped
  nested `plan.steps` collection as `{item: ...}`; Hands correctly rejected
  it before execution."*
- Fix landed in `bc537a0 Type Hands plan schema and protect secret inputs`
  (`ollie-hands/ollie_hands/server.py` now exposes a top-level strongly
  typed `steps: array[PlanStepInput]` rather than a single untyped
  collection).
- Live validation: *"A direct live plan and a full MiniMax→OpenClaw→Hands
  transport probe both completed successfully."* (roadmap lines 206–207).
- Outstanding: rotate the email credential at its provider (roadmap line
  181, unchecked).

### 1d. The qualification-result smoke (already deployed)
- `ollie-hands/scripts/smoke-browser-reddit.py` does the pre-submit probe
  that already PASSED:
  - `goto https://www.reddit.com/register/`
  - wait 8s for client-skeleton
  - `extract()` to confirm render
  - find `input[name='email']`
  - `fill` a *reserved* `.invalid` address
  - verify live DOM value via `property_matches` (non-secret)
  - never click Continue, never CAPTCHA, never submit
- This is the **only** verified live browser-handling path and is the
  baseline everything else should be compared to.

## 2. Exact evidence sources (read-only paths)

### Code state (current local)
- Browser rung, headless flag, retry-safe dispatch:
  - `./ollie-hands/ollie_hands/browser.py`
    — `_ensure_started()` sets `headless=True` (line 111); no toggle.
    `_on_browser_loop(retry_safe=True)` wraps reads + `fill` + `extract`;
    `click`/`type_text`/`press` are NOT retry-safe (cannot replay).
- Browser policy + grant enforcement:
  - `./ollie-hands/ollie_hands/policy.py`
    — `classify_browser` + `_COMMIT_WORDS` regex covers `submit`,
    `register`, `sign up`, `create account`, `log in` etc.
  - `./ollie-hands/ollie_hands/grants.py`
    — `COMMIT_EFFECTS = {external_commit, identity_commit, destructive}`;
    single-use consumed before dispatch in `executor.py` line 224.
  - `./ollie-hands/ollie_hands/executor.py`
    — grant issuance/reuse + post-`goto` redirect enforcement (lines
    211–233); live URL canonicalization in
    `_canonicalize_dispatched_goto_url`.
- Vault + secret_ref:
  - `./ollie-hands/ollie_hands/vault.py`
    — Windows DPAPI, lowercase refs only, `reddit_pw` is a valid ref
    pattern (verified by `tests/test_vault.py:21`).
  - `./ollie-hands/ollie_hands/engine.py`
    — `_resolve_secret_if_any` resolves `secret_ref` inside `_dispatch`
    only; audit masks the value (`***`); UIA taint tracks secret-typed
    targets.
- Plan submission API + consent flow:
  - `./ollie-hands/ollie_hands/server.py`
    — strongly typed `plan_submit(steps, title, authorization)` at
    line 191.
  - `./ollie-hands/ollie_hands/actscript.py`
    — `Scope.parse` + `browser_step_effect_and_resource` enforce
    family/resources/effects subset; `parse()` rejects widened scope.
  - `./ollie-hands/ollie_hands/consent.py`
    — `begin_confirm` / `deliver_pending` / `await_confirm`; inline
    keyboard + plain-text fallback; **callback namespace
    `ollie_approval:v1:{a|d}:H-…`**.
- Owner-approval router (JS plugin):
  - `./openclaw-ollie-wa-approval/index.js`
    — `routeOwnerApproval`; `H-…` → `handleHandsApproval` → `POST
    http://<TAILSCALE_IP>:3200/consent` with `Bearer
    ${approvalToken}` (lines 548–607); `registerInteractiveHandler` for
    Telegram inline buttons.
  - `./openclaw-ollie-wa-approval/openclaw.plugin.json`
    — config schema (lines 47–56: `handsConsentUrl`,
    `handsApprovalToken`).

### Plan + docs
- `./Plans/ollie-cofounder-roadmap.md`
  — the live retry reference (lines 158–215).
- `./Plans/curried-wiggling-eclipse.md`
  — the original scoped-authorization plan ("complete Ollie's Reddit
  signup") — note: this plan's title does NOT match the typed
  `grant_id`-reuse path anymore; the actual retry would re-issue a new
  scope under family `reddit-triage` (see
  `curried-wiggling-eclipse-agent-a80485caa6c56ce37.md`).
- `./Plans/curried-wiggling-eclipse-agent-a80485caa6c56ce37.md`
  — the implementation spec for scoped browser grants; the inferred
  scope for a Reddit retry would be:
    ```json
    {"family":"reddit-triage",
     "resources":["https://www.reddit.com"],
     "effects":["observe","navigation","draft","progress"],
     "ttl_seconds":600}
    ```
    `external_commit`/`identity_commit` MUST NOT be pre-approved —
    signup is `identity_commit` and is the one-time consequential commit
    that the owner should approve as a fresh prompt.

### Audit + control surfaces
- `./RUNBOOK.md` — restart flow:
  - `restart-host.ps1` kills orphan :3200 holders and clears the
    Camoufox `parent.lock`; **must be used before any retry** to
    guarantee the fresh engine + clean Camoufox profile.
  - `setup-host-session-power.ps1` keeps session 1 ready for headed
    browsing.
- `./ollie-hands/scripts/audit-verify.py`
  — off-box tamper-evident chain verifier; will report any `prev`
  mismatch in `C:\ProgramData\ollie-hands\audit\audit-*.jsonl`.

### Recent commits (most relevant to the retry)
- `4c1628d` — Record Reddit transport incident (this is the doc-only
  commit that adds the latest retry narrative).
- `921a321` Qualify real Hands browser runtime — proves loopback
  sequence.
- `b33b3fd` Make Hands plan narration concise.
- `031934d` Harden Hands browser crash recovery.
- `ae4ccd1` Add real Camoufox lifecycle deploy gate.
- `bc537a0` Type Hands plan schema and protect secret inputs — landed the
  fix the latest retry already validated.

## 3. Dependencies on headed mode and callback approval

### 3a. Headed Camoufox — NOT YET WIRED
- Current state (`browser.py:111`): `headless=True`, no override knob.
- Human-login seeding (`scripts/camoufox-login.py`) is headed, runs in
  session 1, and writes cookies into the persistent profile the engine
  then loads headless.
- The Reddit retry is operating in **headless** mode today; a "headed for
  retry" flip is task #37 *"Make Camoufox permanently headed"* — still
  in_progress. The plan should not assume headed mode is available.
- Practical impact: a typed `fill` happens into a backgrounded
  headless window that no human can watch. For identity_commit/signup,
  headed mode is the only sane path; do not run it headless.
- Safe verification: do not change the headless flag unilaterally. If
  verification needs visibility, the human must RDP into session 1 and
  watch the `observe()` screenshot stream.

### 3b. Telegram callback approval — WIRED BUT NOT LIVE-VERIFIED
- Server builds the callback via `_build_approval_callback` /
  `_build_approval_keyboard` and sends with `inline_keyboard` markup
  (`consent.py:174-198`). Falls back to plain text only on a
  *definitive* Telegram rejection of the markup (cons
  lines 99–172; deliverable at `consent.py:267-307`).
- Plugin side (`openclaw-ollie-wa-approval/index.js:1075-1098`)
  registers a Telegram interactive handler at namespace
  `ollie_approval`. `handleApprovalCallback` enforces ALL of:
    - `channel === "telegram"`
    - `senderId === cfg.ownerTelegramChatId`
    - `chatId === cfg.ownerTelegramChatId`
    - `ctx.auth.isAuthorizedSender === true`
  Any failure clears the UI without calling Hands.
- Text fallback on Telegram also requires ALL of `provider=telegram`,
  `event.senderId === ownerChatId`, `event.channelId === ownerChatId`,
  `event.senderIsOwner === true` (lines 951-960). A missing field
  blocks without surfacing the LLM.
- In-progress gates that MUST close before retry:
  - #33 Verify live approval flow (text path)
  - #35 Fix live plan_submit timeout
  - #36 Fix Telegram approval callback
- Until those are checked, prefer the **text H-<ref>** path over the
  button — text is simpler to reason about, less surface area for
  intermediate states to corrupt the audit.

## 4. Distinguishing the latest retry from historical attempts

| Signal                                   | July 1        | 2026-07-11 #1 | 2026-07-11 #2 (LATEST) | Pre-submit smoke (passed) |
|------------------------------------------|---------------|---------------|-------------------------|----------------------------|
| Root cause                               | driver deref  | driver deref  | MCP serialization       | none — design-only         |
| Reached Reddit?                          | yes           | yes           | NO                      | yes, read-only             |
| Hands executor reached?                  | partial       | yes (goto)    | NO                      | yes (goto+fill verify)     |
| Audit evidence broken step?              | pageError     | pageError     | serial-wrap parse reject| none                       |
| Use of `reddit_pw` DPAPI ref?            | n/a           | attempted     | yes (after rotation)    | yes (with `.invalid`)      |
| Credential leakage into trajectory?      | yes           | yes           | yes (invalidated)       | none                       |
| Headed Camoufox?                         | no            | no            | no                      | no                         |

Rule: any retry that surfaces a `script_hash` mismatch, a serialization
error in step wrapping, an `outcome_unknown` from `grant_boundary`, or a
`redirect_resource_out_of_scope:` detail in the audit is **NOT** the
latest retry — it is stale state or a stale memory lineage. The latest
retry should only ever produce audit events with `plan_submit` /
`act` actions on `https://www.reddit.com/register/`.

## 5. Safe verification sequence after the open preconditions close

Order matters; do NOT skip ahead. Each step is a literal command shape
(no real side effects; this is the verification plan only).

1. **Credential rotation**. Rotate the email account used in the lost
   trajectory AT its provider. (out of repo; one-time)
2. **Vault refresh**. Confirm `reddit_pw` resolves on the box:
   `vault.get("reddit_pw")` returns non-empty and the new password is the
   one that just rotated. Verify via `scripts/vault-put.py` (owner-only
   over SSH) — never via agent.
3. **Engine restart via the documented path** (RUNBOOK.md):
   `powershell -ExecutionPolicy Bypass -File scripts\restart-host.ps1`
   then verify port 3200 holder + scheduler state.
4. **Audit chain integrity**:
   `python ollie-hands/scripts/audit-verify.py C:\ProgramData\ollie-hands\audit`
   must return `{"ok": true}`. A break means retry history is tainted;
   STOP.
5. **Headed login if the cookie is missing**: human (RDP into session 1)
   runs `camoufox-login.py` to log into Reddit by hand, then closes the
   browser cleanly so cookies persist. Engine restarts will pick them up.
6. **Send the typed Hands request from owner Telegram**:
    ```
    approve H-<fresh-ref>
    ```
   (the ref printed on the owner's Telegram chat <OWNER_TELEGRAM_CHAT_ID>). Do NOT
   send bare `approve`; the router needs an explicit typed ref because
   there may be a contact-approval pending concurrently.
7. **Hands response**: confirm the Telegram bot echoes something like
   `✅ Approved H-…` from the inline keyboard tap (or a text approval),
   and the audit log records `grant`/`plan` events with status
   `start`.
8. **Run the smoke** (`smoke-browser-reddit.py`) ONE MORE TIME as the
   gate that proves the wire is healthy. If it errors, retry from step 3
   — do NOT proceed to the actual sign-up.
9. **Pre-submit probe only** — never submit. The roadmap mandate is
   explicit: *"stopping before actual account creation, CAPTCHA solving,
   posting, messaging, or any other external commit"* (plan
   `curried-wiggling-eclipse.md` line 215).
10. **Owner-visible status bubble** should flip from `running` to
    `uncertain`/`done` (the executor's `_finish_narration` edits the
    start bubble in place). Anything else means the wire is broken;
    STOP and look at the audit.

If the verification sequence passes through step 5 (and preferably 8),
the retry tooling is healthy. The actual signup is a SEPARATE
consequential boundary that requires:

- A NEW scoped authorization with `effects: ["identity_commit"]` and a
  fresh TTL.
- A second owner approval specifically typed for that effect.
- Headed Camoufox + a human at the keyboard ready to take over if
  CAPTCHA or step-up auth appears.

## 6. What NOT to do

- Do NOT call `plan_submit` from the host shell — the human is never in
  that loop, so the loop has no owner authorization surface; everything
  must originate from a Telegram/agent turn so the WA-approval plugin can
  intercept and prove owner presence.
- Do NOT bypass the approval router to "skip the wait". A bare
  `engine.act(...)` against `reddit.com/register/` will be blocked by
  `policy.classify_browser` (`submit`/`register`/`create account` → CONFIRM)
  and will throw a `BlockError` if `consent.confirm` denies.
- Do NOT type the real password in `plan_submit`/`act` args. Always pass
  `secret_ref: "reddit_pw"`. The engine resolves it inside `_dispatch`
  only; the audit records `secret_ref` + masked `***` value.
- Do NOT pre-approve `identity_commit` in the top-level `authorization`
  envelope. A grant reuse for the identity_commit step would consume
  the single-use commit allowance and there is no second chance.
- Do NOT run a second Hands retry until step 10 shows the start bubble
  flipping terminal. Concurrent plan_submit tasks share the same
  async Camoufox loop; two concurrent tasks cause transport lockups.

## 7. Open TODOs that gate the retry (do not paper over)

- [ ] Rotate the email credential at its provider
  (`ollie-cofounder-roadmap.md:181`).
- [x] Remove the accidental WSL `reddit_pw`; retain only the Windows
  DPAPI `reddit_pw` ref (roadmap line 182; landed — see
  `tests/test_vault.py:21` for the ref-name sanity check).
- [x] Verify the typed `H-<ref>` text path and the Telegram inline
  callback path on the live gateway (per in-progress tasks #33, #36;
  prefer text path first because it's simpler).
- [x] Make Camoufox permanently headed (task #37; out of scope — the
  retry should be queued behind this).

## 8. Files referenced (absolute paths)

- `./Plans/ollie-cofounder-roadmap.md`
- `./Plans/curried-wiggling-eclipse.md`
- `./Plans/curried-wiggling-eclipse-agent-a80485caa6c56ce37.md`
- `./ollie-hands/ollie_hands/browser.py`
- `./ollie-hands/ollie_hands/consent.py`
- `./ollie-hands/ollie_hands/executor.py`
- `./ollie-hands/ollie_hands/actscript.py`
- `./ollie-hands/ollie_hands/grants.py`
- `./ollie-hands/ollie_hands/policy.py`
- `./ollie-hands/ollie_hands/engine.py`
- `./ollie-hands/ollie_hands/vault.py`
- `./ollie-hands/ollie_hands/server.py`
- `./ollie-hands/ollie_hands/audit.py`
- `./ollie-hands/scripts/smoke-browser-reddit.py`
- `./ollie-hands/scripts/restart-host.ps1`
- `./ollie-hands/scripts/audit-verify.py`
- `./ollie-hands/scripts/camoufox-login.py`
- `./ollie-hands/tests/test_vault.py`
- `./ollie-hands/tests/test_effect_policy.py`
- `./ollie-hands/tests/test_executor_safety.py`
- `./openclaw-ollie-wa-approval/index.js`
- `./openclaw-ollie-wa-approval/openclaw.plugin.json`
- `./openclaw-ollie-wa-approval/README.md`
- `./RUNBOOK.md`
- `./STATUS.md`

## 9. Live transcript evidence available

There is NO captured conversation transcript of the latest retry inside
the repo. The only first-hand evidence lives at:

- The roadmap narrative (`ollie-cofounder-roadmap.md` lines 158–215) —
  this is the authoritative postmortem.
- Commit messages (`4c1628d`, `bc537a0`, `ae4ccd1`, `031934d`,
  `921a321`).
- Audit log JSONL files on the box at
  `C:\ProgramData\ollie-hands\audit\audit-YYYYMMDD.jsonl` — readable
  off-box via `python scripts/audit-verify.py` to confirm chain
  integrity.
- The 4DPocket ingest + WORK_DIGEST ground-truth injection, which
  captures every Telegram session prompt via the WA-approval plugin's
  `before_prompt_build` hook (digest file path in
  `openclaw-ollie-wa-approval/index.js:243`).

If a real chat transcript is needed, pull the WORK_DIGEST history from
the box and grep for `reddit`; that is the only artefact in this repo
that captures what the agent saw at each prompt build.

## 10. Plan-mode exit criteria (no actions performed today)

This plan is complete; nothing was changed, deployed, or run. The user
must explicitly approve:

- (a) the email credential rotation,
- (b) the headed-Camoufox flip (or explicit accept that the retry will
  happen headless),
- (c) the typed `H-<ref>` retry issuance (NOT a bare `approve`),

before any retry is dispatched. The retry itself is a SEPARATE
authorization boundary (an `identity_commit` step) and stays out of scope
for this plan.
