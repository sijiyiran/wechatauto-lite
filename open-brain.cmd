@echo off
rem ============================================================
rem  WeChat Auto-Reply  -  one-click open.
rem  Starts it if it is not running, then shows live status.
rem  ASCII only: cmd.exe reads .cmd as ANSI/GBK, non-ASCII
rem  comments corrupt the parse.
rem ============================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

echo.
echo ============================================================
echo    WeChat Auto-Reply   (group brain)
echo ============================================================
echo.
echo  [1/2] starting (skipped if already running) ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-daemon.ps1"
echo.
echo  [2/2] current status:
echo.
"C:\Users\pc\.dsh-wxauto\venv311\Scripts\python.exe" "%~dp0brain.py" --status
echo.
echo ============================================================
echo   Logs
echo     daemon.log      what it decided, and why
echo     supervisor.log  auto-restarts after a freeze
echo   Stop
echo     stop-daemon.cmd
echo   Tune
echo     config.json          all the switches
echo     prompts\persona.md   its character and voice
echo ============================================================
echo.
pause
