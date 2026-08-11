# setup-audit-sync-task.ps1 — register the hourly off-box audit sync (Track D2).
#
# Runs as Source (the OllieHands principal) in the auto-logged-on interactive
# session so `wsl -u openclaw` reaches the gateway distro. Battery-friendly
# (this box is a laptop). Idempotent: -Force re-registers cleanly. Kept in
# source so a reinstall reproduces the working host.
$ErrorActionPreference = "Stop"

$TaskName = "OllieHandsAuditSync"
$bat = "C:\ollie-hands\scripts\audit-sync.bat"
if (-not (Test-Path $bat)) { throw "missing $bat (deploy scripts first)" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "Source" `
    -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Output ("registered " + $TaskName + " -> " + (Get-ScheduledTask -TaskName $TaskName).State)
