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
$PayloadPath = Join-Path $Root "docs\data\reports.json"
$Payload = Get-Content -Raw -Encoding UTF8 $PayloadPath
foreach ($Pattern in $Forbidden) {
    if ($Payload -match $Pattern) {
        throw "Privacy check failed for pattern: $Pattern"
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

$ApiPath = "repos/$Repository/contents/docs/data/reports.json"
$Remote = $null
try {
    $RemoteJson = & $GitHubCli api "$ApiPath`?ref=$Branch" 2>$null
    if ($LASTEXITCODE -eq 0 -and $RemoteJson) {
        $Remote = $RemoteJson | ConvertFrom-Json
    }
} catch {
    $Remote = $null
}

$LocalBytes = [System.IO.File]::ReadAllBytes($PayloadPath)
$LocalBase64 = [Convert]::ToBase64String($LocalBytes)
if ($Remote -and $Remote.content) {
    $RemoteBase64 = [string]$Remote.content -replace '\s', ''
    if ($RemoteBase64 -eq $LocalBase64) {
        Write-Output "Public report archive is already up to date."
        exit 0
    }
}

$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$Request = @{
    message = "Update public reports $Stamp"
    content = $LocalBase64
    branch = $Branch
}
if ($Remote -and $Remote.sha) {
    $Request.sha = [string]$Remote.sha
}

$RequestJson = $Request | ConvertTo-Json -Compress
$RequestJson | & $GitHubCli api --method PUT $ApiPath --input - | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to publish the public report archive through GitHub API."
}

Write-Output "Public report archive synchronized to $Repository ($Branch)."
