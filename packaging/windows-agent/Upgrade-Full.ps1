$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'FlowHubAgent'
if (-not (Test-Path (Join-Path $root 'device.json'))) { throw 'Complete device enrollment first.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $root /inheritance:r /grant:r "${identity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect agent directory' }
& py.exe -3.12 -c "import sys; assert sys.version_info[:2] == (3,12)"
if ($LASTEXITCODE -ne 0) { throw 'This model package requires Python 3.12. Install it alongside any other Python version.' }
$venv = Join-Path $root 'compute-venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    & py.exe -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create compute environment' }
}
$python = Join-Path $venv 'Scripts\python.exe'
$vendor = Join-Path $PSScriptRoot 'compareBot'
& $python -m pip install -c (Join-Path $PSScriptRoot 'requirements-tested.txt') "${vendor}[search1688,dinov2]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Existing device credentials remain available.' }
foreach ($name in @('agent.py','production_agent.py','full_agent.py','compute_worker.py','dossier_packet.py')) {
    Copy-Item (Join-Path $PSScriptRoot $name) (Join-Path $root $name) -Force
}
if (Test-Path (Join-Path $PSScriptRoot 'model-cache')) {
    Copy-Item (Join-Path $PSScriptRoot 'model-cache') $root -Recurse -Force
}
$oldTask = Get-ScheduledTask -TaskName 'FlowHub Acceptance Agent' -ErrorAction SilentlyContinue
if ($oldTask) { Stop-ScheduledTask -TaskName 'FlowHub Acceptance Agent'; Disable-ScheduledTask -TaskName 'FlowHub Acceptance Agent' | Out-Null }
Write-Host 'Close the previous Start-Production window before continuing. No new enrollment is needed.'
& $python -u (Join-Path $root 'full_agent.py') --config (Join-Path $root 'device.json')
if ($LASTEXITCODE -ne 0) { throw 'Full agent stopped; inspect the visible error and restart Start-Full.cmd.' }
