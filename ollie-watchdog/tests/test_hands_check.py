"""Tests for D4 hands liveness checks (MCP-based).

Replaces the old HTTP-probe check_hands() tests.  Exercises check_hands_reachable,
check_hands_enabled, check_screenshot_status and _mcp_call_hands against stubbed
network — no real Hands server.  Run from ollie-watchdog/:

    python3 -m unittest tests.test_hands_check -v
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ollie_watchdog as wd  # noqa: E402


def _fake_mcp_response(text_dict):
    """Build a fake urllib response carrying an MCP SSE data line."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "result": {"content": [{"type": "text",
                                 "text": json.dumps(text_dict)}]}
    })
    raw = f"data: {payload}\n\n".encode()

    class FakeResp:
        headers = {"mcp-session-id": "test-sid"}
        def read(self):
            return raw
    return FakeResp()


def _fake_openclaw(*, hands_disabled=False):
    """Return a fake openclaw.json dict with hands config."""
    cfg = {"mcp": {"servers": {"hands": {"url": "http://127.0.0.1:3200/mcp",
                                          "headers": {"Authorization": "Bearer test-token"}}}}}
    if hands_disabled:
        cfg["mcp"]["servers"]["hands"]["disabled"] = True
    return cfg


class McpCallHandsTest(unittest.TestCase):
    def test_loads_token_from_openclaw(self):
        fake_cfg = _fake_openclaw()
        m = mock.mock_open(read_data=json.dumps(fake_cfg))
        with mock.patch("builtins.open", m), \
             mock.patch("urllib.request.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _fake_mcp_response({"hands_enabled": True})
            wd._mcp_call_hands("session_info")
            first_call = urlopen_mock.call_args_list[0]
            auth = first_call[0][0].headers["Authorization"]
            self.assertEqual(auth, "Bearer test-token")

    def test_raises_when_config_missing(self):
        m = mock.mock_open(read_data="{}")
        with mock.patch("builtins.open", m):
            with self.assertRaises(RuntimeError):
                wd._mcp_call_hands("session_info")


class HandsReachableTest(unittest.TestCase):
    def test_returns_none_when_mcp_succeeds(self):
        with mock.patch.object(wd, "_mcp_call_hands", return_value={"hands_enabled": True}):
            self.assertIsNone(wd.check_hands_reachable())

    def test_returns_error_on_exception(self):
        with mock.patch.object(wd, "_mcp_call_hands", side_effect=RuntimeError("conn refused")):
            r = wd.check_hands_reachable()
            self.assertIn("engine unreachable", r)


class HandsEnabledTest(unittest.TestCase):
    def test_returns_none_when_enabled(self):
        with mock.patch.object(wd, "_mcp_call_hands", return_value={"hands_enabled": True}):
            self.assertIsNone(wd.check_hands_enabled())

    def test_returns_error_when_disabled(self):
        with mock.patch.object(wd, "_mcp_call_hands", return_value={"hands_enabled": False}):
            r = wd.check_hands_enabled()
            self.assertIn("disabled", r)

    def test_returns_none_when_field_absent(self):
        with mock.patch.object(wd, "_mcp_call_hands", return_value={"other": "val"}):
            self.assertIsNone(wd.check_hands_enabled())


class ScreenshotStatusTest(unittest.TestCase):
    def test_returns_none_when_ok(self):
        with mock.patch.object(wd, "_mcp_call_hands",
                               return_value={"screenshot_status": "ok"}):
            self.assertIsNone(wd.check_screenshot_status())

    def test_returns_error_when_degraded(self):
        with mock.patch.object(wd, "_mcp_call_hands",
                               return_value={"screenshot_status": "timeout"}):
            r = wd.check_screenshot_status()
            self.assertIn("degraded", r)

    def test_returns_none_when_field_absent(self):
        with mock.patch.object(wd, "_mcp_call_hands", return_value={"other": "val"}):
            self.assertIsNone(wd.check_screenshot_status())


class RunCycleIntegrationTest(unittest.TestCase):
    def test_run_cycle_includes_hands_checks(self):
        """Verify the D4 checks appear in HEALTH_CHECKS (no _SKIP filtering)."""
        self.assertIn("hands-reachable", wd.HEALTH_CHECKS)
        self.assertIn("hands-enabled", wd.HEALTH_CHECKS)
        self.assertIn("hands-screenshot", wd.HEALTH_CHECKS)
        self.assertNotIn("hands-mcp", wd.HEALTH_CHECKS)


if __name__ == "__main__":
    unittest.main()
