# Decisions

A log of the choices that shaped Ollie, **and the WHY**, so future
sessions don't have to re-litigate them.

Format: `D-YYYYMMDD-NN — Title — Status`. Status: `accepted`,
`superseded`, or `rejected`.

---

## D-20260604-01 — OpenClaw as the agent runtime — accepted

**Choice:** OpenClaw.

**Why:** the user already operates OpenClaw in production via
`bossclaw2/` (their Go control plane that one-click provisions
OpenClaw deployments on DigitalOcean droplets for customers).
Switching to Hermes / OpenHands / Goose / Cline would discard
that expertise for marginal gain. Other evaluated options
(Hermes Agent, OpenHands, Goose, Cline, n8n) were a step down on
the axis the user cares about: a runtime they already know
end-to-end.

**Tradeoff accepted:** OpenClaw has a bad 2026 CVE record (7+
CVEs incl. RCEs and the "Claw Chain" sandbox-escape). Containment
becomes non-negotiable — see D-20260604-04.

---

## D-20260604-02 — Enroll the Windows host in Insider for MXC — accepted

**Choice:** enroll the user's actual Dell Pro 16 in Windows
Insider **Dev / "Experimental"** channel, targeting build
`26300.8553`, to get MXC `isolation_session` natively.

**Why:** the Build-2026 (2026-06-02) demo showed OpenClaw running
natively on Windows inside MXC, with a one-click Companion app
providing per-folder read-only/write/hidden sandboxing. The user
wanted that.

**Tradeoff accepted:** enrollment is hard to reverse (exit = clean
reinstall of C:). BitLocker is OFF on both drives, so no
recovery-key lockout risk. D: is on a separate physical drive
so an OS reinstall of C: does not wipe data. Telemetry set to
Full (AllowTelemetry=3) over SSH on 2026-06-04 — Insider requires
at least 1.

**Channel reasoning:** Dev (now renamed to "Experimental" via a
phased rollout) is the only channel that ships MXC. Canary
("Experimental Future Platforms", 29599.x) is too unstable; Beta
(26220.x) does not have MXC.

**Corrected fact:** the shipping Companion does **NOT** use MXC
(see D-20260605-01). Native MXC integration would be a
from-source build of `microsoft/mxc`, not a turnkey.

---

## D-20260604-03 — MXC IS integrated (corrected) — accepted

**Initial (wrong) claim:** "MXC blocks tool calls" / "MXC blocks
input injection, so Ollie cannot work."

**Corrected understanding:**
- MXC does block input injection (the shipping Companion's
  `mxc-direct-appc` sandbox has hard-coded
  `policyJson.ui.allowInputInjection: false`).
- MXC does NOT block tool calls — that's a different thing.
- "Tool calls" = MCP/`mcp.servers.*` invocations. They ride the
  gateway's network egress, not the host's MXC sandbox. They work
  fine (4DPocket MCP proves this end-to-end).
- "Input injection" = `SendInput` / UI-Automation on the desktop.
  MXC blocks this. This is the only thing we needed to route
  around.

**Why the correction matters:** an earlier research conclusion
called MXC a non-starter for Ollie. The reality is narrower:
MXC is fine for everything except desktop input injection, and
desktop input injection is a separate, gated component (Tier 2)
that explicitly runs unsandboxed by design.

---

## D-20260605-01 — Companion sandbox is WSL, not MXC — accepted

**Choice:** ship on the WSL-gateway sandbox (the shipping
Companion's actual model) and defer native MXC integration.

**Why:** the shipping OpenClaw Windows Companion (`Molty`) does
NOT use MXC. Its sandbox is the WSL distro `OpenClawGateway`,
with interop disabled and Windows drives hidden — so the agent
**cannot see C:/D:** by default. The Build-2026 MXC per-folder
read-only/write/hidden toggles are NOT in the shipping
Companion; there's no documented way to make the Companion
consume MXC `isolation_session`.

To use MXC natively you'd build `microsoft/mxc` from source
(Rust 1.93, `build.bat`, `IsolationProxy.exe`,
`wxc-exec.exe --experimental`) and wire it by hand — an
experimental POC, not a setup.

**Layered controls in lieu of native MXC:**
- Scoped 4DPocket editor-PAT (not admin).
- Implicit network egress limits (only 4DPocket host + LLM API
  reachable).
- Telegram locked to owner chat_id.
- Approval gates on irreversible actions.
- Tier 2 (desktop input) is a separate, gated component.

**Tradeoff accepted:** the WSL sandbox does not give per-folder
visibility controls like the MXC demo promised. We get
"agent can't see host" instead of "agent sees host, read-only
on folder X." Net: still safe enough for the POC; per-folder
controls are a later ask if we need them.

---

## D-20260605-02 — Drop Clicky as the Tier-2 actuator — rejected

**Choice considered:** integrate `farzaa/clicky` as Ollie's
"act as me on the real browser" primitive.

**Why rejected:**
1. **Clicky is macOS-only.** Swift + SwiftUI + AppKit, requires
   ScreenCaptureKit / NSPanel / CGEvent tap / NSScreen. Zero
   non-macOS code. No Windows build, no plans for one
   (heyclicky.com is waitlist-only for Windows).
2. **Clicky does NOT do input injection.** Its `[POINT:x,y]`
   output is a *drawing* of a fake blue cursor on a transparent
   overlay. The real cursor never moves. Grep of the source
   confirms no `CGEventPost` / `HIDPost` / AppleScript emission.
3. **No external interface.** `LSUIElement=true` menu-bar app,
   no CLI / library / HTTP API / IPC / URL scheme. Can't be
   driven programmatically.
4. **heyclicky** (the private "new" version) doesn't fix any of
   this. It's macOS, cloud, and still doesn't advertise input
   injection.

**The user's mental model was wrong.** "OS-level controller"
was a misreading of what Clicky is. Clicky is a study-buddy
overlay, not an actuator. Even on a Mac, it wouldn't do the
job.

**Full study:** `~/.claude/MEMORY/WORK/20260605-082636_clicky-openclaw-feasibility/feasibility-study.md`

---

## D-20260605-03 — Tier 2 = standalone computer-use engine, consumed via MCP — accepted

**Choice:** build computer-use as a **standalone Windows
"computer-use engine"** (its own component, exposed via an MCP
server) that captures screen → vision/grounding model → real
input injection. **Ollie/OpenClaw is just the first CONSUMER**
(via MCP), not the owner.

**Why:**
- **Independence.** The engine is independently maintainable
  and distributable to other Windows machines — not coupled to
  the OpenClaw release cadence.
- **Explicitly not porting Clicky.** ≈90% of Clicky is a macOS UI
  shell with no input injection. Nothing useful to port.
- **Stand on a foundation.** Do NOT build grounding from
  scratch. Stand on an existing OSS Windows computer-use
  foundation: UI-TARS / Microsoft UFO / OmniParser (TBD by
  research).
- **Right trust boundary.** MXC blocks input injection by
  design, so the engine/desktop node runs UNsandboxed but
  narrow: Telegram owner-confirm + per-site allowlist +
  kill-switch. (See D-20260605-04 for the POC actuator.)

**POC:** `domdomegg/computer-use-mcp` v1.8.0 (MIT, audited
SAFE — 5 reputable deps, no install scripts, no network egress;
maintainer Adam Jones/domdomegg is an Anthropic employee; no
provenance attestation so pin @1.8.0 + verify SHA-512).

**Distribution plan:** Python + MCP server + code-signing so the
engine is a single installable for other Windows machines.

---

## D-20260605-04 — POC actuator = `domdomegg/computer-use-mcp` — accepted

**Choice:** use `domdomegg/computer-use-mcp` v1.8.0 as the
POC Tier-2 actuator, not Clicky and not a from-scratch build.

**Why:**
- MIT license, audited SAFE (5 reputable deps, no install
  scripts, no network egress).
- Maintainer (Adam Jones / domdomegg) is an Anthropic employee —
  meaningful but not dispositive.
- Standalone test PASSED on 2026-06-05: ran as an unsandboxed
  host process in session 1, drove the live Notepad via
  screenshot + SendInput.
- 11 actions (screenshot/clicks/type/key/scroll/cursor), ~2.3s
  per screenshot, ~14 chars/sec typing. PIXEL grounding (calling
  model supplies coords → MiniMax M3 is the limiter).

**Caveats:**
- No provenance attestation. Pin `@1.8.0` and verify SHA-512.
  Expected tarball hash:
  `C0Qrcztq7Dh6SyTUGjqG667EGewa2uXIUKBXI5HGpa84+PBjgjqW51TATFMzrXIFGRcvt3qUHD+2CbF/js4ykg==`.
- HTTP transport has a latent bug: package's `dist/main.js` uses
  a single StreamableHTTPServerTransport with
  `sessionIdGenerator: undefined`, throws on 2nd request.
  Workaround: use a `sessionIdGenerator: () => randomUUID()`
  patch and track the `mcp-session-id` header client-side.
- The package has no built-in auth — add bearer auth before
  exposing beyond loopback.

**Goal:** as competent as heyclicky or better, structurally
stable, maintainable, distributable. The package is a means to
that end, not the end itself.

---

## D-20260605-05 — Grounding model = MiniMax M3 (start) — accepted

**Choice:** start with MiniMax M3 as the computer-use engine's
grounding model. Possibly add UI-TARS / OmniParser later if M3's
aim is flaky.

**Why:**
- M3 is the brain we're already calling — re-using it removes a
  second provider, second billing relationship, second key.
- M3 is multimodal and supports tool use; same as the brain.
- **Opus is NOT a candidate for the runtime.** User has Opus
  4.8 only via Claude Code subscription — **no Anthropic API
  key**. The engine needs a callable endpoint. Opus/Claude Code
  can be used for *building*, M3 for *runtime*.
- The standalone test confirmed M3 pixel-grounding works
  (with the qualifier that screenshot → coords is the M3
  call; the engine just passes the image through).

**Switching criterion:** if pixel-aim is flaky (e.g. < 90%
single-shot click accuracy on common UI), evaluate UI-TARS
(7B/72B), OmniParser (YOLO-based), or Microsoft UFO's
two-stage grounding (UIA-tree + pixel) as a swap-in.

---

## D-20260605-06 — Tier-2 gating = prompt-level (for now) — superseded-by-roadmap

**Choice (current):** the Tier-2 gating is enforced in
**prompt-level instructions to Ollie** (e.g. "screenshot first,
open a fresh file, never act on ambient state"). Action allowlist
is the package's closed enum. Per-site allowlist is the package
default (POC: Notepad only).

**Why this is not enough:**
- Prompt-level gating is bypassable by a hijacked prompt.
- A bug in the brain should not be able to call `click` on
  LinkedIn just because the package happens to allow it.
- The right place for the allowlist is the engine's plugin
  layer, not the LLM's instructions.

**Roadmap (see `TIER2-PLAN.md`):** harden gating from soft to
hard — per-action + per-site allowlist enforced in the Tier-2
plugin, not in the prompt. Telegram confirm remains the
owner-side gate.

---

## D-20260610-01 — OpenCLI adopted for the browser rung (L2), v1.8.3 pinned

**Choice:** adopt `jackwener/OpenCLI` (npm `@jackwener/opencli`)
as computer-use v2's L2 browser layer, on a dedicated "Ollie"
Chrome profile. Verdict: **adopt-with-pins** (audit 2026-06-10).

**Pin:** `1.8.3`
- shasum  `7771d922c29eb37ff30dc687f8f1776b0c74f8cd`
- sha512  `oz2Q2RSSw442dN0O0pgHA+clZoXt/crWF05wOJEsJWlEfEb5jjxCi+215WhOJZMPa1Mnz50CE/VxVndfLgmPJg==`
- Extension v1.0.19, loaded **unpacked** from a release zip (no
  Web Store listing) — keep the unpacked dir read-only and pin
  it to the same release as the npm package.

**Audit findings:**
- Provenance: repo created 2026-03-14 (young, ~3 months), but
  24k stars / 2.4k forks, Apache-2.0, multiple contributors,
  dependabot enabled, git tags match npm versions.
- Deps: 8 production deps, all mainstream (commander, ws,
  undici, readability, turndown(+gfm), js-yaml, cli-table3).
- Install scripts: `postinstall` = shell completions + local
  adapter-hash sync — **explicitly no network calls**.
  `preuninstall` pings localhost daemon shutdown only.
- Egress: no telemetry / auto-update / phone-home found. Daemon
  binds **127.0.0.1:19825 only**.
- CSRF/hijack defenses: Origin check (only
  `chrome-extension://` or no-Origin clients), mandatory
  `X-OpenCLI` header (browsers can't forge it cross-origin), no
  CORS allow header, WS upgrade rejects browser origins.
- Residual risk #1: **no auth between local processes and the
  daemon** — any process on the box can drive the browser.
  Accepted: the box is dedicated to Ollie; the engine is the
  only privileged local actor, and all OpenCLI write verbs are
  T3-confirm in the engine policy.
- Residual risk #2: extension permissions are maximal
  (`debugger`, `tabs`, `cookies`, `<all_urls>`, `downloads`) —
  inherent to its function. Mitigated by the dedicated Ollie
  profile: blast radius = only accounts Tushar logs in there.
- Integration contract confirmed: sysexits `0/66/69/75/77/78`
  (+1/2/130) documented and present; `-f json` output mode.

**Re-audit trigger:** any version bump (diff the postinstall
scripts + daemon security block before re-pinning).

---

## D-20260610-02 — Browser rung = Camoufox (stealth), OpenCLI dropped

**Choice:** the L2 browser rung uses **Camoufox** (Firefox fork, OSS MPL-2.0,
engine-level C++ anti-fingerprinting), driven by Playwright from the Python
engine, on ONE persistent profile holding Tushar's logins. **Supersedes
D-20260610-01 (OpenCLI).**

**Why OpenCLI was dropped:**
- Tushar (emphatic): never use vanilla Chrome — it's trivially bot-detectable
  (CDP/automation leaks) and Chrome 149 actively blocks loading unpacked /
  self-hosted extensions (the OpenCLI Browser-Bridge path). Verified on the
  box: `--load-extension` ignored; `ExtensionInstallForcelist` + a local CRX/
  update-server also rejected (self-hosted http update URL).
- OpenCLI is fundamentally Chrome-extension based → incompatible with a
  stealth-browser requirement.

**Why Camoufox over alternatives** (researched 2026-06-10, GitHub + DuckDuckGo
+ Castle.io): driver-level stealth (nodriver/patchright) still leaks; engine-
level masking is the robust tier. Camoufox = best OSS engine-level option
(no JS injection, no main-world leaks, Playwright-Python, persistent profiles,
free, self-hosted). Kameleo was the strong commercial alternative (Chromium+
Firefox, local API, free tier→€59/mo) — kept as fallback. Tushar chose
Camoufox.

**Verified on box:** Camoufox launches with a persistent profile; bot signals
clean (`navigator.webdriver=false`, real plugins/UA, no leaked `chrome`
object); reads (goto/links/extract) work through the engine `act`/`plan_submit`
tools (gated `notify`); a `commit` click correctly escalates to T3 confirm and
proceeds only after `/consent` approval.

**Consent model (browser):** reads → notify; interactions → notify; commits
(send/post/buy/connect/apply/submit — by explicit `commit:true` flag OR
auto-detected from the button text) → T3 confirm. Defense-in-depth: the engine
resolves a click target's text before deciding, so a hijacked planner can't
dodge confirm by omitting the flag.

**Human-only setup remaining:** Tushar logs the Camoufox persistent profile
(`C:\OllieChrome\camoufox-profile`) into the sites he wants Ollie to use
(credentials/2FA can't be automated). After that, logged-in flows work.
