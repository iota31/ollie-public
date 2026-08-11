#!/usr/bin/env python3
"""Offline stdlib-unittest coverage for research_gate.py.

Fully OFFLINE: the embedder and the borderline judge are injected stubs, so no
fastembed import and no Groq call ever happens. One test deliberately leaves the
embedder unresolved to exercise the lexical backstop (fastembed-unavailable).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import research_gate as gate  # noqa: E402

FIXED_NOW = 1_700_000_000.0  # deterministic "now" for all recency math


def stub_embed(texts):
    """Deterministic 3-axis stub: [interest, anti, neutral].

    - crypto/nft text         -> anti axis
    - 'borderline-marker'     -> a mid vector that lands cosine ~0.45 (in band)
    - llm/agent/on-device/mcp -> interest axis
    - everything else         -> neutral axis
    """
    out = []
    for t in texts:
        tl = t.lower()
        if "crypto" in tl or "nft" in tl:
            out.append([0.0, 1.0, 0.0])
        elif "borderline-marker" in tl:
            out.append([0.45, 0.0, 0.9])  # cosine vs [1,0,0] ~= 0.447
        elif any(k in tl for k in
                 ("llm", "agent", "on-device", "mcp", "embedding", "inference")):
            out.append([1.0, 0.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return out


INTERESTS = {
    "domains": ["local LLM", "agent infrastructure", "on-device AI", "MCP servers"],
    "keywords_boost": ["inference latency", "embedding"],
    "anti_interests": ["crypto token shilling", "NFT mint"],
    "updated_at": "2026-06-14T00:00:00",
}


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def cand(source_id, title, text, ts):
    return {
        "source_id": source_id, "source_type": "rss",
        "url": f"http://x/{title}", "title": title, "text": text, "ts": ts,
        "domain_tags": [], "fingerprint": title,
    }


class RecencyTest(unittest.TestCase):
    def test_none_ts_is_half(self):
        self.assertEqual(gate.recency_factor(None, 14, now_ts=FIXED_NOW), 0.5)

    def test_unparseable_ts_is_half(self):
        self.assertEqual(gate.recency_factor("not-a-date", 14, now_ts=FIXED_NOW), 0.5)

    def test_fresh_near_one(self):
        rf = gate.recency_factor(iso(FIXED_NOW), 14, now_ts=FIXED_NOW)
        self.assertAlmostEqual(rf, 1.0, places=2)

    def test_window_edge_near_floor(self):
        edge = FIXED_NOW - 14 * 86400 + 60  # just inside the 14d window
        rf = gate.recency_factor(iso(edge), 14, now_ts=FIXED_NOW)
        self.assertAlmostEqual(rf, gate.RECENCY_FLOOR, places=2)

    def test_stale_dropped_to_zero(self):
        old = FIXED_NOW - 20 * 86400
        self.assertEqual(gate.recency_factor(iso(old), 14, now_ts=FIXED_NOW), 0.0)

    def test_monotonic_decreasing(self):
        vals = [gate.recency_factor(iso(FIXED_NOW - d * 86400), 30, now_ts=FIXED_NOW)
                for d in range(0, 30, 3)]
        for a, b in zip(vals, vals[1:]):
            self.assertGreaterEqual(a, b)

    def test_now_ts_is_deterministic(self):
        ts = iso(FIXED_NOW - 5 * 86400)
        r1 = gate.recency_factor(ts, 14, now_ts=FIXED_NOW)
        r2 = gate.recency_factor(ts, 14, now_ts=FIXED_NOW)
        self.assertEqual(r1, r2)


class RelevanceStubTest(unittest.TestCase):
    def setUp(self):
        gate.EMBED_FN = stub_embed

    def tearDown(self):
        gate.EMBED_FN = None

    def test_on_interest_scores_high(self):
        c = cand("s", "Local LLM agent runtime", "running an on-device LLM", iso(FIXED_NOW))
        self.assertGreaterEqual(gate.relevance_score(c, INTERESTS), gate.BORDERLINE_HIGH)

    def test_off_interest_scores_low(self):
        c = cand("s", "Sourdough bread recipe", "flour water salt", iso(FIXED_NOW))
        self.assertLess(gate.relevance_score(c, INTERESTS), gate.LOW_THRESHOLD)

    def test_anti_interest_penalized(self):
        c = cand("s", "Crypto token pump", "buy this crypto coin now", iso(FIXED_NOW))
        rel = gate.relevance_score(c, INTERESTS)
        self.assertLess(rel, gate.LOW_THRESHOLD)
        # and removing anti-interests should NOT make it any lower (penalty real)
        no_anti = dict(INTERESTS, anti_interests=[])
        self.assertLessEqual(rel, gate.relevance_score(c, no_anti))


class GateFilterTest(unittest.TestCase):
    def setUp(self):
        gate.EMBED_FN = stub_embed
        self.sources = {"s": {"recency_days": 14}}

    def tearDown(self):
        gate.EMBED_FN = None
        gate.JUDGE_FN = None

    def _candidates(self):
        return [
            cand("s", "Local LLM inference", "on-device agent", iso(FIXED_NOW)),       # keep
            cand("s", "Old LLM news", "agent", iso(FIXED_NOW - 30 * 86400)),           # stale drop
            cand("s", "Knitting patterns", "yarn and needles", iso(FIXED_NOW)),        # off-interest drop
            cand("s", "borderline-marker piece", "borderline-marker", iso(FIXED_NOW)), # borderline
        ]

    def test_stale_and_off_interest_dropped_borderline_judged_false(self):
        seen = {}

        def judge(borderline, interests):
            seen["n"] = len(borderline)
            return [False] * len(borderline)  # reject borderline

        gate.JUDGE_FN = judge
        out = gate.score_and_filter(self._candidates(), INTERESTS,
                                    now_ts=FIXED_NOW, sources_by_id=self.sources)
        titles = [c["title"] for c in out]
        self.assertIn("Local LLM inference", titles)
        self.assertNotIn("Old LLM news", titles)        # stale
        self.assertNotIn("Knitting patterns", titles)   # off-interest
        self.assertNotIn("borderline-marker piece", titles)  # judge rejected
        self.assertEqual(seen["n"], 1)                  # exactly one borderline routed

    def test_borderline_kept_when_judge_accepts(self):
        gate.JUDGE_FN = lambda b, i: [True] * len(b)
        out = gate.score_and_filter(self._candidates(), INTERESTS,
                                    now_ts=FIXED_NOW, sources_by_id=self.sources)
        titles = [c["title"] for c in out]
        self.assertIn("borderline-marker piece", titles)

    def test_borderline_kept_when_no_judge(self):
        gate.JUDGE_FN = None
        out = gate.score_and_filter(self._candidates(), INTERESTS,
                                    now_ts=FIXED_NOW, sources_by_id=self.sources)
        bl = [c for c in out if c["title"] == "borderline-marker piece"]
        self.assertEqual(len(bl), 1)
        self.assertIn("kept-no-judge", bl[0]["gate_reason"])

    def test_survivors_have_added_fields(self):
        gate.JUDGE_FN = lambda b, i: [True] * len(b)
        out = gate.score_and_filter(self._candidates(), INTERESTS,
                                    now_ts=FIXED_NOW, sources_by_id=self.sources)
        for c in out:
            self.assertIn("relevance", c)
            self.assertIn("recency_factor", c)
            self.assertIn("gate_reason", c)
            self.assertTrue(0.0 <= c["relevance"] <= 1.0)
            self.assertTrue(0.0 <= c["recency_factor"] <= 1.0)

    def test_judge_exception_keeps_borderline_safely(self):
        def boom(b, i):
            raise RuntimeError("judge down")

        gate.JUDGE_FN = boom
        out = gate.score_and_filter(self._candidates(), INTERESTS,
                                    now_ts=FIXED_NOW, sources_by_id=self.sources)
        self.assertIn("borderline-marker piece", [c["title"] for c in out])


class FastembedUnavailableTest(unittest.TestCase):
    """No embedder injected and fastembed not installed -> lexical backstop,
    never crashes."""
    def setUp(self):
        gate.EMBED_FN = None
        gate._LAZY_EMBED = None
        gate._LAZY_TRIED = False

    def tearDown(self):
        gate.EMBED_FN = None
        gate._LAZY_EMBED = None
        gate._LAZY_TRIED = False

    def test_get_embedder_degrades_to_none(self):
        # fastembed is intentionally absent in the offline test venv.
        self.assertIsNone(gate._get_embedder())

    def test_lexical_backstop_ranks_and_never_crashes(self):
        on = cand("s", "local LLM agent", "running a local LLM on-device", iso(FIXED_NOW))
        off = cand("s", "garden tomatoes", "soil and sunlight", iso(FIXED_NOW))
        r_on = gate.relevance_score(on, INTERESTS)
        r_off = gate.relevance_score(off, INTERESTS)
        self.assertIsInstance(r_on, float)
        self.assertGreater(r_on, r_off)

    def test_score_and_filter_works_without_embedder(self):
        c = [cand("s", "local LLM agent infrastructure",
                  "on-device inference latency", iso(FIXED_NOW))]
        out = gate.score_and_filter(c, INTERESTS, now_ts=FIXED_NOW,
                                    sources_by_id={"s": {"recency_days": 14}})
        # must not raise; lexical backstop should keep the clearly on-topic item
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
