$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'FlowHubAgent'
if (-not (Test-Path (Join-Path $root 'device.json'))) { throw 'Complete initial enrollment first.' }
& py.exe -3 -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ required' }
# Stop the old window before upgrading. The shared lock enforces this as well.
$oldTask = Get-ScheduledTask -TaskName 'FlowHub Acceptance Agent' -ErrorAction SilentlyContinue
if ($oldTask) {
    Stop-ScheduledTask -TaskName 'FlowHub Acceptance Agent'
    Disable-ScheduledTask -TaskName 'FlowHub Acceptance Agent' | Out-Null
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $root /inheritance:r /grant:r "${identity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect agent directory' }
Copy-Item (Join-Path $PSScriptRoot 'agent.py') (Join-Path $root 'agent.py') -Force
Copy-Item (Join-Path $PSScriptRoot 'production_agent.py') (Join-Path $root 'production_agent.py') -Force
& py.exe -3 (Join-Path $root 'production_agent.py') --config (Join-Path $root 'device.json')
if ($LASTEXITCODE -ne 0) { throw 'Agent stopped. Close phase-1 Start.cmd or scheduled agent before retrying.' }
