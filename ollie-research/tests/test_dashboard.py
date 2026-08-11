#!/usr/bin/env python3
"""
Tests for research_dashboard.py
Drives the HTTP handler via a real ThreadingHTTPServer on 127.0.0.1:0.
Fully offline — no external calls, no secrets, no CDN.
"""
import http.client
import http.server
import importlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import unittest

# ── Make ollie-research importable ────────────────────────────────────────
_TESTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR  = os.path.dirname(_TESTS_DIR)
sys.path.insert(0, _MODULE_DIR)

import research_dashboard as rd   # noqa: E402  (after sys.path patch)

# ── Test-wide fixtures ────────────────────────────────────────────────────
_TOKEN = "test-token-deadbeef1234"

_SAMPLE_SOURCES = [
    {
        "id": "src_abc123",
        "type": "rss",
        "target": "https://example.com/feed.xml",
        "domain_tags": ["ai", "ml"],
        "weight": 1.5,
        "enabled": True,
        "recency_days": 7,
        "added_at": "2026-06-14T00:00:00Z"
    },
    {
        "id": "src_def456",
        "type": "reddit",
        "target": "r/MachineLearning",
        "domain_tags": ["ml"],
        "weight": 1.0,
        "enabled": True,
        "recency_days": 3,
        "added_at": "2026-06-14T00:00:00Z"
    }
]

_SAMPLE_INTERESTS = {
    "domains": ["ai", "dev-tools"],
    "keywords_boost": ["embedding", "llm"],
    "anti_interests": ["crypto"],
    "updated_at": "2026-06-14T00:00:00Z"
}

_SAMPLE_QUEUE = [
    {
        "fingerprint": "fp_aaa",
        "source_id": "src_abc123",
        "url": "https://example.com/post1",
        "title": "Embeddings in 2026",
        "text": "...",
        "ts": "2026-06-14T00:00:00Z",
        "domain_tags": ["ai"],
        "relevance": 0.92,
        "recency_factor": 0.85,
        "weight": 1.5,
        "score": 1.17,
        "status": "pending",
        "added_at": "2026-06-14T00:00:00Z",
        "manual_priority": None
    },
    {
        "fingerprint": "fp_bbb",
        "source_id": "src_def456",
        "url": "https://reddit.com/r/ML/post2",
        "title": "New LLM tricks",
        "text": "...",
        "ts": "2026-06-14T01:00:00Z",
        "domain_tags": ["ml"],
        "relevance": 0.75,
        "recency_factor": 0.9,
        "weight": 1.0,
        "score": 0.68,
        "status": "pending",
        "added_at": "2026-06-14T01:00:00Z",
        "manual_priority": None
    }
]


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class TestDashboardServer(unittest.TestCase):
    """Integration tests: real HTTP server on localhost:0."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ce_dash_test_")

        # Write a minimal index.html so GET / doesn't 503
        dash_dir = os.path.join(cls.tmpdir, "dashboard")
        os.makedirs(dash_dir, exist_ok=True)
        with open(os.path.join(dash_dir, "index.html"), "w") as fh:
            fh.write("<html><body>TEST</body></html>")

        # Write a fake budget.py
        budget_path = os.path.join(cls.tmpdir, "budget.py")
        with open(budget_path, "w") as fh:
            fh.write(textwrap.dedent("""\
                import sys
                if len(sys.argv) > 1 and sys.argv[1] == 'status':
                    print('date 2026-06-14  counts {}  ceilings {research:6}  global 10')
                    sys.exit(0)
                sys.exit(2)
            """))

        # Patch module globals
        rd.BEARER_TOKEN    = _TOKEN
        rd.DATA_DIR        = cls.tmpdir
        rd.SOURCES_FILE    = os.path.join(cls.tmpdir, "sources.json")
        rd.INTERESTS_FILE  = os.path.join(cls.tmpdir, "interests.json")
        rd.QUEUE_FILE      = os.path.join(cls.tmpdir, "queue.json")
        rd.INDEX_HTML      = os.path.join(dash_dir, "index.html")
        rd.BUDGET_BIN      = budget_path
        rd.SPEND_LOG       = os.path.join(cls.tmpdir, "spend.log")

        # Seed data files
        _write_json(rd.SOURCES_FILE,   _SAMPLE_SOURCES)
        _write_json(rd.INTERESTS_FILE, _SAMPLE_INTERESTS)
        _write_json(rd.QUEUE_FILE,     _SAMPLE_QUEUE)

        # Start server
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), rd.DashboardHandler
        )
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # Reset data files before every individual test so mutations don't bleed
    def setUp(self):
        _write_json(rd.SOURCES_FILE,   _SAMPLE_SOURCES)
        _write_json(rd.INTERESTS_FILE, _SAMPLE_INTERESTS)
        _write_json(rd.QUEUE_FILE,     _SAMPLE_QUEUE)

    # ── helpers ────────────────────────────────────────────────────────────
    def _req(self, method, path, body=None, token=_TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        try:
            data = json.loads(payload)
        except Exception:
            data = payload
        return resp.status, data

    def _reload_sources(self):
        with open(rd.SOURCES_FILE) as fh:
            return json.load(fh)

    def _reload_queue(self):
        with open(rd.QUEUE_FILE) as fh:
            return json.load(fh)

    # ── auth ───────────────────────────────────────────────────────────────
    def test_auth_401_no_token(self):
        status, _ = self._req("GET", "/api/sources", token=None)
        self.assertEqual(status, 401, "must reject missing Authorization")

    def test_auth_401_wrong_token(self):
        status, _ = self._req("GET", "/api/sources", token="wrong-token")
        self.assertEqual(status, 401, "must reject wrong bearer token")

    def test_auth_200_correct_token(self):
        status, data = self._req("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)

    # ── GET / serves HTML ──────────────────────────────────────────────────
    def test_get_root_no_auth_required(self):
        """Index is served without a bearer token (so the JS can load)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = resp.read()
        self.assertIn(b"TEST", body)

    # ── GET endpoints ──────────────────────────────────────────────────────
    def test_get_sources(self):
        status, data = self._req("GET", "/api/sources")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["id"], "src_abc123")

    def test_get_interests(self):
        status, data = self._req("GET", "/api/interests")
        self.assertEqual(status, 200)
        self.assertIn("domains", data)
        self.assertIn("ai", data["domains"])

    def test_get_queue(self):
        status, data = self._req("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 2)

    def test_get_budget(self):
        status, data = self._req("GET", "/api/budget")
        self.assertEqual(status, 200)
        self.assertIn("status", data)
        self.assertIn("spend_tail", data)
        # budget subprocess returns our fake output
        self.assertIn("2026-06-14", data["status"])

    # ── No secrets in responses ────────────────────────────────────────────
    def test_no_secret_in_sources_response(self):
        _, data = self._req("GET", "/api/sources")
        txt = json.dumps(data)
        self.assertNotIn(_TOKEN, txt, "bearer token must not appear in sources response")

    def test_no_secret_in_interests_response(self):
        _, data = self._req("GET", "/api/interests")
        txt = json.dumps(data)
        self.assertNotIn(_TOKEN, txt)

    def test_no_secret_in_queue_response(self):
        _, data = self._req("GET", "/api/queue")
        txt = json.dumps(data)
        self.assertNotIn(_TOKEN, txt)

    def test_budget_response_no_api_key_echo(self):
        _, data = self._req("GET", "/api/budget")
        # budget endpoint must not echo the bearer token itself
        self.assertNotIn(_TOKEN, json.dumps(data))

    # ── POST /api/sources ──────────────────────────────────────────────────
    def test_add_source_success(self):
        body = {
            "type": "blog",
            "target": "https://example.com/blog",
            "domain_tags": ["tools"],
            "weight": 1.2,
            "recency_days": 5,
            "enabled": True
        }
        status, data = self._req("POST", "/api/sources", body)
        self.assertEqual(status, 201)
        self.assertIn("id", data)
        self.assertEqual(data["target"], "https://example.com/blog")
        # verify persisted
        saved = self._reload_sources()
        ids = [s["id"] for s in saved]
        self.assertIn(data["id"], ids)

    def test_add_source_invalid_type(self):
        status, data = self._req("POST", "/api/sources", {"type": "ftp", "target": "ftp://x"})
        self.assertEqual(status, 400)

    def test_add_source_missing_target(self):
        status, _ = self._req("POST", "/api/sources", {"type": "rss"})
        self.assertEqual(status, 400)

    def test_add_source_bad_weight_type(self):
        status, _ = self._req("POST", "/api/sources", {"type":"rss","target":"x","weight":"heavy"})
        self.assertEqual(status, 400)

    def test_add_source_duplicate_id(self):
        body = {"id": "src_abc123", "type": "rss", "target": "x"}
        status, _ = self._req("POST", "/api/sources", body)
        self.assertEqual(status, 400)

    # ── PUT /api/sources/<id> ──────────────────────────────────────────────
    def test_put_source_toggle_enabled(self):
        _write_json(rd.SOURCES_FILE, _SAMPLE_SOURCES)
        status, data = self._req("PUT", "/api/sources/src_abc123", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["enabled"])
        saved = self._reload_sources()
        src = next(s for s in saved if s["id"] == "src_abc123")
        self.assertFalse(src["enabled"])

    def test_put_source_edit_weight(self):
        _write_json(rd.SOURCES_FILE, _SAMPLE_SOURCES)
        status, data = self._req("PUT", "/api/sources/src_def456", {"weight": 2.5})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(data["weight"], 2.5)

    def test_put_source_edit_domain_tags(self):
        _write_json(rd.SOURCES_FILE, _SAMPLE_SOURCES)
        status, data = self._req("PUT", "/api/sources/src_abc123",
                                 {"domain_tags": ["ai", "rag", "tools"]})
        self.assertEqual(status, 200)
        self.assertIn("rag", data["domain_tags"])

    def test_put_source_not_found(self):
        status, _ = self._req("PUT", "/api/sources/nonexistent_id", {"enabled": False})
        self.assertEqual(status, 404)

    def test_put_source_invalid_id_pattern(self):
        status, _ = self._req("PUT", "/api/sources/../etc/passwd", {"enabled": False})
        self.assertEqual(status, 400)

    # ── DELETE /api/sources/<id> ───────────────────────────────────────────
    def test_delete_source(self):
        _write_json(rd.SOURCES_FILE, _SAMPLE_SOURCES)
        status, data = self._req("DELETE", "/api/sources/src_def456")
        self.assertEqual(status, 200)
        self.assertEqual(data["deleted"], "src_def456")
        saved = self._reload_sources()
        self.assertTrue(all(s["id"] != "src_def456" for s in saved))

    def test_delete_source_not_found(self):
        status, _ = self._req("DELETE", "/api/sources/ghost_id")
        self.assertEqual(status, 404)

    # ── PUT /api/interests ─────────────────────────────────────────────────
    def test_put_interests(self):
        _write_json(rd.INTERESTS_FILE, _SAMPLE_INTERESTS)
        body = {
            "domains": ["ai", "security", "robotics"],
            "keywords_boost": ["agent", "rag"],
            "anti_interests": ["nft", "crypto"]
        }
        status, data = self._req("PUT", "/api/interests", body)
        self.assertEqual(status, 200)
        self.assertIn("security", data["domains"])
        self.assertIn("agent", data["keywords_boost"])
        self.assertIn("updated_at", data)

    def test_put_interests_bad_domains_type(self):
        status, _ = self._req("PUT", "/api/interests", {"domains": "ai,ml"})
        self.assertEqual(status, 400)

    # ── POST /api/queue/reorder ────────────────────────────────────────────
    def test_queue_reorder_sets_manual_priority(self):
        _write_json(rd.QUEUE_FILE, _SAMPLE_QUEUE)
        # Reverse order: fp_bbb first, fp_aaa second
        status, data = self._req("POST", "/api/queue/reorder", ["fp_bbb", "fp_aaa"])
        self.assertEqual(status, 200)
        self.assertEqual(data["reordered"], 2)
        queue = self._reload_queue()
        by_fp = {item["fingerprint"]: item for item in queue}
        self.assertEqual(by_fp["fp_bbb"]["manual_priority"], 0)
        self.assertEqual(by_fp["fp_aaa"]["manual_priority"], 1)

    def test_queue_reorder_clears_unmentioned(self):
        _write_json(rd.QUEUE_FILE, _SAMPLE_QUEUE)
        # Only mention one item — other gets manual_priority=null
        status, _ = self._req("POST", "/api/queue/reorder", ["fp_aaa"])
        self.assertEqual(status, 200)
        queue = self._reload_queue()
        by_fp = {item["fingerprint"]: item for item in queue}
        self.assertEqual(by_fp["fp_aaa"]["manual_priority"], 0)
        self.assertIsNone(by_fp["fp_bbb"]["manual_priority"])

    def test_queue_reorder_not_a_list(self):
        status, _ = self._req("POST", "/api/queue/reorder", {"fp": "fp_aaa"})
        self.assertEqual(status, 400)

    def test_queue_reorder_empty_list(self):
        _write_json(rd.QUEUE_FILE, _SAMPLE_QUEUE)
        status, data = self._req("POST", "/api/queue/reorder", [])
        self.assertEqual(status, 200)
        self.assertEqual(data["reordered"], 0)

    # ── PUT /api/queue/<fp> ────────────────────────────────────────────────
    def test_put_queue_item_status(self):
        _write_json(rd.QUEUE_FILE, _SAMPLE_QUEUE)
        status, data = self._req("PUT", "/api/queue/fp_aaa", {"status": "muted"})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "muted")

    def test_put_queue_item_not_found(self):
        status, _ = self._req("PUT", "/api/queue/fp_ghost", {"status": "muted"})
        self.assertEqual(status, 404)

    def test_put_queue_invalid_fingerprint(self):
        status, _ = self._req("PUT", "/api/queue/../etc", {"status": "muted"})
        self.assertEqual(status, 400)

    # ── DELETE /api/queue/<fp> ─────────────────────────────────────────────
    def test_delete_queue_item(self):
        _write_json(rd.QUEUE_FILE, _SAMPLE_QUEUE)
        status, data = self._req("DELETE", "/api/queue/fp_bbb")
        self.assertEqual(status, 200)
        self.assertEqual(data["deleted"], "fp_bbb")
        queue = self._reload_queue()
        self.assertTrue(all(it["fingerprint"] != "fp_bbb" for it in queue))
        self.assertEqual(len(queue), 1)

    def test_delete_queue_item_not_found(self):
        status, _ = self._req("DELETE", "/api/queue/fp_ghost")
        self.assertEqual(status, 404)

    def test_delete_queue_invalid_fingerprint(self):
        status, _ = self._req("DELETE", "/api/queue/../../secret")
        self.assertEqual(status, 400)

    # ── Atomic write: writes confined to 3 files ───────────────────────────
    def test_writes_confined_to_data_files_only(self):
        """After mutating all three resources, only the three JSON files exist in tmpdir."""
        _write_json(rd.SOURCES_FILE,   _SAMPLE_SOURCES)
        _write_json(rd.INTERESTS_FILE, _SAMPLE_INTERESTS)
        _write_json(rd.QUEUE_FILE,     _SAMPLE_QUEUE)
        # Trigger writes
        self._req("POST", "/api/sources", {"type": "blog", "target": "https://t.test"})
        self._req("PUT",  "/api/interests", {"domains": ["test"]})
        self._req("DELETE", "/api/queue/fp_aaa")
        # Only JSON + tmp leftovers allowed, not arbitrary paths
        allowed = {
            os.path.basename(rd.SOURCES_FILE),
            os.path.basename(rd.INTERESTS_FILE),
            os.path.basename(rd.QUEUE_FILE),
            "dashboard",
            "budget.py",
            "spend.log",
        }
        for entry in os.listdir(cls := self.tmpdir):
            base = os.path.basename(entry)
            # strip any .tmp suffix for atomic write temp files
            self.assertIn(base.replace(".tmp", ""), allowed,
                          f"unexpected file written: {entry}")

    # ── Validate header check ──────────────────────────────────────────────
    def test_401_on_post_without_auth(self):
        status, _ = self._req("POST", "/api/sources", {"type":"rss","target":"x"}, token=None)
        self.assertEqual(status, 401)

    def test_401_on_delete_without_auth(self):
        status, _ = self._req("DELETE", "/api/sources/src_abc123", token=None)
        self.assertEqual(status, 401)

    def test_401_on_put_without_auth(self):
        status, _ = self._req("PUT", "/api/interests", {"domains": []}, token=None)
        self.assertEqual(status, 401)

    # ── 404 on unknown endpoints ───────────────────────────────────────────
    def test_unknown_get_404(self):
        status, _ = self._req("GET", "/api/nonexistent")
        self.assertEqual(status, 404)

    def test_unknown_post_404(self):
        status, _ = self._req("POST", "/api/unknown", {})
        self.assertEqual(status, 404)


class TestValidation(unittest.TestCase):
    """Unit-test schema validators directly (no server)."""

    def test_valid_source(self):
        ok, err = rd.validate_source({"type": "rss", "target": "https://x.com/feed"})
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_invalid_type(self):
        ok, _ = rd.validate_source({"type": "ftp", "target": "ftp://x"})
        self.assertFalse(ok)

    def test_empty_target(self):
        ok, _ = rd.validate_source({"type": "rss", "target": "   "})
        self.assertFalse(ok)

    def test_bad_weight_string(self):
        ok, _ = rd.validate_source({"type": "rss", "target": "x", "weight": "heavy"})
        self.assertFalse(ok)

    def test_bad_enabled(self):
        ok, _ = rd.validate_source({"type": "rss", "target": "x", "enabled": "yes"})
        self.assertFalse(ok)

    def test_bad_domain_tags(self):
        ok, _ = rd.validate_source({"type": "rss", "target": "x", "domain_tags": "ai"})
        self.assertFalse(ok)

    def test_valid_interests(self):
        ok, _ = rd.validate_interests({"domains": ["ai"], "keywords_boost": [], "anti_interests": []})
        self.assertTrue(ok)

    def test_invalid_interests_not_dict(self):
        ok, _ = rd.validate_interests(["ai", "ml"])
        self.assertFalse(ok)

    def test_interests_bad_list_field(self):
        ok, _ = rd.validate_interests({"domains": "ai,ml"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
