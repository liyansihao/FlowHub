param([switch]$Check)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $configFile = Join-Path $PSScriptRoot 'config.json'
    $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $applyArgs = @()
    if (-not $Check) { $applyArgs += '--apply' }

    # Native installations can explicitly supply their existing Python and DB directory.
    if ($config.python_executable -and $config.data_dir) {
        & $config.python_executable (Join-Path $PSScriptRoot 'refresh.py') --config $configFile @applyArgs
        exit $LASTEXITCODE
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker Desktop is missing. Install/start the existing FlowHub Windows backend first.'
    }
    $engine = & docker info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') {
        throw 'Start Docker Desktop with Linux containers and retry.'
    }
    $backend = [string]$config.docker_dir
    if (-not $backend) {
        foreach ($dir in @($PSScriptRoot, (Split-Path $PSScriptRoot -Parent))) {
            if (Test-Path -LiteralPath (Join-Path $dir 'compose.yaml')) { $backend = $dir; break }
        }
    }
    if (-not $backend) {
        $backend = (Read-Host 'FlowHub backend folder (contains compose.yaml and Start.cmd)').Trim('"')
    }
    $compose = Join-Path $backend 'compose.yaml'
    if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
        throw 'compose.yaml not found. Set docker_dir in config.json to the FlowHub backend folder.'
    }
    $chrome = [string]$config.chrome_root
    if (-not $chrome) { $chrome = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data' }
    if (-not (Test-Path -LiteralPath $chrome -PathType Container)) {
        throw 'Chrome profile folder not found. Set chrome_root in config.json.'
    }
    # Verify that the pre-existing backend container owns the target volume.
    # Do not create a new data volume or run the normal app entrypoint.
    $containerIds = @(& docker compose -f $compose ps -a -q flowhub)
    if ($LASTEXITCODE -ne 0 -or $containerIds.Count -ne 1 -or -not $containerIds[0]) {
        throw 'Exactly one installed FlowHub backend is required. Run its Start.cmd first.'
    }
    $image = & docker inspect --format '{{.Config.Image}}' $containerIds[0]
    if ($LASTEXITCODE -ne 0 -or -not $image) { throw 'Cannot identify the installed backend image.' }
    $profileArgs = @()
    if ($config.profile) { $profileArgs = @('--profile', [string]$config.profile) }
    # Temporary process only. Chrome/tool mounts are read-only; credentials remain in memory.
    # Use the backend's own existing data volumes, including its encryption key.
    & docker run --rm --pull never --network bridge --volumes-from $containerIds[0] `
        --mount "type=bind,source=$chrome,target=/chrome,readonly" `
        --mount "type=bind,source=$PSScriptRoot,target=/credential-tool,readonly" `
        --entrypoint python $image /credential-tool/refresh.py `
        --config /credential-tool/config.json --data /data/app --chrome-root /chrome @profileArgs @applyArgs
    if ($LASTEXITCODE -ne 0) { throw 'Credential refresh did not complete. Read the message above; retry after exiting Chrome normally if files are locked.' }
    Write-Host 'Done. No token was exported. No workflow was started, stopped or rescheduled.'
} catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
