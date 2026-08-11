#!/usr/bin/env python3
"""Offline unit tests for research_fourdpocket.py — the 4DPocket client.

ALL HTTP is mocked via research_fourdpocket.http (the low-level seam). The PAT
read is stubbed and paths are repointed to a tempdir, so nothing touches the
real box or the network. Run: python3 -m unittest tests.test_fourdpocket
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

import research_fourdpocket as fdp  # noqa: E402


class FakeHTTP:
    """Scripted (method, path) -> (status, json-or-bytes) responder. Records
    every call as (method, path, parsed_body_or_None)."""
    def __init__(self, routes):
        self.routes = routes          # {(method, path_prefix): (status, obj)}
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        # strip the base to get the path
        path = url[len(fdp.BASE):] if url.startswith(fdp.BASE) else url
        parsed_body = json.loads(body) if body else None
        self.calls.append((method, path, parsed_body, headers))
        for (m, prefix), (status, obj) in self.routes.items():
            if m == method and path.startswith(prefix):
                raw = b"" if obj is None else json.dumps(obj).encode()
                return status, raw
        return 404, b"{}"

    def paths_called(self):
        return [(m, p) for (m, p, _b, _h) in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fdp.WORKSPACE = f"{self.tmp}/ws"
        fdp.LOGS = f"{self.tmp}/logs"
        os.makedirs(f"{fdp.WORKSPACE}/research", exist_ok=True)
        self._orig_http = fdp.http
        self._orig_pat = fdp._read_pat
        fdp._read_pat = lambda: "test-pat-xxxx"
        fdp._backoff_until = 0.0

    def tearDown(self):
        fdp.http = self._orig_http
        fdp._read_pat = self._orig_pat
        fdp._backoff_until = 0.0


class TestHeadersAndAuth(Base):
    def test_host_header_and_bearer_present(self):
        fdp.http = FakeHTTP({("GET", "/rss"): (200, [])})
        fdp.list_feeds()
        _m, _p, _b, headers = fdp.http.calls[0]
        self.assertEqual(headers["Host"], "localhost:4040")
        self.assertEqual(headers["Authorization"], "Bearer test-pat-xxxx")

    def test_no_pat_short_circuits(self):
        fdp._read_pat = lambda: ""
        fdp.http = FakeHTTP({("GET", "/rss"): (200, [])})
        self.assertEqual(fdp.list_feeds(), [])
        self.assertEqual(fdp.http.calls, [])  # never hit the network


class TestEnsureCollection(Base):
    def test_creates_when_absent_and_caches(self):
        fdp.http = FakeHTTP({
            ("GET", "/collections"): (200, []),
            ("POST", "/collections"): (201, {"id": "cid-123", "name": "curiosity-feed"}),
        })
        cid = fdp.ensure_collection()
        self.assertEqual(cid, "cid-123")
        self.assertIn(("POST", "/collections"), fdp.http.paths_called())
        # cached -> a second call makes NO new HTTP request
        fdp.http = FakeHTTP({})
        self.assertEqual(fdp.ensure_collection(), "cid-123")
        self.assertEqual(fdp.http.calls, [])

    def test_finds_existing(self):
        fdp.http = FakeHTTP({
            ("GET", "/collections"): (200, [{"id": "cid-9", "name": "curiosity-feed"}]),
        })
        self.assertEqual(fdp.ensure_collection(), "cid-9")
        # no create POST
        self.assertNotIn(("POST", "/collections"), fdp.http.paths_called())


class TestIngestUrl(Base):
    def test_success_files_into_collection(self):
        fdp.http = FakeHTTP({
            ("POST", "/items"): (201, {"id": "item-1"}),
            ("POST", "/collections/cid-1/items"): (201, {"ok": True}),
        })
        item_id = fdp.ingest_url("https://x.com/a", "discovery", "cid-1",
                                 title="A", content=None)
        self.assertEqual(item_id, "item-1")
        called = fdp.http.paths_called()
        self.assertIn(("POST", "/items"), called)
        self.assertIn(("POST", "/collections/cid-1/items"), called)
        items_body = next(b for (m, p, b, _h) in fdp.http.calls if p == "/items")
        self.assertEqual(items_body["url"], "https://x.com/a")
        self.assertEqual(items_body["item_type"], fdp.DEFAULT_ITEM_TYPE)  # "url"
        # "discovery" is NOT a valid 4DPocket source_platform enum -> OMITTED
        # (sending it 422s; provenance lives in the curiosity-feed collection).
        self.assertNotIn("source_platform", items_body)

    def test_valid_source_platform_passed_through(self):
        fdp.http = FakeHTTP({
            ("POST", "/items"): (201, {"id": "item-2"}),
            ("POST", "/collections/cid-1/items"): (201, {"ok": True}),
        })
        fdp.ingest_url("https://github.com/x/y", "github", "cid-1")
        items_body = next(b for (m, p, b, _h) in fdp.http.calls if p == "/items")
        self.assertEqual(items_body["source_platform"], "github")  # valid enum kept

    def test_409_is_skip(self):
        fdp.http = FakeHTTP({("POST", "/items"): (409, {"detail": "exists"})})
        item_id = fdp.ingest_url("https://x.com/dup", "discovery", "cid-1")
        self.assertIsNone(item_id)
        # NO collection-add attempted on a 409
        self.assertNotIn(("POST", "/collections/cid-1/items"), fdp.http.paths_called())

    def test_content_passed_through(self):
        fdp.http = FakeHTTP({
            ("POST", "/items"): (201, {"id": "i2"}),
            ("POST", "/collections/c/items"): (204, None),
        })
        fdp.ingest_url("https://x.com/b", "discovery", "c", title="B", content="full text")
        body = next(b for (m, p, b, _h) in fdp.http.calls if p == "/items")
        self.assertEqual(body["content"], "full text")

    def test_no_url_returns_none_without_call(self):
        fdp.http = FakeHTTP({})
        self.assertIsNone(fdp.ingest_url("", "discovery", "c"))
        self.assertEqual(fdp.http.calls, [])


class TestRegisterFeed(Base):
    def test_idempotent_skips_when_present(self):
        fdp.http = FakeHTTP({
            ("GET", "/rss"): (200, [{"id": "f1", "url": "https://feed/a.xml"}]),
            ("POST", "/rss"): (201, {"id": "SHOULD_NOT_BE_CALLED"}),
        })
        fid = fdp.register_feed("https://feed/a.xml", "cat", "cid-1")
        self.assertEqual(fid, "f1")
        self.assertNotIn(("POST", "/rss"), fdp.http.paths_called())

    def test_registers_when_absent_and_clamps_interval(self):
        fdp.http = FakeHTTP({
            ("GET", "/rss"): (200, []),
            ("POST", "/rss"): (201, {"id": "f-new"}),
        })
        fid = fdp.register_feed("https://feed/b.xml", "cat", "cid-1", poll_interval=10)
        self.assertEqual(fid, "f-new")
        body = next(b for (m, p, b, _h) in fdp.http.calls if (m, p) == ("POST", "/rss"))
        self.assertEqual(body["poll_interval"], 300)  # clamped to documented minimum
        self.assertEqual(body["target_collection_id"], "cid-1")
        self.assertEqual(body["format"], "rss")


class TestSearchRecent(Base):
    # empty query -> GET /items (verified live: /search 422s on empty q).
    def test_top_level_array_from_items(self):
        fdp.http = FakeHTTP({("GET", "/items"): (200, [{"id": "a"}, {"id": "b"}])})
        items = fdp.search_recent(after_iso="2026-06-01T00:00:00+00:00", limit=50)
        # items lack created_at -> kept (don't drop on unknown date)
        self.assertEqual([i["id"] for i in items], ["a", "b"])
        _m, path, _b, _h = fdp.http.calls[0]
        self.assertIn("sort_by=created_at", path)
        self.assertIn("limit=50", path)

    def test_client_side_recency_filter(self):
        fdp.http = FakeHTTP({("GET", "/items"): (200, [
            {"id": "new", "created_at": "2026-06-10T00:00:00+00:00"},
            {"id": "old", "created_at": "2026-01-01T00:00:00+00:00"},
        ])})
        items = fdp.search_recent(after_iso="2026-06-01T00:00:00+00:00", limit=50)
        self.assertEqual([i["id"] for i in items], ["new"])  # old dropped client-side

    def test_dict_wrapped_items(self):
        fdp.http = FakeHTTP({("GET", "/items"): (200, {"items": [{"id": "z"}]})})
        items = fdp.search_recent(after_iso=None, limit=10)
        self.assertEqual([i["id"] for i in items], ["z"])

    def test_nonempty_query_uses_search(self):
        fdp.http = FakeHTTP({("GET", "/search"): (200, [{"id": "q1"}])})
        items = fdp.search_recent(after_iso=None, query="agents", limit=5)
        self.assertEqual([i["id"] for i in items], ["q1"])
        _m, path, _b, _h = fdp.http.calls[0]
        self.assertIn("q=agents", path)


class TestRateLimitBackoff(Base):
    def test_429_arms_backoff_and_short_circuits(self):
        fdp.http = FakeHTTP({("GET", "/rss"): (429, {"detail": "slow down"})})
        self.assertEqual(fdp.list_feeds(), [])      # 429 -> [] (sentinel)
        self.assertGreater(fdp._backoff_until, 0.0)  # backoff armed
        n_after_429 = len(fdp.http.calls)
        # subsequent calls short-circuit inside the backoff window (no new HTTP)
        fdp.search_recent(after_iso=None)
        fdp.ensure_collection()
        self.assertEqual(len(fdp.http.calls), n_after_429)


class TestGuardedNeverRaises(Base):
    def test_network_error_returns_sentinel(self):
        def boom(method, url, headers=None, body=None, timeout=30):
            raise ConnectionError("no route to host")
        fdp.http = boom
        self.assertEqual(fdp.list_feeds(), [])
        self.assertIsNone(fdp.ensure_collection())
        self.assertEqual(fdp.search_recent(after_iso=None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
