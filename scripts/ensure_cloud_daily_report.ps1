param(
    [string]$Repo = "ffxdz-ai/us-stock-research-dashboard",
    [string]$Workflow = "deepseek-daily-report.yml",
    [string]$SiteIndexUrl = "https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json",
    [int]$StartHourBeijing = 12,
    [int]$StopHourBeijing = 22,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root "logs"
$LogFile = Join-Path $Logs "cloud_daily_report_keeper.log"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "[$(Get-Date -Format s)] $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Get-BeijingNow {
    $tz = [TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
    return [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
}

function Test-TodayDeepSeekReportExists {
    param(
        [Parameter(Mandatory = $true)][string]$Today,
        [Parameter(Mandatory = $true)]$IndexData
    )

    $reports = @($IndexData.reports)
    foreach ($report in $reports) {
        if (-not $report) {
            continue
        }
        if ($report.kind -ne "deepseek-cloud") {
            continue
        }
        $searchable = @(
            $report.id,
            $report.filename,
            $report.title,
            $report.published_at,
            $report.published_label
        ) -join " "
        if ($searchable -like "*$Today*") {
            return $true
        }
    }
    return $false
}

function Get-PublicIndex {
    $separator = "?"
    if ($SiteIndexUrl.Contains("?")) {
        $separator = "&"
    }
    $url = "$SiteIndexUrl${separator}ts=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
    return $response.Content | ConvertFrom-Json
}

function Test-GhAuthenticated {
    & gh auth status *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-ActiveDailyReportRunCount {
    $json = & gh run list `
        --repo $Repo `
        --workflow $Workflow `
        --json databaseId,status,event,createdAt `
        --limit 20
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query GitHub Actions runs."
    }
    $runs = $json | ConvertFrom-Json
    $active = @($runs | Where-Object { $_.status -in @("queued", "in_progress", "waiting", "requested") })
    return $active.Count
}

$now = Get-BeijingNow
$today = $now.ToString("yyyy-MM-dd")

if (-not $Force -and ($now.Hour -lt $StartHourBeijing -or $now.Hour -ge $StopHourBeijing)) {
    Write-Log "outside watchdog window: Beijing $($now.ToString('yyyy-MM-dd HH:mm:ss')); window ${StartHourBeijing}:00-${StopHourBeijing}:00"
    exit 0
}

$exists = $false
try {
    $index = Get-PublicIndex
    $exists = Test-TodayDeepSeekReportExists -Today $today -IndexData $index
    Write-Log "public index generated_at=$($index.generated_at); latest=$($index.reports[0].published_label); today_report_exists=$exists"
} catch {
    Write-Log "failed to read public index; will trigger if possible: $($_.Exception.Message)"
}

if ($exists -and -not $Force) {
    Write-Log "$today DeepSeek report already exists; no action."
    exit 0
}

if (-not (Test-GhAuthenticated)) {
    Write-Log "GitHub CLI is not authenticated; cannot trigger workflow."
    exit 2
}

$activeCount = Get-ActiveDailyReportRunCount
if ($activeCount -gt 0) {
    Write-Log "daily report workflow already active: $activeCount run(s); no duplicate dispatch."
    exit 0
}

Write-Log "dispatching $Workflow for $today"
& gh workflow run $Workflow --repo $Repo -f mode=full
if ($LASTEXITCODE -ne 0) {
    Write-Log "workflow dispatch failed."
    exit 3
}

Write-Log "workflow dispatch submitted."
