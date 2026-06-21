param(
    [ValidateSet("core", "universe", "all")]
    [string]$Scope = "core",
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path $BundledPython) {
    $Python = $BundledPython
} else {
    $Python = "python"
}

$ArgsList = @(
    (Join-Path $PSScriptRoot "sync_futu_local_snapshot.py"),
    "--scope", $Scope
)
if ($Strict) {
    $ArgsList += "--strict"
}

Push-Location $Root
try {
    & $Python @ArgsList
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
