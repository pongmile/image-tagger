# Creates a Desktop shortcut that launches Image Tagger.
# Prefers the packaged app (apps/desktop/dist/win-unpacked); falls back to run.cmd
# (dev mode) if the app hasn't been packaged yet.
param(
  [string]$Name = "Image Tagger"
)

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$packed = Join-Path $repo "apps\desktop\dist\win-unpacked\Image Tagger.exe"
$runCmd = Join-Path $repo "run.cmd"

if (Test-Path $packed) {
  $target = $packed
  $workdir = Split-Path $packed
  $icon = "$packed,0"
} else {
  $target = $runCmd
  $workdir = $repo
  $icon = "$runCmd,0"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnkPath)
$s.TargetPath = $target
$s.WorkingDirectory = $workdir
$s.IconLocation = $icon
$s.Description = "Local Image Tagger & Search"
$s.Save()

Write-Host "Created shortcut: $lnkPath"
Write-Host "  -> $target"
