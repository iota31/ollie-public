#!/usr/bin/env python3
"""research_fourdpocket.py — stdlib client for OLLIE's 4DPocket account.

4DPocket is the ingestion + storage + extraction + RSS spine for the Curiosity
Engine (owner-approved re-architecture 2026-06-14). It already does scheduled
RSS/Atom polling, URL ingest + trafilatura extraction, URL-dedup and hybrid
search, so the engine's own pollers/storage are redundant. This module is the
ONLY seam the engine uses to talk to 4DPocket.

Verified read-only against the box 2026-06-14:
  * Base http://<TAILSCALE_IP_VPS>:4040/api/v1 ; **Host header "localhost:4040"** is
    MANDATORY (the proxy returns 421 without it).
  * Bearer PAT from /home/openclaw/.openclaw/secrets/fourdpocket-ollie.pat.
  * GET /collections, /rss, /search all return TOP-LEVEL JSON ARRAYS (not
    {"items": [...]}). The client tolerates both shapes anyway.
  * Search item dicts carry id, item_type, source_platform, url, title,
    description, content, created_at, updated_at, enrichment_status, tags.

NOT exercisable read-only (assumed from the 4DPocket team's spec — flagged for
the coordinator's live deploy): POST /items (201 + {id}; 409 on dup),
POST /collections, POST /collections/{cid}/items, POST /rss, 429 behaviour.

Design rules (match the rest of ollie-research / ollie-jobs):
  * Pure Python 3.12 stdlib (urllib). No pip deps.
  * GUARDED: every call degrades to a sentinel (None / []) and logs; it NEVER
    raises to the caller. A broken 4DPocket must never crash a poll cycle.
  * Atomic writes (tmp + os.replace) for the small cid state cache.
  * Testable: HOME via OLLIE_HOME, paths resolved fresh through _paths(); the
    low-level HTTP seam `http` and `_read_pat` are module-level + overridable so
    tests MOCK all network. BASE / PAT_PATH overridable too.

Public API:
  ensure_collection(name="curiosity-feed") -> cid | None
  ingest_url(url, source_platform, cid, title=None, content=None) -> item_id|None
  register_feed(url, category, target_collection_id, poll_interval=3600,
                mode="auto", filters=None) -> feed_id | "exists" | None  (idempotent)
  list_feeds() -> [feed dicts]
  search_recent(after_iso, query="", source_platform=None, limit=50) -> [item dicts]
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# --- runtime config (module globals; _paths() reads them fresh so tests can
#     reassign HOME/WORKSPACE/LOGS before a call) ---
HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
WORKSPACE = f"{HOME}/.openclaw/workspace"
LOGS = f"{HOME}/.openclaw/logs"

# Overridable for tests / future re-homing.
BASE = "http://<TAILSCALE_IP_VPS>:4040/api/v1"
HOST_HEADER = "localhost:4040"          # MANDATORY — proxy 421s without it
PAT_PATH = f"{HOME}/.openclaw/secrets/fourdpocket-ollie.pat"

COLLECTION_NAME = "curiosity-feed"      # where every engine-sourced item is filed
RATE_LIMIT_BACKOFF_S = 60.0             # after a 429, pause all calls this long
# /items enforces enums (verified live 2026-06-14 — a 422 taught us):
#   item_type   in {url, note, image, pdf, code_snippet, video}  -> "url" for our URLs
#   source_platform in {youtube, instagram, reddit, twitter, threads, tiktok,
#                       github, hackernews, stackoverflow, ...}  -> OMIT if not one of
# these (it's a constrained enum, NOT a free-form source label). Engine provenance
# lives in the curiosity-feed collection, not source_platform.
DEFAULT_ITEM_TYPE = "url"
VALID_SOURCE_PLATFORMS = {
    "youtube", "instagram", "reddit", "twitter", "threads", "tiktok",
    "github", "hackernews", "stackoverflow", "substack", "medium",
}
HTTP_TIMEOUT = 30

# Module-level 429 backoff guard (monotonic deadline; shared across calls).
_backoff_until = 0.0


def _paths():
    research = f"{WORKSPACE}/research"
    return {
        "research": research,
        "state": f"{research}/fourdpocket.json",
        "log": f"{LOGS}/research-fourdpocket.log",
    }


# ----------------------------------------------------------------------------
# logging + atomic json io (guarded — never raise)
# ----------------------------------------------------------------------------
def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} fourdpocket {msg}"
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_state():
    try:
        with open(_paths()["state"]) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt -> empty
        return {}


def _write_state(obj):
    path = _paths()["state"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"state write failed {path}: {e}")
        return False


def _cache_collection(name, cid):
    if not cid:
        return
    state = _read_state()
    cols = state.get("collections")
    if not isinstance(cols, dict):
        cols = {}
    cols[name] = cid
    state["collections"] = cols
    _write_state(state)


# ----------------------------------------------------------------------------
# PAT + low-level HTTP (the mock seams)
# ----------------------------------------------------------------------------
def _read_pat():
    """Read the Bearer PAT. Overridable in tests. '' on failure (caller skips)."""
    try:
        return open(PAT_PATH).read().strip()
    except Exception as e:  # noqa: BLE001
        _log(f"PAT unreadable from {PAT_PATH}: {e}")
        return ""


def _http_default(method, url, headers=None, body=None, timeout=HTTP_TIMEOUT):
    """One HTTP round-trip -> (status_code:int, raw:bytes). HTTPError is caught
    so non-2xx (409/429/4xx/5xx) returns its code+body rather than raising;
    other errors (URLError/timeout) DO raise and are caught one level up in
    _req. This is the seam tests monkey-patch (research_fourdpocket.http = ...)."""
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", resp.getcode()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# Public alias — tests monkey-patch this.
http = _http_default  # noqa: N816


def _req(method, path, body=None):
    """Authenticated JSON request -> (status_code|None, parsed_json|None).

    Adds the mandatory Host header + Bearer PAT, JSON-encodes a body, parses a
    JSON response. NEVER raises:
      * no PAT / network error / timeout / bad JSON  -> (None, None)
      * 429 (rate limit)                             -> arm backoff, (429, None)
      * inside an active backoff window              -> (429, None) without a call
      * any other status                             -> (status, parsed_or_None)
    """
    global _backoff_until  # noqa: PLW0603
    now = time.monotonic()
    if now < _backoff_until:
        _log(f"in 429 backoff window ({_backoff_until - now:.0f}s left) -> skip {method} {path}")
        return (429, None)

    pat = _read_pat()
    if not pat:
        _log(f"no PAT -> skip {method} {path}")
        return (None, None)

    url = f"{BASE}{path}"
    headers = {
        "Host": HOST_HEADER,
        "Authorization": f"Bearer {pat}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    try:
        status, raw = http(method, url, headers=headers, body=data, timeout=HTTP_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — network/timeout/etc -> sentinel
        _log(f"{method} {path} request failed: {e}")
        return (None, None)

    if status == 429:
        _backoff_until = time.monotonic() + RATE_LIMIT_BACKOFF_S
        _log(f"429 rate-limited on {method} {path} -> backoff {RATE_LIMIT_BACKOFF_S:.0f}s")
        return (429, None)

    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
    return (status, parsed)


def _as_list(parsed, *dict_keys):
    """Normalize a response that may be a top-level array OR a dict wrapping a
    list under one of dict_keys (e.g. 'items'/'feeds'/'collections')."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in dict_keys:
            v = parsed.get(k)
            if isinstance(v, list):
                return v
    return []


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def ensure_collection(name=COLLECTION_NAME):
    """Resolve the collection id for `name`, creating it if absent. Caches the
    cid in the state file so steady-state needs zero round-trips. None on
    failure (caller still ingests; filing into a collection is best-effort)."""
    cached = _read_state().get("collections", {})
    if isinstance(cached, dict) and cached.get(name):
        return cached[name]

    status, parsed = _req("GET", "/collections")
    for c in _as_list(parsed, "collections"):
        if isinstance(c, dict) and c.get("name") == name and c.get("id"):
            _cache_collection(name, c["id"])
            _log(f"ensure_collection: found existing {name!r} -> {c['id']}")
            return c["id"]

    # Not found -> create. ItemCreate-style; name is the only required field.
    status, parsed = _req("POST", "/collections", {"name": name})
    if isinstance(parsed, dict) and parsed.get("id"):
        _cache_collection(name, parsed["id"])
        _log(f"ensure_collection: created {name!r} -> {parsed['id']}")
        return parsed["id"]
    _log(f"ensure_collection: could NOT resolve/create {name!r} (status={status})")
    return None


def ingest_url(url, source_platform, cid, title=None, content=None):
    """POST /items {url, ...}; on 201 file it into the collection. Returns the
    new item_id, or None on a 409 (already saved -> skip) / any failure.

    409 is the 4DPocket (user,url) dedup and is treated as a NORMAL skip — the
    item is already in our pocket, nothing to do."""
    if not url:
        return None
    body = {"url": url, "item_type": DEFAULT_ITEM_TYPE}
    # source_platform is a constrained enum — only send it if valid, else OMIT
    # (sending "discovery"/etc. -> 422). Let 4DPocket auto-detect otherwise.
    if source_platform and source_platform in VALID_SOURCE_PLATFORMS:
        body["source_platform"] = source_platform
    if title:
        body["title"] = title
    if content:
        body["content"] = content

    status, parsed = _req("POST", "/items", body)
    if status == 409:
        _log(f"ingest_url: already saved (409) -> skip {url}")
        return None
    if status in (200, 201) and isinstance(parsed, dict) and parsed.get("id"):
        item_id = parsed["id"]
        # ItemCreate does NOT take a collection -> file it after create.
        if cid:
            # /collections/{cid}/items wants {"item_ids":[...]} (a LIST) — verified
            # live; {"item_id": x} 422s. Returns 201 {"added":[...]}.
            f_status, _ = _req("POST", f"/collections/{cid}/items", {"item_ids": [item_id]})
            if f_status not in (200, 201, 204):
                _log(f"ingest_url: filed item {item_id} but collection add status={f_status}")
        _log(f"ingest_url: ingested {url} -> {item_id}")
        return item_id
    _log(f"ingest_url: failed {url} (status={status})")
    return None


def register_feed(url, category, target_collection_id, poll_interval=3600,
                  mode="auto", filters=None):
    """Idempotently register an RSS feed in 4DPocket. GET /rss first; if `url`
    is already registered, SKIP the POST and return the existing feed id.
    Returns a new feed id on creation, the existing id when already present, or
    None on failure. poll_interval is clamped to the documented 300s minimum."""
    if not url:
        return None
    status, parsed = _req("GET", "/rss")
    for f in _as_list(parsed, "feeds", "rss"):
        if isinstance(f, dict) and f.get("url") == url:
            _log(f"register_feed: {url} already registered -> skip")
            return f.get("id") or "exists"

    body = {
        "url": url,
        "category": category,
        "target_collection_id": target_collection_id,
        "poll_interval": max(300, int(poll_interval or 3600)),
        "format": "rss",
        "mode": mode,
    }
    if filters:
        body["filters"] = filters
    status, parsed = _req("POST", "/rss", body)
    if isinstance(parsed, dict) and parsed.get("id"):
        _log(f"register_feed: registered {url} -> {parsed['id']}")
        return parsed["id"]
    _log(f"register_feed: failed to register {url} (status={status})")
    return None


def list_feeds():
    """Return the list of registered RSS feed dicts ([] on failure)."""
    status, parsed = _req("GET", "/rss")
    return _as_list(parsed, "feeds", "rss")


def search_recent(after_iso, query="", source_platform=None, limit=50, collection_id=None):
    """Recent items (created_at desc), filtered to after_iso client-side.

    IMPORTANT (verified live 2026-06-14): GET /search REQUIRES a non-empty `q`
    (422 on empty), so it is NOT the right endpoint for "give me all recent
    items" — the queue wants everything fresh, not query-matched. We use
    GET /items?sort_by=created_at&sort_order=desc (which has no server-side
    date filter) and apply the after_iso recency bound client-side. If a `query`
    IS given, we use /search?q=<query> instead (real keyword/semantic search).

    after_iso: ISO-8601 lower bound on created_at (None = no recency bound).
    query:     non-empty -> /search?q=; empty -> recent /items listing.
    Returns the list of item dicts ([] on failure)."""
    lim = min(100, max(1, int(limit or 50)))
    if query:
        params = {"q": query, "limit": lim}
        if after_iso:
            params["after"] = after_iso
        if source_platform:
            params["source_platform"] = source_platform
        _, parsed = _req("GET", f"/search?{urllib.parse.urlencode(params)}")
        items = _as_list(parsed, "items", "results")
    elif collection_id:
        # scope to the curiosity-feed collection — engine-sourced items only,
        # NOT Ollie's whole account (which holds unrelated fact-check reels etc.)
        params = {"limit": lim}
        _, parsed = _req("GET", f"/collections/{collection_id}/items?{urllib.parse.urlencode(params)}")
        items = _as_list(parsed, "items", "results")
    else:
        params = {"sort_by": "created_at", "sort_order": "desc", "limit": lim}
        if source_platform:
            params["source_platform"] = source_platform
        _, parsed = _req("GET", f"/items?{urllib.parse.urlencode(params)}")
        items = _as_list(parsed, "items", "results")
    # client-side recency bound (no server-side `after` on /items)
    if after_iso and not query:
        cutoff = _parse_iso_epoch(after_iso)
        if cutoff is not None:
            # keep items whose created_at is unknown/unparseable (don't drop on
            # ambiguity — the gate treats unknown ts as neutral); drop only the
            # ones provably older than the cutoff.
            kept = []
            for it in items:
                ep = _parse_iso_epoch(it.get("created_at"))
                if ep is None or ep >= cutoff:
                    kept.append(it)
            items = kept
    return items


def _parse_iso_epoch(s):
    """ISO-8601 (tolerate trailing Z) -> epoch float, or None."""
    if not s:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# CLI smoke-test (read-only)
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    cid = ensure_collection()
    print(f"collection {COLLECTION_NAME} -> {cid}", file=sys.stderr)
    feeds = list_feeds()
    print(f"feeds: {len(feeds)}", file=sys.stderr)
    recent = search_recent(after_iso=None, query="", limit=3)
    print(json.dumps([{"id": i.get("id"), "title": i.get("title")} for i in recent], indent=2))
