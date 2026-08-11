# `ollie-hands` — computer-use v2 engine (Phase 0)

The single engine that owns ALL host actions on Ollie's Windows box, per
`Plans/graceful-questing-oasis.md`. Python MCP server (streamable HTTP at
`/mcp`, port **3200**) with bearer auth, **inert-boot** (does nothing until
explicitly enabled), and JSONL+PNG audit.

MCP tool surface (Phase 0–2):

| Tool | What |
|---|---|
| `session_info` | always available; enabled state, session lock, monitors |
| `observe` | screenshot + UIA window snapshot + window list in one call (T0) |
| `act` | ONE policy-gated, consent-enforced action — `kind` in {shell, uia, window, clipboard} |
| `plan_submit` | a multi-step **act-script** run at machine speed; consent decided ONCE |
| `task_status` / `task_abort` | inspect / cooperatively stop a running plan |

`act`/`plan_submit` also take `kind:"browser"` (Camoufox): `goto`/`extract`/
`links`/`screenshot`/`get_attr` (reads → notify) · `click`/`fill`/`type_text`/
`press` (interactions → notify, unless a commit click → confirm).

Plus an independently authenticated `POST /consent` route (owner approval
relay for confirm-tier actions). The MCP bearer is explicitly rejected there.

### Act-scripts (`plan_submit`) — Phase 2
The MCP arguments are top-level `title` and `steps`; `steps` is a strongly
typed JSON array. Do not wrap them inside a generic `plan` object. Nested
`preconditions` are also arrays. Secrets must use host-side `secret_ref`—never
put passwords or tokens in ordinary plan arguments because tool calls persist
in trajectories and logs.

Each step has `kind`/`args` (same as
`act`), plus `preconditions` (assert the world matches BEFORE acting),
a `postcondition` (**required on writes** — the engine verifies the change
happened), `on_fail` (`retry`/`repair`/`escalate`/`abort`), optional
`checkpoint`, and a `timeout`. The executor runs steps locally (no model call
per step), escalates only on a failed condition / timeout / collision /
checkpoint, and auto-pauses if a human touches the box mid-task. Consent is
the script's max tier, bound to its hash. Condition types: `foreground`,
`window_exists`/`absent`, `uia_exists`/`absent`, `uia_text(equals|contains)`,
`file_exists`/`absent`, `shell_exit_zero`, `web_url(contains)`,
`web_text(selector + equals|contains)`, and
`web_property(selector + property + equals|contains|nonempty)`. Schema + classification in
`actscript.py`; checks in `conditions.py`; executor in `executor.py`.

Verify secret fields with `web_property(property=value, nonempty=true)`: the
condition reports only whether it matched and never returns the value. Names
such as `uia_text_contains`, `browser_url`, and `selector_exists` are invalid.
Narrated multi-step plans use one Telegram
status message which is edited to its terminal state (with a send fallback),
instead of adding separate start/completion bubbles.

### Capability ladder status
- **L0 shell** (`shell.py`) — PowerShell, deny-set, timeout. ✅ Phase 1
- **L1 UIA** (`uia_actions.py`) — find/invoke/set_value/get_text, window
  mgmt, clipboard, **`locate`** (UIA grounding → click-ready coords); all tree
  calls on a dedicated COM thread. ✅ Phase 1
- **L2 browser** (`browser.py`) — **Camoufox** stealth browser (Firefox fork,
  engine-level anti-detect — NOT vanilla Chrome), driven by Playwright on a
  dedicated thread; ONE persistent profile holds Tushar's logins. ✅ Phase 3
- **L3 pixels** (`pixels.py`) — raw mouse + keyboard via SendInput
  (move/click/drag-select/scroll/type/key), DPI-correct virtual-desktop coords,
  records own input ticks for collision detection. ✅ actuation done.
  Grounding is tiered: UIA `locate` covers ~90% (deterministic, free); vision
  grounding (cloud-first, then UI-TARS/OmniParser if the eval demands) is the
  fallback — measured by `scripts/grounding-eval.py`. ⏳ vision tier pending.

OpenCLI was evaluated for L2 but DROPPED: it's Chrome-extension based, and
Chrome 149 blocks loading unpacked/self-hosted extensions; more importantly
vanilla Chrome is trivially bot-detectable. Camoufox gives engine-level
stealth + native Python integration (see DECISIONS D-20260610-02).

### Policy & consent (the hard gate — `policy.py`, in-code, LLM-uneditable)
This box is a dedicated spare → local actions are Ollie-scope:

| Tier | Examples | Consent |
|---|---|---|
| T0 read | reads, `observe`, `get_text`, clipboard read | auto |
| T1/T2 local | local shell writes, UIA acts, window/clipboard writes | notify (Telegram) |
| T3 | acts-as-Tushar / high-blast-radius (shutdown, recursive force-delete, reg HKLM delete, format) | confirm (owner) |
| T4 forbidden | Defender/firewall/BitLocker tamper, audit/policy/token edits, vault | blocked |

Consent is engine-owned (independent of the brain): `notify` is one-way
Telegram `sendMessage`; `confirm` blocks until an approval-token-authenticated
`POST /consent {code, approve, script_hash}` arrives. Challenges carry 128 bits
of entropy, are digest-bound, expire, are atomically single-use, and the route
is rate-limited. It **DENIES on timeout** and never auto-approves. Regression
tests: `tests/test_approval_auth.py` and `tests/test_policy.py`.

## Safety properties (Phase 0)
- **Inert boot:** `enabled: false` in config by default; additionally a
  `DISABLED` flag-file next to the config hard-stops every tool per-request.
- **Separated authority:** MCP requests use host-only `bearer.token`; only
  `/consent` accepts host-only `approval.token`. Neither credential grants the
  other's authority.
- **Audit:** every tool call appended to `audit/audit-YYYYMMDD.jsonl`
  (+ screenshot PNGs) before the response is returned.
- **Read-only:** Phase 0 has zero actuation verbs. It cannot click or type.

## Layout
- `ollie_hands/config.py` — config + token + inert/disabled logic
- `ollie_hands/audit.py` — append-only JSONL audit writer
- `ollie_hands/observe.py` — screenshot (mss), window list (ctypes/Win32),
  UIA snapshot (uiautomation)
- `ollie_hands/server.py` — MCP server + bearer middleware (uvicorn)
- `scripts/install-host.ps1` — provision on the box (venv, deps, config,
  token, firewall note)

## Deploy (box)
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-host.ps1
# then set "enabled": true in C:\ProgramData\ollie-hands\config.json
# and add the gateway MCP entry: http://<TAILSCALE_IP>:3200/mcp + bearer
```

Gateway wiring mirrors the Tier-2 POC entry in `CONFIG.md` (`type: http`,
`url`, `Authorization: Bearer ${OLLIE_HANDS_BEARER}`). Set the Hands server's
OpenClaw `timeout` to `240` seconds; this is the supported per-request timeout
seam and exceeds the engine's 180-second owner-confirmation window.

### Approval-auth migration (do not deploy partially)

1. Run `python -m ollie_hands.config` on the Windows host to provision
   `C:\ProgramData\ollie-hands\approval.token` without overwriting existing
   config or secrets. Older configs can omit `approval_token_file`; that
   default path is used.
2. Supply that new secret to the unified approval plugin as
   `handsApprovalToken`. Do **not** copy `bearer.token`.
3. Deploy the Hands engine and unified relay together, then restart both. A
   mixed-version deployment intentionally fails closed.
4. Verify the MCP bearer gets HTTP 401 from `/consent`, the approval token
   works only with the exact challenge and digest, and replay returns 404.
5. Remove obsolete `handsBearerToken` settings; they are no longer read.
