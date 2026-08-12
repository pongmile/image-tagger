param([string]$PythonVersion = "3.12")

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$destination = Join-Path $repoRoot "apps\desktop\build\python"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to prepare the bundled Python runtime. Install uv, then retry."
}

$pythonExe = (& uv python find $PythonVersion).Trim()
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "uv did not return a usable Python $PythonVersion executable"
}
$sourceRoot = Split-Path -Parent $pythonExe

if (Test-Path -LiteralPath $destination) {
    $resolvedDestination = (Resolve-Path -LiteralPath $destination).Path
    $buildRoot = (Resolve-Path (Join-Path $repoRoot "apps\desktop\build")).Path
    if (-not $resolvedDestination.StartsWith($buildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace runtime outside the desktop build directory: $resolvedDestination"
    }
    Remove-Item -LiteralPath $resolvedDestination -Recurse -Force
}

Write-Host "Copying Python $PythonVersion runtime from $sourceRoot"
Copy-Item -LiteralPath $sourceRoot -Destination $destination -Recurse

$bundledPython = Join-Path $destination "python.exe"
$requirements = Join-Path $repoRoot "apps\indexer\requirements.txt"
# uv marks its managed interpreter as externally managed. This is a disposable
# private copy embedded in the app, so installing into the copy is intentional
# and never mutates uv's source interpreter.
& $bundledPython -m pip install --break-system-packages --disable-pip-version-check --no-warn-script-location -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install core indexer dependencies" }

Write-Host "Bundled runtime ready: $bundledPython"
