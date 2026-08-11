# setup-rdp.ps1 — ensure Remote Desktop is enabled and reachable over Tailscale
#
# Run this elevated on the Windows box (via SSH or local console).
#   powershell -ExecutionPolicy Bypass -File C:\ollie-hands\scripts\setup-rdp.ps1
#
# Idempotent. Fixes the most common reasons RDP stops working while SSH still works:
# - fDenyTSConnections registry key
# - "Remote Desktop" firewall group disabled
# - TermService stopped or Manual
#
# Called automatically by setup-host-session-power.ps1.
# This is the persistent version of the quick-fix commands.
# Run after reboots or if RDP suddenly stops responding.

$ErrorActionPreference = "Stop"

Write-Host "== Setting up Remote Desktop (RDP) for recovery access ==" -ForegroundColor Cyan

# 1. Registry: allow RDP connections
$tsPath = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
Set-ItemProperty -Path $tsPath -Name "fDenyTSConnections" -Value 0 -Force
Write-Host "  fDenyTSConnections = 0 (RDP connections allowed)"

# 2. Keep Network Level Authentication (NLA) on — safer, and Tailscale already encrypts
$rdpTcpPath = 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
if (Test-Path $rdpTcpPath) {
    Set-ItemProperty -Path $rdpTcpPath -Name "UserAuthentication" -Value 1 -Force
    Write-Host "  NLA (UserAuthentication) = 1 (recommended)"
}

# 3. Enable the built-in Remote Desktop firewall rules
#    (Tailscale traffic still respects host firewall rules in most configs)
try {
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction Stop | Out-Null
    Write-Host "  Remote Desktop firewall group: enabled"
} catch {
    Write-Host "  Warning: could not enable Remote Desktop firewall group (may need manual check)"
}

# Also make sure the explicit TCP rule is on
Enable-NetFirewallRule -DisplayName "Remote Desktop - User Mode (TCP-In)" -ErrorAction SilentlyContinue | Out-Null

# 4. Service: Automatic + running
Set-Service -Name TermService -StartupType Automatic -ErrorAction SilentlyContinue
$svc = Get-Service TermService
if ($svc.Status -ne 'Running') {
    Write-Host "  Starting TermService..."
    Start-Service TermService -ErrorAction SilentlyContinue
}
$svc = Get-Service TermService
Write-Host "  TermService: $($svc.Status) / StartType=$($svc.StartType)"

# 5. Verify listener
Start-Sleep -Seconds 1
$listener = Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "  Listener on :3389 detected" -ForegroundColor Green
} else {
    Write-Host "  No listener visible yet on :3389 (may appear after a few seconds or a service restart)" -ForegroundColor Yellow
}

# 6. Show current console/RDP sessions for diagnosis
Write-Host ""
Write-Host "Current sessions (query session):"
query session 2>$null

# 7. Make sure the engine user can RDP (Administrators usually suffice, this is belt-and-suspenders)
Add-LocalGroupMember -Group "Remote Desktop Users" -Member "Source" -ErrorAction SilentlyContinue | Out-Null

Write-Host ""
Write-Host "RDP setup complete." -ForegroundColor Green
Write-Host "Try: mstsc /v:<TAILSCALE_IP>   (user: source)"
Write-Host "After connecting/disconnecting, OllieConsoleReattach should restore the console session automatically."