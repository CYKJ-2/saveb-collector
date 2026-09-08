param(
    [string]$Python = '',
    [string]$Output = '',
    [string]$PgContainer = 'saveb-api-postgres',
    [string]$AttachmentsContainer = 'saveb-api-app',
    [string]$Attachments = '/data/attachments'
)
$ErrorActionPreference = 'Stop'
$collectorRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $collectorRoot '.venv/Scripts/python.exe' }
if (-not $Output) { $Output = Join-Path $collectorRoot 'backups' }
New-Item -ItemType Directory -Force -Path $Output | Out-Null
Push-Location $collectorRoot
try {
    $logPath = Join-Path $Output 'backup.log'
    & $Python scripts/backup_restore.py backup --pg-container $PgContainer --attachments-container $AttachmentsContainer --attachments $Attachments --output $Output *>> $logPath
    $resultCode = $LASTEXITCODE
    @{ checkedAt = (Get-Date).ToUniversalTime().ToString('o'); exitCode = $resultCode; status = $(if ($resultCode -eq 0) { 'passed' } else { 'failed' }) } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output 'last-attempt.json') -Encoding utf8
    exit $resultCode
} finally { Pop-Location }
