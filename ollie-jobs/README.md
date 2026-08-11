# `ollie-jobs` — Pillar-1 background jobs system

File-queue background-job runner for Ollie. `ollie_jobs_runner.py` polls
`~/.openclaw/workspace/jobs/queue`, runs each job as a fresh `openclaw agent`
CLI session (25-min cap), and delivers the result via WhatsApp Graph API or
Telegram Bot API. Ledger: `~/.openclaw/workspace/jobs/{queue,running,done,failed}`.

Deploys on the box: runner at `/home/openclaw/bin/ollie_jobs_runner.py`,
supervised by the systemd **user** unit `ollie-jobs.service`
(`~/.config/systemd/user/`). `job-submit.sh` is called by the agent per the
AGENTS.md doctrine to enqueue jobs.

NOTE: the committed `ollie-jobs.service` has its `BRAVE_API_KEY` Environment
line redacted — real value in `secrets.local.md` (gitignored).
