param([ValidateSet('Install','Start','Stop','Status','Backup','Admin','Feishu','ImportStores')][string]$Action='Start')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
function Run-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed. See the message above.' } }
try {
 if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and enable Linux containers / WSL 2, then try again.' }
 $engine = & docker info --format '{{.OSType}}'
 if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') { throw 'Start Docker Desktop and switch to Linux containers before continuing.' }
 if ($Action -eq 'Install') { Run-Docker compose build --pull; Run-Docker compose up -d }
 elseif ($Action -eq 'Start') { Run-Docker compose up -d }
 elseif ($Action -eq 'Stop') { Run-Docker compose stop; Write-Host 'Stopped. Database and keys are preserved.'; exit }
 elseif ($Action -eq 'Status') { Run-Docker compose ps; Run-Docker compose exec -T flowhub python -m flowhub.control status; exit }
 elseif ($Action -eq 'Admin') { Run-Docker compose exec -T flowhub cat /data/app/INITIAL_ADMIN.txt; exit }
 elseif ($Action -eq 'Feishu') {
  $file=Read-Host 'Full path of your private Feishu JSON (token, app_token)'
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw 'Private configuration file not found.' }
  Run-Docker compose exec -T flowhub mkdir -p /data/private
  Run-Docker compose cp "$file" 'flowhub:/data/private/feishu.json'
  Run-Docker compose exec -T --user root flowhub chown 10001:10001 /data/private/feishu.json
  Run-Docker compose exec -T flowhub chmod 600 /data/private/feishu.json
  Write-Host 'Private delist guard configuration installed.'; exit
 }
 elseif ($Action -eq 'ImportStores') {
  $file=Join-Path $PSScriptRoot 'stores.fhconfig'
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $file=Read-Host 'Full path of stores.fhconfig' }
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw 'Connection bundle not found.' }
  Run-Docker compose exec -T flowhub mkdir -p /data/private
  Run-Docker compose cp "$file" 'flowhub:/data/private/stores.fhconfig'
  Run-Docker compose exec -T --user root flowhub chown 10001:10001 /data/private/stores.fhconfig
  Run-Docker compose exec -T flowhub chmod 600 /data/private/stores.fhconfig
  $secure=Read-Host 'Connection unlock code (not your FlowHub password)' -AsSecureString
  $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
   $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
   $plain | & docker compose exec -T flowhub python -m flowhub.connection_bundle /data/private/stores.fhconfig
   if ($LASTEXITCODE -ne 0) { throw 'Store import failed.' }
  } finally { $plain=$null; [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
  Write-Host 'Stores imported. Sign in to review, enable and select stores. No listing was started.'; exit
 }
 elseif ($Action -eq 'Backup') {
  $folder=Join-Path $PSScriptRoot 'backups'; New-Item -ItemType Directory -Force -Path $folder | Out-Null
  $name='flowhub-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.tar.gz'
  Run-Docker compose stop
  try {
   Run-Docker compose run --rm --no-deps --user root --entrypoint tar -v "${folder}:/backup" flowhub -czf "/backup/$name" -C /data .
  } finally { Run-Docker compose up -d }
  Write-Host "Private backup saved: $folder\$name. Contains keys; do not share."; exit
 }
 $ready=$false
 for($i=0;$i -lt 60;$i++) {
  try { $null=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:38427/healthz' -TimeoutSec 3; $ready=$true; break } catch { Start-Sleep -Seconds 2 }
 }
 if (-not $ready) { throw 'Service did not become ready. Run Status; do not reinitialize or delete the data volume.' }
 Start-Process 'http://127.0.0.1:38427/'
 Write-Host 'FlowHub is ready. Initial admin credentials: run Admin.cmd. First login requires a new password.'
} catch { Write-Host $_ -ForegroundColor Red; exit 1 }
