@echo off
rem Group Brain - background daemon launcher (no window, survives this shell).
rem ASCII only: cmd.exe reads .cmd as ANSI/GBK.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-daemon.ps1"
