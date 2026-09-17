@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0FlowHub.ps1" -Action ImportStores
pause
