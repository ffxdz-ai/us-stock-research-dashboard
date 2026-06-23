param(
    [switch]$NoPush,
    [string]$Repository = "ffxdz-ai/us-stock-research-dashboard",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$GitHubCli = "C:\Program Files\GitHub CLI\gh.exe"

if (-not (Test-Path $Python)) {
    $Python = "python"
}
if (-not (Test-Path $GitHubCli)) {
    $GitHubCli = "gh"
}

Set-Location $Root
& $Python (Join-Path $PSScriptRoot "export_public_reports.py")
if ($LASTEXITCODE -ne 0) {
    throw "Public report export failed."
}

$Forbidden = @(
    'portfolio\.json',
    '[A-Z]:\\',
    'cash_usd',
    'cost_basis',
    'estimated_total_assets',
    '\|\s*Ticker\s*\|\s*shares\s*\|'
)
$PublicDataRoot = Join-Path $Root "docs\data"
$PublicDataFiles = @(Get-ChildItem -Path $PublicDataRoot -Recurse -File -Filter "*.json")
if ($PublicDataFiles.Count -eq 0) {
    throw "No public data JSON files were generated."
}

foreach ($File in $PublicDataFiles) {
    $Payload = Get-Content -Raw -Encoding UTF8 $File.FullName
    foreach ($Pattern in $Forbidden) {
        if ($Payload -match $Pattern) {
            throw "Privacy check failed for $($File.FullName) pattern: $Pattern"
        }
    }
}

if ($NoPush) {
    Write-Output "Public report archive exported and validated without publishing."
    exit 0
}

& $GitHubCli auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated."
}

& $GitHubCli auth setup-git | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to configure git authentication through GitHub CLI."
}

$Git = "git"
& $Git rev-parse --is-inside-work-tree | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "This script must be run inside the git repository."
}

& $Git add -- "docs/data/reports.json" "docs/data/index.json" "docs/data/reports"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to stage the public report archive."
}

& $Git diff --cached --quiet --exit-code
if ($LASTEXITCODE -eq 0) {
    Write-Output "Public report archive is already up to date."
    exit 0
}
if ($LASTEXITCODE -ne 1) {
    throw "Unable to inspect staged public archive changes."
}

$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
& $Git commit -m "Update public reports $Stamp"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to commit the public report archive."
}

& $Git push origin "HEAD:$Branch"
if ($LASTEXITCODE -ne 0) {
    Write-Output "Initial push was rejected; rebasing onto origin/$Branch and retrying."
    & $Git fetch origin $Branch
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to fetch origin/$Branch."
    }

    & $Git rebase --autostash "origin/$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to rebase public report archive onto origin/$Branch."
    }

    & $Git push origin "HEAD:$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to publish the public report archive through git push."
    }
}

Write-Output "Public report archive synchronized to $Repository ($Branch)."
