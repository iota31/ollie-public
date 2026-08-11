# setup-shell-user.ps1 — provision the de-privileged shell user (D3 hardening H2).
#
# Creates a standard user `OllieShell` for the brain's L0 shell, stores its
# password DPAPI-encrypted under the engine principal (Source), tightens the
# vault + audit ACLs so OllieShell cannot read them, and creates a neutral work
# dir. Idempotent. Run ONCE on the host as the engine user (Source), over SSH.
#
# Rollback: delete C:\ProgramData\ollie-hands\shelluser.cred (engine reverts to
# the legacy Source shell), then optionally `Remove-LocalUser OllieShell` and
# restore vault/audit ACL inheritance.
$ErrorActionPreference = "Stop"

$User       = "OllieShell"
$EngineUser = $env:USERNAME          # Source, when run as the engine principal
$Root       = "C:\ProgramData\ollie-hands"
$Vault      = Join-Path $Root "vault"
$Audit      = Join-Path $Root "audit"
$Work       = Join-Path $Root "work"
$Cred       = Join-Path $Root "shelluser.cred"

Write-Output "engine user (cred owner) = $EngineUser"

# 1) Standard user with a strong random password (never admin) -----------------
Add-Type -AssemblyName System.Web
Add-Type -AssemblyName System.Security   # for [System.Security.Cryptography.ProtectedData]
$pw  = [System.Web.Security.Membership]::GeneratePassword(28, 8)
$sec = ConvertTo-SecureString $pw -AsPlainText -Force
if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $User -Password $sec
    Write-Output "reset password for existing $User"
} else {
    New-LocalUser -Name $User -Password $sec -PasswordNeverExpires `
        -AccountNeverExpires -UserMayNotChangePassword `
        -Description "ollie-hands de-privileged shell" | Out-Null
    Write-Output "created $User"
}
# Never an administrator.
Remove-LocalGroupMember -Group "Administrators" -Member $User -ErrorAction SilentlyContinue

# 2) Store the password DPAPI-encrypted under the engine user (raw blob, so the
#    Python engine can CryptUnprotectData it in-process) ------------------------
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$bytes = [System.Text.Encoding]::UTF8.GetBytes($pw)
$enc   = [System.Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser')
[System.IO.File]::WriteAllBytes($Cred, $enc)
$pw = $null; $bytes = $null
# cred readable only by SYSTEM/Admins/engine user; OllieShell has no path to it.
icacls $Cred /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" "${EngineUser}:(R)" | Out-Null
Write-Output "stored cred -> $Cred"

# 3) Neutral work dir the shell child can read/write ---------------------------
New-Item -ItemType Directory -Force -Path $Work | Out-Null
icacls $Work /grant "${User}:(OI)(CI)(M)" | Out-Null

# 4) Tighten vault + audit: drop inherited Users, deny OllieShell --------------
foreach ($d in @($Vault, $Audit)) {
    if (Test-Path $d) {
        icacls $d /inheritance:r | Out-Null
        icacls $d /grant:r "SYSTEM:(OI)(CI)(F)" "Administrators:(OI)(CI)(F)" "${EngineUser}:(OI)(CI)(F)" | Out-Null
        icacls $d /deny "${User}:(OI)(CI)(RX)" | Out-Null
        Write-Output "tightened ACL: $d"
    } else {
        Write-Output "skip (absent): $d"
    }
}

# 5) seclogon must be available for CreateProcessWithLogonW --------------------
Set-Service -Name seclogon -StartupType Manual -ErrorAction SilentlyContinue
Write-Output "seclogon startup = $((Get-Service seclogon).StartType)"

Write-Output "=== vault ACL now ==="
icacls $Vault
Write-Output "DONE provisioning $User"
