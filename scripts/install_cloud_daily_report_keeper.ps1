param(
    [string]$TaskName = "USStockCloudDailyReport-Keeper",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"

$EnsureScript = Join-Path $PSScriptRoot "ensure_cloud_daily_report.ps1"
if (-not (Test-Path $EnsureScript)) {
    throw "Missing ensure script: $EnsureScript"
}

$Root = Split-Path -Parent $PSScriptRoot
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskCommand = "`"$PowerShell`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$EnsureScript`""

# Runs all day at a low frequency. The script itself only dispatches during the
# Beijing decision window and exits quickly once today's report exists.
$args = @(
    "/Create",
    "/TN", $TaskName,
    "/TR", $TaskCommand,
    "/SC", "MINUTE",
    "/MO", "$IntervalMinutes",
    "/F"
)

& schtasks.exe @args | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Failed to register scheduled task $TaskName"
}

$startupFolder = [Environment]::GetFolderPath("Startup")
if ($startupFolder) {
    $shortcutPath = Join-Path $startupFolder "$TaskName.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $PowerShell
    $shortcut.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$EnsureScript`""
    $shortcut.WorkingDirectory = $Root
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Trigger GitHub daily report workflow if today's public report is missing"
    $shortcut.Save()
    Write-Host "[cloud-report] startup shortcut installed: $shortcutPath"
}

Write-Host "[cloud-report] installed scheduled keeper: $TaskName"
Write-Host "[cloud-report] interval: every $IntervalMinutes minutes"
Write-Host "[cloud-report] command: $TaskCommand"

& $EnsureScript
