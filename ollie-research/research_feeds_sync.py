#!/usr/bin/env python3
"""research_feeds_sync.py — register Ollie's curated sources as 4DPocket RSS feeds.

The re-architecture (2026-06-14) collapses the engine's RSS/Reddit POLLERS onto
4DPocket's scheduled /rss polling. This module reads sources.json and, for every
ENABLED source, IDEMPOTENTLY registers a 4DPocket feed into the curiosity-feed
collection so 4DPocket does the polling + extraction + dedup from then on:

    type rss        -> register target URL as-is
    type reddit     -> register https://www.reddit.com/r/<target>/.rss
    type x          -> register IF the source carries an RSS URL (rss_url, or a
                       target that already looks like an .rss/.xml/atom feed);
                       otherwise SKIP + log (X has no native per-user RSS)
    type instagram  -> SKIP + log (no RSS)
    type discovery  -> SKIP (handled by research_discovery.feed_discovery, not a feed)

Idempotency lives in research_fourdpocket.register_feed (it GETs /rss first and
skips if the URL is already registered), so re-running this is safe.

Pure Python 3.12 stdlib. Guarded — a bad source never aborts the sync. Logs to
~/.openclaw/logs/research-feeds-sync.log. HOME via OLLIE_HOME (testable).

Public API:
    feed_url_for(source) -> (url|None, reason)   # pure mapping (tested directly)
    sync(sources, cid=None) -> dict summary
    main(argv) -> int
"""
import importlib
import json
import os
import sys
import time

HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
WORKSPACE = f"{HOME}/.openclaw/workspace"
LOGS = f"{HOME}/.openclaw/logs"

DEFAULT_POLL_INTERVAL = 3600   # 4DPocket minimum is 300s; 1h is plenty for curation
_RSS_HINTS = (".rss", ".xml", "/rss", "/feed", "/atom", "format=rss", "format=atom")


def _paths():
    research = f"{WORKSPACE}/research"
    return {
        "research": research,
        "sources": f"{research}/sources.json",
        "log": f"{LOGS}/research-feeds-sync.log",
    }


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} feeds-sync {msg}"
    try:
        path = _paths()["log"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _looks_like_feed(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _RSS_HINTS)


def feed_url_for(source: dict):
    """Map a source dict to the RSS URL 4DPocket should poll, or None + reason
    if it can't be a feed. PURE — no network. Returns (url|None, reason)."""
    if not isinstance(source, dict):
        return None, "not a dict"
    stype = source.get("type", "")
    target = (source.get("target") or "").strip()

    if stype == "rss":
        return (target, "rss as-is") if target else (None, "rss source has no target")
    if stype == "reddit":
        if not target:
            return None, "reddit source has no target"
        sub = target.strip().lstrip("/")
        if sub.lower().startswith("r/"):
            sub = sub[2:]
        return f"https://www.reddit.com/r/{sub}/.rss", "reddit -> .rss"
    if stype == "x":
        rss_url = (source.get("rss_url") or "").strip()
        if rss_url:
            return rss_url, "x via explicit rss_url"
        if _looks_like_feed(target):
            return target, "x target looks like a feed"
        return None, "x has no RSS (no native per-user feed) -> skip"
    if stype == "instagram":
        return None, "instagram has no RSS -> skip"
    if stype == "discovery":
        return None, "discovery handled by feed_discovery (not a feed) -> skip"
    return None, f"unknown type {stype!r} -> skip"


def _fourdpocket():
    try:
        return importlib.import_module("research_fourdpocket")
    except Exception as exc:  # noqa: BLE001
        _log(f"research_fourdpocket unavailable: {exc}")
        return None


def _load_sources() -> list[dict]:
    """Registry first, then workspace sources.json, then the repo copy."""
    try:
        reg = importlib.import_module("research_registry")
        srcs = list(getattr(reg, "load_sources")() or [])
        if srcs:
            return srcs
    except Exception as exc:  # noqa: BLE001
        _log(f"research_registry.load_sources failed: {exc}")
    for path in (_paths()["sources"],
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            continue
    return []


def sync(sources, cid=None) -> dict:
    """Register every enabled source as a 4DPocket feed (idempotent). Returns a
    summary dict {registered, skipped, failed, total}. Never raises."""
    summary = {"registered": 0, "skipped": 0, "failed": 0, "total": 0}
    fdp = _fourdpocket()
    if fdp is None:
        _log("sync: no 4DPocket client -> abort")
        return summary
    if cid is None:
        try:
            cid = fdp.ensure_collection()
        except Exception as exc:  # noqa: BLE001
            _log(f"sync: ensure_collection failed: {exc}")
            cid = None

    for src in sources or []:
        if not isinstance(src, dict):
            continue
        summary["total"] += 1
        sid = src.get("id", "?")
        if not src.get("enabled", True):
            summary["skipped"] += 1
            _log(f"skip {sid}: disabled")
            continue
        url, reason = feed_url_for(src)
        if not url:
            summary["skipped"] += 1
            _log(f"skip {sid}: {reason}")
            continue
        category = sid or src.get("type", "feed")
        try:
            feed_id = fdp.register_feed(
                url, category=category, target_collection_id=cid,
                poll_interval=DEFAULT_POLL_INTERVAL, mode="auto",
            )
        except Exception as exc:  # noqa: BLE001 — one bad register never aborts the sync
            summary["failed"] += 1
            _log(f"fail {sid}: register raised {exc}")
            continue
        if feed_id:
            summary["registered"] += 1
            _log(f"ok {sid}: {reason} -> {url} (feed {feed_id})")
        else:
            summary["failed"] += 1
            _log(f"fail {sid}: register returned None for {url}")

    _log(f"sync summary: {summary}")
    return summary


def main(argv=None) -> int:
    try:
        sync(_load_sources())
    except Exception as exc:  # noqa: BLE001
        _log(f"main: sync crashed (unexpected): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
