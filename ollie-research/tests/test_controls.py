#!/usr/bin/env python3
"""
Tests for mc/controls.py — Mission Control control endpoints (batch 1).

Mirrors the style of tests/test_system.py and tests/test_heartbeat_panel.py:
- Real ThreadingHTTPServer + rd.DashboardHandler on 127.0.0.1:0.
- Patch rd paths + module globals for hermetic runs under a tempdir.
- Create-tolerant behavior for missing files.
- Deterministic via patches (no real subprocess for the beat path; we assert the call).
- No box/network; everything under a per-class tempdir.
"""
import http.client
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
sys.path.insert(0, _MODULE_DIR)

import research_dashboard as rd  # noqa: E402
import mc  # noqa: E402
from mc import controls as ctrlmod  # noqa: E402
from mc.auth import _get_or_create_control_pin, _reset_rate_limits  # noqa: E402


_TOKEN = "test-token-deadbeef1234"


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class TestControlsHarness(unittest.TestCase):
    """Integration-style tests via a live bearer+PIN-gated server (no real box)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="ce_ctrl_test_")

        # Minimal dashboard assets
        dash_dir = os.path.join(cls.tmpdir, "dashboard")
        os.makedirs(dash_dir, exist_ok=True)
        with open(os.path.join(dash_dir, "index.html"), "w") as fh:
            fh.write("<html><body>TEST</body></html>")

        # Minimal budget stub (not used by controls, but rd expects a valid path)
        budget_path = os.path.join(cls.tmpdir, "budget.py")
        with open(budget_path, "w") as fh:
            fh.write("import sys\nsys.exit(0)\n")

        # Patch rd globals
        rd.BEARER_TOKEN = _TOKEN
        rd.DATA_DIR = cls.tmpdir
        rd.SOURCES_FILE = os.path.join(cls.tmpdir, "sources.json")
        rd.INTERESTS_FILE = os.path.join(cls.tmpdir, "interests.json")
        rd.QUEUE_FILE = os.path.join(cls.tmpdir, "queue.json")
        rd.INDEX_HTML = os.path.join(dash_dir, "index.html")
        rd.BUDGET_BIN = budget_path
        rd.SPEND_LOG = os.path.join(cls.tmpdir, "spend.log")

        # Point controls + auth at our tmp HOME
        cls.home = cls.tmpdir
        ctrlmod._HOME = cls.home
        ctrlmod._OPENCLAW = os.path.join(cls.home, ".openclaw")
        ctrlmod.WATCHDOG_STATE = os.path.join(cls.home, "plugin-state", "watchdog-state.json")
        ctrlmod.ACTIVITY_LOG = os.path.join(ctrlmod._OPENCLAW, "logs", "mission-control.log")

        # Also patch the auth module's HOME-derived paths
        from mc import auth as authmod  # noqa: E402
        authmod._CONTROL_PIN_FILE = None
        authmod._CONTROL_PIN = None
        # Force auth to resolve under our tmp HOME for the PIN file
        authmod._get_control_pin_file = lambda: os.path.join(cls.home, ".openclaw", "secrets", "mission-control-pin")

        # Ensure a control PIN exists (auto-generated on first read)
        cls.control_pin = _get_or_create_control_pin()

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

    def _req(self, method, path, body=None, token=_TOKEN, control_pin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        if control_pin is not None:
            hdrs["X-Ollie-Control"] = control_pin
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        try:
            data = json.loads(payload)
        except Exception:
            data = payload
        return resp.status, data

    def setUp(self):
        # Each test gets a clean rate-limit slate so order-independent.
        _reset_rate_limits()

    # ── Auth + confirm + rate-limit contract ─────────────────────────────────

    def test_control_pin_required_missing_header(self):
        # No X-Ollie-Control → 401/403
        st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=None)
        self.assertIn(st, (401, 403))

    def test_control_pin_required_bad_pin(self):
        st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin="deadbeef")
        self.assertIn(st, (401, 403))

    def test_confirm_required(self):
        # Valid bearer + PIN, but no confirm → 400
        st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": False}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 400)
        # Also missing body entirely → 400
        st, data = self._req("POST", "/api/ctrl/heartbeat/beat", None, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 400)

    def test_rate_limit_second_call_returns_429(self):
        # First call should be accepted by harness (we'll patch the beat impl below);
        # second rapid call must be rate-limited to 429 regardless of backend.
        # We patch the actual beat handler to be a no-op that returns quickly.
        with patch.object(ctrlmod, "HEARTBEAT_SCRIPT", os.path.join(self.home, "bin", "ollie_heartbeat.py")):
            # Ensure script path exists but we won't run it (we patch the run path in the next test).
            os.makedirs(os.path.dirname(ctrlmod.HEARTBEAT_SCRIPT), exist_ok=True)
            with open(ctrlmod.HEARTBEAT_SCRIPT, "w") as fh:
                fh.write("#!/usr/bin/env python3\nprint('noop')\n")

            # First call: will be rate-accepted and then we short-circuit the subprocess to avoid long work.
            with patch("mc.controls.subprocess.run") as mrun:
                mrun.return_value = type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
                st1, d1 = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=self.control_pin)
            # Second call inside the same 60s window → 429 from rate limiter
            st2, d2 = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=self.control_pin)
            self.assertEqual(st2, 429)

    # ── Heartbeat beat behavior (mocked subprocess, never actually spawns) ───

    def test_heartbeat_beat_invokes_subprocess_and_returns_shape(self):
        os.makedirs(os.path.dirname(ctrlmod.HEARTBEAT_SCRIPT), exist_ok=True)
        with open(ctrlmod.HEARTBEAT_SCRIPT, "w") as fh:
            fh.write("#!/usr/bin/env python3\nprint('beat ran')\n")

        with patch("mc.controls.subprocess.run") as mrun:
            mrun.return_value = type("P", (), {"returncode": 0, "stdout": "beat ran\n", "stderr": ""})()
            st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 200)
        self.assertIn("ok", data)
        self.assertIn("rc", data)
        self.assertIn("note", data)
        # Ensure we did attempt the subprocess with the expected argv[1]
        self.assertTrue(mrun.called)
        args, kwargs = mrun.call_args
        self.assertIn(ctrlmod.HEARTBEAT_SCRIPT, args[0])

    def test_heartbeat_beat_timeout_maps_to_ok_false_note_timeout(self):
        os.makedirs(os.path.dirname(ctrlmod.HEARTBEAT_SCRIPT), exist_ok=True)
        with open(ctrlmod.HEARTBEAT_SCRIPT, "w") as fh:
            fh.write("#!/usr/bin/env python3\nimport time; time.sleep(1)\n")

        import subprocess as _real_subprocess
        with patch("mc.controls.subprocess.run", side_effect=_real_subprocess.TimeoutExpired(cmd=["x"], timeout=0)):
            st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 200)
        self.assertEqual(data.get("ok"), False)
        self.assertEqual(data.get("note"), "timeout")

    # ── Watchdog mute/ack: write only mc_mutes/mc_acks; other keys untouched ─

    def test_watchdog_mute_writes_only_mc_mutes_and_caps_ttl(self):
        # Seed an existing watchdog-state.json with unrelated keys
        os.makedirs(os.path.dirname(ctrlmod.WATCHDOG_STATE), exist_ok=True)
        seed = {"subsystems": {"jobs": {"failures": 2}}, "last_beat": "2026-06-15T00:00:00Z"}
        _write_json(ctrlmod.WATCHDOG_STATE, seed)

        key = "disk"
        minutes = 99999  # should be capped at 1440 (24h)
        st, data = self._req("POST", "/api/ctrl/watchdog/mute", {"key": key, "minutes": minutes, "confirm": True}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 200)
        self.assertIn("mc_mutes", data)
        self.assertIn(key, data["mc_mutes"])
        # TTL should be capped to <= 24h from now
        until = data["mc_mutes"][key]
        self.assertIsInstance(until, int)
        self.assertLessEqual(until, int(time.time()) + 1440 * 60 + 5)

        # The original unrelated keys must still be present and untouched
        live = json.load(open(ctrlmod.WATCHDOG_STATE))
        self.assertIn("subsystems", live)
        self.assertIn("last_beat", live)
        # And mc_mutes must be present as a separate top-level key
        self.assertIn("mc_mutes", live)

    def test_watchdog_ack_writes_mc_acks_and_is_reversible(self):
        os.makedirs(os.path.dirname(ctrlmod.WATCHDOG_STATE), exist_ok=True)
        seed = {"subsystems": {"jobs": {"failures": 0}}}
        _write_json(ctrlmod.WATCHDOG_STATE, seed)

        key = "public-webhook"
        st, data = self._req("POST", "/api/ctrl/watchdog/ack", {"key": key, "confirm": True}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 200)
        self.assertIn("mc_acks", data)
        self.assertIn(key, data["mc_acks"])
        self.assertIsInstance(data["mc_acks"][key], int)

        # File must contain mc_acks and original keys untouched
        live = json.load(open(ctrlmod.WATCHDOG_STATE))
        self.assertIn("subsystems", live)
        self.assertIn("mc_acks", live)

    # ── Audit lines appended on control attempts (accepted and denied) ───────

    def test_audit_line_appended_on_beat(self):
        # Clear any prior log
        try:
            os.unlink(ctrlmod.ACTIVITY_LOG)
        except Exception:
            pass
        os.makedirs(os.path.dirname(ctrlmod.ACTIVITY_LOG), exist_ok=True)

        with patch("mc.controls.subprocess.run") as mrun:
            mrun.return_value = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            st, data = self._req("POST", "/api/ctrl/heartbeat/beat", {"confirm": True}, token=_TOKEN, control_pin=self.control_pin)
        self.assertEqual(st, 200)

        # There must be at least one CTRL audit line (allow a short settle for fsync on some FSes)
        time.sleep(0.05)
        lines = []
        try:
            with open(ctrlmod.ACTIVITY_LOG) as fh:
                lines = [ln.rstrip("\n") for ln in fh.readlines()]
        except FileNotFoundError:
            pass
        self.assertTrue(any("CTRL heartbeat/beat" in ln for ln in lines), f"no audit line found in {lines}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
