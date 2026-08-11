# Status

Living status + dated TODO. Last refresh: 2026-06-05.

---

## DONE (verified)

- [x] **2026-06-04** — Windows host enrolled in Insider Dev /
      "Experimental" channel, on build `26300.8553`. MXC
      `isolation_session` floor met.
- [x] **2026-06-04** — Telemetry set to Full (AllowTelemetry=3)
      over SSH; required for Insider.
- [x] **2026-06-04** — OpenClaw Windows Companion installed,
      running in session 1, paired to the WSL gateway as
      `Windows Node (MBD25-30)`.
- [x] **2026-06-04** — `system.run whoami` round-trip verified
      end-to-end. M0 host-execution path proven.
- [x] **2026-06-04** — M1 (SendInput via Companion) NO-GO:
      shipping Companion's `mxc-direct-appc` sandbox hard-codes
      `allowInputInjection: false`. Decision: bypass the
      Companion's sandbox for input injection (separate host
      process, see Tier 2).
- [x] **2026-06-04** — Clicky feasibility study. Verdict: drop
      Clicky. macOS-only, no input injection, no API. Study at
      `~/.claude/MEMORY/WORK/20260605-082636_clicky-openclaw-feasibility/feasibility-study.md`.
- [x] **2026-06-04** — MiniMax M3 wired as the LLM provider.
      Telegram channel live with owner-locked allowlist
      (chat id <OWNER_TELEGRAM_CHAT_ID>). 4DPocket MCP integration
      verified.
- [x] **2026-06-05** — `domdomegg/computer-use-mcp` v1.8.0
      standalone test PASSED: ran as unsandboxed host
      process in session 1, drove the live Notepad via
      screenshot + SendInput. 11 actions, ~2.3s/screenshot,
      ~14 chars/sec. `sessionIdGenerator` HTTP-transport bug
      identified and patched.
- [x] **2026-06-05** — Tier-2 architecture decision: build a
      standalone computer-use engine (consumed via MCP), not
      port Clicky. POC actuator = `computer-use-mcp` until the
      standalone engine exists.
- [x] **2026-06-05** — Tier-2 POC integration LIVE. Patched
      `computer-use-mcp` runs on host session 1 (`:3100`,
      firewall-restricted to the WSL subnet); WSL `desktop-proxy`
      adds bearer auth (loopback `:3101`). Wired into Ollie as
      the `desktop` MCP server (`desktop__computer`). Gate =
      SOFT/prompt-based (`~/.openclaw/workspace/DESKTOP_GATE.md`):
      Notepad-only, Telegram owner-confirm before each action,
      screenshot-first → FRESH file, kill-switch documented.
      Verified end-to-end (real screenshot through the full
      chain). Backup + revert saved.
- [x] **2026-06-05** — Groq Whisper STT live. `tools.media.audio`
      (groq `whisper-large-v3-turbo`) + `groq:global` auth
      profile. Verified end-to-end (`infer audio transcribe`
      returned text; `infer audio providers` → available+
      configured). Telegram voice notes now transcribe.
- [x] **2026-06-10** — Computer-use v2 plan APPROVED
      (`Plans/graceful-questing-oasis.md`). Kickoff decisions
      resolved: box is a dedicated spare → Ollie owns it
      (Option A permanent, NO Hyper-V VM, engine elevated,
      auto-logon instead of vault unlock; T3-confirm kept for
      all acts-as-Tushar externals). Browser rung = OpenCLI on
      a dedicated "Ollie" Chrome profile.
- [x] **2026-06-10** — **Phase 0 of v2 LIVE: `ollie-hands`
      engine** (repo `ollie-hands/`, box `C:\ollie-hands`).
      Python MCP server, streamable-HTTP `:3200/mcp`, bearer
      auth (401 verified from tailnet + WSL), inert-boot +
      `DISABLED` flag kill switch (both verified), JSONL audit
      (`C:\ProgramData\ollie-hands\audit\`). `observe` tool =
      screenshot + window list + monitors/DPI + bounded UIA
      snapshot in one call. Runs in session 1 via `OllieHands`
      scheduled task (ONLOGON, interactive token). Gateway MCP
      entry `hands` wired; **Phase-0 demo passed**: agent
      described the live screen accurately end-to-end. Box
      sleep/hibernate/monitor-off disabled, lock screen
      disabled (NoLockScreen=1, InactivityTimeoutSecs=0).
      Learned: SSH lands in session 0 — capture needs session 1
      (`tscon 1 /dest:console` reattached it); PowerShell 5.1
      chokes on non-ASCII in .ps1 (keep scripts ASCII).
      Auto-logon: already configured by owner (confirmed
      2026-06-10) — full unattended boot→session-1→engine chain.
- [x] **2026-06-10** — **Phase 1 of v2 LIVE: L0 shell + L1 UIA
      + policy + consent.** New engine modules: `policy.py`
      (hard tier gate, LLM-uneditable, 37 regression tests in
      `tests/test_policy.py` green), `shell.py` (L0 PowerShell,
      deny-set, timeout), `uia_actions.py` (L1 find/invoke/
      set_value/get_text + window mgmt + clipboard, marshalled
      onto a dedicated COM-initialized thread), `consent.py`
      (Telegram notify one-way + confirm blocking w/ DENY-on-
      timeout), `engine.py` (policy→consent→dispatch→audit choke
      point). New MCP tool **`act`** (kind=shell|uia|window|
      clipboard). New bearer-authed **`/consent`** Starlette
      route for owner approval. Verified on box: read→auto,
      Defender-tamper→blocked, scratch-write→notify(+Telegram),
      `Restart-Computer -WhatIf`→confirm→/consent approve→ran;
      Calculator 1+2= via UIA → "Display is 3"; clipboard
      roundtrip. **Phase-1 agent demo passed**: real Ollie used
      `act` to report C: free (113.67 GB) + open Settings.
      Bugs fixed: `\bformat\b` matched Format-List/Table (read
      cmdlets) → tightened to `format-volume`/`format X:`;
      sync tool blocked the event loop during confirm → made
      `act` async + `anyio.to_thread` so `/consent` stays live;
      UIA in worker thread needed per-thread CoInitialize.
- [x] **2026-06-10** — **Phase 2 of v2 LIVE: act-scripts &
      verification at speed.** New modules: `actscript.py`
      (plan schema v1 + validation + stable hash + script-level
      consent = max tier; write steps REQUIRE a postcondition),
      `conditions.py` (pre/postcondition checkers: foreground,
      window/uia exists-absent, uia_text, file exists-absent,
      shell_exit_zero — the structural ambient-state fix),
      `executor.py` (consent ONCE → per-step pre→dispatch→verify
      with on_fail retry/repair/escalate/abort, verify-after-act
      re-ground+retry, per-step/script timeouts, human-collision
      auto-pause via GetLastInputInfo, task registry). New MCP
      tools `plan_submit`/`task_status`/`task_abort`; `type_text`
      3 strategies (ValuePattern→clipboard→SendKeys). Tests:
      `tests/test_actscript.py` (+ policy) green = 15 cases.
      **Phase-2 demo passed** (direct MCP client): 6-step
      Calculator 7×8... flow on ONE consent → "Display is 42",
      every step pre/post-verified; **stale-state trap
      escalated** (precondition caught wrong foreground) instead
      of acting. AGENTS.md desktop section replaced with the
      hands doctrine (capability ladder, observe-before-plan,
      postconditions mandatory, engine-owned consent). NOTE: the
      agent-*authoring* demo (Ollie writes its own act-script)
      is pending — LLM provider quota (MiniMax M3 + Zeus) was
      exhausted at test time; engine path already proven via the
      identical direct-client run.
- [x] **2026-06-10** — **Phase 3 of v2 LIVE: L2 stealth browser
      (Camoufox) + Telegram approval relay.** Vanilla Chrome /
      OpenCLI DROPPED (Tushar: never vanilla chrome; Chrome 149
      blocks unpacked/self-hosted extensions anyway) — see
      DECISIONS D-20260610-02. **Camoufox** (Firefox fork, OSS,
      engine-level C++ anti-detect) installed in the engine venv
      + browser binary fetched; driven by Playwright on a
      dedicated thread; ONE persistent profile
      (`C:\OllieChrome\camoufox-profile`). New `browser.py` (L2
      verbs) + `policy.classify_browser` (reads→notify,
      commits→T3 confirm, commit auto-detected from button text)
      + `browser` kind in act/plan_submit + `web_url`/`web_text`
      conditions. Verified on box: stealth real
      (`navigator.webdriver=false`, clean UA/plugins); reads
      (goto HN / links / extract) work through the engine;
      commit-click → confirm → /consent approve → dispatch
      end-to-end. NEW gateway plugin
      **`openclaw-ollie-hands-approval`**: relays owner Telegram
      `approve <code>`/`deny <code>` → engine `/consent`,
      pre-LLM + integrity-preserving (reuses the hands MCP
      bearer, no secret dup). Loaded + active on box
      (`bearer=set`). Bug fixed: plugin entry needs `register()`
      not `setup()` (found via `openclaw plugins doctor`).
      HUMAN-ONLY remaining: Tushar logs the Camoufox profile into
      his sites; + one live Telegram approve-reply test.
- [x] **2026-06-10** — OpenCLI supply-chain audit PASSED:
      adopt-with-pins, v1.8.3 (hashes + findings in
      `DECISIONS.md` D-20260610-01). Loopback-only daemon,
      no telemetry, benign install scripts; residual risks
      (no local-process auth, maximal extension perms)
      accepted via dedicated-box + Ollie-profile + T3-confirm.

---

## IN PROGRESS

- [ ] **User acceptance testing.** Run from Telegram:
      (1) *"open a new Notepad file and type hello from Ollie"*
      (desktop POC — needs RDP session 1 active/unlocked or
      screenshots are black); (2) a **voice note** (STT). Report
      issues to fix.

---

## NEXT (in priority order)

1. [ ] **Harden Tier-2 gating from soft (prompt) to hard
       (plugin).** Per-action + per-site allowlist enforced
       in the engine, not in Ollie's instructions.
2. [ ] **Evaluate aim.** Benchmark M3 pixel-grounding on
       representative UI (Notepad, Calculator, Settings,
       Chrome with 5 common sites, a job-apply form). If
       <90% single-shot, swap to UI-TARS / OmniParser / UFO.
3. [ ] **Build the standalone engine.** Python + MCP server +
       code-signing. Replace `domdomegg/computer-use-mcp` as
       the default actuator.
4. [ ] **Tailscale auto-reconnect on the box.** Currently
       requires physical/RDP after every reboot.
5. [x] **Bearer auth on the Tier-2 MCP server** — DONE
       (WSL `desktop-proxy` bearer + host firewall WSL-only).
6. [ ] **Second-host install test.** Validate the
       "distributable to other Windows machines" goal.
7. [ ] **Native MXC integration (POC).** Build
       `microsoft/mxc` from source, hand-wire it. Deferred
       from Tier 1's "shipping" path; revisit when
       distribution is real.

---

## DEFERRED / NOT-STARTED

- [ ] Per-folder MXC sandbox controls (the Build-2026 demo's
      per-folder read-only/write/hidden toggles). Not in the
      shipping Companion; would require a from-source MXC
      build.
- [ ] Linking Ollie's LLM to the local Portkey-style AI
      gateway instead of the raw MiniMax endpoint.
- [ ] Anything browser-driving (LinkedIn, Reddit, job boards)
      on the Tier-2 side — POC is Notepad only.

---

## Dated log

- **2026-06-10 (evening)** — "Make Ollie alive" sprint. WhatsApp: `/new`/`/reset`/`/clear`
      session reset via per-sender epochs; per-sender turn queue (4s debounce +
      serialization) fixes duplicate job acks / context-less jobs / session races;
      jobs-runner prompt hardened (one-shot rule, OPSEC, login-walled-source
      workaround). Personality: witty co-conspirator pass (SOUL/IDENTITY/AGENTS).
      **Voice LIVE**: on-device TTS (`ollie-tts/`, Kokoro-82M int8, am_michael,
      speed 1.1, pronunciation lexicon, static ffmpeg → OGG/Opus) — voice note in
      → voice note out, verified e2e. **Heartbeat LIVE**: systemd timer every 30min
      → context-rich agent turn → strict SILENCE/MESSAGE protocol (fail-closed) →
      WhatsApp delivery; OPEN_LOOPS.md promises ledger + AGENTS.md doctrine; first
      beat correctly chose silence (quiet hours). Model chain: minimaxb (sk-api
      credits key) added as first fallback (gotcha: provider blocks reject "name"
      key — validate with `openclaw doctor` BEFORE restart; 2 brief gateway outages
      learning this). Ollie git identity set (multi-repo self-PR env ready). Both
      zeus rungs 503 (proxy upstream down). 4DPocket service down on VPS (host up)
      — reel-via-4DPocket blocked; restart needed.

- **2026-06-07** — Versioning + GitHub DONE. Recipe pushed off-box to PRIVATE
      `onllm-dev/ollie` (org-owned, iota31-authored, secrets + MEMORY/ excluded via
      hard secret-scan gate). Ollie bot GitHub account `ollie-onllm` wired on the
      box (`gh` = ollie-onllm): fine-grained PAT, all onllm-dev repos, Contents/PRs/
      Issues R/W, no delete/workflow/admin → read repos + raise PRs (gated; branch
      protection blocks merges). NEXT (layer 2): encrypted off-box state-backup of
      `~/.openclaw/{workspace/memory,memory,agents,credentials}`.
- **2026-06-07** — Real Maps added: OpenRouteService MCP (`openroute`,
      stdio via user-space `uvx openroute-mcp`; `uv` 0.11.19 installed in distro).
      6 `openroute__*` tools (geocode/reverse/route/isochrone/POI). AGENTS.md
      updated: use openroute for directions/distance/geocoding, never web_search.
      Verified (geocode + Pune→Mumbai route). ORS free key (no card). Upstream
      isochrone tool has a schema bug (routing/geocode/POI unaffected).
      ALSO: co-founder Telegram id 813569043 added to allowlist (shared context;
      multi-user separation still TODO). WhatsApp PARKED (Baileys linked-device
      sync unreliable; gate plugin built+deployed but channel parked) — Telegram
      is the reliable channel. Official WhatsApp Cloud API = possible future
      (24h-window + template limits + custom build; not native in OpenClaw).
- **2026-06-07** — Multi-user design researched (serve Tushar + co-founder
      **Prakersh** from one Ollie, personalized, no info-leak). Verdict: doable,
      ~1 dev-day, no core changes. KEY CORRECTION: sessions are NOT per-sender
      today — `dmScope` is default `main`, so all DMs currently SHARE one session
      (adding a 2nd person now would blend convos). Design: native `dmScope:
      per-channel-peer` + `identityLinks` + `accessGroups:cofounders`; build
      per-user `workspace/users/<name>/{USER,MEMORY}.md` + `workspace/company/`
      (shared), an `agent:bootstrap` hook (~150 LOC) to swap profile+memory per
      sender, + a `before_tool_call` guard (~60 LOC) to stop memory/4dpocket
      cross-user leakage. Shared voice (AGENTS/SOUL) stays shared. NOT a boundary
      for adversarial/external users (that = separate agents). Needs from user:
      Prakersh's Telegram id + WhatsApp E.164 + an "about Prakersh" blurb;
      decide if 4DPocket is joint or split. FUTURE build.
- **2026-06-07** — WhatsApp HARD auto-approval gate built + deployed LIVE.
      Custom in-process plugin `@ollie/openclaw-wa-approval`
      (`~/PycharmProjects/ollie/openclaw-ollie-wa-approval/`): `before_agent_run`
      blocks unknown WhatsApp senders pre-LLM, pushes approve/deny to owner's
      Telegram (<OWNER_TELEGRAM_CHAT_ID>), `message_received` catches the reply, dynamic
      allow/block persisted in `whatsapp-contacts.json`; `dmPolicy` flipped to
      `open` (allowFrom kept as 2nd wall). Deployed via safe inert→active boot.
      PENDING: user's live E2E test to validate 2 API assumptions
      (ctx.messageProvider/ctx.channelId == "whatsapp"); if wrong = one-line fix.
- **2026-06-06** — Email LIVE (himalaya → Hostinger `Ollie@onllm.dev`,
      send+receive verified). Tailscale `--unattended` enabled
      (reboot-proof) + service Automatic. summarize skill + `gh` CLI
      installed. Box survived a power-cut reboot clean (everything
      auto-recovered). Next: Ollie's own scoped GitHub bot account.
- **2026-06-05** — Capability Phase 1: corrected the "Ollie is empty"
      assumption (it already had web/code/files/memory/subagents/15
      skills). Quick wins shipped: MiniMax selected as web_search
      provider (live-tested), keyless URL reading via exec+curl,
      and rich USER.md + AGENTS.md context (verified loaded). Next:
      Phase 2 keys (summarize/Firecrawl, Tavily/Brave, GitHub).
- **2026-06-05** — Tier-2 POC integration LIVE (gated `desktop`
      MCP, soft gate) + Groq STT live. Ollie is now text + voice
      + gated desktop control. Next: harden the soft gate.
- **2026-06-05** — Status doc created. Tier 1 live & tested
      end-to-end. Tier 2 standalone test passed; POC
      integration in progress. Groq STT block wired but
      inactive pending key.
- **2026-06-05** — Tier-2 architecture decision
      (`domdomegg/computer-use-mcp` POC, standalone engine as
      end-state).
- **2026-06-05** — `computer-use-mcp` v1.8.0 standalone test
      PASSED on <TAILSCALE_IP>. SHA-512 verified.
- **2026-06-04** — Clicky feasibility study: dropped.
- **2026-06-04** — MiniMax M3 + Telegram + 4DPocket MCP live.
- **2026-06-04** — M0/M1 POC: M0 PASSED, M1 NO-GO via
      Companion's MXC sandbox. Bypass strategy chosen.
- **2026-06-04** — Windows Insider Dev / "Experimental"
      enrolled. Build 26300.8553.
