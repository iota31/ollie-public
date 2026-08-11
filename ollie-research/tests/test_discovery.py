#!/usr/bin/env python3
"""tests/test_discovery.py — offline unit tests for research_discovery.py.

All HTTP mocked via research_discovery.fetch.
Budget calls mocked via research_discovery._budget_check / _budget_record.
Firecrawl key reading mocked via research_discovery._read_fc_key.
Brave key reading mocked via research_discovery._read_brave_key.
Run: python3 -m unittest tests.test_discovery
"""
import json
import sys
import time
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

import research_discovery as disc  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BRAVE_RESPONSE_FULL = json.dumps({
    "web": {
        "results": [
            {
                "url": "https://techblog.com/ai-agents",
                "title": "AI Agents in 2026",
                "description": "<b>New</b> frameworks for building AI agents.",
                "age": "2026-06-10T12:00:00",
            },
            {
                "url": "https://research.ai/llm-paper",
                "title": "LLM Efficiency Paper",
                "description": "A study on LLM inference optimization.",
                "page_age": "2026-06-08T09:00:00",
            },
        ]
    }
}).encode()

BRAVE_RESPONSE_EMPTY = json.dumps({"web": {"results": []}}).encode()

FC_RESPONSE = json.dumps({
    "data": {
        "web": [
            {
                "url": "https://firecrawl-result.com/page",
                "title": "Firecrawl Result",
                "description": "Found via firecrawl.",
            }
        ]
    },
    "creditsUsed": 5,
}).encode()

FC_RESPONSE_EMPTY = json.dumps({"data": {"web": []}, "creditsUsed": 0}).encode()

SOURCE_DISCOVERY = {
    "id": "disc-ai-agents",
    "type": "discovery",
    "target": "AI agent frameworks 2026",
    "domain_tags": ["ai", "ml"],
    "weight": 1.0,
    "enabled": True,
    "recency_days": 7,
    "added_at": "2026-06-14T00:00:00",
    "escalate": False,
}

SOURCE_DISCOVERY_ESCALATE = {
    **SOURCE_DISCOVERY,
    "id": "disc-ai-escalate",
    "escalate": True,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_fetch(response: bytes):
    def _mock(url, headers=None, method="GET", body=None, timeout=30, **kw):  # noqa: ARG001
        return response
    return _mock


def make_fetch_raise(exc: Exception):
    def _mock(url, headers=None, method="GET", body=None, timeout=30, **kw):  # noqa: ARG001
        raise exc
    return _mock


class MockContext:
    """Context manager to temporarily replace module-level callables."""
    def __init__(self, module, **overrides):
        self._module = module
        self._overrides = overrides
        self._originals = {}

    def __enter__(self):
        for name, val in self._overrides.items():
            self._originals[name] = getattr(self._module, name)
            setattr(self._module, name, val)
        return self

    def __exit__(self, *_):
        for name, orig in self._originals.items():
            setattr(self._module, name, orig)


# ---------------------------------------------------------------------------
# Tests: fingerprint (re-exported)
# ---------------------------------------------------------------------------

class TestDiscoveryFingerprint(unittest.TestCase):
    def test_deterministic(self):
        fp1 = disc._fingerprint("https://x.com", "title")
        fp2 = disc._fingerprint("https://x.com", "title")
        self.assertEqual(fp1, fp2)

    def test_hex_64(self):
        self.assertEqual(len(disc._fingerprint("u", "t")), 64)


# ---------------------------------------------------------------------------
# Tests: search() via Brave
# ---------------------------------------------------------------------------

class TestSearch(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = disc.fetch
        self._orig_key = disc._read_brave_key
        disc._read_brave_key = lambda: "test-brave-key-xxx"
        # Reset rate-limit timer so tests don't sleep
        disc._last_brave_call = 0.0

    def tearDown(self):
        disc.fetch = self._orig_fetch
        disc._read_brave_key = self._orig_key
        disc._last_brave_call = 0.0

    def test_returns_candidates(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents", recency="pw", domain_tags=["ai"])
        self.assertEqual(len(cands), 2)

    def test_candidate_shape(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents", domain_tags=["ai"])
        c = cands[0]
        self.assertIn("source_id", c)
        self.assertIn("source_type", c)
        self.assertEqual(c["source_type"], "discovery")
        self.assertIn("url", c)
        self.assertIn("title", c)
        self.assertIn("text", c)
        self.assertIn("ts", c)
        self.assertIn("domain_tags", c)
        self.assertIn("fingerprint", c)

    def test_html_stripped_from_description(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents")
        self.assertNotIn("<b>", cands[0]["text"])
        self.assertIn("New", cands[0]["text"])

    def test_domain_tags_propagated(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents", domain_tags=["ai", "ml"])
        for c in cands:
            self.assertEqual(c["domain_tags"], ["ai", "ml"])

    def test_ts_parsed_from_age(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents")
        self.assertIsNotNone(cands[0]["ts"])
        self.assertIn("2026-06-10", cands[0]["ts"])

    def test_empty_brave_returns_empty(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_EMPTY)
        cands = disc.search("AI agents")
        self.assertEqual(cands, [])

    def test_fetch_error_returns_empty(self):
        disc.fetch = make_fetch_raise(ConnectionError("no network"))
        cands = disc.search("AI agents")
        self.assertEqual(cands, [])

    def test_missing_key_returns_empty(self):
        disc._read_brave_key = lambda: ""
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents")
        self.assertEqual(cands, [])

    def test_brave_auth_header_set(self):
        captured_headers = {}

        def tracking_fetch(url, headers=None, **kw):
            captured_headers.update(headers or {})
            return BRAVE_RESPONSE_FULL

        disc.fetch = tracking_fetch
        disc.search("AI agents")
        self.assertIn("X-Subscription-Token", captured_headers)
        self.assertEqual(captured_headers["X-Subscription-Token"], "test-brave-key-xxx")
        self.assertIn("Accept", captured_headers)
        self.assertEqual(captured_headers["Accept"], "application/json")

    def test_rate_limit_sleep_invoked(self):
        """Verify _BRAVE_MIN_INTERVAL enforces >= 1 s gap between calls."""
        sleep_amounts = []
        orig_sleep = time.sleep
        disc.fetch = make_fetch(BRAVE_RESPONSE_EMPTY)

        try:
            time.sleep = lambda s: sleep_amounts.append(s)  # type: ignore[method-assign]
            # Simulate last call was just now
            disc._last_brave_call = time.monotonic()
            disc.search("query A")
        finally:
            time.sleep = orig_sleep  # type: ignore[method-assign]

        # At least one sleep should have occurred >= min interval
        self.assertTrue(
            any(s >= 0.0 for s in sleep_amounts),
            "Expected time.sleep to be called when calls are rapid"
        )
        total_sleep = sum(sleep_amounts)
        # When last call was NOW, total sleep should be close to 1s
        self.assertGreaterEqual(total_sleep + 0.01, disc._BRAVE_MIN_INTERVAL - 0.1)

    def test_freshness_param_in_url(self):
        captured_urls = []

        def tracking_fetch(url, headers=None, **kw):
            captured_urls.append(url)
            return BRAVE_RESPONSE_EMPTY

        disc.fetch = tracking_fetch
        disc._last_brave_call = 0.0
        disc.search("query", recency="pd")
        self.assertTrue(any("freshness=pd" in u for u in captured_urls))

    def test_fingerprint_64_char_hex(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc.search("AI agents")
        for c in cands:
            self.assertEqual(len(c["fingerprint"]), 64)


# ---------------------------------------------------------------------------
# Tests: poll_discovery
# ---------------------------------------------------------------------------

class TestPollDiscovery(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = disc.fetch
        self._orig_key = disc._read_brave_key
        disc._read_brave_key = lambda: "test-key"
        disc._last_brave_call = 0.0

    def tearDown(self):
        disc.fetch = self._orig_fetch
        disc._read_brave_key = self._orig_key
        disc._last_brave_call = 0.0

    def test_returns_candidates_from_discovery_sources(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc._discovery_candidates([SOURCE_DISCOVERY])
        self.assertEqual(len(cands), 2)
        for c in cands:
            self.assertEqual(c["source_id"], "disc-ai-agents")

    def test_skips_non_discovery_types(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        sources = [
            {**SOURCE_DISCOVERY, "type": "rss"},
            {**SOURCE_DISCOVERY, "type": "reddit"},
            SOURCE_DISCOVERY,
        ]
        cands = disc._discovery_candidates(sources)
        # Only the discovery-type source fires
        self.assertEqual(len(cands), 2)

    def test_skips_disabled(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        cands = disc._discovery_candidates([{**SOURCE_DISCOVERY, "enabled": False}])
        self.assertEqual(cands, [])

    def test_one_bad_source_does_not_kill_batch(self):
        call_n = [0]

        def selective_fetch(url, headers=None, **kw):
            call_n[0] += 1
            if call_n[0] == 1:
                raise ConnectionError("first source fails")
            return BRAVE_RESPONSE_FULL

        disc.fetch = selective_fetch
        sources = [SOURCE_DISCOVERY, {**SOURCE_DISCOVERY, "id": "disc-second"}]
        cands = disc._discovery_candidates(sources)
        # Second source should still deliver
        self.assertGreater(len(cands), 0)

    def test_recency_days_maps_to_freshness(self):
        captured_urls = []

        def tracking_fetch(url, headers=None, **kw):
            captured_urls.append(url)
            return BRAVE_RESPONSE_EMPTY

        disc.fetch = tracking_fetch
        disc._discovery_candidates([{**SOURCE_DISCOVERY, "recency_days": 1}])
        self.assertTrue(any("freshness=pd" in u for u in captured_urls))

        captured_urls.clear()
        disc._last_brave_call = 0.0
        disc._discovery_candidates([{**SOURCE_DISCOVERY, "recency_days": 7}])
        self.assertTrue(any("freshness=pw" in u for u in captured_urls))

        captured_urls.clear()
        disc._last_brave_call = 0.0
        disc._discovery_candidates([{**SOURCE_DISCOVERY, "recency_days": 30}])
        self.assertTrue(any("freshness=pm" in u for u in captured_urls))


# ---------------------------------------------------------------------------
# Tests: Firecrawl escalation path
# ---------------------------------------------------------------------------

class TestFirecrawlEscalation(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = disc.fetch
        self._orig_brave_key = disc._read_brave_key
        self._orig_fc_key = disc._read_fc_key
        self._orig_budget_check = disc._budget_check
        self._orig_budget_record = disc._budget_record
        self._orig_log_credits = disc._log_fc_credits

        disc._read_brave_key = lambda: "brave-key"
        disc._read_fc_key = lambda: "fc-key-xxx"
        disc._last_brave_call = 0.0

        self.budget_check_calls = []
        self.budget_record_calls = []
        self.spend_log_entries = []

        disc._budget_check = lambda lane="research": self.budget_check_calls.append(lane) or True
        disc._budget_record = lambda lane="research": self.budget_record_calls.append(lane)
        disc._log_fc_credits = lambda credits, query: self.spend_log_entries.append((credits, query))

    def tearDown(self):
        disc.fetch = self._orig_fetch
        disc._read_brave_key = self._orig_brave_key
        disc._read_fc_key = self._orig_fc_key
        disc._budget_check = self._orig_budget_check
        disc._budget_record = self._orig_budget_record
        disc._log_fc_credits = self._orig_log_credits
        disc._last_brave_call = 0.0

    def _make_fetch_brave_empty_then_fc(self):
        """Returns fetch fn: Brave → empty, Firecrawl → results."""
        def _fetch(url, headers=None, method="GET", body=None, **kw):
            if "brave.com" in url:
                return BRAVE_RESPONSE_EMPTY
            if "firecrawl.dev" in url:
                return FC_RESPONSE
            return b'{}'
        return _fetch

    def test_firecrawl_called_when_brave_empty_and_escalate_true(self):
        disc.fetch = self._make_fetch_brave_empty_then_fc()
        cands = disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        fc_candidates = [c for c in cands if "firecrawl-result" in c["url"]]
        self.assertGreater(len(fc_candidates), 0)

    def test_firecrawl_not_called_when_escalate_false(self):
        called_fc = []

        def tracking_fetch(url, headers=None, method="GET", body=None, **kw):
            if "firecrawl.dev" in url:
                called_fc.append(url)
                return FC_RESPONSE
            return BRAVE_RESPONSE_EMPTY

        disc.fetch = tracking_fetch
        disc._discovery_candidates([SOURCE_DISCOVERY])  # escalate=False
        self.assertEqual(called_fc, [], "Firecrawl must NOT be called when escalate=False")

    def test_firecrawl_not_called_when_brave_has_results(self):
        called_fc = []

        def tracking_fetch(url, headers=None, method="GET", body=None, **kw):
            if "firecrawl.dev" in url:
                called_fc.append(url)
                return FC_RESPONSE
            return BRAVE_RESPONSE_FULL  # Brave has results

        disc.fetch = tracking_fetch
        disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertEqual(called_fc, [], "Firecrawl must NOT be called when Brave returns results")

    def test_budget_check_called_before_firecrawl(self):
        disc.fetch = self._make_fetch_brave_empty_then_fc()
        disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertIn("research", self.budget_check_calls)

    def test_budget_record_called_after_firecrawl(self):
        disc.fetch = self._make_fetch_brave_empty_then_fc()
        disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertIn("research", self.budget_record_calls)

    def test_credits_used_logged(self):
        disc.fetch = self._make_fetch_brave_empty_then_fc()
        disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertTrue(len(self.spend_log_entries) > 0)
        credits, query = self.spend_log_entries[0]
        self.assertEqual(credits, 5)  # FC_RESPONSE creditsUsed=5
        self.assertIsInstance(query, str)

    def test_budget_check_refused_skips_firecrawl(self):
        called_fc = []
        disc._budget_check = lambda lane="research": (self.budget_check_calls.append(lane), False)[1]

        def tracking_fetch(url, headers=None, method="GET", body=None, **kw):
            if "firecrawl.dev" in url:
                called_fc.append(url)
                return FC_RESPONSE
            return BRAVE_RESPONSE_EMPTY

        disc.fetch = tracking_fetch
        cands = disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertEqual(called_fc, [], "Firecrawl must be skipped when budget check fails")
        self.assertEqual(self.budget_record_calls, [], "budget.record must NOT be called")
        self.assertEqual(self.spend_log_entries, [], "spend log must NOT be written")
        self.assertEqual(cands, [])

    def test_firecrawl_bearer_token_in_header(self):
        captured_headers = {}

        def tracking_fetch(url, headers=None, method="GET", body=None, **kw):
            if "firecrawl.dev" in url:
                captured_headers.update(headers or {})
                return FC_RESPONSE
            return BRAVE_RESPONSE_EMPTY

        disc.fetch = tracking_fetch
        disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertIn("Authorization", captured_headers)
        self.assertTrue(captured_headers["Authorization"].startswith("Bearer fc-key-xxx"))

    def test_no_fc_key_skips_firecrawl_gracefully(self):
        disc._read_fc_key = lambda: ""
        disc.fetch = self._make_fetch_brave_empty_then_fc()
        # Should not raise; just return no candidates
        cands = disc._discovery_candidates([SOURCE_DISCOVERY_ESCALATE])
        self.assertEqual(cands, [])

    def test_fc_key_not_read_when_escalate_false(self):
        key_read_calls = []
        disc._read_fc_key = lambda: key_read_calls.append(1) or "fc-key"
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        disc._discovery_candidates([SOURCE_DISCOVERY])  # escalate=False
        self.assertEqual(key_read_calls, [], "FC key must not be read when escalate is False")


# ---------------------------------------------------------------------------
# Tests: key resolution
# ---------------------------------------------------------------------------

class TestKeyResolution(unittest.TestCase):
    def test_brave_key_from_env(self):
        import os
        orig = os.environ.get("BRAVE_API_KEY")
        try:
            os.environ["BRAVE_API_KEY"] = "env-test-key"
            # Force re-read (the real fn reads fresh each call)
            key = disc._read_brave_key()
            self.assertEqual(key, "env-test-key")
        finally:
            if orig is None:
                os.environ.pop("BRAVE_API_KEY", None)
            else:
                os.environ["BRAVE_API_KEY"] = orig

    def test_brave_key_missing_returns_empty(self):
        import os
        orig_env = os.environ.pop("BRAVE_API_KEY", None)
        orig_json = disc._OPENCLAW_JSON
        try:
            disc._OPENCLAW_JSON = "/nonexistent/openclaw.json"
            key = disc._read_brave_key()
            self.assertEqual(key, "")
        finally:
            disc._OPENCLAW_JSON = orig_json
            if orig_env is not None:
                os.environ["BRAVE_API_KEY"] = orig_env


# ---------------------------------------------------------------------------
# Tests: feed_discovery (runs queries + INGESTS into 4DPocket)
# ---------------------------------------------------------------------------

class FakeFourDPocket:
    """Stub of research_fourdpocket injected via sys.modules. Records ingests
    and simulates 4DPocket's (user,url) dedup: a re-seen url ingests as None."""
    def __init__(self, dup_urls=None):
        self.ingested = []
        self.collection_calls = 0
        self._dup = set(dup_urls or [])
        self._seen = set()

    def ensure_collection(self, name="curiosity-feed"):
        self.collection_calls += 1
        return "cid-feed"

    def ingest_url(self, url, source_platform, cid, title=None, content=None):
        self.ingested.append({"url": url, "source_platform": source_platform,
                              "cid": cid, "title": title, "content": content})
        if url in self._dup or url in self._seen:
            return None  # 409-style skip
        self._seen.add(url)
        return f"item-{len(self._seen)}"


class TestFeedDiscovery(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = disc.fetch
        self._orig_key = disc._read_brave_key
        self._orig_fc_key = disc._read_fc_key
        self._orig_bc = disc._budget_check
        self._orig_br = disc._budget_record
        self._orig_lc = disc._log_fc_credits
        disc._read_brave_key = lambda: "brave-key"
        disc._read_fc_key = lambda: "fc-key"
        disc._last_brave_call = 0.0
        self.fake = FakeFourDPocket()
        sys.modules["research_fourdpocket"] = self.fake
        # neutralize budget side effects unless a test overrides
        disc._budget_check = lambda lane="research": True
        disc._budget_record = lambda lane="research": None
        disc._log_fc_credits = lambda credits, query: None

    def tearDown(self):
        disc.fetch = self._orig_fetch
        disc._read_brave_key = self._orig_key
        disc._read_fc_key = self._orig_fc_key
        disc._budget_check = self._orig_bc
        disc._budget_record = self._orig_br
        disc._log_fc_credits = self._orig_lc
        disc._last_brave_call = 0.0
        sys.modules.pop("research_fourdpocket", None)

    def test_ingests_each_url_and_counts_new(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        n = disc.feed_discovery([SOURCE_DISCOVERY])
        self.assertEqual(n, 2)                       # 2 NEW items ingested
        self.assertEqual(len(self.fake.ingested), 2)  # ingest_url called per URL
        for rec in self.fake.ingested:
            self.assertEqual(rec["source_platform"], "discovery")
            self.assertEqual(rec["cid"], "cid-feed")

    def test_dedup_409_not_counted(self):
        disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
        self.fake = FakeFourDPocket(dup_urls=["https://techblog.com/ai-agents"])
        sys.modules["research_fourdpocket"] = self.fake
        n = disc.feed_discovery([SOURCE_DISCOVERY])
        self.assertEqual(n, 1)                        # one was a 409 skip
        self.assertEqual(len(self.fake.ingested), 2)  # both still attempted

    def test_budget_gated_firecrawl_escalation_ingests(self):
        def _fetch(url, headers=None, method="GET", body=None, **kw):
            if "brave.com" in url:
                return BRAVE_RESPONSE_EMPTY
            if "firecrawl.dev" in url:
                return FC_RESPONSE
            return b"{}"
        disc.fetch = _fetch
        bc_calls = []
        disc._budget_check = lambda lane="research": bc_calls.append(lane) or True
        n = disc.feed_discovery([SOURCE_DISCOVERY_ESCALATE])
        self.assertEqual(n, 1)                        # the firecrawl result ingested
        self.assertIn("research", bc_calls)           # budget checked before FC
        self.assertTrue(any("firecrawl-result" in r["url"] for r in self.fake.ingested))

    def test_firecrawl_blocked_by_budget_ingests_nothing(self):
        def _fetch(url, headers=None, method="GET", body=None, **kw):
            return BRAVE_RESPONSE_EMPTY
        disc.fetch = _fetch
        disc._budget_check = lambda lane="research": False
        n = disc.feed_discovery([SOURCE_DISCOVERY_ESCALATE])
        self.assertEqual(n, 0)
        self.assertEqual(self.fake.ingested, [])

    def test_no_4dpocket_client_returns_zero(self):
        sys.modules.pop("research_fourdpocket", None)
        # make import fail by pointing at a non-importable name is hard; instead
        # stub _fourdpocket to return None directly
        orig = disc._fourdpocket
        disc._fourdpocket = lambda: None
        try:
            disc.fetch = make_fetch(BRAVE_RESPONSE_FULL)
            n = disc.feed_discovery([SOURCE_DISCOVERY])
            self.assertEqual(n, 0)
        finally:
            disc._fourdpocket = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
