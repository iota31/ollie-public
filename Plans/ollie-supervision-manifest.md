# Ollie Supervision Manifest

> One page that makes the **supervision contract** explicit, so the next
> asymmetry is caught by reading this — not discovered at 3am. Born from the D4
> finding that the host engine was the only component that didn't self-heal.

## The contract (every component must meet all four)
1. **Auto-restart on crash** — the process comes back without a human.
2. **Auto-start on boot** — comes up after a reboot without a human.
3. **Health-checked by the watchdog** — the single observability authority pages
   the owner (Telegram) when it's down.
4. **Consistent logging** — a known log to read.

There is **no single supervisor process** — and there can't be: the hands engine
must run on the **Windows host desktop (session 1)** for UIA/screenshots, which
systemd (in WSL) cannot supervise, and a Windows *service* runs in session 0
(no desktop). So the contract is met **per-context** (systemd in WSL, scheduled
task + Python supervisor on the host), and the watchdog is the one pane of glass.

## Components

| Component | Where | Supervisor | Auto-restart on crash | Boot | Health check |
|---|---|---|---|---|---|
| **Gateway / brain** (`openclaw gateway`, :18789) | WSL `OpenClawGateway`, systemd-user | `openclaw-gateway.service` `Restart=always` | ✅ ~5s | ✅ `WantedBy=default.target` | watchdog `check_gateway` |
| **Watchdog** (`ollie_watchdog.py`) | WSL, systemd-user | `ollie-watchdog.service` | ✅ (systemd) | ✅ | self (systemd restarts it; it watches everything else) |
| **4DPocket proxy** (`4dpocket-proxy.py`, :4040) | WSL, systemd-user | systemd-user unit | ✅ | ✅ | (gateway MCP `4dpocket`) |
| **Desktop proxy** (`desktop-proxy.py`) | WSL, systemd-user | systemd-user unit | ✅ | ✅ | — |
| **ngrok** (tunnel → :18789) | WSL, systemd-user | systemd-user unit | ✅ | ✅ | watchdog `check_public` |
| **Jobs runner** (`ollie_jobs_runner.py`) | WSL, systemd-user | systemd-user unit | ✅ | ✅ | watchdog `check_jobs_runner` |
| **ollie-hands engine** (`ollie_hands.server`, :3200) | **Windows host, session 1** | `OllieHands` task → `run.bat` → **`supervisor.py`** | ✅ ~5s (Python supervisor, sentinel-port single-instance) | ✅ AutoLogon(Source)+AtLogon | watchdog `check_hands_reachable/enabled/screenshot` (D4) |
| **Audit off-box sync** | Windows host | `OllieHandsAuditSync` task (hourly) | n/a (re-fires hourly) | ✅ | (push lands in `onllm-dev/ollie-state`) |
| **Console reattach** | Windows host | `OllieConsoleReattach` SYSTEM task (on session-disconnect) | n/a (event-triggered) | ✅ | observe `screenshot_status` |
| **State backup** (nightly, WSL state) | WSL | systemd-user timer `ollie-state-backup` | n/a (timer) | ✅ | — |

## Recovery layers (what self-heals vs what needs hands)
1. **A process crashes (host/WSL stays up)** → ✅ auto-restarts (systemd `Restart=always` in WSL; the Python supervisor on the host). The hands engine gap is now closed.
2. **The OS reboots** (crash that auto-reboots, or powered back on) → ✅ everything restarts: WSL systemd units on boot; the host engine via AutoLogon(Source)+AtLogon.
3. **The laptop powers off and stays off** (hard halt / power loss / shutdown) → ⚠️ **software can't help** — needs BIOS "Restore on AC Power Loss" and/or Wake-on-LAN (wakes from *sleep* only). Owner-verified hardware/firmware config; not remotely settable.

## How to check the whole system's health
- **One pane of glass:** the watchdog (`~/.openclaw/logs/watchdog.log`) runs every
  15 min and pages Telegram on any component down. `state["failures"]` in
  `~/.openclaw/plugin-state/watchdog-state.json` is the current failure set.
- **Host engine logs:** `C:\ProgramData\ollie-hands\server.log` (engine) +
  `C:\ProgramData\ollie-hands\supervisor.log` (supervisor lifecycle).
- **Host engine restart:** `scripts/restart-host.ps1` (sets `SUPERVISOR-STOP`,
  kills supervisor+engine by port, restarts). Auto-restart config:
  `scripts/setup-engine-restart.ps1` (IgnoreNew + RestartCount=0).

## Open / owner-verified
- **BIOS auto-power-on + Wake-on-LAN** (recovery layer 3) — owner to verify on the
  physical machine.
- **Who watches the watchdog itself** — systemd restarts it on crash, but a silent
  watchdog (running but wedged) has no external check. Acceptable for one box;
  note it if scaling.
