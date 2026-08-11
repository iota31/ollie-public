#!/usr/bin/env python3
"""Containment-leak prevention tests (QW-2).

Runnable WITHOUT the box:  python3 ollie-jobs/tests/test_leak_prevention.py

Guards the fix for the 2026-06-11 leak: a heartbeat-composed "LAB POC" job
paraphrased the HEARTBEAT.md template, dropped the lab-sandbox rule, and
pip/uv-installed internet code straight into the gateway distro. The runner now
RE-INJECTS the doctrine preamble for any POC task, so a sloppy task can't bypass
the sandbox.

Same off-box pattern as test_continuity.py: import the module once, repoint the
module-level path constants to a tempdir, exercise the pure functions. No box,
no network, stdlib only.
"""
import json
import os
import sys
import tempfile
import unittest
import warnings

# Production code uses the repo's flat `json.load(open(...))` house style; that
# trips ResourceWarning under the test runner's GC. Silence it so the bare
# `python3 .../test_leak_prevention.py` invocation exits clean.
warnings.simplefilter("ignore", ResourceWarning)

# Make the flat bin-style imports work (modules live beside each other).
HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.dirname(HERE)
sys.path.insert(0, JOBS_DIR)

import ollie_jobs_runner as runner  # noqa: E402


# The exact rule text the runner must re-inject — kept verbatim so a doctrine
# edit that silently weakens the rule fails this test.
EXPECTED_RULE = (
    "Use the lab sandbox CLI via the exec tool — `lab` is the ONLY way to run "
    "save-derived code, NEVER install/clone/run it on this machine. If lab is "
    "unavailable, do web research only and say so."
)

# The actual leak job's composed task (doctrine-stripped: "install it, benchmark
# it on this machine"). The guard must catch THIS exact string.
LEAK_TASK = (
    "LAB POC on a save of Tushar's: On-device TTS model (66M params, 17x faster "
    "than realtime, open source). Instagram source: https://example/reel. "
    "Find the actual GitHub repo. Install it, benchmark it on this machine, "
    "compare quality/speed to our current Ollie TTS stack."
)


class PocDetection(unittest.TestCase):
    def test_leak_task_is_poc(self):
        self.assertTrue(runner.is_poc_task(LEAK_TASK))

    def test_marker_case_insensitive(self):
        self.assertTrue(runner.is_poc_task("lab poc on a save: foo"))
        self.assertTrue(runner.is_poc_task("Lab  Poc with extra spaces"))

    def test_research_task_not_poc(self):
        self.assertFalse(
            runner.is_poc_task("LAB RESEARCH on a save of Tushar's: Nango"))

    def test_plain_task_not_poc(self):
        self.assertFalse(runner.is_poc_task("Compare headphone prices on Amazon"))

    def test_word_boundary_not_substring(self):
        # "collab poclike" must NOT trip the marker — \b boundaries required.
        self.assertFalse(runner.is_poc_task("collaboration poclike research"))

    def test_none_and_empty(self):
        self.assertFalse(runner.is_poc_task(None))
        self.assertFalse(runner.is_poc_task(""))


class PreambleInjection(unittest.TestCase):
    def test_poc_gets_full_rule(self):
        pre = runner.lab_preamble(LEAK_TASK)
        self.assertTrue(pre)
        self.assertIn(EXPECTED_RULE, pre)
        self.assertIn("CONTAINMENT", pre)
        # explicitly forbids the install/clone that caused the leak
        self.assertIn("pip/uv/npm-install", pre)
        self.assertIn("git clone", pre)
        # ends with a blank-line separator so it reads cleanly before "Task:"
        self.assertTrue(pre.endswith("\n\n"))

    def test_non_poc_untouched(self):
        self.assertEqual(runner.lab_preamble("LAB RESEARCH on a save: Nango"), "")
        self.assertEqual(runner.lab_preamble("set a reminder for 5 minutes"), "")
        self.assertEqual(runner.lab_preamble(""), "")

    def test_preamble_overrides_sloppy_task(self):
        # Even though the task literally says "on this machine", the prepended
        # doctrine explicitly overrides it and bans on-machine install.
        pre = runner.lab_preamble(LEAK_TASK)
        self.assertIn("overrides any conflicting instruction", pre)
        self.assertIn("NEVER install/clone/run it on this machine", pre)


class PromptComposition(unittest.TestCase):
    """The preamble must land BETWEEN the job header and the task body, exactly
    as run_job composes it — verified without spawning openclaw."""

    def _compose(self, task):
        job_id = "20260611-231647-16562"
        job = {"channel": "telegram", "to": "<OWNER_TELEGRAM_CHAT_ID>", "task": task}
        preamble = runner.lab_preamble(job["task"])
        return (
            f"BACKGROUND JOB {job_id} (requested via {job['channel']} by {job['to']}).\n"
            f"{preamble}"
            f"Task: {job['task']}\n\n"
        )

    def test_poc_prompt_has_doctrine_before_task(self):
        prompt = self._compose(LEAK_TASK)
        self.assertIn(EXPECTED_RULE, prompt)
        # doctrine appears before the task body
        self.assertLess(prompt.index("CONTAINMENT"), prompt.index("Task:"))

    def test_non_poc_prompt_unchanged(self):
        task = "Compare headphone prices on Amazon India"
        prompt = self._compose(task)
        self.assertNotIn("CONTAINMENT", prompt)
        self.assertNotIn("lab sandbox", prompt)
        self.assertIn(f"Task: {task}", prompt)


class GuardNeverBreaksJob(unittest.TestCase):
    """A bug in the guard must degrade to no-preamble, never raise into the job
    execution path (the runner is a Restart=always daemon)."""

    def test_is_poc_swallows_errors(self):
        # An object whose truthiness/regex use would explode -> False, no raise.
        class Boom:
            def __bool__(self):
                raise RuntimeError("boom")
        # passing a non-str: re.search raises TypeError internally -> caught
        self.assertFalse(runner.is_poc_task(12345))
        self.assertFalse(runner.is_poc_task(["LAB POC"]))

    def test_preamble_swallows_errors(self):
        # Monkeypatch is_poc_task to blow up; lab_preamble must still return ""
        orig = runner.is_poc_task
        try:
            def boom(_):
                raise RuntimeError("kaboom")
            runner.is_poc_task = boom
            self.assertEqual(runner.lab_preamble("LAB POC anything"), "")
        finally:
            runner.is_poc_task = orig


class RunJobIntegration(unittest.TestCase):
    """End-to-end through run_job with openclaw stubbed: prove a POC job's
    prompt carries the doctrine and a non-POC job's does not. Repoints the
    runner's path + subprocess like test_continuity repoints DONE."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (runner.QUEUE, runner.RUNNING, runner.DONE, runner.FAILED,
                       runner.LOG, subprocess_run := runner.subprocess.run,
                       runner.deliver, runner.refresh_work_digest)
        for attr in ("QUEUE", "RUNNING", "DONE", "FAILED"):
            d = os.path.join(self.tmp, attr.lower())
            os.makedirs(d, exist_ok=True)
            setattr(runner, attr, d)
        runner.LOG = os.path.join(self.tmp, "jobs.log")
        self.captured_prompt = {}

        def fake_run(argv, **kw):
            # capture the -m prompt the runner would have sent to openclaw
            self.captured_prompt["prompt"] = argv[argv.index("-m") + 1]

            class P:
                returncode = 0
                stdout = "All done."
                stderr = ""
            return P()

        runner.subprocess.run = fake_run
        runner.deliver = lambda *a, **k: None
        runner.refresh_work_digest = lambda: None

    def tearDown(self):
        (runner.QUEUE, runner.RUNNING, runner.DONE, runner.FAILED,
         runner.LOG, runner.subprocess.run, runner.deliver,
         runner.refresh_work_digest) = self._saved

    def _queue_and_run(self, task):
        job = {"id": "j1", "channel": "telegram", "to": "<OWNER_TELEGRAM_CHAT_ID>",
               "task": task, "deliver": False, "agent": "main"}
        path = os.path.join(runner.QUEUE, "j1.json")
        with open(path, "w") as f:
            json.dump(job, f)
        runner.run_job(path)

    def test_poc_job_prompt_carries_doctrine(self):
        self._queue_and_run(LEAK_TASK)
        prompt = self.captured_prompt["prompt"]
        self.assertIn(EXPECTED_RULE, prompt)
        self.assertIn("CONTAINMENT", prompt)

    def test_non_poc_job_prompt_clean(self):
        self._queue_and_run("Compare headphone prices on Amazon")
        prompt = self.captured_prompt["prompt"]
        self.assertNotIn("CONTAINMENT", prompt)
        self.assertNotIn("lab sandbox", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
