@echo off
rem Double-clickable wrapper around open-master.ps1, which insists on /portable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-master.ps1" %*
