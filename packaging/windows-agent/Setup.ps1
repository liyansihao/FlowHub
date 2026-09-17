$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'FlowHubAgent'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $root /inheritance:r /grant:r "${identity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect local agent credentials' }
$python = (Get-Command py.exe -ErrorAction SilentlyContinue)
if (-not $python) { throw 'Install Python 3.12 or later from python.org with the Python launcher, then rerun Setup.cmd.' }
& py.exe -3 -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ required' }
Copy-Item (Join-Path $PSScriptRoot 'agent.py') (Join-Path $root 'agent.py') -Force
& py.exe -3 (Join-Path $root 'agent.py') --config (Join-Path $root 'device.json') --enroll
if ($LASTEXITCODE -ne 0) { throw 'Enrollment failed' }
Write-Host 'Setup complete. Run Check.cmd, then Start.cmd.'
