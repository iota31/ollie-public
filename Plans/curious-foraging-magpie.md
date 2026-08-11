# Curiosity Engine — Ollie's afferent (sensory) subsystem

**Status:** PLAN, converged with Tushar 2026-06-14. Build authorized. Deploy gated.
**Origin:** voice memo (Prakersh1.m4a) — research is sourced only from manual 4DPocket
saves, no recency/relevance gate, no discovery, no priority control, no UI → stale,
irrelevant, token-wasteful output.

## Anatomy & guiding principles
- **Afferent vs efferent.** ollie-hands = efferent (acts on the desktop, AS Tushar,
  consent-gated, session-locked, the most dangerous surface). The Curiosity Engine =
  afferent (perceives the world, ranks it, read-only, low-trust). Two different nerves.
  **This is a NEW subsystem, sibling to `ollie-jobs`, NOT an extension of hands.**
  It owns its registry/gate/queue/dashboard. It CALLS ollie-hands as a client only for
  logged-in social reads (one seam).
- **Useful > free; free is the tiebreaker.** "Free but useless" is the failure mode.
  Lean on the box's own compute (verified: 31.5GB host, ~14GB free in WSL now) for the
  heavy lifting; spend metered credits ONLY where compute can't help (live search,
  login-walled social). Everything billable is capped + visible via budget.py.
- **Discover, don't just poll.** The deepest fix for "stale" is shifting from passive
  feed-polling to active, recency-scoped trend DISCOVERY (search). Search is
  recency+relevance-native. Curated feeds augment it.
- **Don't build blind.** Each component grounds in the real API/lib on the box before
  coding (offline-tests-pass != works — the dream-promoter & \\wsl$ bugs taught us).

## The pipeline
```
 REGISTRY (sources.json + interests.json) — UI-editable
        │
        ▼  ACQUISITION adapters (best tool per job, daemons call APIs directly):
   ┌──────────────────────────────────────────────────────────────┐
   │ discovery (domain queries) → Brave API (free) → Firecrawl       │
   │                               search/deep_research (metered)    │
   │ blogs/news/docs/extract    → crawl4ai (self-hosted on box, FREE)│
   │ subreddits                 → Reddit JSON API (free)             │
   │ rss feeds                  → stdlib xml (free)                  │
   │ logged-in X / Instagram    → ollie-hands MCP Camoufox (client)  │
   │                               Apify for key accounts (metered)  │
   │ 4dpocket saves             → existing lab_watcher (folded in)   │
   └──────────────────────────────────────────────────────────────┘
        │  candidates {source_id,url,title,text,ts,domain_tags,fingerprint}
        ▼  GATE — recency HARD filter + SEMANTIC relevance
   recency: drop older than source.recency_days
   relevance: local EMBEDDINGS (cosine vs interests profile; free, on-box) +
              fast-LLM tiebreak on the borderline band  (NOT keyword overlap —
              that's the free-but-dumb trap)
        ▼  QUEUE — rank = relevance × source_weight × recency_decay; dedup; queue.json
        ▼  existing job pipeline (job-submit --lane research, budgets, taxonomy, digest)
        ▲
   DASHBOARD (Tailscale-bound, owner-private, no secrets): view/add/reorder/rewire/
   remove sources + interests + queue items.
```

## Component / tool decisions (converged)
| Layer | Tool | Cost | Notes |
|---|---|---|---|
| Extraction / deep-crawl | **crawl4ai self-hosted on box** | free (our RAM) | stealth mode (satisfies never-vanilla-Chrome); headless, no-login, public web; its OWN trust profile, legitimately separate from hands |
| Discovery | Brave API (free tier) → Firecrawl search/deep_research | mostly free | recency-scoped queries from interests domains |
| Relevance gate | local embeddings + fast-LLM tiebreak | ~free | semantic, not keyword |
| Feeds | RSS (stdlib) + Reddit JSON | free | table stakes coverage |
| Logged-in X/IG | ollie-hands Camoufox (MCP client) ; Apify for key accts | free / metered | the one seam to hands |
| Anti-bot/CAPTCHA fallback | Firecrawl scrape | metered, rare | only when crawl4ai blocked |

**Integration note:** daemons call underlying HTTP APIs directly (Firecrawl API, Brave
API — keys already on box) rather than speaking MCP; crawl4ai as local lib; ollie-hands
via its MCP. RECON confirms before build.

### RECON-CONFIRMED (2026-06-14, box untouched)
- **Firecrawl**: hosted `api.firecrawl.dev`, key `secrets/firecrawl-key` (35B, fc-). 21,023
  credits. `POST /v2/search {query,limit,sources:["web"]}`, `Authorization: Bearer`, returns
  `data.web[]` + **`creditsUsed`** per call (log it). `/v2/scrape`, `/v2/deep-research` exist.
- **Brave**: `GET api.search.brave.com/res/v1/web/search?q=&count=&freshness=pw`, header
  `X-Subscription-Token` (key in openclaw.json mcp.servers['brave-search'].env.BRAVE_API_KEY,
  also ollie-jobs.service env). Free tier ~1 rps / ~2k mo → engine MUST rate-limit ≤1 rps.
- **ollie-hands MCP**: **`http://<TAILSCALE_IP>:3200/mcp`** (Tailscale IP, NOT 127.0.0.1 —
  plan/memory were wrong). Bearer in openclaw.json mcp.servers.hands.headers.Authorization.
  FastMCP streamable-HTTP (SSE responses): initialize→capture `mcp-session-id`→
  notifications/initialized→tools/call; parse `data:` lines. Browser reads via
  `act(kind="browser", op="goto"|"extract"|"links")` = **T2 NOTIFY, no consent**. Probe
  `session_info` first; degrade on hands_enabled:false/401/refused. CAVEAT: session.locked
  currently true — verify Camoufox reads behave under a locked Windows session at integration.
- **crawl4ai**: Docker/podman ABSENT in gateway distro → Docker path IMPOSSIBLE. Only route =
  `uv venv /home/openclaw/ollie-research/.venv` + `uv pip install crawl4ai` +
  `python -m playwright install chromium`. System python is stdlib-only (no pip). Footprint
  ~few hundred MB (949G free); RAM fine at concurrency 1–2 (13Gi free); NO .wslconfig bump.
- **embeddings**: nothing installed (no torch/numpy). Use **fastembed** (BAAI/bge-small-en-v1.5,
  ONNX, ~100MB, no torch) in the SAME venv. (Cheap-API embeddings = fallback only.)
- **budget.py**: COUNT-based per-lane daily ceilings (not dollars). budget-config.json
  live-editable (`ceilings:{research:6,poc:2,project:6}`). `check(lane)`/`record(lane)`. Add a
  `discovery` lane by editing budget-config.json (no code change). Log Firecrawl `creditsUsed`
  to a research-spend log for true credit visibility.
- **Apify**: NO token on box or in PAI skill. Paid X/IG key-account fallback is INERT until a
  token is provisioned at secrets/apify-token. Scoped OUT of P1; social = Camoufox best-effort.

### ⚠️ Cross-system interaction (must handle at deploy)
Installing crawl4ai's venv + chromium will trip the **lab-bypass watchdog** (it alerts on new
venv / large ~/.cache trees outside the lab — built to catch the supertonic leak). Deploy MUST
add `/home/openclaw/ollie-research/.venv` + crawl4ai/playwright cache paths to the watchdog
allowlist (ollie_watchdog.py CACHE_ALLOWLIST / venv baseline) BEFORE the install, or it
self-alarms on our own legit install.

## Layout (new dir `ollie-research/`)
research_registry.py (sources/interests load/save) · research_sources.py (RSS+Reddit) ·
research_crawl.py (crawl4ai client) · research_discovery.py (Brave/Firecrawl search) ·
research_social.py (ollie-hands MCP client for X/IG) · research_gate.py (recency +
embedding relevance) · research_queue.py (orchestrator: poll→gate→rank→dispatch) ·
research_dashboard.py (+ index.html) · systemd units · tests/.

## Build phases
- **P0 RECON (now):** verify on box — crawl4ai install path + resource fit; Firecrawl &
  Brave keys present + direct-API shape; embedding runtime choice (fastembed/onnx vs
  api); re-confirm hands MCP read verbs; budget.py hook points. READ-ONLY/sandbox.
- **P1 BUILD (parallel agents, worktrees, opus/sonnet — never fable):** registry+gate ·
  sources(RSS/Reddit)+discovery(Brave/FC) · crawl4ai client · social(hands) · queue
  orchestrator+budget wiring · dashboard. Shared contracts fixed up front.
- **P2 INTEGRATE:** I merge, review every diff, /simplify, offline tests green.
- **P3 DEPLOY (GATED):** serial, backups+hash-compare; crawl4ai install on box is the
  one heavy step — confirm before it; new systemd services via symlink-enable; dashboard
  Tailscale-only; .wslconfig memory bump ONLY if needed + careful restart (keepalive
  footgun); no live openclaw.json edits. Dream-style dry-run first where it dispatches.

## Risks / honest flags
- crawl4ai = +1 service on a fragile box (RAM fine, ops cost real) → own systemd unit,
  lazy-start, isolated.
- X/IG = login walls + ToS arms race; Camoufox flaky → Apify for accounts that matter;
  never let social block the RSS/Reddit/search backbone.
- Embedding gate false-drops good-but-oddly-worded items → keep threshold conservative +
  LLM tiebreak on borderline; tunable from dashboard.
- All credit spend capped + logged via budget.py; dashboard shows it.

## Anti-goals
No rebuild of job runner/budgets/taxonomy. Not an extension of ollie-hands. No public
dashboard. No routing bulk research-crawl through the consent-gated hands engine. No
fable subagents. No deploy without backups + gated confirm on heavy box changes.

## RE-ARCHITECTURE 2026-06-14 — collapse ingestion into 4DPocket (owner decision)
4DPocket capability audit (from the 4DPocket agent) settled this. 4DPocket DOES:
scheduled RSS/Atom/JSONFeed polling (/api/v1/rss — keyword filters, review queue,
auto-import to collection, 15min), URL ingest+trafilatura extraction (POST /items,
409 URL-dedup), hybrid keyword+semantic+recency search (/search?after/before/tag/
source). 4DPocket does NOT: subreddit/X/IG account polling, auto-running saved
searches, JS-render/bot-wall crawling, per-item priority/weight.

Decision: 4DPocket = the ingestion+storage+extraction+RSS spine. Ollie's engine
keeps only the novel parts. Target: OLLIE's 4DPocket account, dedicated
`curiosity-feed` collection. PAT: /home/openclaw/.openclaw/secrets/fourdpocket-ollie.pat.
Base http://<TAILSCALE_IP_VPS>:4040/api/v1, Host header "localhost:4040" (proxy 421 quirk).

- DELETE research_sources.py (RSS/Reddit pollers) — 4DPocket /rss replaces them;
  register subreddit/X feeds as RSS (reddit.com/r/X/.rss etc.) in 4DPocket.
- REWIRE research_discovery.py: Brave/Firecrawl find fresh URLs -> push each into
  4DPocket POST /items (curiosity-feed collection) instead of returning candidates.
- NEW research_feeds_sync.py: read sources.json (rss/subreddit/x), idempotently
  register each as a 4DPocket feed via /rss (GET first, skip dupes).
- REWIRE research_queue.py: run() pulls RECENT items from 4DPocket /search (recency
  + the curiosity-feed collection) -> gate (interests) -> score/rank/dedup -> queue
  -> dispatch (dry-run). Drop poll_all/poll_discovery/social from the cycle.
- KEEP crawl4ai (research_crawl) for JS/bot-wall hard cases (extract -> push to 4DP);
  KEEP gate (interest scoring), queue+dispatch (priority lives in item_metadata or
  local queue.json since 4DP has no weight field), dashboard (sources view = feeds +
  discovery queries). KEEP social best-effort/off (mostly subsumed by X-RSS feeds).

## 4DPocket refactor — DEPLOYED + VERIFIED LIVE (2026-06-14)
Engine collapsed onto 4DPocket spine; deployed + verified end-to-end on the box.
- 3 live POST-contract bugs the mocked tests couldn't catch (found via live validation):
  (1) /items item_type enum (must be 'url' not 'article') + source_platform enum
  (omit non-enum like 'discovery'); (2) /search REQUIRES non-empty q (422 on empty) ->
  search_recent reads GET /items (or /collections/{cid}/items) + client-side recency;
  (3) /collections/{cid}/items needs {item_ids:[...]} not {item_id:x}. All fixed + tested.
- Feeds: 6 registered in 4DPocket /rss incl 3 subreddits as .rss (LocalLLaMA/ML/LLMDevs)
  — SOLVES the Reddit-403 (4DPocket fetches RSS, not the blocked JSON API) — + anthropic,
  HF blog, simonwillison. 4DPocket auto-imports into curiosity-feed (mode=auto), polls ≤15min.
- Discovery: validated push to 4DPocket /items (ingest works, 409-dedup works).
- Queue: reads curiosity-feed (collection-scoped, ignores Ollie's unrelated items) -> gate
  -> rank -> queue.json -> dry-run dispatch. Verified.
- Dashboard: live, systemd-managed, http://<TAILSCALE_IP>:3400 (host portproxy + boot-refresh
  task), bearer-gated.
- Timers enabled: ollie-research-feed (push) + ollie-research-poll (read), offset. Dispatch
  stays --dry-run until a few clean cycles. 200 offline tests green; ~12 granular commits.
OPEN/next: flip dispatch live after clean cycles; tune gate threshold (lenient now) via
dashboard; old pre-refactor queue.json items re-rank out naturally; X/IG still best-effort
(no Apify); consider per-item priority in 4DPocket item_metadata later.
