# Group Brain - detached background launcher.
#
# ASCII ONLY. Windows PowerShell 5.1 reads .ps1 as ANSI/GBK; a UTF-8 file
# containing Chinese comments fails to parse (this file was broken that way
# once - "Unexpected token '}'" at a line with no brace on it). Keep every
# character in this file plain ASCII, comments included.
#
# Launches supervisor.py (which keeps brain.py alive across crashes and
# watchdog hard-exits) via WMI Win32_Process.Create. WMI is used because a
# process started from a shell inside a job object dies with that job.
[CmdletBinding()]
param(
  [string]$Root     = "",
  [string]$PythonW  = "",
  [switch]$SkipSelftest
)

$ErrorActionPreference = "Stop"

# Resolve $Root when not given: prefer the script's own folder.
if (-not $Root) {
  if ($PSScriptRoot) { $Root = $PSScriptRoot }
  elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
  else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path $Root).Path

# ---- locate pythonw.exe ----
# This used to hardcode one machine's venv path, so it could never work on
# another PC. Search in order and only fail when nothing is found, printing
# every location that was tried.
if (-not $PythonW) {
  $cands = @(
    (Join-Path $Root "runtime\pythonw.exe"),
    (Join-Path (Split-Path $Root -Parent) "venv311\Scripts\pythonw.exe"),
    (Join-Path $Root "venv311\Scripts\pythonw.exe")
  )
  foreach ($c in $cands) { if (Test-Path $c) { $PythonW = $c; break } }
  if (-not $PythonW) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $PythonW = $cmd.Source }
  }
}
if (-not $PythonW -or -not (Test-Path $PythonW)) {
  Write-Host "pythonw.exe not found - cannot start. Searched:"
  Write-Host ("  " + (Join-Path $Root "runtime\pythonw.exe"))
  Write-Host ("  " + (Join-Path (Split-Path $Root -Parent) "venv311\Scripts\pythonw.exe"))
  Write-Host ("  " + (Join-Path $Root "venv311\Scripts\pythonw.exe"))
  Write-Host "  and PATH."
  Write-Host ""
  Write-Host "Install Python 3.11, or put a runtime in <program>\runtime\."
  exit 1
}

$sup = Join-Path $Root "supervisor.py"
$python = Join-Path (Split-Path $PythonW -Parent) "python.exe"
$selftest = Join-Path $Root "selftest.py"

# ---- Already running? Check FIRST, before the self-test. ----
# Order matters: a keep-alive scheduled task calls this every few minutes, and
# running a 20-second self-test on every tick (only to discover the daemon is
# already up) is pure waste. Also, we must be able to answer "is it alive?"
# without touching the code at all.
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*supervisor.py*" -and $_.CommandLine -like "*$Root*" }
if ($running) {
  Write-Host "supervisor already running:"
  $running | ForEach-Object { Write-Host ("  pid=" + $_.ProcessId) }
  Write-Host "check status: run.cmd --status"
  exit 0
}

# ---- Gate: run the self-test BEFORE starting. ----
# Why: a wrong variable name (`extra` instead of `concede_note`) once shipped
# in a rarely-hit branch. The verification script had hand-copied that logic
# and copied it correctly, so the test passed while the real code was broken:
# every tick threw and the bot went completely silent in production.
# selftest.py exercises the REAL code paths, so a broken branch fails here
# instead of in the group. Refusing to start is deliberate - a bot that is
# silently broken is worse than a bot that is visibly down.
if (-not $SkipSelftest) {
  if ((Test-Path $selftest) -and (Test-Path $python)) {
    Write-Host "running selftest ..."
    & $python $selftest
    if ($LASTEXITCODE -ne 0) {
      Write-Host ""
      Write-Host "REFUSING TO START: selftest failed (see the failures above)."
      Write-Host "A broken branch would make the bot silently stop replying."
      Write-Host "Fix the code, then run start-daemon.cmd again."
      Write-Host ""
      Write-Host "To start anyway (NOT recommended):"
      Write-Host "  powershell -File start-daemon.ps1 -SkipSelftest"
      exit 1
    }
    Write-Host "selftest OK"
    Write-Host ""
  }
}

$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine      = "`"$PythonW`" `"$sup`""
  CurrentDirectory = $Root
}
if ($r.ReturnValue -ne 0) {
  Write-Host ("FAILED to start supervisor. ReturnValue=" + $r.ReturnValue)
  exit 1
}

Start-Sleep -Seconds 6
$now = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*supervisor.py*" -and $_.CommandLine -like "*$Root*" }
if (-not $now) {
  Write-Host "WARNING: supervisor not alive 6s after start - check supervisor.log"
  exit 1
}

Write-Host ("started supervisor pid=" + $r.ProcessId)
Write-Host ""
Write-Host "Logs  : daemon.log (brain) / supervisor.log (restarts)"
Write-Host "Status: run.cmd --status"
Write-Host "Stop  : stop-daemon.cmd"
