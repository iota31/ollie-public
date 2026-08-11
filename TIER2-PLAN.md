# Tier 2 — Computer-Use Engine Roadmap

> The "hands" half of Ollie. Tier 1 is the sandboxed brain; Tier 2
> is the gated unsandboxed component that actually drives the
> desktop.

---

## Why this exists

The MXC sandbox in the shipping OpenClaw Windows Companion sets
`policyJson.ui.allowInputInjection: false` — it explicitly blocks
the agent from typing, clicking, or moving the mouse on the
host desktop. That's the right default for a brand-new product,
but it makes "apply to jobs," "reply to a DM," and "fill out
this form" impossible from the agent.

So we extend the agent with a narrow, explicitly-gated
**unsandboxed** component that does only what the sandbox
forbids, behind strong owner-side gates. The agent is one
*consumer* of this component, not the owner.

---

## Goals

1. **As competent as heyclicky (or better).** heyclicky's value
   is "the AI sees your screen and acts on it." Tier 2 has to
   hit that bar for the high-value use cases.
2. **Structurally stable.** Audit-friendly, deterministic,
   testable, single-process. No shell escapes.
3. **Maintainable.** A small team can read the code, fix a bug,
   and ship a patch without learning a framework.
4. **Distributable.** Other Windows machines should be able to
   install Tier 2 with one signed installer + a one-line MCP
   config edit.

---

## OSS foundation research

Three candidates surveyed (full notes in the Clicky feasibility
study's appendix; not reproduced wholesale here):

| Project         | Org      | Grounding           | Maturity         | Verdict          |
|-----------------|----------|---------------------|------------------|------------------|
| **UI-TARS**     | ByteDance| Pixel (native)      | Active (7B, 72B) | Strong standalone model. Pure pixel = needs bigger model. |
| **Microsoft UFO**| Microsoft| UIA-tree + pixel (two-stage) | Active, Windows-first | Best architectural match — hybrid symbolic (UIA) + pixel. Heavier lift. |
| **OmniParser**  | Microsoft| YOLO-based element detection | Active, model-only | Use as **detector** on top of any grounding model. Pluggable. |

**Recommended approach:** hybrid UIA + pixel grounding, in
Python, exposed as an MCP server, code-signed installer.
- Use **UI Automation** (UIA) for tree-based actions
  (find by name, get text, get role/control-type) — these are
  free, fast, and don't need a model.
- Use a **vision model** for pixel-grounded clicks (anything
  UIA can't address: canvas, custom controls, image-only UI).
  Start with MiniMax M3. Swap in UI-TARS if M3 aim is flaky.
- Use **OmniParser** as an element detector on top of the
  vision model (YOLO-style boxes + labels).

---

## Current POC path (gated `computer-use-mcp`)

The standalone test on 2026-06-05 proved the architecture is
sound. Stand it up the same way for the POC integration:

```
[WSL OpenClaw gateway] ── MCP/HTTP (loopback via Tailscale) ──►
   │
   │ Bearer auth (TODO)
   ▼
[host process in session 1]
   computer-use-mcp@1.8.0
   + sessionIdGenerator patch (fixes the SDK bug)
   bound 127.0.0.1:3100
   + Telegram owner-confirm wrapper
   + Notepad-only allowlist (POC)
```

### What the POC proves (already done 2026-06-05)

- Unsandboxed host process in session 1 reaches the live
  desktop — sidesteps the MXC input-injection block.
- ~2.3s screenshot latency, ~14 chars/sec typing speed,
  1386x869 native — acceptable for short scripts.
- 11 actions exposed by the package: `key`, `type`,
  `mouse_move`, `left_click`, `left_click_drag`,
  `right_click`, `middle_click`, `double_click`, `scroll`,
  `get_screenshot`, `get_cursor_position`.

### `sessionIdGenerator` HTTP-transport bug + fix

The package's `dist/main.js` uses a single
`StreamableHTTPServerTransport` with
`sessionIdGenerator: undefined`. The MCP SDK throws
`"Stateless transport cannot be reused across requests"` on
the 2nd request. **Fix:** use a custom main with
`sessionIdGenerator: () => randomUUID()` and have the client
persist the `mcp-session-id` header across requests. Tested
fix in the standalone test (`fix2-main.js`).

CLI usage via `npx computer-use-mcp` (stdio) is unaffected —
only HTTP consumers need the patch.

### Gating design (POC)

The package exposes a single `computer` tool. The POC gating
adds three layers in front of it:

1. **Telegram owner-confirm.** Every write-side action
   (`type`, `key`, `click`, `scroll`, `drag`) posts a
   confirmation to the owner (chat id <OWNER_TELEGRAM_CHAT_ID>) with a
   request id, and waits for a `yes <id>` / `no <id>` reply.
   Read-side actions (`get_screenshot`, `get_cursor_position`)
   skip confirm.
2. **Notepad-only allowlist.** Window-title / pid geofence:
   the action is allowed only if the focused window's process
   is `notepad.exe`. A misrouted call cannot drive Chrome.
3. **Kill switch.** Telegram `/stop` revokes the node pairing;
   the engine raises `access_revoked` on the next call.
4. **Append-only audit log.** Every action recorded with
   payload + screenshot + timestamp. Log is on D: (which is on
   a separate physical drive from the OS) and copied off-box.

### Capabilities the POC deliberately omits

- No browser-driving (LinkedIn, Reddit, etc). POC is Notepad
  only.
- No multi-action sequences. Each tool call is one action with
  a confirm.
- No public network. Bound to loopback.
- No shell. Closed action enum.

---

## Hard behavior lesson (bake into Ollie's instructions)

> Desktop actions must NOT act on ambient state.

**First real bug:** told to "open Notepad and type X," the
agent typed into an OLD file that was already open — appending
to whatever was focused, not creating a fresh document.

**Correct behavior, always:**

1. Screenshot first.
2. Assess the visible state. Is the requested app open? On the
   right screen? Showing the right doc?
3. If anything is off, **close it / open a fresh one**. Do not
   append, do not assume the foreground is correct, do not
   "just send the keystrokes and see what happens."
4. Then act.

This is a **prompt-level rule today** (see `DECISIONS.md`
D-20260605-06) and a **planned hard rule** in the engine's
plugin layer (window-title + content-hash preconditions on every
write-side action).

---

## Roadmap (post-POC)

### Phase 1 — Hardening (next 1-2 weeks)

- [ ] Move gating from prompt to plugin: per-action + per-site
      allowlist enforced *in the engine*, not in Ollie's
      instructions.
- [ ] Add bearer auth (engine has no built-in auth — currently
      loopback-only).
- [ ] Add UIA preconditions: every `type`/`click` must verify
      the foreground window title + content-hash first.
- [ ] Add a second host (a second Windows box) install test
      for the engine. Validate the "distributable" goal.

### Phase 2 — Standalone engine (weeks 2-6)

- [ ] Port the core loop off `domdomegg/computer-use-mcp` to a
      own Python + MCP server. Keep the package's action
      semantics; replace its transport with a clean one.
- [ ] Add UIA-tree actions (free, fast, model-free) alongside
      pixel-grounded ones.
- [ ] Plug OmniParser as the element detector.
- [ ] Add a "Telegram confirm + audit log" middleware that's
      reusable across consumers (not just Ollie).
- [ ] Code-sign the engine. Single signed installer.

### Phase 3 — Better grounding (weeks 4-8, parallel)

- [ ] Benchmark MiniMax M3 pixel-aim on a representative UI
      corpus (Notepad, Calculator, Settings, Chrome with 5
      common sites, a job-apply form).
- [ ] If M3 aim is flaky (<90% single-shot on common UI):
      - [ ] Evaluate UI-TARS-7B / 72B as the pixel model.
      - [ ] Evaluate Microsoft UFO's two-stage grounding
            (UIA-tree + pixel) as the orchestration pattern.
- [ ] Don't add a second provider until the data says we need
      to.

### Phase 4 — Operational (parallel)

- [ ] Tailscale auto-reconnect on the box (or a watchdog that
      pages the owner).
- [ ] Onboarding for the engine on a fresh Windows box:
      installer + a one-line MCP config edit + a
      smoke-test (Notepad-allowlist type).

---

## What is NOT in Tier 2

- LLM brain (Tier 1).
- Network egress except loopback to the LLM and the WSL
  gateway.
- File system access (no shell, no `open`, no read).
- Cross-app automation beyond the allowlist (POC: Notepad
  only).
- Anything that bypasses the Telegram confirm step for
  write-side actions.

---

## Success metric

"When the user says 'apply to this job' or 'reply to that DM,'
Ollie posts a clear preview to Telegram, asks for yes/no, and
on yes does the action with one screenshot per step — without
ever typing into a wrong window, without ever needing the user
to babysit, and without ever exceeding the allowlist."

If the engine is doing that, the architecture is right and the
remaining work is content (which sites to support, which
forms, which channels), not infrastructure.
