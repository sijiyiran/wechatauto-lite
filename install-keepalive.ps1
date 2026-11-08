# ---------------------------------------------------------------
#  Install / remove the keep-alive scheduled task.
#
#  ASCII ONLY (Windows PowerShell 5.1 reads .ps1 as ANSI/GBK).
#
#  WHY THIS EXISTS
#  ---------------
#  The supervisor keeps brain.py alive. But **nothing kept the supervisor
#  alive** - if it died (antivirus kill, crash, wrong process killed) the whole
#  thing went permanently silent, and no alarm could fire, because the alarm
#  itself lives inside the supervisor.
#
#  Measured 2026-10-14: the whole process tree vanished at 10:31 and nobody
#  noticed for 12 hours. The watchdog was watching the wrong thing.
#
#  RUNS pythonw.exe, NOT powershell.exe
#  ------------------------------------
#  The first version registered `powershell.exe -WindowStyle Hidden -File
#  start-daemon.ps1`. Windows creates the console window FIRST and PowerShell
#  hides it afterwards, so a black box flashed on screen every 5 minutes.
#  The user reported it as "a popup keeps appearing and closing, very annoying".
#
#  pythonw.exe is a GUI-subsystem binary: it never creates a console at all,
#  so there is nothing to flash. Hence keepalive.py.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File install-keepalive.ps1
#    powershell -ExecutionPolicy Bypass -File install-keepalive.ps1 -Remove
#    powershell -ExecutionPolicy Bypass -File install-keepalive.ps1 -EveryMinutes 3
# ---------------------------------------------------------------
[CmdletBinding()]
param(
  [string]$Root = "",
  [int]$EveryMinutes = 5,
  [switch]$Remove
)

$ErrorActionPreference = "Stop"

if (-not $Root) {
  if ($PSScriptRoot) { $Root = $PSScriptRoot }
  elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
  else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path $Root).Path

$taskName = "WeChatAutoReply-KeepAlive"

if ($Remove) {
  & schtasks /delete /tn $taskName /f 2>&1 | ForEach-Object { "  $_" }
  exit 0
}

$ka = Join-Path $Root "keepalive.py"
if (-not (Test-Path $ka)) { throw "keepalive.py not found in $Root" }

# Same search order as supervisor.py / start-daemon.ps1.
$pythonw = $null
foreach ($c in @((Join-Path $Root "pyvenv.cfg"),
                 (Join-Path (Split-Path $Root -Parent) "venv311\pyvenv.cfg"))) {
  if (Test-Path $c) {
    # NOTE: do NOT name these $home / $dir. `$home` is a read-only automatic
    # variable in PowerShell (variables are case-insensitive), so assigning to
    # it fails with "Cannot overwrite variable HOME because it is read-only".
    $homeLine = (Get-Content $c -Encoding UTF8 |
             Where-Object { $_ -match '^\s*home\s*=' } |
             Select-Object -First 1)
    if ($homeLine) {
      $baseDir = ($homeLine -split '=', 2)[1].Trim()
      $cand = Join-Path $baseDir "pythonw.exe"
      if (Test-Path $cand) { $pythonw = $cand; break }
    }
  }
}
if (-not $pythonw) {
  $cand = Join-Path $env:APPDATA "uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
  if (Test-Path $cand) { $pythonw = $cand }
}
if (-not $pythonw) {
  # Packaged layout: bundle-runtime.ps1 copies the BASE interpreter, so this is real.
  $cand = Join-Path $Root "runtime\pythonw.exe"
  if (Test-Path $cand) { $pythonw = $cand }
}
if (-not $pythonw) {
  Write-Host "WARNING: real pythonw.exe not found; falling back to a venv one."
  Write-Host "         A venv pythonw is a CONSOLE app and WILL flash a window."
  foreach ($c in @((Join-Path (Split-Path $Root -Parent) "venv311\Scripts\pythonw.exe"),
                   (Join-Path $Root "venv311\Scripts\pythonw.exe"))) {
    if (Test-Path $c) { $pythonw = $c; break }
  }
}
if (-not $pythonw) { throw "pythonw.exe not found - cannot register the task" }

$inner = "\`"$pythonw\`" \`"$ka\`""

Write-Host "== registering scheduled task: $taskName"
Write-Host "   every $EveryMinutes minutes"
Write-Host "   runs (windowless): $pythonw"
Write-Host "   script           : $ka"
Write-Host ""

& schtasks /create /tn $taskName /tr $inner /sc minute /mo $EveryMinutes /f /rl LIMITED 2>&1 |
  ForEach-Object { "  $_" }

if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "schtasks failed (exit $LASTEXITCODE)."
  exit 1
}

Write-Host ""
& schtasks /query /tn $taskName /fo LIST 2>&1 |
  Where-Object { $_ -match 'TaskName|Status|Next Run|Task To Run' } |
  ForEach-Object { "  " + $_.Trim() }

Write-Host ""
Write-Host "== done. Windowless: pythonw never creates a console, so nothing flashes."
Write-Host "   It revives the daemon within $EveryMinutes minutes of it being killed,"
Write-Host "   and shortly after every logon."
Write-Host ""
Write-Host "   remove with: powershell -File install-keepalive.ps1 -Remove"
Write-Host "   run now    : schtasks /run /tn $taskName"
Write-Host "   its log    : $Root\keepalive.log"
