@echo off
"%LOCALAPPDATA%\FlowHubAgent\compute-venv\Scripts\python.exe" -u "%LOCALAPPDATA%\FlowHubAgent\full_agent.py" --config "%LOCALAPPDATA%\FlowHubAgent\device.json"
pause
