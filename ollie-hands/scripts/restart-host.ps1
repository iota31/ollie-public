# Reliable restart of the ollie-hands engine (run on the box).
#
# Why this exists: Stop-ScheduledTask kills run.bat but ORPHANS the python
# process that holds :3200. A plain Start-ScheduledTask then spawns a new
# engine that cannot bind the port and exits immediately (task shows "Ready",
# not "Running") -- so the box keeps serving the OLD code silently. This script
# kills the actual :3200 holder by port, so a deploy/restart truly takes effect.
#
#   powershell -ExecutionPolicy Bypass -File scripts\restart-host.ps1

$ErrorActionPreference = "SilentlyContinue"
$Port = 3200
$StopFlag = "C:\ProgramData\ollie-hands\SUPERVISOR-STOP"

Write-Host "== ollie-hands reliable restart ==" -ForegroundColor Cyan

# Halt the run.bat respawn loop first so the kills below are not re-spawned
# (the loop checks this flag and exits instead of relaunching the engine).
New-Item -ItemType File -Path $StopFlag -Force | Out-Null

Stop-ScheduledTask -TaskName OllieHands

# Kill whatever is actually listening on :3200 (the real engine, orphan or not).
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen
foreach ($c in $conns) {
    $p = Get-Process -Id $c.OwningProcess
    if ($p) { Write-Host "killing :$Port holder pid=$($p.Id) (started $($p.StartTime))"; Stop-Process -Id $p.Id -Force }
}
# Kill any lingering run.bat cmd + ollie python: a leftover run.bat keeps the
# task's instance "running" and the MultipleInstances=IgnoreNew policy then
# silently refuses the new Start (task stays Queued, old code keeps serving).
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
    Where-Object { $_.CommandLine -like '*run.bat*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*ollie_hands*' -or $_.CommandLine -like '*supervisor.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# Also clear any stray browser + stale profile lock so the next launch is clean.
Stop-Process -Name camoufox -Force
Remove-Item C:\OllieChrome\camoufox-profile\parent.lock -Force

Start-Sleep -Seconds 4
$still = Get-NetTCPConnection -LocalPort $Port -State Listen
if ($still) { Write-Host "WARNING: :$Port still held after kill" -ForegroundColor Yellow }

# Clear the stop flag so the freshly-started loop runs (and auto-respawns on crash).
Remove-Item $StopFlag -Force
Start-ScheduledTask -TaskName OllieHands

# Cold start (imports + Camoufox readiness) can take 15-20s; poll up to 40s.
$conn = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen
    if ($conn) { break }
}
if ($conn) {
    $p = Get-Process -Id $conn.OwningProcess
    Write-Host "engine UP: pid=$($p.Id) started=$($p.StartTime) task=$((Get-ScheduledTask -TaskName OllieHands).State)" -ForegroundColor Green
} else {
    Write-Host "ENGINE DID NOT BIND :$Port -- check C:\ProgramData\ollie-hands\server.log" -ForegroundColor Red
}
