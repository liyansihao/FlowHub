@echo off
set "FLOWHUB_AGENT_ROOT=%LOCALAPPDATA%\FlowHubAgent"
"%FLOWHUB_AGENT_ROOT%\compute-venv\Scripts\python.exe" "%~dp0upgrade_versions.py" --root "%FLOWHUB_AGENT_ROOT%"
if errorlevel 1 (
  echo Keep this output. No agent was stopped automatically.
  pause
  exit /b 1
)
echo Version-reporting code installed. Starting existing full agent.
"%FLOWHUB_AGENT_ROOT%\compute-venv\Scripts\python.exe" -u "%FLOWHUB_AGENT_ROOT%\full_agent.py" --config "%FLOWHUB_AGENT_ROOT%\device.json"
pause
