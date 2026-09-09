param(
    [string]$TaskName = "USStockFutuQuoteBridge",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$IntervalSeconds = 15,
    [int]$HeartbeatSeconds = 60
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

$safeInterval = [Math]::Max(5, $IntervalSeconds)
$safeHeartbeat = [Math]::Max(30, $HeartbeatSeconds)
$arguments = '"{0}" --scope all --interval-seconds {1} --heartbeat-seconds {2}' -f $daemonScript, $safeInterval, $safeHeartbeat
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $resolvedRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $logonTrigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Upload market-only Futu OpenD quotes through an authenticated HTTPS bridge; no account or trading access." `
        -Force `
        -ErrorAction Stop | Out-Null
    $startupDirectory = [Environment]::GetFolderPath("Startup")
    $legacyShortcut = Join-Path $startupDirectory ($TaskName + ".lnk")
    if (Test-Path -LiteralPath $legacyShortcut -PathType Leaf) {
        Remove-Item -LiteralPath $legacyShortcut -Force
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Output "Installed hidden continuous Futu quote bridge task: $TaskName (${safeInterval}s coalescing; ${safeHeartbeat}s heartbeat)."
} catch {
    $startupDirectory = [Environment]::GetFolderPath("Startup")
    if (-not (Test-Path -LiteralPath $startupDirectory -PathType Container)) {
        throw "The current user's Startup folder is unavailable: $startupDirectory"
    }
    $shortcutPath = Join-Path $startupDirectory ($TaskName + ".lnk")
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"{0}" --scope all --interval-seconds {1} --heartbeat-seconds {2}' -f $daemonScript, $safeInterval, $safeHeartbeat
    $shortcut.WorkingDirectory = $resolvedRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Invisible read-only Futu market quote bridge; no account or trading access"
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw "Failed to register the invisible per-user Futu Startup shortcut."
    }
    Start-Process `
        -FilePath $pythonw `
        -ArgumentList @('"' + $daemonScript + '"', "--scope", "all", "--interval-seconds", $safeInterval, "--heartbeat-seconds", $safeHeartbeat) `
        -WorkingDirectory $resolvedRoot `
        -WindowStyle Hidden `
        -ErrorAction Stop | Out-Null
    Write-Output "Installed invisible continuous Futu Startup bridge: $TaskName (${safeInterval}s coalescing; no administrator required)."
}
