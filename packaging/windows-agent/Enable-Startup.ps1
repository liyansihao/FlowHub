$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'FlowHubAgent'
if (-not (Test-Path (Join-Path $root 'device.json'))) { throw 'Run Setup.cmd first.' }
$launcher = (Get-Command py.exe).Source
$action = New-ScheduledTaskAction -Execute $launcher -Argument "-3 `"$root\agent.py`" --config `"$root\device.json`""
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'FlowHub Acceptance Agent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host 'Runs when this Windows user logs in. Close any manual Start.cmd window before using this task.'
