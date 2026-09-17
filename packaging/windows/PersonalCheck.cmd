@echo off
cd /d "%~dp0"
docker compose exec -T flowhub python -c "import json,urllib.request,torch,search1688api; h=json.load(urllib.request.urlopen('http://127.0.0.1:38427/healthz')); assert h['source_revision'].startswith('54fbf19'); print(h); print('torch',torch.__version__)"
pause
