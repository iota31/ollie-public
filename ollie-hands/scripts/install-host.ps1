# ollie-hands Phase-0 host install (run on the Windows box, elevated PowerShell).
# Idempotent: never overwrites an existing config or bearer token.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-host.ps1
#
# Installs to C:\ollie-hands (venv + package), provisions
# C:\ProgramData\ollie-hands\{config.json,bearer.token,audit\} (INERT:
# enabled=false), and prints the gateway wiring snippet.

$ErrorActionPreference = "Stop"

$InstallDir = "C:\ollie-hands"
$DataDir    = "$env:ProgramData\ollie-hands"
$RepoSrc    = Split-Path -Parent $PSScriptRoot   # ...\ollie-hands

Write-Host "== ollie-hands Phase-0 install ==" -ForegroundColor Cyan

# 1. Python check
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH. Install Python 3.11+ first." }
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "python $ver at $($py.Source)"

# 2. Copy package + venv + deps
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Recurse -Force "$RepoSrc\ollie_hands" "$InstallDir\ollie_hands"
Copy-Item -Force "$RepoSrc\requirements.txt" "$InstallDir\requirements.txt"
if (-not (Test-Path "$InstallDir\venv")) {
    & python -m venv "$InstallDir\venv"
}
& "$InstallDir\venv\Scripts\pip.exe" install -q -r "$InstallDir\requirements.txt"
& "$InstallDir\venv\Scripts\python.exe" "$RepoSrc\scripts\patch-playwright-driver.py"

# 3. Provision config + token (inert; no-op if they exist)
Push-Location $InstallDir
& "$InstallDir\venv\Scripts\python.exe" -m ollie_hands.config
Pop-Location
Write-Host "config dir: $DataDir (enabled=false - INERT until you flip it)"

# 4. Firewall: restrict 3200 to the tailnet + WSL, like the Tier-2 POC port.
if (-not (Get-NetFirewallRule -DisplayName "ollie-hands 3200" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "ollie-hands 3200" -Direction Inbound `
        -Protocol TCP -LocalPort 3200 -Action Allow `
        -RemoteAddress 100.64.0.0/10, 172.16.0.0/12 | Out-Null
    Write-Host "firewall rule created (tailnet + WSL subnets only)"
}

$token = Get-Content "$DataDir\bearer.token"
Write-Host ""
Write-Host "== next steps ==" -ForegroundColor Yellow
Write-Host "1. Edit $DataDir\config.json -> ""host"": ""0.0.0.0"", ""enabled"": true"
Write-Host "2. Run:  $InstallDir\venv\Scripts\python.exe -m ollie_hands.server"
Write-Host "3. Gateway (~/.openclaw/openclaw.json) MCP entry:"
Write-Host "   ""hands"": { ""type"": ""http"", ""url"": ""http://<TAILSCALE_IP>:3200/mcp"","
Write-Host "              ""timeout"": 240,"
Write-Host "              ""headers"": { ""Authorization"": ""Bearer $token"" } }"
Write-Host "Kill switch:  ni $DataDir\DISABLED   (delete the file to re-arm)"
