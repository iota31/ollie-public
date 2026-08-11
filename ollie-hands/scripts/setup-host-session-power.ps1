# One-time host setup for reliable, always-on screen capture (run elevated).
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-host-session-power.ps1
#
# 1. Keep the box awake (it is a laptop acting as Ollie's always-on body):
#    never sleep / never blank the monitor / do nothing on lid close, on BOTH
#    AC and battery.
# 2. Auto-reattach Ollie's session to the console whenever an RDP session
#    disconnects, so observe()'s screenshots keep working unattended. Done via
#    a SYSTEM scheduled task (tscon-to-console needs SeTcbPrivilege) triggered
#    on TerminalServices session-disconnect events.

$ErrorActionPreference = "Stop"
$ReattachScript = "C:\ollie-hands\scripts\reattach-console.ps1"

Write-Host "== keep-awake (powercfg) ==" -ForegroundColor Cyan
foreach ($s in @("standby-timeout-ac","standby-timeout-dc",
                 "monitor-timeout-ac","monitor-timeout-dc",
                 "hibernate-timeout-ac","hibernate-timeout-dc")) {
    powercfg /change $s 0
}
# Lid close = do nothing (laptop runs closed)
powercfg -setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg -setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg -setactive SCHEME_CURRENT
Write-Host "keep-awake applied (never sleep/blank; lid close = nothing)"

Write-Host "== console auto-reattach task ==" -ForegroundColor Cyan
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ReattachScript`""
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
    -LogonType ServiceAccount -RunLevel Highest

# Event trigger: TerminalServices session disconnected (LocalSessionManager 24).
$cls = Get-CimClass -ClassName MSFT_TaskEventTrigger `
    -Namespace Root/Microsoft/Windows/TaskScheduler
$trigger = New-CimInstance -CimClass $cls -ClientOnly
$trigger.Enabled = $true
$trigger.Subscription =
'<QueryList><Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"><Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[(EventID=24)]]</Select></Query></QueryList>'

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "OllieConsoleReattach" -Action $action `
    -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "registered OllieConsoleReattach (SYSTEM, on session-disconnect)"
Write-Host "Done." -ForegroundColor Green
