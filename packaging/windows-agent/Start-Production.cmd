@echo off
py -3 "%LOCALAPPDATA%\FlowHubAgent\production_agent.py" --config "%LOCALAPPDATA%\FlowHubAgent\device.json"
pause
