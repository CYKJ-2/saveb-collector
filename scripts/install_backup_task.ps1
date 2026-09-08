param([string]$At = '02:15', [string]$TaskName = 'Saveb-Collector-Backup-Verify')
$ErrorActionPreference = 'Stop'
$collectorRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot 'backup_daily.ps1'
if (-not (Test-Path -LiteralPath (Join-Path $collectorRoot '.venv/Scripts/python.exe'))) {
    throw 'Create .venv and install project dependencies before installing the backup task.'
}
# Interactive user token preserves access to this user's Docker Desktop engine.
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" -WorkingDirectory $collectorRoot
$triggerTime = [datetime]::ParseExact($At, 'HH:mm', [System.Globalization.CultureInfo]::InvariantCulture)
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Back up shared Saveb PostgreSQL database and attachments; verify restore in a temporary database.' -Force | Select-Object TaskName,State
