# Core-unify pass — spec

**Status:** spec / not started (2026-06-26).
**What it is:** one bundled, supervised execution window that lands every remaining
core-channel change at once, behind a single reboot — instead of nibbling.

## Why bundle (not piecemeal)

All five remaining items are **core-channel surgery and/or need a supervised reboot**:
- WhatsApp→core unification (c), Telegram voice-out, and the STT swap all touch how
  channels feed the OpenClaw core / each other → easier to do once, sharing one path.
- The dbus-session activation and the new code both need the gateway/user-manager to
  restart. We need **one** reboot anyway → do it deliberately, manage the WSL
  keepalive footgun once, and verify everything on the way back up.

Doing them separately means N reboots, N keepalive-footgun exposures, and two
half-built voice paths. Bundle = one risk window, one voice path, one verification sweep.

## Components

> **WhatsApp → core unification (option c) is SPLIT OUT of this window** (decision #3,
> owner 2026-06-26). Recon confirmed it's a **4-6 day, 5-PR** effort (full delivery
> adapter ~600-900 lines; no shortcut) — too big to bundle, and it carries a
> silent-wa-approval-bypass landmine that demands incremental care. It runs as its own
> track: see **`Plans/whatsapp-core-unify.md`**. This window is now the tractable,
> reboot-needing work only. (Consequence: WhatsApp stays on its own session/command
> model until that track lands — full "one system" for WhatsApp is deferred to it.)

1. **Telegram voice-out — BUILT (plugin `openclaw-ollie-telegram-voice`, committed
   2026-06-26, inert).** The earlier agent that "looped 9h" actually *built* this
   plugin and never reported it — recovered intact. It mirrors the WhatsApp rule:
   `message_received` flags "last inbound was voice" per chat; `message_sending`
   (telegram, reply ≤ voiceMaxChars, flag fresh) returns `{cancel:true}` to skip the
   text send, synthesizes via the box `tts_say.py` (now Kokoro **am_onyx**) and POSTs
   a Bot API `sendVoice`. Falls back to text on any failure. Chosen over the
   config-only tts-tool route because it's **guaranteed voice-note format** +
   **unconditional** (no agent discretion / no file-vs-bubble risk).
   - **Phase-B (on-box) verification gate:** deploy plugin → flip
     `plugins.entries["ollie-telegram-voice"].config.enabled=true` (+ load path) →
     run its `node index.js` self-checks on the box (gateway env has the `openclaw`
     SDK) → owner sends a Telegram voice note → confirm an am_onyx voice-note reply
     (round purple bubble, not a file). Verified locally only at the syntax+read level.
   - (Skipped the alternate tts-tool + AGENTS.md route — it would double-fire with
     this plugin.)

3. **STT swap → faster-whisper-small (local), both channels.**
   Replace the Groq-Whisper `tools.media.audio` path with a local
   faster-whisper-small (int8) runner. Decided by lab A/B (0% WER, 464MB, RTFx ~6).
   Removes the cloud dependency (no Groq 429 breaking inbound voice).
   - Install: faster-whisper into a dedicated venv on the box (mirror the
     `ollie-research/.venv` pattern). **Allowlist the venv + model cache in the
     watchdog FIRST** (or it alarms lab-bypass, like the research venv did).
   - Wire: a small `stt` runner both channels call; config points
     `tools.media.audio` (or the channels' transcribe path) at it.
   - Caveat: the eval was ONE clean clip. Validate on a real (noisy/accented/
     code-mixed) voice note before declaring done.

4. **TTS wired to Telegram** — falls out of #2 (Telegram voice-out uses the same
   Kokoro am_onyx path WhatsApp uses). WhatsApp is already live on am_onyx.

5. **dbus-session activation — ABANDONED (owner decision, Option A, 2026-06-27).**
   The custom `ollie-dbus-session.service` unit failed to activate on the 2026-06-26
   reboot and was removed from the box + repo. We do NOT use `systemctl --user` /
   `journalctl --user` (no session bus over SSH). Restart the gateway via SIGTERM-the-PID
   (unit is `Restart=always`); read log files directly. `deploy-wa-plugin.sh` now uses
   the SIGTERM path and fails loudly if no new gateway PID comes up on :18789.

6. **ollie-hands recovery + supervision fix (HOST-side). Owner decision: FIX (bring
   live).** CONFIRMED root cause 2026-06-26: the Scheduled Task DOES exist (`OllieHands`,
   not `ollie-hands` — earlier pattern missed it). It fired once at the 2026-06-20 boot
   (04:43:33), result **267014 (SCHED_E_TASK_ATTEMPTED)** — the supervisor **died on
   first launch**. Its trigger is a **LogonTrigger with NO `<Repetition>`** → fires once
   per logon; supervisor died → nothing retried for 6 days (`NextRunTime` empty).
   AutoLogon (`AutoAdminLogon=1`, user Source) + session 1 are fine, so the trigger
   fired correctly. The D4 work did NOT break the task, but `setup-engine-restart.ps1`
   set `RestartCount=0` (to fix an earlier duplicate-supervisor bug), removing Task
   Scheduler's own restart-on-failure net. **The actual supervisor-death cause is
   currently INVISIBLE** — supervisor stderr is redirected to `server.log` (run-host.bat
   line 8), which got overwritten; likely `ollie_hands.server` import crash taking
   python down.
   - **Fix (3 parts):**
     (a) Add `<Repetition>` (Interval PT5M, StopAtDurationEnd=false) to the LogonTrigger
         in `setup-host-task.ps1` so a one-shot boot death can't = 6 days dead.
     (b) Make `supervisor.py` / `run-host.bat` tee stderr to BOTH supervisor.log AND
         server.log so the next crash is debuggable.
     (c) Add a `hands :3200` reachability check to `ollie_watchdog.py` (only when
         `mcp.servers.hands` enabled) so a future silent outage pages, not rots.
   - **Bring it back live:** deploy (a)+(b), trigger `OllieHands` (NOT plain Stop/Start —
     footgun; use the proper relaunch), read the now-tee'd crash log, fix the real
     import/launch crash, confirm :3200 reachable. The supervised reboot also re-triggers
     it cleanly. Host-side (separate domain from #1-#4) but shares the reboot.

## Execution order

### Phase A — build everything OFF the live box (no disruption)
- Worktree 1: WhatsApp→core routing refactor (#1).
- Worktree 2: unified voice-out hook (#2) + `tts` tool re-add.
- Worktree 3: faster-whisper-small STT runner (#3) + watchdog allowlist entry.
- Stage config edits (tools.media.audio → local STT; tts tool profile) as ready-to-apply
  diffs — NOT applied yet.
- Each worktree: implement + local test. **I verify each diff before it goes near the box.**

### Phase B — the supervised window (one reboot)
1. Pre-flight: confirm a current age-encrypted state backup exists (rollback safety).
2. Install faster-whisper venv on the box + watchdog allowlist (non-disruptive; no restart).
3. Deploy all staged code (WhatsApp plugin/core routing, voice-out, STT runner) + apply
   staged config edits. Do NOT restart yet.
4. **Supervised WSL reboot** (manage the keepalive footgun per RUNBOOK: after
   `wsl --shutdown`/reboot, re-run `OllieGatewayKeepalive`). This: activates
   dbus-session, brings up all new code/config fresh.
5. **Verify on the way back up (BY ME, per component — verified-or-it-didn't-happen):**
   - `systemctl --user is-active` works (dbus fixed) — else fall back to `dbus.socket`.
   - Gateway healthy (HTTP 200), brain = M3, compaction config present.
   - WhatsApp `/compact` works (proves it's on core now) + a normal WA turn works.
   - Telegram voice note → am_onyx voice reply. WhatsApp voice note → am_onyx reply.
   - Inbound voice transcribes via local faster-whisper (no Groq call) — test a real clip.
   - Watchdog quiet (no lab-bypass alarm from the STT venv).

### Rollback (per component, before declaring done)
- WhatsApp→core: revert the plugin commit, redeploy old bypass path, restart.
- STT: revert `tools.media.audio` to Groq-Whisper config.
- Voice-out: disable the hook (text-only reply) — non-fatal.
- Config: every box edit takes a timestamped `.bak`; restore + restart.
- dbus: remove unit + symlink (revert documented in scripts/deploy-wa-plugin.sh comments).

## Risks / ordering rationale

- **#1 before #2:** voice-out is built once WhatsApp is also core, so one path serves both.
- **STT venv allowlist BEFORE install:** avoid self-tripping the lab-bypass watchdog.
- **Reboot last:** all code/config staged + deployed first, so the reboot both activates
  dbus AND loads the new code in one bring-up — and the keepalive footgun is handled once.
- **Biggest unknown = the WhatsApp→core refactor depth.** If it proves larger than a
  bounded change, split: ship voice-out + STT + dbus in this window, and do the
  WhatsApp→core unification as its own focused follow-up (it's the only piece that
  doesn't strictly need the reboot).

## Open decisions for owner
1. **Throwaway test number** for the WhatsApp→core refactor, or test on the live number?
2. **Reboot timing** — the supervised window needs the box rebooted (brief full outage +
   the keepalive dance). When's OK?
3. If WhatsApp→core turns out big, **OK to split it out** of this window (voice/STT/dbus
   ship first)?
4. **Hands: live or parked?** — DECIDED: FIX/live (owner, 2026-06-26). Root cause
   confirmed (one-shot LogonTrigger + supervisor died on boot + RestartCount=0 net
   removed + crash cause hidden). Fix = Repetition + stderr tee + watchdog :3200 check,
   then find & fix the real launch crash.
