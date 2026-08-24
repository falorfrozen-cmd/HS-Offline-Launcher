$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $projectRoot 'src\hs_offline_launcher.py'
$dist = Join-Path $projectRoot 'dist'
$work = Join-Path $projectRoot 'build'

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'HS Offline Launcher' `
    --distpath $dist `
    --workpath $work `
    --specpath $projectRoot `
    --collect-all webview `
    $entry

Get-Item (Join-Path $dist 'HS Offline Launcher.exe') | Select-Object Name,Length,LastWriteTime
