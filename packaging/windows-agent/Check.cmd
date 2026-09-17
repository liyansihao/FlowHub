@echo off
py -3 "%LOCALAPPDATA%\FlowHubAgent\agent.py" --config "%LOCALAPPDATA%\FlowHubAgent\device.json" --once
pause
