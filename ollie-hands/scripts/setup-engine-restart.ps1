# setup-engine-restart.ps1 — make the OllieHands engine auto-restart on crash.
#
# Mechanism: run.bat is a supervised respawn loop (the host-side equivalent of
# the WSL brain's systemd Restart=always). This script configures the task to
# support that cleanly:
#   - MultipleInstances = IgnoreNew  (a restart can't stack a 2nd loop)
#   - RestartCount = 0               (DISABLE Task Scheduler restart-on-failure:
#                                     it does NOT reliably fire for a crashed
#                                     child, AND it belatedly launches duplicate
#                                     loops ~1 min after a stop — the bug we hit)
# Auto-restart comes from the loop, not the scheduler. Idempotent.
$ErrorActionPreference = "Stop"
$t = Get-ScheduledTask -TaskName OllieHands
$t.Settings.MultipleInstances = 'IgnoreNew'
$t.Settings.RestartCount       = 0
$t.Settings.RestartInterval    = $null
$t | Set-ScheduledTask | Out-Null
$v = (Get-ScheduledTask -TaskName OllieHands).Settings
Write-Output ("MultipleInstances=" + $v.MultipleInstances + " RestartCount=" + $v.RestartCount + " RestartInterval=" + $v.RestartInterval)
