param(
    [switch]$Send,
    [string]$SendOnly,
    [ValidateSet("Quick", "Full", "Weekly")]
    [string]$Mode = "Full"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path $BundledPython) {
    $Python = $BundledPython
} else {
    $Python = "python"
}

if ($SendOnly) {
    & $Python (Join-Path $PSScriptRoot "send_feishu.py") --file $SendOnly
    exit $LASTEXITCODE
}

$ReportsDir = Join-Path $Root "reports"
$DataDir = Join-Path $Root "data"
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd-HHmm"
$ModeLower = $Mode.ToLowerInvariant()
switch ($Mode) {
    "Quick" {
        $ReportPath = Join-Path $ReportsDir "$Timestamp-market-quick.md"
        $LatestReportPath = Join-Path $ReportsDir "latest-market-quick.md"
        $PackPath = Join-Path $DataDir "latest_quick_market_pack.json"
        $CompactPath = Join-Path $DataDir "latest_quick_agent_input.json"
    }
    "Weekly" {
        $ReportPath = Join-Path $ReportsDir "$Timestamp-weekly-market-scan.md"
        $LatestReportPath = Join-Path $ReportsDir "latest-weekly-market-scan.md"
        $PackPath = Join-Path $DataDir "latest_weekly_market_pack.json"
        $CompactPath = Join-Path $DataDir "latest_weekly_agent_input.json"
    }
    default {
        $ReportPath = Join-Path $ReportsDir "$Timestamp-market-brief.md"
        $LatestReportPath = Join-Path $ReportsDir "latest-market-brief.md"
        $PackPath = Join-Path $DataDir "latest_market_pack.json"
        $CompactPath = Join-Path $DataDir "latest_agent_input.json"
    }
}

& $Python (Join-Path $PSScriptRoot "collect_market_data.py") --mode $ModeLower --out $PackPath --compact-out $CompactPath --report $ReportPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Copy-Item -Force $ReportPath $LatestReportPath

& $Python (Join-Path $PSScriptRoot "research_discipline.py") --market-pack $PackPath --compact-input $CompactPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($Send) {
    & $Python (Join-Path $PSScriptRoot "send_feishu.py") --file $LatestReportPath
}
