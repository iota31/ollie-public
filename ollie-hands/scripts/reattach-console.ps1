# Reattach Ollie's interactive session to the physical console so screen
# capture (observe) keeps working after an RDP connect/disconnect.
#
# Why: the box has ONE interactive session running the engine. When someone
# RDPs in and out, that session is left "Disconnected" and its desktop stops
# rendering -> BitBlt screenshots fail with "Access is denied". Moving the
# session back to the console (tscon) makes its desktop active again.
#
# Run as SYSTEM (tscon to console needs SeTcbPrivilege). Invoked by the
# OllieConsoleReattach scheduled task on a session-disconnect event, and safe
# to run by hand any time.

$ErrorActionPreference = "SilentlyContinue"
$EngineUser = "Source"

Start-Sleep -Seconds 2   # let the disconnect settle before we reconnect

# Find the engine user's session and its state from `query session`.
$lines = query session 2>$null
foreach ($l in $lines) {
    if ($l -match "$EngineUser\s+(\d+)\s+(\w+)") {
        $id = $matches[1]; $state = $matches[2]
        if ($state -ne "Active") {
            Write-Output "reattaching session $id ($EngineUser, $state) -> console"
            tscon $id /dest:console
        } else {
            Write-Output "session $id ($EngineUser) already Active; nothing to do"
        }
        break
    }
}
