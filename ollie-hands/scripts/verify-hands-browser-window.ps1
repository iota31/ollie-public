# Verify that a headed Hands Camoufox browser is attached to the interactive desktop.
#
# Usage (from the interactive desktop after a browser operation has launched Camoufox):
#   powershell -ExecutionPolicy Bypass -File scripts\verify-hands-browser-window.ps1
#
# Exit codes:
#   0 - PASS: at least one visible top-level window belongs to a Hands-owned browser tree
#   1 - FAIL: no matching visible window found (or no profile-bearing browser)
#   2 - ERROR: could not enumerate (e.g., not on Windows, CIM unavailable)
#
# Ownership proof:
# - Root process(es) are Camoufox/Firefox whose CommandLine contains $ProfileDir
# - We walk the process tree via CIM (Win32_Process.ParentProcessId)
# - We require at least one visible (IsWindowVisible + not minimized) top-level HWND
#   whose PID is in the collected tree (or whose owning process tree contains a tree PID).

$ErrorActionPreference = "Stop"

$ProfileDir = "C:\OllieChrome\camoufox-profile"

function Get-DescendantPids {
    param([int[]]$RootPids)
    $all = New-Object System.Collections.Generic.HashSet[int]
    foreach ($r in $RootPids) { [void]$all.Add($r) }
    $q = New-Object System.Collections.Generic.Queue[int]
    foreach ($r in $RootPids) { $q.Enqueue($r) }
    while ($q.Count -gt 0) {
        $parentPid = $q.Dequeue()
        $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentPid" -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty ProcessId
        foreach ($k in $kids) {
            if (-not $all.Contains($k)) { [void]$all.Add([int]$k); $q.Enqueue([int]$k) }
        }
    }
    return ,$all
}

function Get-VisibleTopLevelHwnds {
    $user32 = Add-Type -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool EnumWindows(System.IntPtr lpEnumFunc, System.IntPtr lParam);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(System.IntPtr hWnd, out int lpdwProcessId);
[DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr hWnd);
"@ -Name "Win32" -Namespace "HandsGate" -PassThru

    $results = @()
    # Use a script-level list captured by the callback via a small holder
    $holder = [pscustomobject]@{ items = New-Object System.Collections.Generic.List[object] }

    $cb = {
        param([IntPtr]$hwnd, [IntPtr]$lparam)
        try {
            if (-not [HandsGate.Win32]::IsWindowVisible($hwnd)) { return $true }
            $pid = 0
            [void][HandsGate.Win32]::GetWindowThreadProcessId($hwnd, [ref]$pid)
            $min = [HandsGate.Win32]::IsIconic($hwnd)
            if (-not $min) {
                $holder.items.Add([pscustomobject]@{ hwnd = $hwnd.ToInt64(); pid = [int]$pid })
            }
        } catch {}
        return $true
    }

    # We cannot directly pass a PowerShell delegate to EnumWindows reliably across all hosts.
    # Fallback: use a small C# helper if available; otherwise use a pragmatic loop over Get-Process windows via UIA/Win32.
    # Simpler robust path: iterate processes in our tree and ask user32 for top-level windows by PID via EnumWindows in C#.

    # Build a tiny in-memory assembly to do the enumeration safely.
    $src = @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class HandsGateEnum {
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
  delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  public static List<Tuple<long,int>> VisibleNonMinimized() {
    var list = new List<Tuple<long,int>>();
    EnumWindows((h,l) => {
      try {
        if (!IsWindowVisible(h)) return true;
        if (IsIconic(h)) return true;
        int pid = 0; GetWindowThreadProcessId(h, out pid);
        list.Add(Tuple.Create((long)h.ToInt64(), (int)pid));
      } catch {}
      return true;
    }, IntPtr.Zero);
    return list;
  }
}
"@
    Add-Type -TypeDefinition $src -Language CSharp -ErrorAction SilentlyContinue | Out-Null
    try {
        $pairs = [HandsGateEnum]::VisibleNonMinimized()
        foreach ($p in $pairs) {
            $results += [pscustomobject]@{ hwnd = [long]$p.Item1; pid = [int]$p.Item2 }
        }
    } catch {
        # If C# path fails (e.g., Add-Type policy), degrade gracefully.
        Write-Warning "EnumWindows via C# unavailable; falling back to empty set."
    }
    return $results
}

if ($PSVersionTable.PSEdition -ne "Desktop" -and -not $IsWindows) {
    Write-Host "This gate must run on Windows." -ForegroundColor Yellow
    exit 2
}

try {
    $roots = Get-CimInstance Win32_Process -Filter "Name='camoufox.exe' OR Name='firefox.exe'" |
        Where-Object { $_.CommandLine -like ('*' + $ProfileDir + '*') } |
        Select-Object -ExpandProperty ProcessId
} catch {
    Write-Host "CIM enumeration failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}

if (-not $roots -or $roots.Count -eq 0) {
    Write-Host "FAIL: No profile-bearing browser root found for $ProfileDir" -ForegroundColor Red
    exit 1
}

$tree = Get-DescendantPids -RootPids @($roots)
Write-Host ("Roots: " + ($roots -join ',')) -ForegroundColor Cyan
Write-Host ("Tree size: " + $tree.Count) -ForegroundColor Cyan

$visible = Get-VisibleTopLevelHwnds
if (-not $visible -or $visible.Count -eq 0) {
    Write-Host "FAIL: No visible top-level windows found on this desktop." -ForegroundColor Red
    exit 1
}

$match = @()
foreach ($window in $visible) {
    if ($tree.Contains([int]$window.pid)) { $match += $window }
}
if ($match.Count -gt 0) {
    Write-Host ("PASS: Found " + $match.Count + " visible top-level window(s) belonging to Hands browser tree.") -ForegroundColor Green
    foreach ($m in $match) { Write-Host ("  HWND=" + $m.hwnd + " PID=" + $m.pid) }
    exit 0
} else {
    Write-Host "FAIL: Visible windows exist but none belong to the Hands profile-bearing tree." -ForegroundColor Red
    Write-Host "Visible sample (first 5):"
    $visible | Select-Object -First 5 | ForEach-Object { Write-Host ("  HWND=" + $_.hwnd + " PID=" + $_.pid) }
    exit 1
}
