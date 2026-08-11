@echo off
REM Hourly off-box audit sync (plan Track D2). Host reads its own audit and pipes
REM it (base64, pipe-safe) into the isolated WSL gateway, which age-encrypts and
REM pushes it off-box. No filesystem bridge -> the gateway's host-FS isolation is
REM preserved. Invoked by the OllieHandsAuditSync scheduled task.
python "C:\ollie-hands\scripts\audit-export.py" | wsl -d OpenClawGateway -u openclaw bash -lc "/home/openclaw/.openclaw/bin/ollie-hands-audit-sync.sh"
