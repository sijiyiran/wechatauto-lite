@echo off
rem ---------------------------------------------------------------
rem  Launch the WeChat Auto-Reply control panel.
rem
rem  ASCII only: cmd.exe reads .cmd as ANSI/GBK.
rem
rem  IMPORTANT: venv\Scripts\pythonw.exe is a FAKE pythonw.
rem  Its PE Subsystem is 3 (console app), so starting it pops up an
rem  extra black console window on the desktop. The real GUI-only
rem  pythonw (Subsystem=2) lives in the base Python install that the
rem  venv points at (pyvenv.cfg home=...). We prefer that one so the
rem  panel is the ONLY window that appears.
rem ---------------------------------------------------------------
setlocal
set HERE=%~dp0

if exist "%HERE%WeChatAutoReply.exe" (
  start "" "%HERE%WeChatAutoReply.exe"
  exit /b 0
)

set PY=

rem ---- 1) base install pythonw (real GUI, no console) ----
for /f "usebackq tokens=2 delims==" %%A in (`findstr /b /i "home" "%HERE%..\venv311\pyvenv.cfg" 2^>nul`) do (
  set "PY=%%A\pythonw.exe"
)
if defined PY if not exist "%PY%" set PY=

rem ---- 2) bundled runtime ----
if not defined PY if exist "%HERE%runtime\pythonw.exe" set "PY=%HERE%runtime\pythonw.exe"

rem ---- 3) uv default install ----
if not defined PY if exist "%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe" (
  set "PY=%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
)

rem ---- 4) last resort: venv pythonw (will show a console) ----
if not defined PY if exist "%HERE%..\venv311\Scripts\pythonw.exe" set "PY=%HERE%..\venv311\Scripts\pythonw.exe"
if not defined PY if exist "%HERE%venv311\Scripts\pythonw.exe" set "PY=%HERE%venv311\Scripts\pythonw.exe"

if not exist "%HERE%gui.py" (
  echo gui.py not found next to this script.
  pause
  exit /b 1
)
if not defined PY (
  echo No pythonw.exe found.
  pause
  exit /b 1
)

start "" "%PY%" "%HERE%gui.py"
exit /b 0
