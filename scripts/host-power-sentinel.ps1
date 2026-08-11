# host-power-sentinel.ps1 — Windows-side power bridge for Ollie.
#
# WHY: the gateway lives in the WSL distro OpenClawGateway, which has
# Windows-interop DISABLED — it cannot see Win32_Battery. So the host samples
# the battery and writes power state INTO the distro; the WSL watchdog reads
# that file and alerts.
#
# HOW (delivery): we write via `wsl.exe -d OpenClawGateway -u openclaw -- ...`,
# NOT the \\wsl$ UNC share. The \\wsl$ P9 share is session-bound and does not
# resolve reliably from a scheduled-task / non-interactive context on this box
# (verified: Test-Path \\wsl$\... -> False). `wsl.exe` exec is always available
# to the host and writes the file as the openclaw user inside the distro,
# atomically (temp + mv within one shell). It also respects the distro's
# automount-off boundary (the distro can't read C:, so a shared Windows file
# would never work).
#
# Runs SINGLE-SHOT (no loop): the scheduled task re-fires it every 5 minutes.
#
# Win32_Battery.BatteryStatus semantics (subset we care about):
#   1 = Discharging (running on battery)
#   2 = AC line / fully charged on mains
#   (3..11 are charging / charged / low / critical variants — all imply mains
#    is connected, so anything that is NOT 1 we treat as on_ac = true)
# EstimatedChargeRemaining = integer percent 0..100.

$ErrorActionPreference = 'Stop'

$distroPath = '/home/openclaw/.openclaw/workspace/host-power.json'

try {
    $bat = Get-CimInstance Win32_Battery -ErrorAction Stop | Select-Object -First 1
} catch {
    $bat = $null
}

if ($null -eq $bat) {
    # No battery present (e.g. desktop / dock) — declare permanently on AC.
    $payload = [ordered]@{ ts = (Get-Date).ToUniversalTime().ToString('o')
                           on_ac = $true; pct = $null; status_raw = $null }
} else {
    $status = [int]$bat.BatteryStatus
    $payload = [ordered]@{
        ts         = (Get-Date).ToUniversalTime().ToString('o')
        on_ac      = ($status -ne 1)            # only status 1 == discharging
        pct        = [int]$bat.EstimatedChargeRemaining
        status_raw = $status
    }
}

$json = $payload | ConvertTo-Json -Compress

# Write into the distro via wsl exec. Atomic: write a temp file then mv (rename)
# inside the same shell so the watchdog never reads a half-written file. If the
# distro is down, wsl.exe fails — exit silently (the watchdog's stale-file check
# covers prolonged outages). Pipe JSON via stdin to avoid any quoting hazard.
$tmp = "$distroPath.$PID.tmp"
try {
    $json | & wsl.exe -d OpenClawGateway -u openclaw -- bash -c "cat > '$tmp' && mv -f '$tmp' '$distroPath'"
} catch {
    exit 0
}
