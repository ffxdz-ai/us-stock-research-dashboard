param(
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (-not (Test-Path $Python)) {
    $Python = "python"
}

Set-Location $Root
& $Python (Join-Path $PSScriptRoot "export_public_reports.py")
if ($LASTEXITCODE -ne 0) {
    throw "Public report export failed."
}

$Forbidden = @(
    'portfolio\.json',
    '[A-Z]:\\',
    '估算总资产\s*[:：]',
    '现金比例\s*[:：]',
    '\|\s*Ticker\s*\|\s*股数\s*\|'
)
$PayloadPath = Join-Path $Root "docs\data\reports.json"
$Payload = Get-Content -Raw -Encoding UTF8 $PayloadPath
foreach ($Pattern in $Forbidden) {
    if ($Payload -match $Pattern) {
        throw "Privacy check failed for pattern: $Pattern"
    }
}

git add -- "docs/data/reports.json"
git diff --cached --quiet -- "docs/data/reports.json"
if ($LASTEXITCODE -eq 0) {
    Write-Output "Public report archive is already up to date."
    exit 0
}

$Stamp = Get-Date -Format "yyyy-MM-dd"
git commit -m "Update public reports $Stamp" -- "docs/data/reports.json"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to commit the public report archive."
}

if (-not $NoPush) {
    $Branch = git branch --show-current
    if (-not $Branch) {
        throw "Cannot push from a detached HEAD."
    }
    git push origin $Branch
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to push public report archive."
    }
}

Write-Output "Public report archive synchronized."
