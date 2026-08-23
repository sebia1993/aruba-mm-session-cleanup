param(
    [string]$PythonExe = "python",
    [string]$ReleaseTag = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Wait-ForReadableFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [int]$Attempts = 20,
        [int]$DelayMilliseconds = 500
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
            $stream.Close()
            return
        }
        catch {
            if ($attempt -eq $Attempts) {
                throw "File is not ready for reading: $Path`n$($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

Write-Host "Installing runtime and build dependencies..."
& $PythonExe -m pip install --require-hashes -r ".\requirements.lock"
if ($LASTEXITCODE -ne 0) {
    throw "locked runtime install failed with exit code $LASTEXITCODE"
}
& $PythonExe -m pip install -e . --no-deps
if ($LASTEXITCODE -ne 0) {
    throw "project install failed with exit code $LASTEXITCODE"
}
& $PythonExe -m pip install pyinstaller -c ".\constraints.txt"
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed with exit code $LASTEXITCODE"
}
& $PythonExe -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "pip check failed with exit code $LASTEXITCODE"
}

$version = & $PythonExe -c "from aruba_mm_cleanup import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "version lookup failed with exit code $LASTEXITCODE"
}
$guiExeName = "ArubaMMCleanupGUI"
$webExeName = "ArubaMMCleanupWeb"
$packageTag = if ($ReleaseTag) { $ReleaseTag } else { "v$version" }
$distDir = Join-Path $PSScriptRoot "dist"
$buildRoot = Join-Path $PSScriptRoot ".pyinstaller_build"
$buildDir = Join-Path $buildRoot ([DateTime]::Now.ToString("yyyyMMdd_HHmmss"))
$specDir = Join-Path $buildDir "spec"
$releaseRoot = Join-Path $buildDir "release"
$releaseGuiDir = Join-Path $releaseRoot "gui"
$releaseWebDir = Join-Path $releaseRoot "web"
$distGuiExe = Join-Path $distDir "$guiExeName.exe"
$distWebExe = Join-Path $distDir "$webExeName.exe"
$releaseZip = Join-Path $distDir "aruba-mm-session-cleanup_${packageTag}_windows.zip"

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
New-Item -ItemType Directory -Force -Path $specDir | Out-Null
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
New-Item -ItemType Directory -Force -Path $releaseGuiDir | Out-Null
New-Item -ItemType Directory -Force -Path $releaseWebDir | Out-Null

if (Test-Path $distGuiExe) { Remove-Item -LiteralPath $distGuiExe -Force }
if (Test-Path $distWebExe) { Remove-Item -LiteralPath $distWebExe -Force }
if (Test-Path $releaseZip) { Remove-Item -LiteralPath $releaseZip -Force }

Write-Host "Building Windows GUI executable..."
& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $guiExeName `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $specDir `
    --paths ".\src" `
    ".\gui_launcher.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller GUI build failed with exit code $LASTEXITCODE"
}

Write-Host "Building Windows web app executable..."
& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name $webExeName `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $specDir `
    --paths ".\src" `
    ".\web_launcher.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller web app build failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath $distGuiExe -Destination $releaseGuiDir -Force
Copy-Item -LiteralPath ".\docs\USER_GUIDE_KO.md" -Destination $releaseGuiDir -Force
New-Item -ItemType Directory -Force -Path (Join-Path $releaseGuiDir "config\mock_scenarios") | Out-Null
Copy-Item -LiteralPath ".\config\mock_scenarios\profiling_users.txt" -Destination (Join-Path $releaseGuiDir "config\mock_scenarios") -Force

Copy-Item -LiteralPath $distWebExe -Destination $releaseWebDir -Force
New-Item -ItemType Directory -Force -Path (Join-Path $releaseWebDir "config\mock_scenarios") | Out-Null
Copy-Item -LiteralPath ".\config\mock_scenarios\profiling_users.txt" -Destination (Join-Path $releaseWebDir "config\mock_scenarios") -Force

$startWebApp = @"
@echo off
setlocal
cd /d "%~dp0"
ArubaMMCleanupWeb.exe %*
"@
$startWebApp | Set-Content -LiteralPath (Join-Path $releaseWebDir "start_webapp.cmd") -Encoding ASCII

$startHere = @"
Aruba MM Cleanup Windows 실행 안내

1. 다운로드 파일
- GitHub Release에서 aruba-mm-session-cleanup_<tag>_windows.zip 파일을 다운로드합니다.
- 같은 Release의 .sha256 파일로 ZIP 무결성을 확인하고, sbom.cdx.json에서 포함 구성요소를 확인할 수 있습니다.
- GitHub가 자동으로 표시하는 Source code (zip), Source code (tar.gz)는 소스 아카이브이며 일반 사용자가 실행할 파일이 아닙니다.

2. GUI 실행
- ZIP 압축을 풉니다.
- gui\ArubaMMCleanupGUI.exe 를 실행합니다.

3. 웹앱 실행
- ZIP 압축을 풉니다.
- web\start_webapp.cmd 를 더블클릭합니다.
- 브라우저가 열리면 장비 정보와 Role을 입력해 대상을 조회합니다.
- 표시된 대상 snapshot을 확인하고 DELETE N을 정확히 입력한 경우에만 삭제됩니다.

4. 웹앱 포트/설정 변경
- 기본 주소는 127.0.0.1, 기본 포트는 8765입니다.
- 포트를 바꾸려면 명령 프롬프트에서 web 폴더로 이동한 뒤 아래처럼 실행합니다.
  start_webapp.cmd --port 9876
- 브라우저 자동 열기를 막으려면 아래처럼 실행합니다.
  start_webapp.cmd --no-browser
- smoke 검증은 아래처럼 실행합니다.
  start_webapp.cmd --smoke

5. 배포 구성
- 이 ZIP에는 일반 사용자용 GUI와 웹앱만 포함됩니다.
- CLI 실행 파일은 최종 사용자용 Release ZIP에 포함하지 않습니다.
"@
$startHere.Replace("<tag>", $packageTag) | Set-Content -LiteralPath (Join-Path $releaseRoot "README_START_HERE_KO.txt") -Encoding UTF8

Get-ChildItem -Path $releaseRoot -Recurse -File | ForEach-Object {
    Wait-ForReadableFile -Path $_.FullName
}

if (Test-Path $releaseZip) {
    Remove-Item -LiteralPath $releaseZip -Force
}
Compress-Archive -Path (Join-Path $releaseRoot "*") -DestinationPath $releaseZip -Force

Write-Host ""
Write-Host "Build completed."
Write-Host "GUI executable: $distGuiExe"
Write-Host "Web app executable: $distWebExe"
Write-Host "Release zip: $releaseZip"
