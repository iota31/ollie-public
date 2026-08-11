# Ollie watchdog

Health + quota monitor that kills *silent* degradation. Alerts the owner on
Telegram only on **state changes** (fail→ok, ok→fail), so no spam.

- **Deploys to:** `/home/openclaw/bin/ollie_watchdog.py` on the box.
- **Service:** `ollie-watchdog.service` → `~/.config/systemd/user/` (+ `default.target.wants` symlink). `systemctl --user enable --now ollie-watchdog.service`.
- **Every 15 min:** gateway webhook challenge (local + public via ngrok), jobs runner alive, tailnet/4DPocket reachable, stale running jobs, root disk usage.
- **Once/day:** provider quota probes — MiniMax LLM, Zeus (Opus proxy), Groq, NVIDIA Nemotron, Brave search. Catches the "search/LLM silently died" class of failure.
- Reads all credentials at runtime from existing box files; stores no secrets.
- State: `~/.openclaw/plugin-state/watchdog-state.json`; log: `~/.openclaw/logs/watchdog.log`.
