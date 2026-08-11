#!/usr/bin/env python3
"""
Tests for mc/reads_system.py — Mission Control system/health endpoints.
Unit + small integration style; fully offline, no real processes/ports/filesystem.
"""
import http.client
import http.server
import importlib
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
from mc import reads_system as rs  # noqa: E402


_TOKEN = "test-token-deadbeef1234"


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class TestSystemHealthRollup(unittest.TestCase):
    """Pure unit tests for _worst, _RANK, _compute_health rollup, _watchdog_state_for."""

    def test_rank_ordering(self):
        # Higher rank is worse
        self.assertEqual(rs._RANK["ok"], 0)
        self.assertEqual(rs._RANK["maintenance"], 1)
        self.assertEqual(rs._RANK["stale"], 2)
        self.assertEqual(rs._RANK["warn"], 3)
        self.assertEqual(rs._RANK["critical"], 4)

    def test_worst_reduction(self):
        self.assertEqual(rs._worst(["ok"]), "ok")
        self.assertEqual(rs._worst(["ok", "stale"]), "stale")
        self.assertEqual(rs._worst(["ok", "warn", "stale"]), "warn")
        self.assertEqual(rs._worst(["ok", "critical"]), "critical")
        self.assertEqual(rs._worst(["maintenance", "ok"]), "maintenance")
        # Unknown states are treated as rank 0 (ok) by _worst
        self.assertEqual(rs._worst(["ok", "nonsense"]), "ok")

    def test_verdict_mapping(self):
        # _compute_health maps overall_state through _VERDICT_FOR_STATE
        # We exercise it via _compute_health with patched inputs.
        pass  # covered by the integration-style cases below

    def test_compute_health_all_ok(self):
        fake_liveness = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "ok"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
                "watchdog": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        fake_watchdog = {"last_beat": "2026-06-15T00:00:00Z"}
        with patch.object(rs, "get_liveness_cached", return_value=fake_liveness), \
             patch.object(rs, "_load_watchdog", return_value=fake_watchdog):
            out = rs._compute_health()
        self.assertEqual(out["verdict"], "NOMINAL")
        self.assertEqual(out["overall_state"], "ok")
        for p in rs.PILLS:
            self.assertEqual(out["pills"][p], "ok")

    def test_compute_health_critical_wins(self):
        fake_liveness = {
            "services": {
                "gateway": {"state": "critical"},
                "hands": {"state": "ok"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        fake_watchdog = {}
        with patch.object(rs, "get_liveness_cached", return_value=fake_liveness), \
             patch.object(rs, "_load_watchdog", return_value=fake_watchdog):
            out = rs._compute_health()
        self.assertEqual(out["verdict"], "CRITICAL")
        self.assertEqual(out["overall_state"], "critical")

    def test_compute_health_warn_becomes_attention(self):
        fake_liveness = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "ok"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        # watchdog reports a non-critical failure for 'jobs'
        fake_watchdog = {"subsystems": {"jobs": {"failures": 1}}}
        with patch.object(rs, "get_liveness_cached", return_value=fake_liveness), \
             patch.object(rs, "_load_watchdog", return_value=fake_watchdog):
            out = rs._compute_health()
        self.assertEqual(out["verdict"], "ATTENTION")
        self.assertEqual(out["overall_state"], "warn")
        self.assertEqual(out["pills"]["jobs"], "warn")

    def test_compute_health_stale_becomes_degraded(self):
        fake_liveness = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "stale"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        fake_watchdog = {}
        with patch.object(rs, "get_liveness_cached", return_value=fake_liveness), \
             patch.object(rs, "_load_watchdog", return_value=fake_watchdog):
            out = rs._compute_health()
        self.assertEqual(out["verdict"], "DEGRADED")
        self.assertEqual(out["overall_state"], "stale")

    def test_compute_health_maintenance_becomes_maintenance(self):
        fake_liveness = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "ok"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
                "watchdog": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        fake_watchdog = {"subsystems": {"curiosity": {"maintenance": True}}}
        with patch.object(rs, "get_liveness_cached", return_value=fake_liveness), \
             patch.object(rs, "_load_watchdog", return_value=fake_watchdog):
            out = rs._compute_health()
        self.assertEqual(out["verdict"], "MAINTENANCE")
        self.assertEqual(out["overall_state"], "maintenance")
        self.assertEqual(out["pills"]["curiosity"], "maintenance")

    def test_watchdog_state_for_shapes(self):
        # Explicit state wins
        self.assertEqual(rs._watchdog_state_for("x", {"subsystems": {"x": {"state": "warn"}}}), "warn")
        # maintenance/paused/muted → maintenance
        self.assertEqual(rs._watchdog_state_for("x", {"x": {"paused": True}}), "maintenance")
        # critical flag
        self.assertEqual(rs._watchdog_state_for("x", {"subsystems": {"x": {"critical": True}}}), "critical")
        # failures > 0 → warn
        self.assertEqual(rs._watchdog_state_for("x", {"subsystems": {"x": {"failures": 2}}}), "warn")
        self.assertEqual(rs._watchdog_state_for("x", {"subsystems": {"x": {"fail_count": 1}}}), "warn")
        # Untracked / unknown subsystem → documented default "ok"
        self.assertEqual(rs._watchdog_state_for("ghost", {}), "ok")
        self.assertEqual(rs._watchdog_state_for("ghost", {"subsystems": {}}), "ok")
        # Bad node shape → ok (treated as untracked)
        self.assertEqual(rs._watchdog_state_for("x", {"subsystems": {"x": "not-a-dict"}}), "ok")


class TestSystemEndpointsCreateTolerant(unittest.TestCase):
    """Endpoints must not crash on missing files; return documented shapes."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ce_system_test_")

        # Write a minimal index.html so GET / doesn't 503
        dash_dir = os.path.join(cls.tmpdir, "dashboard")
        os.makedirs(dash_dir, exist_ok=True)
        with open(os.path.join(dash_dir, "index.html"), "w") as fh:
            fh.write("<html><body>TEST</body></html>")

        # Minimal budget stub
        budget_path = os.path.join(cls.tmpdir, "budget.py")
        with open(budget_path, "w") as fh:
            fh.write("import sys\nsys.exit(0)\n")

        # Patch rd + token
        rd.BEARER_TOKEN = _TOKEN
        rd.DATA_DIR = cls.tmpdir
        rd.SOURCES_FILE = os.path.join(cls.tmpdir, "sources.json")
        rd.INTERESTS_FILE = os.path.join(cls.tmpdir, "interests.json")
        rd.QUEUE_FILE = os.path.join(cls.tmpdir, "queue.json")
        rd.INDEX_HTML = os.path.join(dash_dir, "index.html")
        rd.BUDGET_BIN = budget_path
        rd.SPEND_LOG = os.path.join(cls.tmpdir, "spend.log")

        # IMPORTANT: point the system module at our tmp HOME so its paths are under tmp
        # We override the computed globals in reads_system after import.
        cls.home = cls.tmpdir
        rs._HOME = cls.home
        rs._OPENCLAW = os.path.join(cls.home, ".openclaw")
        rs.WATCHDOG_STATE = os.path.join(cls.home, "plugin-state", "watchdog-state.json")
        rs.HOST_POWER = os.path.join(rs._OPENCLAW, "workspace", "host-power.json")
        rs.ACTIVITY_LOG = os.path.join(rs._OPENCLAW, "logs", "mission-control.log")

        # Start server (reuses the same handler registration path via mc.load_handlers already done at import)
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

    def test_health_json_shape(self):
        status, data = self._req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("verdict", data)
        self.assertIn("overall_state", data)
        self.assertIn("pills", data)
        self.assertIn("last_beat", data)
        self.assertIn("checked_at", data)
        self.assertIsInstance(data["pills"], dict)

    def test_liveness_json_shape(self):
        status, data = self._req("GET", "/api/system/liveness")
        self.assertEqual(status, 200)
        self.assertIn("services", data)
        self.assertIn("system", data)
        self.assertIn("checked_at", data)
        self.assertIsInstance(data["services"], dict)

    def test_activity_missing_log_returns_empty_list(self):
        # Ensure the log path does not exist
        try:
            os.unlink(rs.ACTIVITY_LOG)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        # Also ensure parent dir may be absent — _tail should still return []
        parent = os.path.dirname(rs.ACTIVITY_LOG)
        if os.path.exists(parent):
            shutil.rmtree(parent, ignore_errors=True)
        status, data = self._req("GET", "/api/activity")
        self.assertEqual(status, 200)
        self.assertEqual(data, [])

    def test_power_missing_file_returns_null(self):
        # Ensure host-power.json is absent
        try:
            os.unlink(rs.HOST_POWER)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        parent = os.path.dirname(rs.HOST_POWER)
        if os.path.exists(parent):
            shutil.rmtree(parent, ignore_errors=True)
        status, data = self._req("GET", "/api/system/power")
        self.assertEqual(status, 200)
        self.assertIsNone(data)


class TestSystemProbesPatched(unittest.TestCase):
    """Force deterministic probe results by patching internals (no real pgrep/ports)."""

    def test_pills_reflect_patched_liveness(self):
        fake = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "stale"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "critical"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        with patch.object(rs, "get_liveness_cached", return_value=fake), \
             patch.object(rs, "_load_watchdog", return_value={}):
            out = rs._compute_health()
        # jobs critical should dominate → CRITICAL
        self.assertEqual(out["verdict"], "CRITICAL")
        self.assertEqual(out["pills"]["jobs"], "critical")
        self.assertEqual(out["pills"]["hands"], "stale")

    def test_watchdog_pill_non_ok_when_process_absent(self):
        """Watchdog pill must reflect actual watchdog process liveness (pgrep), not just watchdog-state.json.
        When the ollie_watchdog process is absent, the pill must go non-ok (critical from liveness child).
        """
        fake = {
            "services": {
                "gateway": {"state": "ok"},
                "hands": {"state": "ok"},
                "factcheck": {"state": "ok"},
                "jobs": {"state": "ok"},
                "dashboard": {"state": "ok"},
                "ollielab": {"state": "ok"},
                "watchdog": {"state": "critical", "detail": "pgrep -f ollie_watchdog: not found"},
            },
            "system": {"mem": None, "loadavg": None, "disk": None},
            "checked_at": "2026-06-15T00:00:00Z",
        }
        with patch.object(rs, "get_liveness_cached", return_value=fake), \
             patch.object(rs, "_load_watchdog", return_value={}):
            out = rs._compute_health()
        # The liveness child for 'watchdog' is critical → pill must be critical (non-ok)
        self.assertEqual(out["pills"]["watchdog"], "critical")
        self.assertIn(out["pills"]["watchdog"], ("critical", "warn", "stale"))  # explicitly non-ok
        # Overall verdict should reflect the critical child
        self.assertEqual(out["verdict"], "CRITICAL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
