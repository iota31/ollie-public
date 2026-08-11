#!/usr/bin/env python3
"""Offline unit tests for the curiosity-engine queue orchestrator.

Pure stdlib unittest. NO network, NO subprocess to the real budget/job-submit:
siblings are STUBBED via sys.modules and the budget/job-submit calls are
injected. Paths are repointed to a per-test tempdir (the _paths() / module-
globals pattern), so nothing touches the real box layout.
"""
import json
import os
import sys
import tempfile
import types
import unittest

# import the module under test (parent dir of tests/)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import research_queue as rq  # noqa: E402


# --- sibling stub plumbing ---------------------------------------------------
SIBLINGS = ("research_fourdpocket", "research_gate", "research_registry")


def _install(name, **funcs):
    mod = types.ModuleType(name)
    for k, v in funcs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _clear_siblings():
    for n in SIBLINGS:
        sys.modules.pop(n, None)


def _cand(source_id, url, title="", text="", ts=None, **extra):
    c = {"source_id": source_id, "source_type": "rss", "url": url,
         "title": title, "text": text, "ts": ts, "domain": ""}
    c.update(extra)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # repoint every path root the module resolves through _paths()
        rq.WORKSPACE = f"{self.tmp}/ws"
        rq.LOGS = f"{self.tmp}/logs"
        rq.BIN = f"{self.tmp}/bin"
        rq.AUTONOMOUS_DISPATCH_PAUSE = (
            f"{self.tmp}/ws/executive/PAUSE_AUTONOMOUS_RESEARCH"
        )
        os.makedirs(f"{rq.WORKSPACE}/research", exist_ok=True)
        _clear_siblings()

    def tearDown(self):
        _clear_siblings()

    # helpers
    def read(self, key):
        return rq._read_json(rq._paths()[key], None)


# --- pure helpers ------------------------------------------------------------
class TestComputeScore(Base):
    def test_formula_is_product(self):
        self.assertAlmostEqual(rq.compute_score(0.5, 2.0, 0.5), 0.5)
        self.assertAlmostEqual(rq.compute_score(1.0, 1.0, 1.0), 1.0)
        self.assertAlmostEqual(rq.compute_score(0.8, 2.0, 1.0), 1.6)

    def test_bad_inputs_neutralize_not_crash(self):
        # non-numeric relevance/weight/recency fall back to defaults, no raise
        self.assertEqual(rq.compute_score(None, None, None),
                         rq.DEFAULT_RELEVANCE * rq.DEFAULT_WEIGHT * 1.0)


class TestRecencyFactor(Base):
    def test_decay(self):
        now = 1_000_000.0
        self.assertEqual(rq.recency_factor(now, now), 1.0)            # brand new
        one_half_life = now - rq.RECENCY_HALF_LIFE_DAYS * rq.DAY_S
        self.assertAlmostEqual(rq.recency_factor(one_half_life, now), 0.5, places=6)
        self.assertEqual(rq.recency_factor(None, now), 1.0)          # unknown -> neutral
        self.assertEqual(rq.recency_factor(now + 99999, now), 1.0)   # future clamped


class TestRerank(Base):
    def test_manual_priority_then_score(self):
        q = [
            {"fingerprint": "a", "score": 0.1, "manual_priority": None},
            {"fingerprint": "b", "score": 0.9, "manual_priority": None},
            {"fingerprint": "c", "score": 0.5, "manual_priority": 2},
            {"fingerprint": "d", "score": 0.2, "manual_priority": 1},
        ]
        order = [i["fingerprint"] for i in rq.rerank(q)]
        # manual_priority asc first (d=1, c=2), then score desc (b=0.9, a=0.1)
        self.assertEqual(order, ["d", "c", "b", "a"])


class TestDedup(Base):
    def test_drops_seen_and_intra_batch(self):
        a = _cand("s", "http://x/1")
        b = _cand("s", "http://x/2")
        dup = _cand("s", "http://x/1")  # same url -> same fingerprint as a
        seen = [rq.fingerprint(b)]
        out = rq.dedup([a, b, dup], seen)
        urls = [c["url"] for c in out]
        self.assertEqual(urls, ["http://x/1"])  # b dropped (seen), dup dropped (batch)


class TestMerge(Base):
    def test_keeps_existing_and_adds_new(self):
        existing = [{"fingerprint": "a", "status": "dispatched", "score": 0.1,
                     "manual_priority": None}]
        new = [
            {"fingerprint": "a", "status": "queued", "score": 0.9},  # collision
            {"fingerprint": "b", "status": "queued", "score": 0.5},
        ]
        merged = rq.merge_into_queue(existing, new)
        fps = {m["fingerprint"]: m for m in merged}
        self.assertEqual(len(merged), 2)
        self.assertEqual(fps["a"]["status"], "dispatched")  # existing WON
        self.assertEqual(fps["b"]["status"], "queued")      # new added


class TestItemToCandidate(Base):
    def test_maps_4dpocket_item(self):
        item = {
            "id": "abc", "source_platform": "github", "item_type": "repo",
            "url": "http://repo/x", "title": "Cool repo",
            "content": "full content body", "summary": "a summary",
            "description": "does things",
            "created_at": "2026-06-10T08:00:00+00:00",
            "tags": [{"id": 1, "name": "ai"}, {"id": 2, "name": "ml"}],
        }
        c = rq.item_to_candidate(item)
        self.assertEqual(c["source_id"], "github")       # source_platform
        self.assertEqual(c["source_type"], "repo")        # item_type
        self.assertEqual(c["url"], "http://repo/x")
        self.assertEqual(c["title"], "Cool repo")
        self.assertEqual(c["text"], "full content body")  # content preferred
        self.assertEqual(c["ts"], "2026-06-10T08:00:00+00:00")  # created_at passthrough
        self.assertEqual(c["domain_tags"], ["ai", "ml"])  # tag NAMES
        self.assertEqual(c["raw_id"], "abc")

    def test_text_falls_back_and_caps(self):
        item = {"url": "u", "summary": "x" * 3000}  # no content
        c = rq.item_to_candidate(item)
        self.assertEqual(len(c["text"]), 1500)            # capped
        self.assertEqual(c["source_id"], "4dpocket")      # no source_platform

    def test_flat_string_tags(self):
        c = rq.item_to_candidate({"url": "u", "tags": ["a", "b"]})
        self.assertEqual(c["domain_tags"], ["a", "b"])

    def test_missing_fields_default(self):
        c = rq.item_to_candidate({})
        self.assertEqual(c["url"], "")
        self.assertEqual(c["domain_tags"], [])


# --- run() end-to-end with stubbed siblings ---------------------------------
def _item(source_platform, url, title="", content="", created_at=2_000_000_000, **extra):
    """A minimal 4DPocket search item (what search_recent returns)."""
    it = {"id": url, "source_platform": source_platform, "item_type": "article",
          "url": url, "title": title, "content": content, "created_at": created_at,
          "tags": [{"id": 1, "name": "ai"}]}
    it.update(extra)
    return it


class TestRunEndToEnd(Base):
    def _stub_registry(self):
        _install("research_registry",
                 load_sources=lambda: [{"id": "s1", "weight": 2.0, "recency_days": 7}],
                 load_interests=lambda: {"topics": ["ai"]})

    def _stub_4dp(self, items):
        _install("research_fourdpocket",
                 ensure_collection=lambda name="curiosity-feed": "cid-test",
                 search_recent=lambda after_iso=None, query="", source_platform=None,
                 limit=50, collection_id=None: list(items))

    def test_ranked_queue_written(self):
        self._stub_registry()
        # 4DPocket returns two recent items: A on weighted source s1, B on s2.
        self._stub_4dp([
            _item("s1", "http://a", title="A", content="body a"),
            _item("s2", "http://b", title="B", content="body b"),
        ])
        # gate assigns relevance by url and keeps all
        rels = {"http://a": 0.8, "http://b": 0.4}
        _install("research_gate", score_and_filter=lambda c, i, now_ts=None,
                 sources_by_id=None: [dict(x, relevance=rels.get(x["url"], 0.5)) for x in c])

        ranked = rq.run(now_ts=2_000_000_000)
        self.assertEqual([i["title"] for i in ranked], ["A", "B"])  # A scores higher
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[0]["weight"], 2.0)  # source_platform s1 matched a source id
        # persisted to queue.json
        disk = self.read("queue")
        self.assertEqual([i["title"] for i in disk], ["A", "B"])
        for it in disk:
            self.assertEqual(it["status"], "queued")
            self.assertIsNone(it["manual_priority"])
            self.assertIn("added_at", it)
        # seen.json captured both fingerprints
        seen = self.read("seen")
        self.assertEqual(len(seen["fingerprints"]), 2)

    def test_maps_4dpocket_items_to_candidates(self):
        self._stub_registry()
        self._stub_4dp([_item("github", "http://saved", title="Saved",
                              content="d")])
        _install("research_gate", score_and_filter=lambda c, i, now_ts=None,
                 sources_by_id=None: [dict(x, relevance=0.6) for x in c])
        ranked = rq.run(now_ts=2_000_000_000)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["source_id"], "github")
        self.assertEqual(ranked[0]["domain_tags"], ["ai"])

    def test_raising_4dp_degrades(self):
        self._stub_registry()

        def boom(after_iso=None, query="", source_platform=None, limit=50):
            raise RuntimeError("4dpocket exploded")
        _install("research_fourdpocket", search_recent=boom)     # raises
        _install("research_gate", score_and_filter=lambda c, i, now_ts=None,
                 sources_by_id=None: list(c))
        ranked = rq.run(now_ts=2_000_000_000)  # must not raise
        self.assertEqual(ranked, [])           # degraded to empty, no crash

    def test_missing_4dp_module_degrades(self):
        self._stub_registry()
        # no research_fourdpocket installed -> _search_4dp returns [] (logged)
        _install("research_gate", score_and_filter=lambda c, i, now_ts=None,
                 sources_by_id=None: list(c))
        ranked = rq.run(now_ts=2_000_000_000)
        self.assertEqual(ranked, [])

    def test_broken_gate_passthrough_degrades(self):
        self._stub_registry()
        self._stub_4dp([_item("s1", "http://a", title="A")])
        # gate RAISES -> run() must degrade to pass-through (keep item with
        # DEFAULT_RELEVANCE), never silently empty the pipeline.
        def _boom_gate(c, i, now_ts=None, sources_by_id=None):
            raise RuntimeError("gate down")
        _install("research_gate", score_and_filter=_boom_gate)
        ranked = rq.run(now_ts=2_000_000_000)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["relevance"], rq.DEFAULT_RELEVANCE)


# --- dispatch() budget gating ------------------------------------------------
class TestDispatch(Base):
    def _seed_queue(self, n=3):
        q = [{"fingerprint": f"fp{i}", "title": f"T{i}", "url": f"http://u{i}",
              "text": "ctx", "score": 1.0 - i * 0.1, "status": "queued",
              "manual_priority": None} for i in range(n)]
        rq._write_json(rq._paths()["queue"], q)
        return q

    def test_ok_submits_records_and_marks_seen(self):
        self._seed_queue(3)
        recorded = []  # the box's job-submit.sh records spend on submit; stub it

        def check_fn(lane):
            return True, f"{lane} ok"

        def submit_fn(task, lane):
            recorded.append((lane, task))  # represents job-submit + budget record
            return True, "submitted"

        out = rq.dispatch(top_n=2, check_fn=check_fn, submit_fn=submit_fn)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(recorded), 2)           # 2 jobs submitted/recorded
        self.assertTrue(recorded[0][1].startswith("LAB RESEARCH"))  # curiosity template
        disk = self.read("queue")
        dispatched = [i for i in disk if i["status"] == "dispatched"]
        self.assertEqual(len(dispatched), 2)
        seen = self.read("seen")["fingerprints"]
        self.assertEqual(set(seen), {"fp0", "fp1"})  # top-2 fingerprints remembered

    def test_budget_refused_stops_immediately(self):
        self._seed_queue(3)
        submitted = []

        def check_fn(lane):
            return False, "research daily cap reached (6/6)"

        def submit_fn(task, lane):
            submitted.append(task)
            return True, "submitted"

        out = rq.dispatch(top_n=2, check_fn=check_fn, submit_fn=submit_fn)
        self.assertEqual(out, [])
        self.assertEqual(submitted, [])             # never submitted
        disk = self.read("queue")
        self.assertTrue(all(i["status"] == "queued" for i in disk))  # untouched
        self.assertIsNone(self.read("seen"))        # no seen.json written

    def test_stops_when_submit_refuses_midway(self):
        self._seed_queue(3)
        calls = {"n": 0}

        def check_fn(lane):
            return True, "ok"

        def submit_fn(task, lane):
            calls["n"] += 1
            return (calls["n"] == 1), ("submitted" if calls["n"] == 1 else "refused")

        out = rq.dispatch(top_n=3, check_fn=check_fn, submit_fn=submit_fn)
        self.assertEqual(len(out), 1)               # first ok, second refused -> stop
        disk = self.read("queue")
        self.assertEqual(len([i for i in disk if i["status"] == "dispatched"]), 1)

    def test_dry_run_writes_nothing(self):
        original = self._seed_queue(3)

        def check_fn(lane):
            return True, "ok"

        def submit_fn(task, lane):
            raise AssertionError("dry-run must not submit")

        out = rq.dispatch(top_n=2, dry_run=True, check_fn=check_fn, submit_fn=submit_fn)
        self.assertEqual(out, [])
        # queue.json byte-identical, no seen.json created
        self.assertEqual(self.read("queue"), original)
        self.assertIsNone(self.read("seen"))

    def test_pause_marker_keeps_collection_but_skips_dispatch(self):
        os.makedirs(os.path.dirname(rq.AUTONOMOUS_DISPATCH_PAUSE), exist_ok=True)
        open(rq.AUTONOMOUS_DISPATCH_PAUSE, "w").close()
        calls = []
        original_run, original_dispatch = rq.run, rq.dispatch
        try:
            rq.run = lambda: calls.append("run")
            rq.dispatch = lambda **_kwargs: calls.append("dispatch")
            self.assertEqual(rq.main([]), 0)
        finally:
            rq.run, rq.dispatch = original_run, original_dispatch
        self.assertEqual(calls, ["run"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
