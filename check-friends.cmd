@echo off
rem Trigger a friend-request check. ASCII only (PowerShell/cmd reads as GBK).
cd /d "%~dp0"
echo trigger> "_check-friends.flag"
echo.
echo Flag written. The daemon will check friend requests within a few seconds.
echo Watch daemon.log for lines starting with [FR] .
echo.
timeout /t 3 >nul