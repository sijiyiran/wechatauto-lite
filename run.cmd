@echo off
rem ---------------------------------------------------------------
rem  Group Brain launcher - passes arguments straight to brain.py.
rem
rem  ASCII ONLY. cmd.exe reads .cmd as ANSI/GBK, so non-ASCII bytes can
rem  break parsing. Keep this file plain ASCII.
rem
rem  Relocatable: works from the source folder and from a packaged
rem  dist folder (where runtime\ holds the bundled interpreter). It used
rem  to hardcode one machine's venv path, so it could only ever run there.
rem
rem  Examples:
rem    run.cmd --list-groups
rem    run.cmd --group "GROUP" --simulate "some text"
rem    run.cmd --group "GROUP" --test
rem    run.cmd --status
rem    run.cmd                      (foreground daemon)
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set HERE=%~dp0
set PY=
if exist "%HERE%runtime\python.exe"                          set PY=%HERE%runtime\python.exe
if not defined PY if exist "%HERE%..\venv311\Scripts\python.exe" set PY=%HERE%..\venv311\Scripts\python.exe
if not defined PY if exist "%HERE%venv311\Scripts\python.exe"    set PY=%HERE%venv311\Scripts\python.exe
if not defined PY set PY=python

if not exist "%HERE%brain.py" (
  echo brain.py not found next to this script.
  pause
  exit /b 1
)

"%PY%" "%HERE%brain.py" %*
