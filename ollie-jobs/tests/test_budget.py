import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import budget
import ollie_project_tick as project_tick


def _reserve_once(state, config, item_id, out):
    budget.STATE = state
    budget.LOCK = f"{state}.lock"
    budget.CONFIG = config
    out.put(budget.reserve("research", item_id)[0])


def configure(tmp_path, monkeypatch, *, research=2, global_cap=2):
    state = tmp_path / "spend-state.json"
    config = tmp_path / "budget-config.json"
    config.write_text(json.dumps({
        "ceilings": {"research": research, "poc": 2, "project": 2},
        "global_self_directed": global_cap,
    }))
    monkeypatch.setattr(budget, "STATE", str(state))
    monkeypatch.setattr(budget, "LOCK", f"{state}.lock")
    monkeypatch.setattr(budget, "CONFIG", str(config))
    return state, config


def test_reserve_is_idempotent(tmp_path, monkeypatch):
    state, _ = configure(tmp_path, monkeypatch)
    assert budget.reserve("research", "job-1")[0]
    assert budget.reserve("research", "job-1")[0]
    assert json.loads(state.read_text())["counts"] == {"research": 1}


def test_reserve_rejects_cap_and_cross_lane_id_reuse(tmp_path, monkeypatch):
    state, _ = configure(tmp_path, monkeypatch, research=1)
    assert budget.reserve("research", "job-1")[0]
    assert not budget.reserve("research", "job-2")[0]
    ok, why = budget.reserve("poc", "job-1")
    assert not ok
    assert "already reserved" in why
    assert json.loads(state.read_text())["counts"] == {"research": 1}


def test_concurrent_reservations_cannot_overshoot(tmp_path, monkeypatch):
    state, config = configure(tmp_path, monkeypatch, research=3, global_cap=3)
    ctx = multiprocessing.get_context("fork")
    out = ctx.Queue()
    workers = [ctx.Process(target=_reserve_once,
                           args=(str(state), str(config), f"job-{i}", out))
               for i in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
    assert sum(out.get(timeout=1) for _ in workers) == 3
    assert json.loads(state.read_text())["counts"]["research"] == 3


def test_uncapped_lane_does_not_touch_state(tmp_path, monkeypatch):
    state, _ = configure(tmp_path, monkeypatch)
    assert budget.reserve("reactive", "reply-1")[0]
    assert not state.exists()


def test_job_submit_exports_lane_and_reserves_once(tmp_path):
    home = tmp_path / "home"
    bindir = home / "bin"
    bindir.mkdir(parents=True)
    calls = tmp_path / "calls"
    stub = bindir / "budget.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    stub.chmod(0o755)
    script = Path(__file__).parents[1] / "job-submit.sh"
    result = subprocess.run(
        ["bash", str(script), "--channel", "telegram", "--to", "<OWNER_TELEGRAM_CHAT_ID>",
         "--task", "test", "--lane", "research"],
        env={**os.environ, "OLLIE_HOME": str(home)}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines()[0].startswith("reserve research ")
    jobs = list((home / ".openclaw/workspace/jobs/queue").glob("*.json"))
    assert len(jobs) == 1
    assert json.loads(jobs[0].read_text())["lane"] == "research"


def test_project_tick_reserves_before_starting_session(tmp_path, monkeypatch):
    state = {"status": "active", "sessions_today": 0, "sessions_total": 7}
    calls = []

    monkeypatch.setattr(project_tick, "LOCK", str(tmp_path / "tick.lock"))
    monkeypatch.setattr(project_tick, "health_ok", lambda: None)
    monkeypatch.setattr(project_tick, "pick_project", lambda: ("test-project", state))
    monkeypatch.setattr(project_tick, "log", lambda message: calls.append(("log", message)))
    monkeypatch.setattr(project_tick, "run_session",
                        lambda *_: (_ for _ in ()).throw(AssertionError("session must not run")))

    class Refused:
        returncode = 3
        stdout = "project daily cap reached (6/6)\n"
        stderr = ""

    def refuse(command, **kwargs):
        calls.append(("run", command, kwargs))
        return Refused()

    monkeypatch.setattr(project_tick.subprocess, "run", refuse)
    assert project_tick.main() == 0
    command = next(call[1] for call in calls if call[0] == "run")
    assert command[-3:] == ["reserve", "project", "project:test-project:8"]
    assert state["sessions_today"] == 0
    assert any("project daily cap reached" in call[1]
               for call in calls if call[0] == "log")
