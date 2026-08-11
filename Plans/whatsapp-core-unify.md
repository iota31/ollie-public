# WhatsApp → core unification — split track (5 PRs, ~4-6 days)

**Status:** spec / not started (2026-06-26). **Split OUT of the core-unify reboot window**
(decision #3, owner) — it's too big to bundle; it's its own incremental track.
**Goal:** make WhatsApp a first-class core channel (route inbound through OpenClaw's
channel-inbound pipeline) so it inherits the full command registry (`/new`, `/compact`,
…), core session management, and compaction — i.e. truly unified with Telegram.

## Why it's a track, not a window item
Verified (read-only, 2026-06-26): the core API IS plugin-reachable —
`api.runtime.channel.inbound.{run, dispatchReply, buildContext}` (dist
`types-BQnYToyl.d.ts:4818-4850`); the plugin already uses `api.runtime.agent.runEmbeddedAgent`.
BUT there's no "fire-and-forget webhook→turn" shortcut — every core channel (Telegram,
Discord) ships its own ~150-900 line delivery adapter. The smallest correct entry is
`dispatchChannelInboundReply` (= `dispatchAssembledChannelTurn`), which still needs a
fully-built `ctxPayload` (~40 fields via `buildChannelInboundEventContext`,
`kernel-CYjskkiY.js:237-294`) + a `delivery.deliver` callback implementing WA reply
chunking, voice-out, and Graph media upload. Net: ~600-900 new lines. Forcing it into
one PR ships a half-correct adapter that silently drops messages — explicitly avoided.

## The 5 PRs (each independently shippable + reversible behind a `useCoreRouting` flag)

- **PR A — channel-event payload plumbing (~250-400 lines, ~1 day). THE KEYSTONE SLICE.**
  Build `buildChannelCtxPayload({...})` producing a full `ctxPayload`; wire core
  `buildAgentSessionKey` + `resolveAgentRoute`. No delivery yet — proves the payload +
  route pass through `before_agent_run` (wa-approval gate fires for the right tiers) and
  core handles `/new` `/compact`. **Test:** owner text → `messageProvider:"whatsapp"` in
  wa-approval logs; `/new` `/compact` routed by core not the plugin's epoch store.
- **PR B — delivery adapter (~300-500 lines, ~1-2 days).** `delivery.deliver` → chunk via
  `runtime.channel.text.chunkText`, `sendWhatsAppText` for text, `sendWhatsAppAudio` for
  voice (preserve `trySendVoiceReply`), typing keeper. **Test:** voice→voice, image→text,
  guest→guest agent.
- **PR C — 75s soft-timeout → background job (~80-150 lines, ~0.5 day).** `Promise.race`
  around the dispatch; keep `submitBackgroundJob` + `job-submit.sh`. **Test:** 60s+ turn →
  "On it…" ack → result via jobs runner.
- **PR D — per-sender debounce (~100 lines, ~0.5 day).** Replace the hand-rolled
  `senderQueues` with core `runtime.channel.debounce.createInboundDebouncer`. **Test:** 3
  rapid msgs → 1 turn.
- **PR E — slash-command delegation (~30-60 lines, ~0.5 day).** Remove hand-rolled
  `/new|/reset|/clear` + epoch store (`loadEpoch`/`bumpEpoch`/`EPOCH_FILE`); core registry
  handles them. **Test:** `/new` → fresh session.
- **PR F — cutover.** Flip `useCoreRouting` true, keep bypass alive 7 days behind the flag,
  then delete bypass + dead plugin-state.

## Critical risks (from recon)
1. **Channel-id mismatch silently disables wa-approval.** Gate matches literal `"whatsapp"`;
   plugin currently uses `"whatsapp-cloud"`. The adapter MUST send `OriginatingChannel:
   "whatsapp"` (1-line plugin change) OR widen the gate. **Test post-change: an unknown
   number must still be blocked pre-LLM.** Highest-severity risk.
2. **Session-history orphaning on cutover.** Plugin keys `whatsapp-cloud:dm:<digits>`; core
   builds a different key → Ollie loses WA conversation history at cutover unless a one-shot
   migration runs. Warn or migrate.
3. **Reply chunking / markdown** differences (Graph 4096-char limit; core's chunker differs).
4. **`stripToolCallLeak` semantics** — moving the strip upstream may change tool-execution
   visibility; verify with a tool-using query before/after.

## MUST-preserve (load-bearing): owner/guest tiering, wa-approval gate, voice-out, inbound
voice transcription, 75s→job, owner-number routing, the unified owner-approval router
(approve/deny works identically across channels — cross-channel test).

## Rollback: per-PR `useCoreRouting` boolean (default false until each PR's test passes);
full cutover keeps bypass alive 7 days; emergency = set flag false + `systemctl restart
openclaw-gateway` (~30s). Bypass code is NOT deleted until 7 stable days post-cutover.

## Recommendation
Start with **PR A** (~1 day) — smallest change that gets the wa-approval gate firing for WA
Cloud + proves the core route. Layer B-E. This track runs AFTER (or alongside) the
core-unify reboot window, not inside it.
