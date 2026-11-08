@echo off
rem One-click launch for the portable build. ASCII only.
setlocal
set HERE=%~dp0
cd /d "%HERE%"
if exist "%HERE%runtime\pythonw.exe" (
  start "" "%HERE%runtime\pythonw.exe" "%HERE%\gui.py"
) else (
  echo runtime\pythonw.exe is missing - the bundle is incomplete.
  pause
  exit /b 1
)
exit /b 0
