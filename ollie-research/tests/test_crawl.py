#!/usr/bin/env python3
"""Offline tests for research_crawl. stdlib unittest only.

crawl4ai is NOT installed locally and is NEVER imported here — the single
import seam research_crawl._import_crawl4ai() is patched to inject fakes.
These tests must pass on any python 3.12 without crawl4ai/playwright present.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

# Make the package dir importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import research_crawl  # noqa: E402


# ---- fakes that mimic the crawl4ai surface we use ------------------------

class _FakeMarkdown:
    def __init__(self, fit=None, raw=None):
        if fit is not None:
            self.fit_markdown = fit
        if raw is not None:
            self.raw_markdown = raw


class _FakeResult:
    def __init__(self, markdown=None, success=True, error_message=""):
        self.markdown = markdown
        self.success = success
        self.error_message = error_message


class _FakeCfg:
    """Stands in for BrowserConfig/CrawlerRunConfig — accepts any kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_crawler_class(arun_impl):
    """Build a fake AsyncWebCrawler class whose arun() uses arun_impl(url)."""

    class _FakeCrawler:
        last_browser_cfg = None

        def __init__(self, config=None):
            type(self).last_browser_cfg = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def arun(self, url=None, config=None):
            return await arun_impl(url)

    return _FakeCrawler


def _syms_with(crawler_cls):
    return {
        "AsyncWebCrawler": crawler_cls,
        "BrowserConfig": _FakeCfg,
        "CrawlerRunConfig": _FakeCfg,
        "CacheMode": mock.Mock(BYPASS="bypass"),
    }


class CrawlTests(unittest.TestCase):

    def setUp(self):
        # Redirect logs into a temp HOME so we never touch ~/.openclaw.
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"OLLIE_HOME": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    # -- happy path: fit_markdown preferred --------------------------------

    def test_fetch_returns_fit_markdown(self):
        async def arun(url):
            return _FakeResult(_FakeMarkdown(fit="# clean fit", raw="# raw"))

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertEqual(out, "# clean fit")

    def test_fetch_falls_back_to_raw_markdown(self):
        async def arun(url):
            return _FakeResult(_FakeMarkdown(raw="# only raw"))

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertEqual(out, "# only raw")

    def test_fetch_handles_plain_string_markdown(self):
        async def arun(url):
            return _FakeResult("# legacy string markdown")

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertEqual(out, "# legacy string markdown")

    # -- stealth flag is requested -----------------------------------------

    def test_browser_config_requests_stealth(self):
        async def arun(url):
            return _FakeResult(_FakeMarkdown(fit="ok"))

        crawler_cls = _make_crawler_class(arun)
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(crawler_cls),
        ):
            research_crawl.fetch("https://example.com")
        cfg = crawler_cls.last_browser_cfg
        self.assertTrue(cfg.kwargs.get("enable_stealth"))
        self.assertTrue(cfg.kwargs.get("headless"))

    # -- failure modes degrade to None, never raise ------------------------

    def test_import_error_returns_none(self):
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            side_effect=ImportError("no module named crawl4ai"),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertIsNone(out)

    def test_arun_exception_returns_none(self):
        async def arun(url):
            raise RuntimeError("network boom")

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertIsNone(out)

    def test_unsuccessful_result_returns_none(self):
        async def arun(url):
            return _FakeResult(None, success=False, error_message="403")

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertIsNone(out)

    def test_empty_markdown_returns_none(self):
        async def arun(url):
            return _FakeResult(_FakeMarkdown(fit="   "))

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertIsNone(out)

    def test_failure_is_logged(self):
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            side_effect=ImportError("nope"),
        ):
            research_crawl.fetch("https://example.com")
        log = research_crawl._log_path()
        self.assertTrue(os.path.exists(log))
        with open(log, encoding="utf-8") as fh:
            self.assertIn("not importable", fh.read())

    # -- size cap ----------------------------------------------------------

    def test_size_cap_enforced(self):
        big = "x" * (research_crawl.MAX_CHARS + 5000)

        async def arun(url):
            return _FakeResult(_FakeMarkdown(fit=big))

        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch("https://example.com")
        self.assertEqual(len(out), research_crawl.MAX_CHARS)

    # -- fetch_many ---------------------------------------------------------

    def test_fetch_many_returns_per_url(self):
        async def arun(url):
            return _FakeResult(_FakeMarkdown(fit=f"md::{url}"))

        urls = ["https://a.com", "https://b.com", "https://c.com"]
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch_many(urls)
        self.assertEqual(out, {u: f"md::{u}" for u in urls})

    def test_fetch_many_import_error_all_none(self):
        urls = ["https://a.com", "https://b.com"]
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            side_effect=ImportError("nope"),
        ):
            out = research_crawl.fetch_many(urls)
        self.assertEqual(out, {u: None for u in urls})

    def test_fetch_many_concurrency_cap_respected(self):
        # Track peak in-flight arun() calls; with the semaphore it must never
        # exceed the clamped cap (2), even when caller asks for more.
        state = {"cur": 0, "peak": 0}
        lock = asyncio.Lock()

        async def arun(url):
            async with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.02)
            async with lock:
                state["cur"] -= 1
            return _FakeResult(_FakeMarkdown(fit=f"md::{url}"))

        urls = [f"https://x{i}.com" for i in range(6)]
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            out = research_crawl.fetch_many(urls, concurrency=10)  # asks 10
        self.assertEqual(len(out), 6)
        self.assertLessEqual(state["peak"], research_crawl.MAX_CONCURRENCY)
        self.assertGreaterEqual(state["peak"], 1)

    def test_fetch_many_clamps_low_concurrency(self):
        # concurrency=1 must serialize -> peak exactly 1.
        state = {"cur": 0, "peak": 0}
        lock = asyncio.Lock()

        async def arun(url):
            async with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.01)
            async with lock:
                state["cur"] -= 1
            return _FakeResult(_FakeMarkdown(fit="ok"))

        urls = [f"https://y{i}.com" for i in range(4)]
        with mock.patch.object(
            research_crawl, "_import_crawl4ai",
            return_value=_syms_with(_make_crawler_class(arun)),
        ):
            research_crawl.fetch_many(urls, concurrency=1)
        self.assertEqual(state["peak"], 1)

    def test_fetch_many_empty_urls(self):
        self.assertEqual(research_crawl.fetch_many([]), {})

    # -- path resolution honours OLLIE_HOME --------------------------------

    def test_log_path_uses_ollie_home(self):
        self.assertTrue(
            research_crawl._log_path().startswith(self._tmp.name)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
