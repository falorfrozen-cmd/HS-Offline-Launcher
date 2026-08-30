$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $projectRoot 'src\hs_offline_launcher.py'
$tests = Join-Path $projectRoot 'tests'
$requirements = Join-Path $projectRoot 'requirements-build.txt'
$versionInfo = Join-Path $projectRoot 'version_info.txt'
$dist = Join-Path $projectRoot 'dist'
$buildRoot = Join-Path $projectRoot 'build'
$work = Join-Path $buildRoot 'pyinstaller'
$venv = Join-Path $buildRoot 'packaging-venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$artifactName = 'HS-Offline-Launcher'
$artifact = Join-Path $dist "$artifactName.exe"
$obsoleteArtifact = Join-Path $dist 'HS Offline Launcher.exe'
$checksumPath = Join-Path $projectRoot 'SHA256SUMS.txt'
$distChecksumPath = Join-Path $dist 'SHA256SUMS.txt'
$generatedOutputs = @($artifact, $obsoleteArtifact, $checksumPath, $distChecksumPath)
$buildSucceeded = $false
$hadNoUserSite = Test-Path Env:PYTHONNOUSERSITE
$previousNoUserSite = $env:PYTHONNOUSERSITE
$hadPythonPath = Test-Path Env:PYTHONPATH
$previousPythonPath = $env:PYTHONPATH

# Remove only exact generated outputs. If any build stage fails, no stale EXE
# or checksum can be mistaken for the current release candidate.
Remove-Item -LiteralPath $generatedOutputs -Force -ErrorAction SilentlyContinue

try {
    $env:PYTHONNOUSERSITE = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    python -m venv --clear $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the isolated build environment failed with exit code $LASTEXITCODE"
    }

    & $venvPython -m pip install --disable-pip-version-check --no-cache-dir -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Installing pinned build dependencies failed with exit code $LASTEXITCODE"
    }

    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated build environment failed pip check with exit code $LASTEXITCODE"
    }

    & $venvPython -m unittest discover -s $tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Tests failed with exit code $LASTEXITCODE"
    }

    $pyinstallerArgs = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--onefile',
        '--windowed',
        '--noupx',
        '--icon', 'NONE',
        '--name', $artifactName,
        '--distpath', $dist,
        '--workpath', $work,
        '--specpath', $work,
        '--version-file', $versionInfo,
        '--exclude-module', 'webview.platforms.android',
        '--exclude-module', 'webview.platforms.cef',
        '--exclude-module', 'webview.platforms.cocoa',
        '--exclude-module', 'webview.platforms.gtk',
        '--exclude-module', 'webview.platforms.qt',
        $entry
    )
    & $venvPython @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        throw "PyInstaller did not create the expected artifact: $artifact"
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText(
        $checksumPath,
        "$hash  $artifactName.exe`r`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    Copy-Item -LiteralPath $checksumPath -Destination $distChecksumPath -Force
    $buildSucceeded = $true

    Get-Item -LiteralPath $artifact | Select-Object Name, Length, LastWriteTime
    Write-Output "SHA256: $hash"
}
finally {
    if ($hadNoUserSite) {
        $env:PYTHONNOUSERSITE = $previousNoUserSite
    }
    else {
        Remove-Item Env:PYTHONNOUSERSITE -ErrorAction SilentlyContinue
    }
    if ($hadPythonPath) {
        $env:PYTHONPATH = $previousPythonPath
    }
    else {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    if (-not $buildSucceeded) {
        Remove-Item -LiteralPath $generatedOutputs -Force -ErrorAction SilentlyContinue
    }
}
