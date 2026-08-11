#!/usr/bin/env python3
"""Offline unit tests for research_feeds_sync.py.

feed_url_for is pure (tested directly). sync() is tested with a stubbed
research_fourdpocket injected via sys.modules — no network. Paths repointed to a
tempdir. Run: python3 -m unittest tests.test_feeds_sync
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

import research_feeds_sync as fs  # noqa: E402


class FakeFDP:
    def __init__(self):
        self.registered = []   # urls passed to register_feed
        self._existing = set()

    def ensure_collection(self, name="curiosity-feed"):
        return "cid-1"

    def register_feed(self, url, category, target_collection_id,
                      poll_interval=3600, mode="auto", filters=None):
        self.registered.append({"url": url, "category": category,
                                "cid": target_collection_id})
        if url in self._existing:
            return "exists"
        self._existing.add(url)
        return f"feed-{len(self._existing)}"


class TestFeedUrlFor(unittest.TestCase):
    def test_rss_as_is(self):
        url, _ = fs.feed_url_for({"type": "rss", "target": "https://blog/atom.xml"})
        self.assertEqual(url, "https://blog/atom.xml")

    def test_reddit_to_rss(self):
        url, reason = fs.feed_url_for({"type": "reddit", "target": "LocalLLaMA"})
        self.assertEqual(url, "https://www.reddit.com/r/LocalLLaMA/.rss")
        self.assertIn("reddit", reason)

    def test_reddit_strips_r_prefix(self):
        url, _ = fs.feed_url_for({"type": "reddit", "target": "r/MachineLearning"})
        self.assertEqual(url, "https://www.reddit.com/r/MachineLearning/.rss")

    def test_x_without_rss_skipped(self):
        url, reason = fs.feed_url_for({"type": "x", "target": "https://x.com/simonw"})
        self.assertIsNone(url)
        self.assertIn("no RSS", reason)

    def test_x_with_explicit_rss_url(self):
        url, _ = fs.feed_url_for({"type": "x", "target": "https://x.com/simonw",
                                  "rss_url": "https://nitter/simonw/rss"})
        self.assertEqual(url, "https://nitter/simonw/rss")

    def test_x_with_feed_looking_target(self):
        url, _ = fs.feed_url_for({"type": "x", "target": "https://example.com/u.rss"})
        self.assertEqual(url, "https://example.com/u.rss")

    def test_instagram_skipped(self):
        url, reason = fs.feed_url_for({"type": "instagram", "target": "https://ig/x"})
        self.assertIsNone(url)
        self.assertIn("instagram", reason)

    def test_discovery_skipped(self):
        url, reason = fs.feed_url_for({"type": "discovery", "target": "query"})
        self.assertIsNone(url)
        self.assertIn("discovery", reason)


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fs.WORKSPACE = f"{self.tmp}/ws"
        fs.LOGS = f"{self.tmp}/logs"
        os.makedirs(f"{fs.WORKSPACE}/research", exist_ok=True)
        self.fake = FakeFDP()
        sys.modules["research_fourdpocket"] = self.fake

    def tearDown(self):
        sys.modules.pop("research_fourdpocket", None)

    def test_registers_enabled_rss_and_reddit_only(self):
        sources = [
            {"id": "r1", "type": "rss", "target": "https://a/feed.xml", "enabled": True},
            {"id": "sub1", "type": "reddit", "target": "LocalLLaMA", "enabled": True},
            {"id": "x1", "type": "x", "target": "https://x.com/u", "enabled": True},
            {"id": "ig1", "type": "instagram", "target": "https://ig/u", "enabled": True},
            {"id": "d1", "type": "discovery", "target": "q", "enabled": True},
            {"id": "off", "type": "rss", "target": "https://b/feed.xml", "enabled": False},
        ]
        summary = fs.sync(sources)
        self.assertEqual(summary["registered"], 2)       # rss + reddit only
        self.assertEqual(summary["skipped"], 4)          # x, ig, discovery, disabled
        urls = [r["url"] for r in self.fake.registered]
        self.assertIn("https://a/feed.xml", urls)
        self.assertIn("https://www.reddit.com/r/LocalLLaMA/.rss", urls)

    def test_idempotent_dup_skip(self):
        sources = [{"id": "r1", "type": "rss", "target": "https://a/feed.xml",
                    "enabled": True}]
        first = fs.sync(sources)
        self.assertEqual(first["registered"], 1)
        # second run: register_feed returns "exists" (truthy) -> still counts as ok,
        # but the underlying client made no duplicate POST (verified in the client tests)
        second = fs.sync(sources)
        self.assertEqual(second["registered"], 1)
        self.assertEqual(len(self.fake.registered), 2)   # both calls reached the client
        # the client only actually created ONE feed (the dup returned "exists")
        self.assertEqual(len(self.fake._existing), 1)

    def test_no_client_aborts_cleanly(self):
        sys.modules.pop("research_fourdpocket", None)
        orig = fs._fourdpocket
        fs._fourdpocket = lambda: None
        try:
            summary = fs.sync([{"id": "r1", "type": "rss", "target": "u", "enabled": True}])
            self.assertEqual(summary["registered"], 0)
        finally:
            fs._fourdpocket = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
