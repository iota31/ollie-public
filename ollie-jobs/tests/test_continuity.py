#!/usr/bin/env python3
"""Continuity / ground-truth evidence-layer tests.

Runnable WITHOUT the box:  python3 ollie-jobs/tests/test_continuity.py

These scripts compute their paths at import time (HOME hardcoded to
/home/openclaw). Tests import the modules once, then REPOINT the relevant
module-level path constants to a per-test tempdir before exercising the
functions. No box, no network, stdlib only.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import warnings

# Production code uses the repo's flat `json.load(open(...))` house style; that
# trips ResourceWarning under the test runner's GC. Silence it so the bare
# `python3 .../test_continuity.py` invocation exits clean.
warnings.simplefilter("ignore", ResourceWarning)

# Make the flat bin-style imports work (modules live beside each other).
HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.dirname(HERE)
sys.path.insert(0, JOBS_DIR)

import ollie_work_digest as dig          # noqa: E402
import ollie_jobs_runner as runner       # noqa: E402
import ollie_heartbeat as hb             # noqa: E402


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def _write_done_job(done_dir, name, task, finished_epoch, **extra):
    os.makedirs(done_dir, exist_ok=True)
    job = {"task": task, "status": "done", "finished": _iso(finished_epoch)}
    job.update(extra)
    with open(f"{done_dir}/{name}.json", "w") as f:
        json.dump(job, f)


class TrailerParsing(unittest.TestCase):
    def test_present(self):
        result = ("Did the thing.\n"
                  "STATUS_CLASS: benchmarked\n"
                  "EVIDENCE: /tmp/a.txt, ~/b.json")
        claimed, ev = runner.parse_trailers(result)
        self.assertEqual(claimed, "benchmarked")
        self.assertEqual(ev, ["/tmp/a.txt", "~/b.json"])

    def test_case_insensitive(self):
        claimed, ev = runner.parse_trailers("status_class: Shipped\nevidence: /x")
        self.assertEqual(claimed, "shipped")
        self.assertEqual(ev, ["/x"])

    def test_absent(self):
        claimed, ev = runner.parse_trailers("just a normal answer, no trailers")
        self.assertIsNone(claimed)
        self.assertEqual(ev, [])

    def test_invalid_class_ignored(self):
        claimed, ev = runner.parse_trailers("STATUS_CLASS: magicked\nEVIDENCE: /y")
        self.assertIsNone(claimed)            # invalid -> not accepted
        self.assertEqual(ev, ["/y"])

    def test_result_left_intact(self):
        # parse_trailers must not mutate / strip the result.
        text = "answer\nSTATUS_CLASS: installed\n"
        runner.parse_trailers(text)
        self.assertIn("STATUS_CLASS: installed", text)


class DowngradeMatrix(unittest.TestCase):
    def test_benchmarked_no_evidence_downgrades(self):
        self.assertEqual(
            runner.derive_status_class("benchmarked", lab_ran=False, ev_ok=False),
            "researched")

    def test_benchmarked_with_lab_keeps(self):
        self.assertEqual(
            runner.derive_status_class("benchmarked", lab_ran=True, ev_ok=False),
            "benchmarked")

    def test_benchmarked_with_evidence_keeps(self):
        self.assertEqual(
            runner.derive_status_class("benchmarked", lab_ran=False, ev_ok=True),
            "benchmarked")

    def test_shipped_no_evidence_downgrades(self):
        self.assertEqual(
            runner.derive_status_class("shipped", lab_ran=False, ev_ok=False),
            "researched")

    def test_installed_with_evidence_keeps(self):
        self.assertEqual(
            runner.derive_status_class("installed", lab_ran=False, ev_ok=True),
            "installed")

    def test_installed_no_evidence_downgrades(self):
        self.assertEqual(
            runner.derive_status_class("installed", lab_ran=False, ev_ok=False),
            "researched")

    def test_missing_claim_is_researched(self):
        self.assertEqual(
            runner.derive_status_class(None, lab_ran=True, ev_ok=True),
            "researched")


class EvidenceVerified(unittest.TestCase):
    def test_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.txt")
            open(p, "w").write("x")
            self.assertTrue(runner.evidence_verified([p]))

    def test_missing_file(self):
        self.assertFalse(runner.evidence_verified(["/no/such/path/xyz"]))

    def test_empty(self):
        self.assertFalse(runner.evidence_verified([]))


class RanInLabWindow(unittest.TestCase):
    def _audit(self, d, lines):
        p = os.path.join(d, "audit.log")
        open(p, "w").write("\n".join(lines) + "\n")
        return p

    def test_exec_inside_window(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            mid = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
            audit = self._audit(d, [
                f"{mid} SPAWN poc-test -> deadbeef",
                f"{mid} EXEC poc-test (600s): python bench.py",
            ])
            started = _iso(now - 120)
            finished = _iso(now)
            self.assertTrue(runner.ran_in_lab(started, finished, audit))

    def test_exec_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 10000))
            audit = self._audit(d, [f"{old} EXEC poc-test (600s): something"])
            self.assertFalse(
                runner.ran_in_lab(_iso(now - 120), _iso(now), audit))

    def test_no_exec_lines(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
            audit = self._audit(d, [f"{t} DENIED bad id", f"{t} HARVEST poc-x -> 3 file(s)"])
            self.assertFalse(
                runner.ran_in_lab(_iso(now - 120), _iso(now), audit))

    def test_missing_audit_file(self):
        self.assertFalse(
            runner.ran_in_lab(_iso(time.time() - 60), _iso(time.time()),
                              "/no/such/audit.log"))


class DigestBuilder(unittest.TestCase):
    def setUp(self):
        self._saved = (dig.WORKSPACE, dig.LOGS)
        self.tmp = tempfile.mkdtemp()
        dig.WORKSPACE = os.path.join(self.tmp, "workspace")
        dig.LOGS = os.path.join(self.tmp, "logs")
        os.makedirs(dig.WORKSPACE, exist_ok=True)

    def tearDown(self):
        dig.WORKSPACE, dig.LOGS = self._saved

    def test_creates_file_and_cap(self):
        now = time.time()
        done = f"{dig.WORKSPACE}/jobs/done"
        _write_done_job(done, "j1", "Benchmark CHANGED Whisper model", now - 100,
                        status_class="benchmarked", ran_in_lab=True,
                        evidence=["/tmp/x"], evidence_verified=True)
        _write_done_job(done, "j2", "Research Postgres pgvector tuning", now - 200,
                        status_class="researched")
        text = dig.build_digest()
        self.assertTrue(os.path.exists(f"{dig.WORKSPACE}/WORK_DIGEST.md"))
        self.assertLessEqual(len(text), 2000)
        self.assertIn("benchmarked", text)
        self.assertIn("researched", text)
        self.assertIn("ground truth", text)

    def test_survives_missing_dirs(self):
        # No jobs/, no lab/, no projects/ — must not raise.
        text = dig.build_digest()
        self.assertIn("WORK DIGEST", text)
        self.assertTrue(os.path.exists(f"{dig.WORKSPACE}/WORK_DIGEST.md"))

    def test_truncation_marker(self):
        now = time.time()
        done = f"{dig.WORKSPACE}/jobs/done"
        for i in range(60):
            _write_done_job(done, f"job{i}", f"Task number {i} with a long descriptive title here",
                            now - i * 60, status_class="researched")
        text = dig.build_digest()
        self.assertLessEqual(len(text), 2000)
        self.assertIn("…(truncated)", text)


class VerbAnnotation(unittest.TestCase):
    def test_researched_item_annotated(self):
        # distinctive token of this title is "Whisper" (longest Capitalized
        # word >=4 chars), which also appears in the brief sentence with a verb.
        jobs = [{"task": "Whisper transcription evaluation", "status_class": "researched",
                 "finished": _iso(time.time())}]
        brief = "Today we ran Whisper on the new clips. All good otherwise."
        out = hb.ground_brief(brief, jobs)
        self.assertIn("[researched only — not executed]", out)
        # annotation lands on the sentence with the verb + token
        self.assertIn("Whisper", out.split("[researched only")[0])

    def test_benchmarked_item_untouched(self):
        jobs = [{"task": "Whisper transcription evaluation", "status_class": "benchmarked",
                 "finished": _iso(time.time())}]
        brief = "Today we ran Whisper on the new clips."
        out = hb.ground_brief(brief, jobs)
        self.assertNotIn("[researched only", out)

    def test_multiline_brief_formatting_preserved(self):
        # annotation must not flatten the brief's line structure (bullets,
        # blank lines) — only the matching line gains the tag.
        jobs = [{"task": "Whisper transcription evaluation", "status_class": "researched",
                 "finished": _iso(time.time())}]
        brief = "🔮 morning brief\n\n— lab —\n- we benchmarked Whisper overnight\n- other note stays"
        out = hb.ground_brief(brief, jobs)
        body = out.split("\n\n— ground truth")[0]
        self.assertEqual(body.count("\n"), brief.count("\n"))
        self.assertIn("- we benchmarked Whisper overnight [researched only — not executed]", body)
        self.assertIn("- other note stays", body)

    def test_no_verb_no_annotation(self):
        jobs = [{"task": "Research Whisper alternatives", "status_class": "researched",
                 "finished": _iso(time.time())}]
        brief = "Whisper is an interesting topic to consider someday."
        out = hb.ground_brief(brief, jobs)
        # 'consider' is not an execution verb -> sentence not annotated
        self.assertNotIn("[researched only", out.split("— ground truth")[0])


class Footer(unittest.TestCase):
    def test_footer_always_present_with_jobs(self):
        jobs = [
            {"task": "Benchmark Whisper", "status_class": "benchmarked", "finished": _iso(time.time())},
            {"task": "Research pgvector", "status_class": "researched", "finished": _iso(time.time())},
        ]
        out = hb.ground_brief("Some brief body.", jobs)
        self.assertIn("— ground truth:", out)
        self.assertIn("1 executed", out)
        self.assertIn("1 researched", out)

    def test_no_footer_without_jobs(self):
        out = hb.ground_brief("Some brief body.", [])
        self.assertNotIn("— ground truth", out)

    def test_name_cap(self):
        jobs = [{"task": f"Research TopicAlpha{i}", "status_class": "researched",
                 "finished": _iso(time.time())} for i in range(5)]
        out = hb.ground_brief("body", jobs)
        self.assertIn("…", out)


class RecentDoneAndLastBrief(unittest.TestCase):
    def setUp(self):
        # recent_done_jobs moved to the runner (it owns the jobs dir layout) —
        # repoint runner.DONE, which the shared loader reads at call time.
        self._saved = (runner.DONE, hb.LAST_BRIEF_MD)
        self.tmp = tempfile.mkdtemp()
        runner.DONE = os.path.join(self.tmp, "jobs", "done")
        hb.LAST_BRIEF_MD = os.path.join(self.tmp, "LAST_BRIEF.md")

    def tearDown(self):
        runner.DONE, hb.LAST_BRIEF_MD = self._saved

    def test_recent_done_window(self):
        now = time.time()
        done = runner.DONE
        _write_done_job(done, "fresh", "Fresh task", now - 100, status_class="researched")
        _write_done_job(done, "stale", "Stale task", now - (48 * 3600), status_class="researched")
        jobs = hb.recent_done_jobs()
        tasks = [j["task"] for j in jobs]
        self.assertIn("Fresh task", tasks)
        self.assertNotIn("Stale task", tasks)

    def test_last_brief_written(self):
        hb.write_last_brief("Delivered payload body here.")
        self.assertTrue(os.path.exists(hb.LAST_BRIEF_MD))
        content = open(hb.LAST_BRIEF_MD).read()
        self.assertTrue(content.startswith("# Last brief — "))
        self.assertIn("Delivered payload body here.", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
