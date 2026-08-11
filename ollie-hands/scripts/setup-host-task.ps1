# Canonical registration of the OllieHands engine scheduled task (run elevated).
# This is the durable source of truth so a reinstall reproduces a working box.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-host-task.ps1
#
# Decisions baked in (see Plans/graceful-questing-oasis.md + hands-completion.md):
# - Runs as Source in the INTERACTIVE session (session 1) so screen capture +
#   UIA see the real desktop.
# - RunLevel Highest: the engine runs ELEVATED on this dedicated box (UAC /
#   secure-desktop-adjacent actions + more apps reachable). Source is admin.
# - Battery conditions OFF: the box is a laptop acting as an always-on body;
#   it must start + keep running on battery (default tasks refuse on battery
#   and silently park "Queued").
# - MultipleInstances Parallel: a restart's new instance must not be blocked by
#   a ghost/lingering instance (restart-host.ps1 frees :3200 first).
# - Trigger AtLogon: auto-start with the (auto-logon) session.
# - Repetition on the logon trigger (Interval=PT5M, StopAtDurationEnd=$false):
#   a one-shot boot-time crash of run.bat/supervisor.py used to silently park the
#   engine for the lifetime of the session — Task Scheduler would never re-fire
#   a LogonTrigger without an explicit <Repetition>. Now any death during boot
#   retries every 5 minutes until the sentinel binds :3201 (engine healthy).

$ErrorActionPreference = "Stop"
$EngineUser = "Source"

$action  = New-ScheduledTaskAction -Execute "C:\ollie-hands\run.bat"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $EngineUser
$trigger.Repetition.Interval = "PT5M"
$trigger.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal -UserId $EngineUser `
    -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances Parallel

Register-ScheduledTask -TaskName "OllieHands" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

$t = Get-ScheduledTask -TaskName OllieHands
Write-Host ("OllieHands registered: user={0} logon={1} runlevel={2} battery_disallow={3}" -f `
    $t.Principal.UserId, $t.Principal.LogonType, $t.Principal.RunLevel,
    $t.Settings.DisallowStartIfOnBatteries) -ForegroundColor Green
Write-Host ("Logon repetition: interval={0} stopAtDurationEnd={1}" -f `
    $t.Triggers[0].Repetition.Interval, $t.Triggers[0].Repetition.StopAtDurationEnd) `
    -ForegroundColor Green
Write-Host "Restart the engine with scripts\restart-host.ps1 to apply." -ForegroundColor Yellow
