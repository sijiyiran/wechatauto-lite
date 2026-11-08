@echo off
rem ---------------------------------------------------------------
rem  Stop Group Brain: supervisor + brain.py.
rem
rem  ASCII only (cmd.exe reads .cmd as ANSI/GBK).
rem
rem  Relocatable: matches processes by THIS script's own folder, not by a
rem  hardcoded substring. The old version filtered on '*group-brain*', which
rem  only ever matched the original install path - on any other machine
rem  (e.g. D:\WeChatAutoReply) the stop button silently did nothing.
rem
rem  Filtering note: matching only on "command line contains <folder>" used to
rem  also match the caller's own shell and kill it. So we filter by process
rem  NAME first (WMI side) and then require brain.py / supervisor.py.
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set HERE=%~dp0
set HERE=%HERE:~0,-1%
echo Stopping Group Brain ...

powershell -NoProfile -Command "$root = '%HERE%'; $pat = '*' + $root + '*'; $t = @(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like $pat -and ($_.CommandLine -like '*brain.py*' -or $_.CommandLine -like '*supervisor.py*') }); if ($t.Count -gt 0) { $t | ForEach-Object { Write-Host ('  kill ' + $_.Name + ' pid=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } else { Write-Host '  not running' }"

echo Done.
