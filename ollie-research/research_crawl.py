#!/usr/bin/env python3
"""research_crawl.py — Curiosity Engine EXTRACTION client (crawl4ai).

Thin, defensive wrapper over crawl4ai's AsyncWebCrawler. Public web only,
no login, headless Linux chromium in WSL. Runs STEALTH (never vanilla
Chrome — see doctrine): BrowserConfig(enable_stealth=True).

USAGE POLICY (ISC-24): this client is for GATE-PASSING / HIGH-VALUE items
only — the relevance+recency gate decides what gets crawled. Do NOT fan it
out across every candidate every cycle; that defeats the gate and burns the
box's RAM. Concurrency is hard-clamped to 1..2.

DESIGN:
  * crawl4ai is imported LAZILY, inside _import_crawl4ai(), so this module
    imports cleanly on ANY python (the box's stdlib-only system python, the
    test runner, etc). crawl4ai is only required at RUNTIME, on the venv
    python created by setup-venv.sh.
  * Every public call degrades to None + a log line on ImportError or any
    other failure. It never raises.

Public contract (shared across the engine):
    fetch(url, timeout=60) -> str | None
    fetch_many(urls, timeout=60, concurrency=2) -> dict[str, str | None]

crawl4ai API targeted (docs.crawl4ai.com, read 2026-06-14):
  * AsyncWebCrawler(config=BrowserConfig); `async with` ctx manager
  * await crawler.arun(url=..., config=CrawlerRunConfig)
  * BrowserConfig(headless=True, enable_stealth=True)   # stealth flag
  * CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=<ms>,
                     word_count_threshold=1)
  * result.success (bool); result.markdown.fit_markdown / .raw_markdown
"""

from __future__ import annotations

import asyncio
import os
import time

# ---- tunables -------------------------------------------------------------

# Max characters of markdown we keep per page. Curiosity items are summaries
# feeding a relevance gate + job pipeline, not archives — cap hard.
MAX_CHARS = 200_000

# Concurrency is clamped into this band regardless of caller input.
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 2

DEFAULT_TIMEOUT = 60  # seconds


# ---- paths / logging (OLLIE_HOME-overridable for testability) -------------

def _home() -> str:
    """Resolve the home dir. OLLIE_HOME wins (tests/integration set it),
    then HOME, then the box default /home/openclaw."""
    return (
        os.environ.get("OLLIE_HOME")
        or os.environ.get("HOME")
        or "/home/openclaw"
    )


def _log_path() -> str:
    return os.path.join(_home(), ".openclaw", "logs", "research-crawl.log")


def _log(msg: str) -> None:
    """Best-effort append to the research-crawl log. Never raises."""
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} research_crawl {msg}\n")
    except Exception:
        # Logging must never take down a fetch.
        pass


# ---- lazy crawl4ai import (the single import seam; mocked in tests) -------

def _import_crawl4ai() -> dict:
    """Import crawl4ai symbols lazily. Raises ImportError if crawl4ai is not
    installed (handled by callers). Tests patch THIS function to inject fakes
    without crawl4ai present."""
    from crawl4ai import (  # type: ignore
        AsyncWebCrawler,
        BrowserConfig,
        CrawlerRunConfig,
    )
    syms: dict = {
        "AsyncWebCrawler": AsyncWebCrawler,
        "BrowserConfig": BrowserConfig,
        "CrawlerRunConfig": CrawlerRunConfig,
    }
    # CacheMode lives at top level in current crawl4ai; tolerate its absence.
    try:
        from crawl4ai import CacheMode  # type: ignore
        syms["CacheMode"] = CacheMode
    except Exception:
        syms["CacheMode"] = None
    return syms


# ---- config builders (tolerant of crawl4ai version drift) -----------------

def _browser_config(syms: dict):
    BrowserConfig = syms["BrowserConfig"]
    # Doctrine: NEVER vanilla Chrome -> stealth mode on.
    try:
        return BrowserConfig(headless=True, enable_stealth=True)
    except TypeError:
        # Older/newer crawl4ai without enable_stealth kwarg: degrade but warn
        # loudly — this violates the never-vanilla-Chrome doctrine.
        _log("WARN BrowserConfig rejected enable_stealth -> running NON-STEALTH")
        return BrowserConfig(headless=True)


def _run_config(syms: dict, timeout: int):
    CrawlerRunConfig = syms["CrawlerRunConfig"]
    CacheMode = syms.get("CacheMode")
    kwargs: dict = {
        "page_timeout": int(timeout * 1000),  # crawl4ai wants milliseconds
        "word_count_threshold": 1,
    }
    if CacheMode is not None:
        kwargs["cache_mode"] = CacheMode.BYPASS
    try:
        return CrawlerRunConfig(**kwargs)
    except TypeError:
        return CrawlerRunConfig()


# ---- result extraction ----------------------------------------------------

def _cap(text: str) -> str:
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS]
    return text


def _extract_markdown(result) -> str | None:
    """Pull clean markdown from a crawl4ai CrawlResult.

    Prefers fit_markdown (content-filtered) over raw_markdown. Handles both
    the current MarkdownGenerationResult object and the legacy plain-string
    `result.markdown`. Returns None on failure/empty."""
    if result is None:
        return None
    if getattr(result, "success", True) is False:
        _log(f"crawl unsuccessful err={getattr(result, 'error_message', '?')!r}")
        return None

    md = getattr(result, "markdown", None)
    if md is None:
        return None

    # MarkdownGenerationResult -> prefer fit, fall back to raw.
    fit = getattr(md, "fit_markdown", None)
    raw = getattr(md, "raw_markdown", None)
    if fit:
        text = fit
    elif raw:
        text = raw
    elif isinstance(md, str):
        text = md
    else:
        text = str(md)

    text = (text or "").strip()
    if not text:
        return None
    return _cap(text)


# ---- async core -----------------------------------------------------------

async def _afetch_many(urls, timeout: int, concurrency: int) -> dict:
    """Crawl `urls` under one stealth browser, gated by an asyncio.Semaphore
    so no more than `concurrency` pages are in flight at once."""
    syms = _import_crawl4ai()  # ImportError handled by sync wrapper
    AsyncWebCrawler = syms["AsyncWebCrawler"]
    browser_cfg = _browser_config(syms)
    run_cfg = _run_config(syms, timeout)

    sem = asyncio.Semaphore(concurrency)
    results: dict = {u: None for u in urls}

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        async def _one(u: str) -> None:
            async with sem:
                try:
                    res = await crawler.arun(url=u, config=run_cfg)
                    results[u] = _extract_markdown(res)
                    if results[u] is None:
                        _log(f"empty markdown url={u}")
                except Exception as exc:  # noqa: BLE001 — degrade per-url
                    _log(f"arun failed url={u} err={exc!r}")
                    results[u] = None

        await asyncio.gather(*(_one(u) for u in urls))

    return results


# ---- public API -----------------------------------------------------------

def fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """Fetch one public URL and return clean markdown (fit preferred), or
    None on any failure. Never raises."""
    try:
        results = asyncio.run(_afetch_many([url], timeout, 1))
        return results.get(url)
    except ImportError as exc:
        _log(f"crawl4ai not importable (install the venv) err={exc!r} url={url}")
        return None
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch failed url={url} err={exc!r}")
        return None


def fetch_many(
    urls,
    timeout: int = DEFAULT_TIMEOUT,
    concurrency: int = MAX_CONCURRENCY,
) -> dict:
    """Fetch several public URLs concurrently (clamped to 1..2). Returns a
    dict {url: markdown|None}. Never raises; on a total failure (e.g.
    crawl4ai missing) every url maps to None."""
    urls = list(urls)
    if not urls:
        return {}
    try:
        concurrency = int(concurrency)
    except (TypeError, ValueError):
        concurrency = MAX_CONCURRENCY
    concurrency = max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, concurrency))

    try:
        return asyncio.run(_afetch_many(urls, timeout, concurrency))
    except ImportError as exc:
        _log(f"crawl4ai not importable (install the venv) err={exc!r}")
        return {u: None for u in urls}
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_many failed err={exc!r}")
        return {u: None for u in urls}


if __name__ == "__main__":
    import sys
    import json as _json

    if len(sys.argv) < 2:
        print("usage: research_crawl.py URL [URL ...]", file=sys.stderr)
        raise SystemExit(2)
    out = fetch_many(sys.argv[1:])
    print(_json.dumps({u: (None if v is None else len(v)) for u, v in out.items()}))
