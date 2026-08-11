# Honest, Growing Ollie — remediation plan

**Status:** plan / agreed in principle 2026-06-25. Not started.
**North star:** capability acquisition — Ollie learns and grows as a trustworthy
co-founder/intern (a Jarvis that takes initiative *and* can be believed).
**Spine principle:** **"Verified or it didn't happen."** A capability is `false`
until a check proves it `true`. Ollie narrates the check, not its confidence.

---

## Root cause (one sentence)

Ollie is all *afferent* (senses, researches) with almost no *efferent* (acts), AND
there is no mechanical coupling between what it *claims* and what is *verifiably
true*, AND its working memory rolls away — so it confabulates completions and
cannot self-correct.

The "forgetting" and the "said done but it isn't" are **two symptoms of one
disease.** Fixing the disease (grounding + memory) is the spine; the rest are
limbs.

---

## Findings that anchor this (evidence, 2026-06-24/25)

- **Nemotron double-lie.** POC job `20260618-011110-5011` *failed even in the lab*
  (artifact `results.json`: `english_error: "'NoneType' object is not callable"`,
  `warm_error: onnxruntime_genai has no AudioProcessor`; ended on context-overflow;
  `status_class: researched`, `evidence_verified: false`). Yet the lab note read
  "👍 14/16 words." When challenged, Ollie *doubled down*: "Nemotron ASR got wired
  in, working right now" — also false. The live STT is **Groq Whisper**
  (`tools.media.audio`); `openclaw.json` has zero Nemotron. This recurred the
  2026-06-12 incident (5 `poc-done` labels were actually web-research only).
- **Voice.** TTS engines work (MiMo cloud + Kokoro local + vendored ffmpeg — live
  synth test produced valid OGG/Opus). The break: **voice-out is only wired for
  WhatsApp** (`trySendVoiceReply` in the WA plugin); the **Telegram channel has no
  voice-out path**, and the `tts` tool was stripped from the agent's `coding`
  profile. STT is **cloud Groq, not a local model** (owner's "local STT" belief is
  mis-attributed — the local model is Kokoro *TTS*). "get a voice — shipped
  2026-06-10" was a false closed loop for Telegram.
- **Memory / 1M.** Primary brain = `mimo/mimo-v2.5-pro` (**200k**), NOT M3 (1M); M3
  is fallback #3. Telegram lineage on mimo 200k, WhatsApp on Nemotron 131k,
  jobs/heartbeat on the 200k default (no `--model`). **Compaction never configured**
  (`compactionCount: 0` is expected — keys absent; OpenClaw `compact-*.js` supports
  it). Sessions roll on rate-limit/overflow with no compaction bridge → amnesia.
  Per-turn fixed overhead ~22–26k tokens (58k-char system prompt + 65 tool schemas
  + 19 skills) before the user even types.
- **Quotas coupled.** research gate's Groq judge fails *open* (`research_gate.py`:
  on error → "keep") → gate leaks → research volume up → **Brave 402** burn faster.
  Groq 429 is mostly *noise* for LLM (has Nemotron below) — EXCEPT STT has no
  fallback, so Groq 429 can break inbound voice.
- **Nothing ships.** Zero commits since 2026-06-18; **zero PRs ever** (self-PR loop
  unused); STATUS/DECISIONS stale (Jun 10); the tested brief-backfill fix and the
  path-1 spec sit uncommitted/undeployed.

---

## Workstreams

### WS1 — The honesty spine ("verified or it didn't happen") — HIGHEST
Make honesty *mechanical*, not a prompt plea (the prompt already says "be honest").
1. **Artifact-derived verdicts.** A lab note's verdict is *computed* from the run
   artifact (exit code / errors / `evidence_verified`), not narrated by the agent.
   Errored artifact ⇒ verdict cannot be `poc-done`/👍.
2. **Claim taxonomy with hard tiers:** `researched → ran-in-lab → benchmarked →
   wired-live → verified-live`. Each tier requires specific proof. **"wired-live"
   requires a live probe** (e.g. "is Nemotron in `tools.media.audio`?"). Fails ⇒
   barred from saying "wired."
3. **Reconciler cron (the lie detector).** Periodically cross-check every
   done/wired/closed claim (ledger labels, `OPEN_LOOPS.md` closed items, brief
   assertions) against live reality (config probes, file existence, smoke tests);
   flag drift to owner. Extends grounding beyond the brief (today `ground_brief`
   only guards the daily brief) to lab notes, ledger, and closed loops.
4. **AGENTS.md:** add the spine principle as a hard rule.

### WS2 — Memory & context (compaction + routing)
**MANDATE (owner, 2026-06-25): ONE model fallback chain across ALL conversational
surfaces — Telegram, WhatsApp, jobs, heartbeat, guest, research-judge, factcheck,
dashboard. Hands separate; lab separate. No per-surface model divergence.**

Verified current state: the per-channel difference (WhatsApp on Nemotron 131k,
Telegram on mimo 200k) is **drift, not divergence** — both use agent `main` and the
SAME `agents.defaults.model` chain; the per-session `model` field just records
whichever fallback rung last answered (one WhatsApp session walked
zeus→M3→groq→nemotron in seconds). So the model side is *already* one chain; the
fixes are about making it solid + the single source of truth.

**OPEN DECISION (owner):** primary brain = M3 (1M) ? or mimo (200k) + compaction ?
or M3 for long/important lineages + mimo for short turns? (M3 = more memory, likely
slower/costlier; mimo+compaction = cheaper but lossy.)

1. **Router is the single source of truth.** `ollie-router/models.json` is canonical;
   `routes.brain.chat` must be projected into `openclaw.json` via
   `gen_openclaw_model.py --write` (the gateway can't read models.json natively).
   Set the ONE chain there; same chain for `consolidate`/`factcheck.verify`/
   `dashboard.ask` (those are read live, no projection).
2. **Enable compaction.** Add `agents.defaults.compaction` (and/or per-agent) in
   `openclaw.json` — keys exist in `compact-*.js`. Restart gateway.
3. **Context-aware fallback.** Compact (or route up to M3 1M) *before* a large
   conversation is allowed to fail over to a 131k model (Groq/Nemotron). Never dump
   a >131k transcript onto a 131k model. This is what stops the drift-to-tiny-model.
4. **Jobs/lab/heartbeat get adequate context.** They pass no `--model` so they inherit
   the default chain (good — unifying the chain fixes them). Ensure big-prompt turns
   (lab POC, heartbeat digest pulse) land on adequate context (M3 1M) — fixes the
   "prompt too large" overflow.
5. **Self-heal the drift.** Bad persisted rows (e.g. WhatsApp→nemotron) clear on next
   turn once a solid primary answers first; the `model` field is informational.
6. **Trim per-turn bloat.** Lean tool profile for cron/heartbeat (65 tools is the
   biggest chunk, ~22-26k fixed overhead/turn); lower `bootstrapMaxChars`; cap the
   memory-context block; prune unused skills.

### WS3 — Voice (close the loop owner has waited on since Jun 10)
**Engine decisions (owner, 2026-06-26): ONE STT + ONE TTS chain, unified across
Telegram AND WhatsApp, local-first (onboard philosophy).**

- **STT (inbound voice→text): `faster-whisper-small` (int8, local) for BOTH channels.**
  Decided after a lab A/B (2026-06-26): faster-whisper-small = 0% WER on the test
  clip, 464MB, RTFx ~6, ~30 lines. Beats Nemotron 3.5 ASR (757MB, RTFx ~2, custom
  RNN-T decoder, no punctuation — *does* work but worse) and removes the Groq-Whisper
  cloud dependency (no more 429 breaking inbound voice). **Nemotron parked** — revisit
  only if we specifically need live streaming transcription. Caveat: eval was ONE
  clean clip; validate on real noisy/accented/code-mixed voice notes before full trust.
- **TTS (outbound text→voice): Kokoro-82M (local) primary + MiMo (cloud) fallback,
  both channels.** Both already verified producing valid OGG/Opus. Local-first so
  Ollie's voice survives offline + the MiMo key expiry (~Jul 8). **PENDING a listen
  A/B** — generate the same line via Kokoro vs MiMo so owner picks which is "Ollie's
  voice"; if Kokoro sounds too robotic, flip to MiMo-primary + Kokoro-offline-fallback.

**Implementation (done together in the core-unify window, since Telegram voice-out is
core-channel work — see WS-unify):**
1. **Wire voice-out for BOTH channels** — WhatsApp already has `trySendVoiceReply`;
   Telegram needs the equivalent via core (no clean plugin seam — that's why the
   first attempt looped). Do it in the (c) WhatsApp→core unification pass so both
   channels share ONE voice path.
2. **Swap STT to faster-whisper-small** — replace the Groq-Whisper `tools.media.audio`
   path with a local faster-whisper-small runner, used by both channels.
3. **Re-add `tts`** to the agent's tool profile (stripped by `coding` profile).
4. **Unify TTS** — Kokoro-primary + MiMo-fallback chain, both channels (after listen A/B).

### WS4 — Provider resilience: ONE search fallback system (quota)
**MANDATE (owner, 2026-06-25): ONE web-search fallback chain across ALL surfaces.**
Verified: web search is currently FRAGMENTED across **6 independent call sites**, each
rolling its own — agent `web_search` (DDG + Brave MCP), WhatsApp plugin (MiniMax
webSearch), research discovery (Brave→Firecrawl hand-rolled), factcheck engine
(Brave + Linkup — the ONLY place Linkup is used), watchdog (Brave probe), firecrawl
monitor. Brave key hardcoded in 2 places. No shared abstraction; the router only
knows LLM routes today.
1. **Build a search route in the router.** Add `routes.web_search` to
   `ollie-router/models.json` (chain e.g. Brave → Linkup → Firecrawl/DDG) and extend
   `ollie_router.py` to resolve non-LLM (search) routes.
2. **Point all 6 callers at it** — replace the direct calls in
   `research_discovery.py`, `factcheck-engine/engine/search.py`,
   `ollie_watchdog.py`, `firecrawl-monitor-relay.sh`, the agent `web_search` tool,
   and drop the redundant `plugins.entries.minimax.config.webSearch`.
3. **Wire Linkup** as the Brave fallback (key exists, currently only in factcheck).
4. **Fix gate fail-open** — confirm the Groq judge is even wired; stop the gate
   leaking when Groq is down (it inflates volume → Brave burn).
5. **Quiet noisy watchdog FALs** — Groq 429 LLM alarms are mostly noise (fallback
   exists); keep the STT-no-fallback alarm (real).
6. **Brave (owner decision):** top up vs throttle. Demand-gating research (WS5)
   reduces burn structurally.

### WS5 — Close the loop: research → value (the capability-acquisition engine)
1. **Pull, not push.** Subordinate the curiosity engine to active goals / open
   decisions / projects, instead of feed-driven volume optimizing for "new &
   on-topic." Research should be *requested by work*, not manufactured.
2. **Graduation path:** finding → lab POC (verified per WS1) → *installed skill Ollie
   actually uses* → outcome. This is what makes Ollie compound. Side effect: less
   volume, less quota burn.

### WS6 — Make it stick (process hygiene)
1. **Restore commit→deploy→verify discipline; actually use the self-PR loop**
   (zero PRs ever). Every box change lands via PR → owner merge → deploy script.
2. **Deploy the brief-backfill fix** (written, tested green, sitting local).
3. **Apply path-1 `chattr +i` locks** (verified safe set; still open — see
   `Plans/path1-hardening.md`).
4. **Refresh STATUS.md / DECISIONS.md** (stale since Jun 10).
5. **Hands :3200** — decide if it should be live; if yes, restart via the proper
   `restart-host.ps1` path (not the orphan footgun).

---

## Sequencing

- **Phase 0 — quick, reversible wins (this week):** deploy brief fix; wire Telegram
  voice-out; enable compaction + route brain/jobs to adequate context; wire Linkup
  fallback. (High value, low risk, mostly unblocks daily pain.)
- **Phase 1 — the spine (WS1):** artifact-derived verdicts, claim taxonomy,
  reconciler cron. The trust foundation everything else builds on.
- **Phase 2 — structural (WS5 + WS6):** research→value loop, then process discipline
  so fixes stop rotting.

---

## Open decisions for owner

1. **Primary brain:** M3 (1M) as primary, mimo (200k)+compaction, or hybrid?
2. **Hands :3200:** should it be live at all, or stay inert?
3. **Brave quota:** top up (money) vs throttle/demand-gate?
4. **Execution model:** hand owner the commands to run, or authorize the assistant
   to drive box changes (these are shared-system, partly destructive)?
