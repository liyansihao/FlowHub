@echo off
cd /d "%~dp0"
docker compose exec -T flowhub python -c "from comparebot.adapters.dinov2.ranker import DinoV2Ranker; r=DinoV2Ranker(device='cpu'); print(r.model_version)"
pause
