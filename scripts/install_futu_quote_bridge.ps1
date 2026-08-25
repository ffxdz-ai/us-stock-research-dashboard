param(
    [string]$TaskName = "USStockFutuQuoteBridge",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$syncScript = Join-Path $resolvedRoot "scripts\sync_futu_local_snapshot.py"
$bridgeScript = Join-Path $resolvedRoot "scripts\futu_cloud_bridge.py"
$daemonScript = Join-Path $resolvedRoot "scripts\futu_quote_bridge_daemon.py"
if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    throw "Futu quote synchronizer does not exist: $syncScript"
}
if (-not (Test-Path -LiteralPath $bridgeScript -PathType Leaf)) {
    throw "Futu cloud bridge does not exist: $bridgeScript"
}
if (-not (Test-Path -LiteralPath $daemonScript -PathType Leaf)) {
    throw "Futu cloud bridge background worker does not exist: $daemonScript"
}

$pythonCommand = Get-Command python.exe -ErrorAction Stop
$pythonw = Join-Path (Split-Path -Parent $pythonCommand.Source) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "pythonw.exe is required so the scheduled quote uploader never opens a shell window."
}

$bridgeUrl = [Environment]::GetEnvironmentVariable("FUTU_BRIDGE_URL", "User")
$bridgeToken = [Environment]::GetEnvironmentVariable("FUTU_BRIDGE_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($bridgeUrl) -or [string]::IsNullOrWhiteSpace($bridgeToken)) {
    throw "Configure per-user FUTU_BRIDGE_URL and FUTU_BRIDGE_TOKEN before installing the uploader."
}

$arguments = '"{0}" --scope all --push-cloud --quiet' -f $syncScript
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $resolvedRoot
$repeatTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($repeatTrigger, $logonTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description "Upload market-only Futu OpenD quotes through an authenticated HTTPS bridge; no account or trading access." `
        -Force `
        -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Output "Installed hidden Futu quote bridge task: $TaskName (every $IntervalMinutes minutes)."
} catch {
    $startupDirectory = [Environment]::GetFolderPath("Startup")
    if (-not (Test-Path -LiteralPath $startupDirectory -PathType Container)) {
        throw "The current user's Startup folder is unavailable: $startupDirectory"
    }
    $shortcutPath = Join-Path $startupDirectory ($TaskName + ".lnk")
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"{0}" --interval-seconds {1}' -f $daemonScript, ($IntervalMinutes * 60)
    $shortcut.WorkingDirectory = $resolvedRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Invisible read-only Futu market quote bridge; no account or trading access"
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw "Failed to register the invisible per-user Futu Startup shortcut."
    }
    Start-Process `
        -FilePath $pythonw `
        -ArgumentList @('"' + $daemonScript + '"', "--interval-seconds", ($IntervalMinutes * 60)) `
        -WorkingDirectory $resolvedRoot `
        -WindowStyle Hidden `
        -ErrorAction Stop | Out-Null
    Write-Output "Installed invisible per-user Futu Startup shortcut: $TaskName (every $IntervalMinutes minutes; no administrator required)."
}
