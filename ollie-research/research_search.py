#!/usr/bin/env python3
"""research_search.py — tiny shared web-search helper.

Ponytail-minimal Phase 1 of the unified web-search router: tries Brave first
(primary, free), and on quota/rate-limit/5xx (HTTP 402/429/5xx) falls back to
Linkup. Normalizes both providers' responses to Brave's web-result shape so
callers (like research_discovery) don't need to care which provider answered.

Public API:
    web_search(query, *, count=10, recency="pw") -> list[dict]

    Each result dict has the Brave-style keys already in use by
    research_discovery._brave_results_to_candidates:
        {"url", "title", "description", "age"|"page_age"|None}

Key resolution (overridable via module-level vars for tests):
    BRAVE_API_KEY   <- env var or openclaw.json (via research_discovery._read_brave_key)
    LINKUP_API_KEY  <- env var LINKUP_API_KEY
                       else /home/openclaw/.openclaw/secrets/linkup-key
                       else /home/openclaw/factcheck-engine/.env (FC_LINKUP_API_KEY=...)

Pure stdlib (urllib) — matches research_discovery's no-pip-deps policy.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request


_HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
_LOG_DIR = os.path.join(_HOME, ".openclaw", "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "research-search.log")


# ---------------------------------------------------------------------------
# Logging — minimal, mirrors research_discovery's style
# ---------------------------------------------------------------------------
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("research_search")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [search] %(message)s")
    sh = logging.StreamHandler()
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
# Fetch — overridable in tests (mirrors research_discovery.fetch pattern)
# ---------------------------------------------------------------------------
def _fetch_default(url: str, headers: dict | None = None,
                   method: str = "GET", body: bytes | None = None,
                   timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Public alias — tests monkey-patch this
fetch = _fetch_default  # noqa: N816


# Optional override: a caller (research_discovery) can register its own fetch
# so existing test seams that monkey-patch `disc.fetch` keep working. Ponytail
# choice: reuse the caller's seam rather than forcing every test to also patch
# research_search.fetch.
_caller_fetch = None  # set via set_caller_fetch()


def set_caller_fetch(fn):
    """Wire in research_discovery.fetch. Returned by _active_fetch()."""
    global _caller_fetch  # noqa: PLW0603
    _caller_fetch = fn


def _active_fetch():
    """Caller-registered fetch wins over module default when present."""
    if _caller_fetch is not None:
        return _caller_fetch
    return fetch


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------
# Overridable for tests
_linkup_key_path: str = os.path.join(_HOME, ".openclaw", "secrets", "linkup-key")
_factcheck_env_path: str = "/home/openclaw/factcheck-engine/.env"


# Brave key reader is provided by research_discovery (env > openclaw.json).
# It's imported lazily so this module stays standalone-importable for tests.
_brave_key_reader = None  # set via set_brave_key_reader()


def set_brave_key_reader(fn):
    """Wire in research_discovery._read_brave_key. Lazy to avoid an import cycle."""
    global _brave_key_reader  # noqa: PLW0603
    _brave_key_reader = fn


def _read_brave_key() -> str:
    if _brave_key_reader is not None:
        return _brave_key_reader()
    return os.environ.get("BRAVE_API_KEY", "")


_ENV_ASSIGN_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def _read_linkup_key_from_factcheck_env() -> str:
    """Best-effort scrape of FC_LINKUP_API_KEY from the factcheck .env file."""
    try:
        with open(_factcheck_env_path) as f:
            for line in f:
                m = _ENV_ASSIGN_RE.match(line)
                if not m:
                    continue
                key, val = m.group(1), m.group(2)
                if key == "FC_LINKUP_API_KEY":
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _read_linkup_key() -> str:
    """LINKUP_API_KEY env > secrets file > factcheck .env scrape > empty."""
    key = os.environ.get("LINKUP_API_KEY", "")
    if key:
        return key
    try:
        key = open(_linkup_key_path).read().strip()
        if key:
            return key
    except OSError:
        pass
    return _read_linkup_key_from_factcheck_env()


# ---------------------------------------------------------------------------
# HTTP error detection
# ---------------------------------------------------------------------------
class _QuotaOrNetwork(Exception):
    """Raised internally when Brave should fall through to Linkup."""


# Map urllib's HTTPError to a friendly status; only quota/rate-limit/5xx
# trigger fallback (see _is_fallback_worthy below).
def _http_status_from_error(exc: Exception) -> int | None:
    """Return HTTP status if exc is urllib's HTTPError, else None."""
    return getattr(exc, "code", None)


def _is_fallback_worthy(exc: Exception) -> bool:
    """Brave hit a quota / rate-limit error worth a Linkup try.

    Network errors (URLError / OSError / TimeoutError / JSON decode) intentionally
    do NOT trigger fallback: those usually mean our own box is offline, in which
    case we'd just waste a Linkup call. Keep fallback narrow to "Brave says no".
    """
    status = _http_status_from_error(exc)
    if status in (402, 429):
        return True
    # 5xx = Brave is sick; one provider fallback is cheap, do it
    if status is not None and 500 <= status < 600:
        return True
    return False


# Import urllib.error lazily for the check above (kept local for clarity)
import urllib.error  # noqa: E402


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------
_BRAVE_BASE = "https://api.search.brave.com/res/v1/web/search"
_LINKUP_BASE = "https://api.linkup.so/v1/search"


def _call_brave(query: str, count: int, recency: str) -> list[dict]:
    """Return Brave's web.results list, or raise _QuotaOrNetwork on fallback-worthy error."""
    api_key = _read_brave_key()
    if not api_key:
        # No key is not a "quota" — caller treats it the same as empty result
        return []
    params = urllib.parse.urlencode({"q": query, "count": count, "freshness": recency})
    url = f"{_BRAVE_BASE}?{params}"
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    try:
        raw = _active_fetch()(url, headers=headers)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        if _is_fallback_worthy(exc):
            status = _http_status_from_error(exc)
            log.warning(
                "brave search failed (status=%s err=%s); falling back to linkup for %r",
                status, exc, query,
            )
            raise _QuotaOrNetwork(exc) from exc
        log.warning("brave search non-fallback error for %r: %s", query, exc)
        return []
    return data.get("web", {}).get("results", [])


def _call_linkup(query: str, count: int) -> list[dict]:
    """Call Linkup, normalize to Brave's web-results shape, return list."""
    api_key = _read_linkup_key()
    if not api_key:
        log.error("linkup key unavailable; cannot fall back")
        return []
    # body + endpoint copied from factcheck-engine/engine/search.py:linkup_search
    body = json.dumps({
        "q": query,
        "depth": "standard",
        "outputType": "searchResults",
    }).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        raw = _active_fetch()(_LINKUP_BASE, headers=headers, method="POST", body=body)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("linkup search failed for %r: %s", query, exc)
        return []
    results = data.get("results", []) or []
    normalized: list[dict] = []
    for w in results:
        # Skip non-text entries (matches factcheck filter w.get("type") != "text")
        if w.get("type") and w.get("type") != "text":
            continue
        normalized.append({
            "url": w.get("url", "") or "",
            "title": w.get("name", "") or "",
            "description": (w.get("content", "") or "")[:1500],
            # Linkup doesn't expose age — leave None so callers don't fake a ts
            "age": None,
            "page_age": None,
            "_provider": "linkup",
        })
        if len(normalized) >= count:
            break
    return normalized


# ---------------------------------------------------------------------------
# Public: web_search
# ---------------------------------------------------------------------------
def web_search(query: str, *, count: int = 10, recency: str = "pw") -> list[dict]:
    """Brave first, Linkup on quota/network. Returns Brave-shaped result dicts.

    Result dict shape (matches what research_discovery already consumes):
        {"url", "title", "description", "age"|"page_age"|None, "_provider" (optional)}
    """
    try:
        results = _call_brave(query, count=count, recency=recency)
        return results
    except _QuotaOrNetwork:
        pass
    return _call_linkup(query, count=count)