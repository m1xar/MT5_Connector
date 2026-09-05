@echo off
rem Double-clickable wrapper around open-master.ps1, which insists on /portable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-master.ps1" %*
rem Hold the window open on failure - a double-click otherwise closes it before
rem the error can be read, which looks exactly like nothing having happened.
if errorlevel 1 pause
