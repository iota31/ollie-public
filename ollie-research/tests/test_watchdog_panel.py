#!/usr/bin/env python3
"""
Tests for mc/reads_watchdog.py — Watchdog & Budget panel endpoints.

Mirrors the style and harness of tests/test_system.py:
- Spin a real bearer-gated server on a random port (ThreadingHTTPServer + rd.DashboardHandler).
- Patch rd paths + module globals on the reads module for deterministic, hermetic runs.
- Cover create-tolerant behavior (missing files/dirs → honest absence, never crash).
- Cover failures-map → alerts transform (severity mapping).
- Cover token aggregation by feeding synthetic trajectory jsonl under a temp sessions dir.
- Exercise TTLCache (clear + call counting via a tiny wrapper).
- Assert JSON shapes and status codes.
- No box/network access; all I/O under a per-class tempdir.
"""

import http.client
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
sys.path.insert(0, _MODULE_DIR)

import research_dashboard as rd  # noqa: E402
import mc  # noqa: E402
from mc import reads_watchdog as rw  # noqa: E402


_TOKEN = "test-token-deadbeef1234"


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class TestWatchdogTransforms(unittest.TestCase):
    """Pure unit tests for internal transforms (failures→alerts, severity)."""

    def test_failures_to_alerts_basic(self):
        # Empty
        self.assertEqual(rw._failures_to_alerts({}), [])
        self.assertEqual(rw._failures_to_alerts(None), [])
        # Populated: severity mapping
        out = rw._failures_to_alerts({
            "disk": "root disk 97% full",
            "public-webhook": "timeout",
            "jobs-runner": "process not running",
        })
        subs = {a["subsystem"]: a for a in out}
        self.assertIn("disk", subs)
        self.assertEqual(subs["disk"]["severity"], "critical")  # contains "disk 9"
        self.assertEqual(subs["public-webhook"]["severity"], "warn")
        self.assertEqual(subs["jobs-runner"]["severity"], "warn")

    def test_load_watchdog_missing_is_empty_dict(self):
        # Point at a path that does not exist; _load_watchdog must return {}
        td = tempfile.mkdtemp(prefix="ce_wd_unit_")
        try:
            missing = os.path.join(td, "nope.json")
            old = rw.WATCHDOG_STATE
            rw.WATCHDOG_STATE = missing
            try:
                self.assertEqual(rw._load_watchdog(), {})
            finally:
                rw.WATCHDOG_STATE = old
        finally:
            shutil.rmtree(td, ignore_errors=True)


class TestWatchdogEndpointsCreateTolerant(unittest.TestCase):
    """Endpoints must not crash on missing files; return documented shapes."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ce_watchdog_test_")

        # Minimal static assets so GET / doesn't 503
        dash_dir = os.path.join(cls.tmpdir, "dashboard")
        os.makedirs(dash_dir, exist_ok=True)
        with open(os.path.join(dash_dir, "index.html"), "w") as fh:
            fh.write("<html><body>TEST</body></html>")

        # Minimal budget stub (subprocess path used by /api/budget in reads.py)
        budget_path = os.path.join(cls.tmpdir, "budget.py")
        with open(budget_path, "w") as fh:
            fh.write("import sys\nsys.exit(0)\n")

        # Patch rd globals (same pattern as test_system)
        rd.BEARER_TOKEN = _TOKEN
        rd.DATA_DIR = cls.tmpdir
        rd.SOURCES_FILE = os.path.join(cls.tmpdir, "sources.json")
        rd.INTERESTS_FILE = os.path.join(cls.tmpdir, "interests.json")
        rd.QUEUE_FILE = os.path.join(cls.tmpdir, "queue.json")
        rd.INDEX_HTML = os.path.join(dash_dir, "index.html")
        rd.BUDGET_BIN = budget_path
        rd.SPEND_LOG = os.path.join(cls.tmpdir, "spend.log")

        # Point the watchdog module at tmp HOME so its paths are under tmp
        cls.home = cls.tmpdir
        rw._HOME = cls.home
        rw._OPENCLAW = os.path.join(cls.home, ".openclaw")
        rw.WATCHDOG_STATE = os.path.join(cls.home, "plugin-state", "watchdog-state.json")
        rw.WATCHDOG_LOG   = os.path.join(rw._OPENCLAW, "logs", "watchdog.log")
        rw.BUDGET_CONFIG  = os.path.join(rw._OPENCLAW, "workspace", "budget-config.json")
        rw.SPEND_STATE    = os.path.join(rw._OPENCLAW, "logs", "spend-state.json")
        rw.SESSIONS_DIR   = os.path.join(rw._OPENCLAW, "agents", "main", "sessions")

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

    def test_watchdog_json_shape_and_create_tolerant(self):
        # No files present yet → still 200 with honest absence
        status, data = self._req("GET", "/api/watchdog")
        self.assertEqual(status, 200)
        self.assertIn("alerts", data)
        self.assertIn("power", data)
        self.assertIn("last_beat", data)
        self.assertIn("history", data)
        self.assertIn("budget", data)
        self.assertIn("checked_at", data)
        self.assertIsInstance(data["alerts"], list)
        self.assertIsInstance(data["history"], list)
        self.assertIsInstance(data["budget"], dict)
        # With nothing present, alerts should be empty and budget lanes absent/empty
        self.assertEqual(data["alerts"], [])

    def test_budget_tokens_json_shape_and_create_tolerant(self):
        # No sessions dir → zeros/empty
        status, data = self._req("GET", "/api/budget/tokens")
        self.assertEqual(status, 200)
        self.assertIn("totals", data)
        self.assertIn("by_lane", data)
        self.assertIn("sessions_scanned", data)
        self.assertIn("as_of", data)
        t = data["totals"]
        for k in ("input", "output", "cacheRead", "total", "calls"):
            self.assertIn(k, t)
            self.assertIsInstance(t[k], int)
        self.assertEqual(data["sessions_scanned"], 0)
        self.assertEqual(data["by_lane"], {})

    def test_watchdog_with_state_and_log(self):
        # Seed a watchdog-state.json and a log; verify transform + tail
        st = {
            "failures": {
                "disk": "root disk 95% full",
                "public-webhook": "timeout",
            },
            "last_beat": "2026-06-15T00:00:00Z",
            "power": {"on_ac": False, "pct": 27},
        }
        os.makedirs(os.path.dirname(rw.WATCHDOG_STATE), exist_ok=True)
        _write_json(rw.WATCHDOG_STATE, st)

        os.makedirs(os.path.dirname(rw.WATCHDOG_LOG), exist_ok=True)
        with open(rw.WATCHDOG_LOG, "w") as fh:
            fh.write("2026-06-15 00:00:00 ok (0 known issues)\n")
            fh.write("2026-06-15 00:15:00 FAIL disk: root disk 95% full\n")
            fh.write("2026-06-15 00:30:00 RECOVERED public-webhook\n")

        status, data = self._req("GET", "/api/watchdog")
        self.assertEqual(status, 200)
        alerts = data["alerts"]
        subs = {a["subsystem"]: a for a in alerts}
        self.assertIn("disk", subs)
        self.assertEqual(subs["disk"]["severity"], "critical")
        self.assertIn("public-webhook", subs)
        self.assertEqual(subs["public-webhook"]["severity"], "warn")
        self.assertEqual(data["last_beat"], "2026-06-15T00:00:00Z")
        self.assertIn("power", data)
        self.assertTrue(any("RECOVERED" in ln for ln in data["history"]) or
                        any("ok (" in ln for ln in data["history"]))


class TestBudgetTokensAggregationPatched(unittest.TestCase):
    """Deterministic token aggregation by writing synthetic trajectory files."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ce_tokens_test_")

        # Minimal static + budget stub
        dash_dir = os.path.join(cls.tmpdir, "dashboard")
        os.makedirs(dash_dir, exist_ok=True)
        with open(os.path.join(dash_dir, "index.html"), "w") as fh:
            fh.write("<html><body>TEST</body></html>")
        budget_path = os.path.join(cls.tmpdir, "budget.py")
        with open(budget_path, "w") as fh:
            fh.write("import sys\nsys.exit(0)\n")

        rd.BEARER_TOKEN = _TOKEN
        rd.DATA_DIR = cls.tmpdir
        rd.SOURCES_FILE = os.path.join(cls.tmpdir, "sources.json")
        rd.INTERESTS_FILE = os.path.join(cls.tmpdir, "interests.json")
        rd.QUEUE_FILE = os.path.join(cls.tmpdir, "queue.json")
        rd.INDEX_HTML = os.path.join(dash_dir, "index.html")
        rd.BUDGET_BIN = budget_path
        rd.SPEND_LOG = os.path.join(cls.tmpdir, "spend.log")

        cls.home = cls.tmpdir
        rw._HOME = cls.home
        rw._OPENCLAW = os.path.join(cls.home, ".openclaw")
        rw.WATCHDOG_STATE = os.path.join(cls.home, "plugin-state", "watchdog-state.json")
        rw.WATCHDOG_LOG   = os.path.join(rw._OPENCLAW, "logs", "watchdog.log")
        rw.BUDGET_CONFIG  = os.path.join(rw._OPENCLAW, "workspace", "budget-config.json")
        rw.SPEND_STATE    = os.path.join(rw._OPENCLAW, "logs", "spend-state.json")
        rw.SESSIONS_DIR   = os.path.join(rw._OPENCLAW, "agents", "main", "sessions")

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

    def _write_traj(self, name, lines):
        os.makedirs(rw.SESSIONS_DIR, exist_ok=True)
        p = os.path.join(rw.SESSIONS_DIR, name)
        with open(p, "w") as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")

    def test_tokens_aggregate_from_synthetic_trajectories(self):
        # Clear any prior cache
        rw._TOKENS_CACHE.clear()

        # Two sessions: one project (two model.completed), one heartbeat (one)
        self._write_traj("s1.trajectory.jsonl", [
            {"type": "session.started", "sessionKey": "agent:main:project-abc", "sessionId": "s1", "ts": "2026-06-15T10:00:00Z"},
            {"type": "model.completed", "ts": "2026-06-15T10:00:01Z", "data": {"usage": {"input": 100, "output": 10, "cacheRead": 5, "total": 115}}},
            {"type": "model.completed", "ts": "2026-06-15T10:00:02Z", "data": {"usage": {"input": 200, "output": 20, "cacheRead": 0, "total": 220}}},
        ])
        self._write_traj("s2.trajectory.jsonl", [
            {"type": "session.started", "sessionKey": "agent:main:heartbeat-1", "sessionId": "s2", "ts": "2026-06-15T11:00:00Z"},
            {"type": "model.completed", "ts": "2026-06-15T11:00:01Z", "data": {"usage": {"input": 50, "output": 5, "cacheRead": 0, "total": 55}}},
        ])

        status, data = self._req("GET", "/api/budget/tokens")
        self.assertEqual(status, 200)
        t = data["totals"]
        self.assertEqual(t["input"], 350)
        self.assertEqual(t["output"], 35)
        self.assertEqual(t["cacheRead"], 5)
        self.assertEqual(t["total"], 390)
        self.assertEqual(t["calls"], 3)
        self.assertEqual(data["sessions_scanned"], 2)

        bl = data["by_lane"]
        self.assertIn("project", bl)
        self.assertIn("heartbeat", bl)
        self.assertEqual(bl["project"]["total"], 335)
        self.assertEqual(bl["heartbeat"]["total"], 55)

    def test_tokens_ttl_cache_avoids_recompute(self):
        # Force a fresh compute, then ensure a second call within TTL hits cache.
        rw._TOKENS_CACHE.clear()

        calls = {"n": 0}
        real_compute = rw._compute_tokens

        def counting_compute():
            calls["n"] += 1
            return real_compute()

        # First call should compute
        with patch.object(rw, "_compute_tokens", side_effect=counting_compute) as m:
            status, data = self._req("GET", "/api/budget/tokens")
            self.assertEqual(status, 200)
            first = calls["n"]
            # Second call should be cached (no additional compute)
            status, data = self._req("GET", "/api/budget/tokens")
            self.assertEqual(status, 200)
            # If cache worked, we should not have called the underlying compute again
            # (the wrapper increments only on real compute path).
            # Because get_or_set short-circuits, counting_compute should be invoked only once.
            self.assertEqual(calls["n"], first)

    def test_tokens_missing_sessions_dir_is_tolerant(self):
        # Remove sessions dir entirely → zeros
        try:
            shutil.rmtree(rw.SESSIONS_DIR)
        except FileNotFoundError:
            pass
        rw._TOKENS_CACHE.clear()
        status, data = self._req("GET", "/api/budget/tokens")
        self.assertEqual(status, 200)
        t = data["totals"]
        self.assertEqual(t["input"], 0)
        self.assertEqual(t["output"], 0)
        self.assertEqual(t["total"], 0)
        self.assertEqual(t["calls"], 0)
        self.assertEqual(data["by_lane"], {})
        self.assertEqual(data["sessions_scanned"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
