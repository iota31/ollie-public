#!/usr/bin/env python3
"""Offline stdlib-unittest coverage for research_registry.py.

Repoints WORKSPACE/LOGS at a tempdir so nothing touches the real box.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import research_registry as reg  # noqa: E402


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (reg.WORKSPACE, reg.LOGS)
        reg.WORKSPACE = os.path.join(self.tmp, ".openclaw", "workspace")
        reg.LOGS = os.path.join(self.tmp, ".openclaw", "logs")

    def tearDown(self):
        reg.WORKSPACE, reg.LOGS = self._orig

    # -- absence / defaults ----------------------------------------------------
    def test_load_sources_absent_returns_empty(self):
        self.assertEqual(reg.load_sources(), [])

    def test_load_interests_absent_returns_default(self):
        i = reg.load_interests()
        self.assertEqual(i["domains"], [])
        self.assertEqual(i["keywords_boost"], [])
        self.assertEqual(i["anti_interests"], [])
        self.assertIn("updated_at", i)

    # -- sources round-trip + schema defaults ----------------------------------
    def test_save_load_sources_roundtrip_and_defaults(self):
        written = reg.save_sources([
            {"id": "rss-x", "type": "rss", "target": "http://x/feed",
             "domain_tags": ["a"]},  # weight/enabled/recency_days/added_at omitted
        ])
        self.assertEqual(len(written), 1)
        s = reg.load_sources()[0]
        self.assertEqual(s["id"], "rss-x")
        self.assertEqual(s["weight"], 1.0)            # default
        self.assertEqual(s["recency_days"], 14)       # default
        self.assertIs(s["enabled"], True)             # default
        self.assertTrue(s["added_at"])                # auto-stamped
        self.assertEqual(s["domain_tags"], ["a"])

    def test_save_coerces_and_validates(self):
        written = reg.save_sources([
            {"id": "ok", "type": "reddit", "target": "LocalLLaMA",
             "weight": "2.5", "recency_days": "7", "enabled": 0,
             "domain_tags": ["x", 5, "y"]},          # 5 dropped (not str)
            {"id": "bad-type", "type": "tiktok", "target": "z"},  # bad type -> drop
            {"type": "rss", "target": "no-id"},                   # no id -> drop
            "not-a-dict",                                          # -> drop
        ])
        self.assertEqual([s["id"] for s in written], ["ok"])
        s = written[0]
        self.assertEqual(s["weight"], 2.5)
        self.assertEqual(s["recency_days"], 7)
        self.assertIs(s["enabled"], False)
        self.assertEqual(s["domain_tags"], ["x", "y"])

    def test_dedup_by_id_first_wins(self):
        written = reg.save_sources([
            {"id": "dup", "type": "rss", "target": "first"},
            {"id": "dup", "type": "rss", "target": "second"},
        ])
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["target"], "first")

    # -- interests round-trip --------------------------------------------------
    def test_save_load_interests_roundtrip_and_stamp(self):
        written = reg.save_interests({
            "domains": ["on-device AI", 7, "MCP"],   # 7 dropped
            "keywords_boost": ["ONNX"],
            "anti_interests": ["crypto"],
        })
        self.assertEqual(written["domains"], ["on-device AI", "MCP"])
        self.assertTrue(written["updated_at"])
        i = reg.load_interests()
        self.assertEqual(i["keywords_boost"], ["ONNX"])
        self.assertEqual(i["anti_interests"], ["crypto"])

    # -- corruption resilience -------------------------------------------------
    def test_corrupt_sources_returns_empty(self):
        p = reg._paths()["sources"]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{ this is not json ]")
        self.assertEqual(reg.load_sources(), [])

    def test_corrupt_interests_returns_default(self):
        p = reg._paths()["interests"]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("garbage")
        self.assertEqual(reg.load_interests()["domains"], [])

    def test_save_is_atomic_no_tmp_left(self):
        reg.save_sources([{"id": "a", "type": "rss", "target": "t"}])
        d = reg._paths()["dir"]
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    # -- fingerprint -----------------------------------------------------------
    def test_fingerprint_stable_and_normalized(self):
        a = reg.fingerprint("https://X.com/Post/", "Hello  World")
        b = reg.fingerprint("https://x.com/post", "hello world")
        self.assertEqual(a, b)             # case/trailing-slash/space normalized
        self.assertEqual(len(a), 64)       # sha256 hex
        c = reg.fingerprint("https://x.com/other", "hello world")
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
