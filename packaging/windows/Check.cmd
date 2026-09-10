@echo off
cd /d "%~dp0"
docker compose exec -T flowhub python -m flowhub.doctor
pause
