"""ollie-hands engine supervisor — the host-side equivalent of the WSL brain's
systemd Restart=always.

Single-instance via an EXCLUSIVE sentinel port: the OS releases the port the
instant this process dies, so there is no stale-lock-blocks-boot problem (the
failure mode a file/dir lock has). However many times Task Scheduler launches
run.bat, only the one that binds the sentinel runs the engine; the rest exit
harmlessly. Respawns the engine on crash with a short throttle. Clean stop: the
SUPERVISOR-STOP flag (restart-host.ps1 sets it, then kills this process).

Launched by the OllieHands scheduled task (session 1, interactive token) via
run.bat, under the venv python so screen capture + UIA see the real desktop.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

SENTINEL_PORT = 3201
STOP_FLAG = r"C:\ProgramData\ollie-hands\SUPERVISOR-STOP"
ENGINE_DIR = r"C:\ollie-hands"
# Own log file (NOT server.log, which run.bat redirects engine stdout into — two
# writers to one file silently lose the supervisor's lines on Windows).
LOG = r"C:\ProgramData\ollie-hands\supervisor.log"
THROTTLE_S = 5


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor: {msg}\n")
    except OSError:
        pass


def main() -> int:
    # Single-instance: bind an exclusive local sentinel port. A second supervisor
    # fails here and exits. The port is freed by the OS when this process dies,
    # so a crash/reboot never leaves a lock that blocks the next start.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SENTINEL_PORT))
        s.listen(1)
    except OSError:
        log(f"another supervisor holds :{SENTINEL_PORT}; this instance exits")
        return 0

    log("up — single-instance acquired")
    try:
        while not os.path.exists(STOP_FLAG):
            log("starting ollie-hands engine")
            try:
                rc = subprocess.run(
                    [sys.executable, "-m", "ollie_hands.server"],
                    cwd=ENGINE_DIR,
                ).returncode
            except Exception as e:  # noqa: BLE001
                log(f"failed to launch engine: {e}")
                rc = -999
            log(f"engine exited (code {rc})")
            if os.path.exists(STOP_FLAG):
                break
            time.sleep(THROTTLE_S)
        log("STOP flag present; supervisor exiting")
    finally:
        s.close()
    return 0


def _entrypoint() -> int:
    """Outer guard so ANY exception in the supervisor (not just the engine
    subprocess) lands in supervisor.log before exit. The 2026-06-20 boot
    supervisor died with LastTaskResult=267014 and we had no idea why because
    run.bat redirected stderr into server.log, which gets overwritten by
    every fresh run. Recording the traceback here is the cheapest possible
    fix that makes the next boot crash debuggable."""
    try:
        return main()
    except SystemExit as e:
        raise
    except BaseException as e:  # noqa: BLE001 — must catch absolutely everything
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                import traceback
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor CRASH: "
                        f"{type(e).__name__}: {e}\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
