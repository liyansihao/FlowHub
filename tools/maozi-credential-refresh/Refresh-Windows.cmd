@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Refresh-Windows.ps1"
set "refresh_exit=%ERRORLEVEL%"
pause
exit /b %refresh_exit%
