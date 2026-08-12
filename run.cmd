@echo off
rem Easy launcher for Image Tagger.
rem  - If the packaged app is built (npm run dist / pack), launch that directly.
rem  - Otherwise fall back to dev mode (builds the renderer + runs Electron).
setlocal
cd /d "%~dp0"

set "PACKED=%~dp0apps\desktop\dist\win-unpacked\Image Tagger.exe"

if exist "%PACKED%" (
  echo Launching Image Tagger...
  start "" "%PACKED%"
  goto :eof
)

echo Packaged app not found - starting in dev mode (first run builds the UI)...
call npm run dev
endlocal
