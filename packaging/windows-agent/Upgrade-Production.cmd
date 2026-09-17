@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Upgrade-Production.ps1"
pause
