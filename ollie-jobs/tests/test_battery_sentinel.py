#!/usr/bin/env python3
"""Offline tests for the battery/power sentinel.

Pure stdlib unittest — no box, no network. Covers:
  - watchdog check_power(): AC->battery transition fires once; low-pct
    escalation threshold + 30-min rate limit; stale/missing detection with the
    6-hour cooldown and first-hour grace window; recovery silence.
  - ollie_work_digest.host_section(): AC / on-battery / unknown / missing.

telegram_alert is monkeypatched to capture sends instead of hitting Telegram.

Run:  python -m pytest ollie-jobs/tests/test_battery_sentinel.py
  or:  python ollie-jobs/tests/test_battery_sentinel.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load(name, relpath):
    """Import a module by file path (the two edited modules live in sibling
    package dirs with hyphens, so load them directly)."""
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

wd = _load("ollie_watchdog", "ollie-watchdog/ollie_watchdog.py")
digest = _load("ollie_work_digest", "ollie-jobs/ollie_work_digest.py")

HOUR = 3600


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class PowerWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.power_file = os.path.join(self.tmp, "host-power.json")
        # Redirect the module's power-file path + capture alerts.
        self._orig_path = wd.HOST_POWER_JSON
        self._orig_alert = wd.telegram_alert
        wd.HOST_POWER_JSON = self.power_file
        self.alerts = []
        wd.telegram_alert = lambda text: (self.alerts.append(text), True)[1]
        # state dict whose baseline is 2h in the past (grace window expired) so
        # blind/stale alerts are allowed by default.
        self.state = {"power": {"baseline_ts": time.time() - 2 * HOUR}}

    def tearDown(self):
        wd.HOST_POWER_JSON = self._orig_path
        wd.telegram_alert = self._orig_alert

    def write(self, on_ac, pct, ts=None, status_raw=None):
        ts = ts if ts is not None else iso(time.time())
        json.dump({"ts": ts, "on_ac": on_ac, "pct": pct,
                   "status_raw": status_raw}, open(self.power_file, "w"))

    # --- (a) transition AC->battery fires exactly once per episode -----------
    def test_transition_alerts_once(self):
        self.write(on_ac=False, pct=80)
        wd.check_power(self.state)
        wd.check_power(self.state)  # still on battery, same episode
        plug = [a for a in self.alerts if "plug me back in" in a]
        self.assertEqual(len(plug), 1, self.alerts)
        self.assertIn("80%", plug[0])

    def test_recovery_then_reunplug_realerts(self):
        self.write(on_ac=False, pct=80)
        wd.check_power(self.state)
        self.write(on_ac=True, pct=82)          # replug -> silent recovery
        wd.check_power(self.state)
        self.write(on_ac=False, pct=79)         # unplug again -> NEW episode
        wd.check_power(self.state)
        plug = [a for a in self.alerts if "plug me back in" in a]
        self.assertEqual(len(plug), 2, self.alerts)

    def test_recovery_is_silent(self):
        self.write(on_ac=True, pct=100)
        wd.check_power(self.state)
        self.assertEqual(self.alerts, [])

    # --- (b) low-pct escalation threshold + rate limit ----------------------
    def test_escalation_threshold(self):
        self.write(on_ac=False, pct=45)         # above 30 -> no escalation
        wd.check_power(self.state)
        self.assertFalse([a for a in self.alerts if "die soon" in a])

        self.write(on_ac=False, pct=25)         # below 30 -> escalate
        wd.check_power(self.state)
        esc = [a for a in self.alerts if "die soon" in a]
        self.assertEqual(len(esc), 1, self.alerts)
        self.assertIn("25%", esc[0])

    def test_escalation_rate_limited(self):
        self.write(on_ac=False, pct=20)
        wd.check_power(self.state)
        self.write(on_ac=False, pct=18)         # immediately again
        wd.check_power(self.state)
        esc = [a for a in self.alerts if "die soon" in a]
        self.assertEqual(len(esc), 1, "escalation should be rate limited")

        # advance the cooldown past 30 min -> second escalation allowed
        self.state["power"]["last_escalate_ts"] -= wd.POWER_ESCALATE_COOLDOWN_S + 1
        self.write(on_ac=False, pct=15)
        wd.check_power(self.state)
        esc = [a for a in self.alerts if "die soon" in a]
        self.assertEqual(len(esc), 2, self.alerts)

    # --- (c) stale / missing detection + grace + cooldown -------------------
    def test_stale_timestamp_alerts(self):
        self.write(on_ac=True, pct=90, ts=iso(time.time() - 30 * 60))  # 30m old
        wd.check_power(self.state)
        self.assertTrue([a for a in self.alerts if "blind" in a], self.alerts)

    def test_missing_file_alerts(self):
        # no file written at all
        wd.check_power(self.state)
        self.assertTrue([a for a in self.alerts if "blind" in a], self.alerts)

    def test_blind_grace_window_suppresses(self):
        # baseline is NOW -> within first-hour grace -> no blind alert yet
        self.state["power"]["baseline_ts"] = time.time()
        wd.check_power(self.state)  # missing file, but in grace
        self.assertEqual(self.alerts, [])

    def test_blind_rate_limited(self):
        wd.check_power(self.state)               # missing -> 1 blind
        wd.check_power(self.state)               # still missing, within 6h
        blind = [a for a in self.alerts if "blind" in a]
        self.assertEqual(len(blind), 1, "blind alert should be 6h rate limited")

    def test_fresh_ac_no_alert(self):
        self.write(on_ac=True, pct=97)
        wd.check_power(self.state)
        self.assertEqual(self.alerts, [])

    def test_corrupt_file_never_raises(self):
        open(self.power_file, "w").write("{ not json")
        # treated as stale/blind, must not raise
        wd.check_power(self.state)
        self.assertTrue([a for a in self.alerts if "blind" in a])


class DigestHostSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # OLLIE_HOME/.openclaw/workspace/host-power.json
        self.ws = os.path.join(self.tmp, ".openclaw", "workspace")
        os.makedirs(self.ws, exist_ok=True)
        self.power_file = os.path.join(self.ws, "host-power.json")

    def write(self, on_ac, pct):
        json.dump({"ts": iso(time.time()), "on_ac": on_ac, "pct": pct},
                  open(self.power_file, "w"))

    def test_on_ac(self):
        self.write(on_ac=True, pct=100)
        s = digest.host_section(home=self.tmp)
        self.assertIn("## Host", s)
        self.assertIn("on AC, battery 100%", s)

    def test_on_battery(self):
        self.write(on_ac=False, pct=64)
        s = digest.host_section(home=self.tmp)
        self.assertIn("ON BATTERY, 64%", s)

    def test_unknown_when_pct_null_and_not_ac(self):
        self.write(on_ac=False, pct=None)
        s = digest.host_section(home=self.tmp)
        self.assertIn("power state unknown", s)

    def test_no_battery_on_ac_pct_null(self):
        self.write(on_ac=True, pct=None)
        s = digest.host_section(home=self.tmp)
        self.assertIn("on AC", s)
        self.assertNotIn("battery", s)

    def test_missing_file_omits_section(self):
        s = digest.host_section(home=self.tmp)   # no file written
        self.assertEqual(s, "")

    def test_build_digest_includes_host(self):
        # build_digest() reads module globals (test_continuity pattern):
        # repoint WORKSPACE/LOGS at the tempdir for the call.
        self.write(on_ac=False, pct=50)
        saved = (digest.WORKSPACE, digest.LOGS)
        digest.WORKSPACE = f"{self.tmp}/.openclaw/workspace"
        digest.LOGS = f"{self.tmp}/.openclaw/logs"
        try:
            d = digest.build_digest()
        finally:
            digest.WORKSPACE, digest.LOGS = saved
        self.assertIn("## Host", d)
        self.assertIn("ON BATTERY, 50%", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
