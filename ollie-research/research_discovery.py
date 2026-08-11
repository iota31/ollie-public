#!/usr/bin/env python3
"""research_discovery.py — trend DISCOVERY -> 4DPocket ingestion.

Brave (primary, free) / Firecrawl (escalation, budget-gated) FIND fresh URLs
for each discovery-type source query; the URLs are then handed to 4DPocket
(research_fourdpocket.ingest_url) which does the storage + trafilatura
extraction + URL-dedup. This is the half 4DPocket can't do for us: 4DPocket
polls feeds on a schedule, but it cannot run ad-hoc recency-scoped SEARCHES —
that's discovery, and it's the engine's job (re-architecture 2026-06-14).

Public API:
    search(query, recency="pw", domain_tags=None) -> list[CANDIDATE]   (URL find)
    feed_discovery(sources, cid=None) -> int    # runs queries + INGESTS; returns
                                                 # the count of NEW items ingested
    main(argv) -> int                            # feeder entrypoint (systemd)
    _discovery_candidates(sources) -> list[CANDIDATE]   # find-only (tested directly)
    _fingerprint(url, title) -> str
    fetch(url, **kw) -> bytes           # overridable in tests

CANDIDATE shape (transient, pre-ingest only — the QUEUE no longer reads these;
it reads back 4DPocket items via research_queue):
    source_id, source_type, url, title, text (<=1500), ts (ISO|None),
    domain_tags [str], fingerprint (sha256 hex)

Brave endpoint:  GET https://api.search.brave.com/res/v1/web/search
                 ?q=<query>&count=<n>&freshness=<pd|pw|pm>
                 Header: X-Subscription-Token: <BRAVE_API_KEY>
                         Accept: application/json
Rate limit:      free tier ~1 req/s — module enforces >= 1.0 s between calls.

Firecrawl:       POST https://api.firecrawl.dev/v2/search
                 Body: {"query":..., "limit":..., "sources":["web"]}
                 Header: Authorization: Bearer <key>
                 Only called when source has "escalate": true.
                 Budget-gated: `budget.py check research` before, record after.
                 creditsUsed logged to ~/.openclaw/logs/research-spend.log.

Key resolution (overridable for tests via module-level vars):
    BRAVE_API_KEY   <- openclaw.json mcp.servers['brave-search'].env.BRAVE_API_KEY
                       (or env var BRAVE_API_KEY)
    _fc_key_path    <- /home/openclaw/.openclaw/secrets/firecrawl-key

Pure stdlib; no pip deps.
"""

import hashlib
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths — mirror ollie_work_digest.py testable-globals pattern
# ---------------------------------------------------------------------------
HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
_LOG_DIR = os.path.join(HOME, ".openclaw", "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "research-discovery.log")
_SPEND_LOG = os.path.join(_LOG_DIR, "research-spend.log")
_OPENCLAW_JSON = os.path.join(HOME, ".openclaw", "openclaw.json")
_BUDGET_BIN = os.path.join(HOME, "bin", "budget.py")

# Overridable in tests
_fc_key_path: str = os.path.join(HOME, ".openclaw", "secrets", "firecrawl-key")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("research_discovery")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [discovery] %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(_LOG_FILE)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger


log = _setup_logger()

# ---------------------------------------------------------------------------
# Rate-limit state for Brave (module-level, shared across all calls in process)
# ---------------------------------------------------------------------------
_last_brave_call: float = 0.0
_BRAVE_MIN_INTERVAL: float = 1.0  # seconds — free tier ~1 req/s


# ---------------------------------------------------------------------------
# HTTP fetch — overridable in tests
# ---------------------------------------------------------------------------
def _fetch_default(url: str, headers: dict | None = None,
                   method: str = "GET", body: bytes | None = None,
                   timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Public alias — tests monkey-patch this
fetch = _fetch_default  # noqa: N816


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------
def _read_brave_key() -> str:
    """Read Brave API key: env var > openclaw.json > empty string."""
    # 1. Environment variable (set in ollie-jobs.service env stanza)
    key = os.environ.get("BRAVE_API_KEY", "")
    if key:
        return key
    # 2. openclaw.json
    try:
        cfg = json.loads(open(_OPENCLAW_JSON).read())
        key = (
            cfg.get("mcp", {})
               .get("servers", {})
               .get("brave-search", {})
               .get("env", {})
               .get("BRAVE_API_KEY", "")
        )
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    log.warning("BRAVE_API_KEY not found in env or openclaw.json")
    return ""


def _read_fc_key() -> str:
    """Read Firecrawl key from secrets file."""
    try:
        return open(_fc_key_path).read().strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot read firecrawl key from %s: %s", _fc_key_path, exc)
        return ""


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _MULTI_WS_RE.sub(" ", text).strip()


def _cap(text: str, limit: int = 1500) -> str:
    return text[:limit] if text else ""


def _fingerprint(url: str, title: str) -> str:
    """sha256 hex of normalized_url + '|' + normalized_title — shared contract."""
    norm_url = (url or "").strip().lower()
    norm_title = (title or "").strip().lower()
    raw = f"{norm_url}|{norm_title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _recency_to_freshness(recency: str) -> str:
    """Map source.recency_days / caller hint to Brave freshness param."""
    mapping = {"pd": "pd", "pw": "pw", "pm": "pm"}
    return mapping.get(recency, "pw")


def _parse_age_ts(age_str: str | None) -> str | None:
    """Try to parse Brave's age/page_age field to ISO timestamp."""
    if not age_str:
        return None
    # Common formats: "2026-06-10T12:00:00", "2 days ago", "June 10, 2026"
    # Try ISO first
    try:
        dt = datetime.fromisoformat(age_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, AttributeError):
        pass
    return None  # relative strings ("2 days ago") are left as None


# ---------------------------------------------------------------------------
# Budget gate (wraps subprocess call to budget.py)
# ---------------------------------------------------------------------------
def _budget_check(lane: str = "research") -> bool:
    """Return True if the budget allows another call on this lane."""
    try:
        result = subprocess.run(
            [sys.executable, _BUDGET_BIN, "check", lane],
            capture_output=True, timeout=10
        )
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("budget check failed (%s); allowing call", exc)
        return True  # fail-open to not break discovery


def _budget_record(lane: str = "research") -> None:
    """Record one spend unit for the lane."""
    try:
        subprocess.run(
            [sys.executable, _BUDGET_BIN, "record", lane],
            capture_output=True, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("budget record failed: %s", exc)


def _log_fc_credits(credits_used: int | float, query: str) -> None:
    """Append Firecrawl creditsUsed to the spend log."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        line = (
            f"{datetime.now(tz=timezone.utc).isoformat()} "
            f"firecrawl creditsUsed={credits_used} query={query!r}\n"
        )
        with open(_SPEND_LOG, "a") as f:
            f.write(line)
    except OSError as exc:
        log.warning("Cannot write spend log: %s", exc)


# ---------------------------------------------------------------------------
# Brave search — now thin wrapper over research_search.web_search
# (Brave → Linkup fallback; Linkup path is what stops the Brave quota burn)
# ---------------------------------------------------------------------------
_BRAVE_BASE = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_COUNT = 10


def _brave_search(query: str, freshness: str = "pw",
                  count: int = _BRAVE_COUNT) -> list[dict]:
    """Call Brave Web Search API; return raw result dicts.

    Thin wrapper — actual call + Linkup fallback live in research_search.
    Rate-limit is enforced inside research_search as well as here so legacy
    callers/tests that bypass it don't blast Brave either.
    """
    global _last_brave_call  # noqa: PLW0603

    # Rate-limit: wait until at least _BRAVE_MIN_INTERVAL seconds since last call
    elapsed = time.monotonic() - _last_brave_call
    if elapsed < _BRAVE_MIN_INTERVAL:
        time.sleep(_BRAVE_MIN_INTERVAL - elapsed)
    _last_brave_call = time.monotonic()

    import research_search  # lazy: avoids cycle in test imports
    # keep the legacy key reader available to the helper
    research_search.set_brave_key_reader(_read_brave_key)
    # share the same fetch seam so existing tests that patch disc.fetch still work
    research_search.set_caller_fetch(fetch)
    return research_search.web_search(query, count=count, recency=freshness)


def _brave_results_to_candidates(
    results: list[dict],
    query: str,
    source_id: str,
    domain_tags: list[str],
) -> list[dict]:
    candidates: list[dict] = []
    for r in results:
        url = r.get("url", "")
        title = _strip_html(r.get("title") or "")
        desc = _cap(_strip_html(r.get("description") or ""))
        age_str = r.get("age") or r.get("page_age")
        ts = _parse_age_ts(age_str)
        if not url:
            continue
        candidates.append({
            "source_id": source_id,
            "source_type": "discovery",
            "url": url,
            "title": title,
            "text": desc,
            "ts": ts,
            "domain_tags": domain_tags,
            "fingerprint": _fingerprint(url, title),
        })
    return candidates


# ---------------------------------------------------------------------------
# Firecrawl search (metered fallback — only when source opts in)
# ---------------------------------------------------------------------------
_FC_BASE = "https://api.firecrawl.dev/v2/search"
_FC_COUNT = 10


def _firecrawl_search(query: str, count: int = _FC_COUNT) -> tuple[list[dict], int]:
    """Call Firecrawl /v2/search; return (results, creditsUsed).

    Budget-gated: caller must call _budget_check first.
    """
    fc_key = _read_fc_key()
    if not fc_key:
        log.error("Firecrawl key unavailable; skipping")
        return [], 0

    body = json.dumps({"query": query, "limit": count, "sources": ["web"]}).encode()
    headers = {
        "Authorization": f"Bearer {fc_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        log.debug("firecrawl search: %r", query)
        raw = fetch(_FC_BASE, headers=headers, method="POST", body=body)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("firecrawl search failed for %r: %s", query, exc)
        return [], 0

    results = data.get("data", {}).get("web", [])
    credits_used = int(data.get("creditsUsed", 0))
    return results, credits_used


def _fc_results_to_candidates(
    results: list[dict],
    source_id: str,
    domain_tags: list[str],
) -> list[dict]:
    candidates: list[dict] = []
    for r in results:
        url = r.get("url", "")
        title = _strip_html(r.get("title") or "")
        text = _cap(_strip_html(r.get("description") or r.get("text") or ""))
        if not url:
            continue
        candidates.append({
            "source_id": source_id,
            "source_type": "discovery",
            "url": url,
            "title": title,
            "text": text,
            "ts": None,
            "domain_tags": domain_tags,
            "fingerprint": _fingerprint(url, title),
        })
    return candidates


# ---------------------------------------------------------------------------
# Public: search
# ---------------------------------------------------------------------------
def search(
    query: str,
    recency: str = "pw",
    domain_tags: list[str] | None = None,
    source_id: str = "discovery",
) -> list[dict]:
    """Run a Brave Web Search and return CANDIDATEs.

    Args:
        query:       Free-text search query.
        recency:     'pd' (past day) | 'pw' (past week, default) | 'pm' (past month).
        domain_tags: Tags to embed in returned candidates.
        source_id:   source_id string for candidates (caller can pass source['id']).

    Returns list of CANDIDATE dicts.
    """
    freshness = _recency_to_freshness(recency)
    tags = list(domain_tags or [])
    results = _brave_search(query, freshness=freshness)
    return _brave_results_to_candidates(results, query, source_id, tags)


# ---------------------------------------------------------------------------
# Discovery candidate-finding (Brave primary, Firecrawl escalation)
# ---------------------------------------------------------------------------
def _discovery_candidates(sources: list[dict]) -> list[dict]:
    """Loop over enabled type=='discovery' sources and run searches to FIND
    fresh URLs. Returns a flat list of transient CANDIDATEs (url/title/text);
    these are NOT queued — feed_discovery hands them to 4DPocket. One bad
    source never kills the batch.

    Each discovery source has:
        target:       the search query (or query template)
        domain_tags:  [str]
        recency_days: int (mapped to Brave freshness)
        escalate:     bool — if True and Brave returns empty, try Firecrawl
    """
    all_candidates: list[dict] = []

    for src in sources:
        if not src.get("enabled", True):
            continue
        if src.get("type") != "discovery":
            continue

        source_id = src.get("id", "discovery")
        query = src.get("target", "")
        domain_tags = list(src.get("domain_tags") or [])
        recency_days = src.get("recency_days", 7)
        escalate = bool(src.get("escalate", False))

        # Map recency_days to Brave freshness string
        if recency_days <= 1:
            freshness = "pd"
        elif recency_days <= 7:
            freshness = "pw"
        else:
            freshness = "pm"

        try:
            brave_results = _brave_search(query, freshness=freshness)
            candidates = _brave_results_to_candidates(
                brave_results, query, source_id, domain_tags
            )

            # Firecrawl escalation: only when opted in AND brave came back empty
            if escalate and not candidates:
                log.info("source %s: brave empty, checking budget for firecrawl escalation", source_id)
                if _budget_check("research"):
                    fc_results, credits_used = _firecrawl_search(query)
                    if fc_results:
                        _budget_record("research")
                        _log_fc_credits(credits_used, query)
                        candidates = _fc_results_to_candidates(fc_results, source_id, domain_tags)
                        log.info(
                            "source %s: firecrawl returned %d results, creditsUsed=%d",
                            source_id, len(candidates), credits_used
                        )
                    else:
                        log.info("source %s: firecrawl also empty", source_id)
                else:
                    log.warning("source %s: firecrawl escalation blocked by budget", source_id)

            log.info("source %s: %d candidates", source_id, len(candidates))
            all_candidates.extend(candidates)

        except Exception as exc:  # noqa: BLE001
            log.error("Unhandled error in _discovery_candidates for %s: %s", source_id, exc)

    log.info("_discovery_candidates: %d total candidates", len(all_candidates))
    return all_candidates


# ---------------------------------------------------------------------------
# Defensive sibling imports (4DPocket client + optional crawl extractor)
# ---------------------------------------------------------------------------
def _fourdpocket():
    """Import the 4DPocket client fresh (returns sys.modules entry if a test
    stubbed it). None if unavailable -> feed_discovery degrades to a no-op."""
    try:
        import importlib
        return importlib.import_module("research_fourdpocket")
    except Exception as exc:  # noqa: BLE001
        log.error("research_fourdpocket unavailable: %s", exc)
        return None


def _crawl_content(url: str) -> str | None:
    """Best-effort full-content extraction for a JS/bot-walled URL via
    research_crawl (crawl4ai). Optional + fully guarded -> None on any failure
    so it never blocks ingestion (4DPocket extracts from the URL anyway)."""
    try:
        import importlib
        crawl = importlib.import_module("research_crawl")
    except Exception:  # noqa: BLE001
        return None
    try:
        return crawl.fetch(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("crawl enrich failed for %s: %s", url, exc)
        return None


# A URL "looks walled" if its host is a known JS/login-heavy platform where
# 4DPocket's server-side extraction tends to come back thin. Kept tiny + cheap;
# crawl enrichment is opt-in per source via src["crawl_enrich"] anyway.
_WALLED_HINTS = ("twitter.com", "x.com", "instagram.com", "facebook.com",
                 "linkedin.com", "medium.com")


def _looks_walled(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _WALLED_HINTS)


# ---------------------------------------------------------------------------
# Public: feed_discovery  (runs queries + INGESTS into 4DPocket)
# ---------------------------------------------------------------------------
# Cap full-content crawl enrichments per cycle (bound RAM/cost on the box).
CRAWL_ENRICH_MAX = 3


def feed_discovery(sources: list[dict], cid: str | None = None) -> int:
    """Run every enabled discovery query, then push each found URL into
    4DPocket's curiosity-feed collection. 4DPocket handles storage, extraction
    and URL-dedup (a re-found URL comes back as a 409 and is skipped).

    Returns the COUNT of NEW items ingested (NOT candidates) — by design the
    discovery push and the queue read are decoupled: discovery pushes URLs now,
    4DPocket processes them asynchronously (seconds-to-minutes), and the NEXT
    queue cycle reads the recent items back out via search.

    Optional crawl enrichment (per-source "crawl_enrich": true): for URLs that
    look JS/bot-walled, fetch full content via research_crawl and pass it as
    content= so 4DPocket doesn't have to (best-effort, capped per cycle).
    """
    fdp = _fourdpocket()
    if fdp is None:
        log.error("feed_discovery: no 4DPocket client -> 0 ingested")
        return 0
    if cid is None:
        try:
            cid = fdp.ensure_collection()
        except Exception as exc:  # noqa: BLE001
            log.error("feed_discovery: ensure_collection failed: %s", exc)
            cid = None

    candidates = _discovery_candidates(sources)
    # which sources opted into crawl enrichment
    enrich_ids = {s.get("id") for s in sources if s.get("crawl_enrich")}
    enrich_budget = CRAWL_ENRICH_MAX

    ingested = 0
    for c in candidates:
        url = c.get("url")
        if not url:
            continue
        content = None
        if c.get("source_id") in enrich_ids and enrich_budget > 0 and _looks_walled(url):
            content = _crawl_content(url)
            if content:
                enrich_budget -= 1
        try:
            item_id = fdp.ingest_url(
                url, source_platform="discovery", cid=cid,
                title=c.get("title") or None, content=content,
            )
        except Exception as exc:  # noqa: BLE001 — one bad ingest never kills the batch
            log.error("feed_discovery: ingest failed for %s: %s", url, exc)
            item_id = None
        if item_id:
            ingested += 1

    log.info("feed_discovery: %d new items ingested from %d candidates",
             ingested, len(candidates))
    return ingested


# ---------------------------------------------------------------------------
# Starter discovery query suggestions (used by dashboard / queue builder)
# ---------------------------------------------------------------------------
STARTER_QUERIES = [
    # AI / ML
    "AI agent frameworks site:github.com OR site:arxiv.org",
    "LLM inference optimization techniques 2026",
    "multimodal foundation models research 2026",
    "autonomous AI systems safety alignment",
    # Dev tools
    "developer productivity tools open source 2026",
    "Python async performance improvements",
    # Entrepreneurship
    "B2B SaaS micro-startup bootstrapped 2026",
    "founder-led growth sales playbooks",
    # Health / longevity (Tushar interest)
    "longevity biomarkers research 2026",
    "sleep optimization neuroscience 2026",
]


# ---------------------------------------------------------------------------
# Source loading (registry first, sources.json fallback)
# ---------------------------------------------------------------------------
def _load_sources() -> list[dict]:
    """Load sources via research_registry, falling back to sources.json next to
    this module, then to []. Guarded — discovery feeding never crashes."""
    try:
        import importlib
        reg = importlib.import_module("research_registry")
        srcs = list(getattr(reg, "load_sources")() or [])
        if srcs:
            return srcs
    except Exception as exc:  # noqa: BLE001
        log.warning("research_registry.load_sources failed: %s", exc)
    for path in (
        os.path.join(HOME, ".openclaw", "workspace", "research", "sources.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json"),
    ):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            continue
    return []


# ---------------------------------------------------------------------------
# Feeder entrypoint (systemd ollie-research-feed.service)
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    """Run one discovery -> 4DPocket feed pass over the registry's sources.
    Never raises (a feed failure must not crash the oneshot unit)."""
    try:
        n = feed_discovery(_load_sources())
        log.info("main: feed_discovery ingested %d new items", n)
    except Exception as exc:  # noqa: BLE001
        log.error("main: feed_discovery crashed (unexpected): %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
