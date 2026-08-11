#!/usr/bin/env python3
"""tests/test_search.py — fallback logic for research_search.web_search.

Pure stdlib, no network. Verifies the Brave→Linkup fallback contract.

Run: python3 -m unittest tests.test_search
"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

import research_search  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LINKUP_RESPONSE = json.dumps({
    "results": [
        {
            "type": "text",
            "name": "Linkup Result One",
            "url": "https://example.com/one",
            "content": "First linkup answer about AI agents.",
        },
        {
            "type": "text",
            "name": "Linkup Result Two",
            "url": "https://example.com/two",
            "content": "Second linkup answer.",
        },
        {
            # Non-text type — must be filtered out (matches factcheck behavior)
            "type": "image",
            "name": "Should Be Skipped",
            "url": "https://example.com/img",
            "content": "image result",
        },
    ]
}).encode()

BRAVE_RESPONSE = json.dumps({
    "web": {
        "results": [
            {"url": "https://brave.example/a", "title": "A", "description": "a", "age": None},
            {"url": "https://brave.example/b", "title": "B", "description": "b", "age": None},
        ]
    }
}).encode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHTTPError(urllib.error.HTTPError):
    """urllib's HTTPError signature is (url, code, msg, hdrs, fp)."""
    def __init__(self, code, msg="err"):
        super().__init__("http://x", code, msg, {}, None)


def make_fetch_brave_402_then_linkup():
    """fetch routes by URL: Brave → 402, Linkup → results."""
    calls = {"brave": 0, "linkup": 0}

    def _fetch(url, headers=None, method="GET", body=None, timeout=30, **kw):
        if "api.search.brave.com" in url:
            calls["brave"] += 1
            raise _FakeHTTPError(402, "Usage limit exceeded")
        if "api.linkup.so" in url:
            calls["linkup"] += 1
            return LINKUP_RESPONSE
        raise AssertionError(f"unexpected fetch url: {url}")
    return _fetch, calls


def make_fetch_500_then_linkup():
    def _fetch(url, headers=None, method="GET", body=None, timeout=30, **kw):
        if "api.search.brave.com" in url:
            raise _FakeHTTPError(503, "Service Unavailable")
        if "api.linkup.so" in url:
            return LINKUP_RESPONSE
        raise AssertionError(f"unexpected fetch url: {url}")
    return _fetch


def make_fetch_brave_429():
    def _fetch(url, headers=None, method="GET", body=None, timeout=30, **kw):
        if "api.search.brave.com" in url:
            raise _FakeHTTPError(429, "Too Many Requests")
        if "api.linkup.so" in url:
            return LINKUP_RESPONSE
        raise AssertionError(f"unexpected fetch url: {url}")
    return _fetch


def make_fetch_always_brave():
    def _fetch(url, headers=None, method="GET", body=None, timeout=30, **kw):
        return BRAVE_RESPONSE
    return _fetch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBraveFirst(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = research_search.fetch
        self._orig_key = research_search._read_brave_key
        self._orig_linkup_key = research_search._read_linkup_key
        self._orig_caller_fetch = research_search._caller_fetch
        research_search._read_brave_key = lambda: "brave-key"
        research_search._read_linkup_key = lambda: "linkup-key"
        research_search._caller_fetch = None  # don't inherit a leftover disc.fetch

    def tearDown(self):
        research_search.fetch = self._orig_fetch
        research_search._read_brave_key = self._orig_key
        research_search._read_linkup_key = self._orig_linkup_key
        research_search._caller_fetch = self._orig_caller_fetch

    def test_brave_succeeds_no_fallback(self):
        research_search.fetch = make_fetch_always_brave()
        results = research_search.web_search("ai agents", count=10, recency="pw")
        self.assertEqual(len(results), 2)
        # Provider not set on direct Brave path; that's fine.
        self.assertEqual(results[0]["url"], "https://brave.example/a")


class TestFallback(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = research_search.fetch
        self._orig_key = research_search._read_brave_key
        self._orig_linkup_key = research_search._read_linkup_key
        self._orig_caller_fetch = research_search._caller_fetch
        research_search._read_brave_key = lambda: "brave-key"
        research_search._read_linkup_key = lambda: "linkup-key"
        research_search._caller_fetch = None  # don't inherit a leftover disc.fetch

    def tearDown(self):
        research_search.fetch = self._orig_fetch
        research_search._read_brave_key = self._orig_key
        research_search._read_linkup_key = self._orig_linkup_key
        research_search._caller_fetch = self._orig_caller_fetch

    def test_402_falls_back_to_linkup(self):
        fetch_fn, calls = make_fetch_brave_402_then_linkup()
        research_search.fetch = fetch_fn
        results = research_search.web_search("ai agents", count=10, recency="pw")
        # Linkup served results
        self.assertEqual(calls["brave"], 1, "Brave should have been called once")
        self.assertEqual(calls["linkup"], 1, "Linkup should have been called once")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://example.com/one")
        self.assertEqual(results[0]["title"], "Linkup Result One")
        # Normalized shape matches what research_discovery consumes
        for r in results:
            self.assertIn("url", r)
            self.assertIn("title", r)
            self.assertIn("description", r)
            # Linkup never produces age; research_discovery._parse_age_ts handles None
            self.assertIsNone(r.get("age"))
            self.assertIsNone(r.get("page_age"))

    def test_429_falls_back_to_linkup(self):
        research_search.fetch = make_fetch_brave_429()
        results = research_search.web_search("ai agents")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://example.com/one")

    def test_503_falls_back_to_linkup(self):
        research_search.fetch = make_fetch_500_then_linkup()
        results = research_search.web_search("ai agents")
        self.assertEqual(len(results), 2)

    def test_linkup_filters_non_text_entries(self):
        """Linkup image-type entries must be filtered (matches factcheck)."""
        fetch_fn, _ = make_fetch_brave_402_then_linkup()
        research_search.fetch = fetch_fn
        results = research_search.web_search("anything")
        urls = [r["url"] for r in results]
        self.assertNotIn("https://example.com/img", urls)

    def test_linkup_no_key_returns_empty(self):
        research_search._read_linkup_key = lambda: ""
        research_search.fetch = make_fetch_always_brave()
        # Brave OK case unaffected
        self.assertEqual(len(research_search.web_search("q")), 2)

        # Now force fallback path with no linkup key
        research_search.fetch = make_fetch_brave_402_then_linkup()[0]
        results = research_search.web_search("q")
        self.assertEqual(results, [])

    def test_linkup_request_shape(self):
        """Verify the Linkup call body matches factcheck: {q, depth, outputType}."""
        captured = {}

        def capture(url, headers=None, method="GET", body=None, timeout=30, **kw):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["method"] = method
            captured["body"] = body
            if "brave" in url:
                raise _FakeHTTPError(402)
            if "linkup" in url:
                return LINKUP_RESPONSE
            return b"{}"

        research_search.fetch = capture
        research_search.web_search("ai agents 2026", count=10)
        # Endpoint
        self.assertEqual(captured["url"], "https://api.linkup.so/v1/search")
        # Method
        self.assertEqual(captured["method"], "POST")
        # Auth
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer linkup-key")
        self.assertEqual(captured["headers"].get("Content-Type"), "application/json")
        # Body
        body = json.loads(captured["body"])
        self.assertEqual(body["q"], "ai agents 2026")
        self.assertEqual(body["depth"], "standard")
        self.assertEqual(body["outputType"], "searchResults")

    def test_network_error_does_not_trigger_fallback(self):
        """Network errors (box offline) should NOT silently hit Linkup."""
        linkup_calls = [0]

        def fetch(url, headers=None, method="GET", body=None, timeout=30, **kw):
            if "api.linkup.so" in url:
                linkup_calls[0] += 1
                return LINKUP_RESPONSE
            raise ConnectionError("box offline")
        research_search.fetch = fetch
        results = research_search.web_search("q")
        self.assertEqual(results, [])
        self.assertEqual(linkup_calls[0], 0, "Linkup must NOT be called on network error")


if __name__ == "__main__":
    unittest.main(verbosity=2)