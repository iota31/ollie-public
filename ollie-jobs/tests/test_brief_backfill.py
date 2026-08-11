#!/usr/bin/env python3
"""Brief-mtime backfill: the pure picker for the newest delivered brief job.

Guards the false 'proactivity stuck' alert — a brief delivered via the jobs
path (not the MESSAGE: protocol) must still be recognized so LAST_BRIEF.md's
mtime tracks reality.

Runnable without the box:  python3 ollie-jobs/tests/test_brief_backfill.py
"""
import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
JOBS = REPO / "ollie-jobs"


def _load():
    sys.path.insert(0, str(JOBS))  # so heartbeat's `import ollie_jobs_runner` resolves
    spec = importlib.util.spec_from_file_location("ollie_heartbeat", JOBS / "ollie_heartbeat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HB = _load()


class BriefBackfill(unittest.TestCase):
    def test_picks_newest_delivered_brief(self):
        jobs = [
            {"task": "Send this brief to Tushar", "delivered": True,  "finished": "2026-06-20T04:31:32"},
            {"task": "morning brief",             "delivered": True,  "finished": "2026-06-20T08:30:00"},
            {"task": "brief draft",               "delivered": False, "finished": "2026-06-20T09:00:00"},  # undelivered
            {"task": "run lab benchmark",         "delivered": True,  "finished": "2026-06-20T10:00:00"},  # not a brief
        ]
        self.assertEqual(HB._newest_delivered_brief_ts(jobs),
                         HB.parse_job_ts("2026-06-20T08:30:00"))

    def test_none_when_no_delivered_brief(self):
        self.assertIsNone(HB._newest_delivered_brief_ts(
            [{"task": "brief", "delivered": False, "finished": "2026-06-20T08:00:00"}]))


if __name__ == "__main__":
    unittest.main()
