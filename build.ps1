param(
    [string]$Python = 'python',
    [string]$OutputDirectory = 'dist-mediaflow-2.1.0',
    [switch]$SkipToolDownload,
    [switch]$SkipDependencyInstall,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$distributionRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $OutputDirectory))
if ((Test-Path -LiteralPath $distributionRoot) -and
    (Get-ChildItem -LiteralPath $distributionRoot -Force | Select-Object -First 1)) {
    throw '发布目录非空；请选择新的输出目录，保留已有稳定版本。'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令执行失败，退出码：$LASTEXITCODE" }
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked $Python @('-m', 'venv', '.venv')
}
if (-not $SkipDependencyInstall) {
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', 'requirements-build.txt')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', 'requirements.txt')
}
Invoke-Checked $venvPython @('assets\create_icon.py')
if (-not $SkipToolDownload) {
    Invoke-Checked $venvPython @('tools\prepare_tools.py')
}
if (-not $SkipTests) {
    $env:PYTHONIOENCODING = 'utf-8'
    # Security-only parsers and schema tooling must not enter the packaged venv.
    $securityPython = Join-Path $PSScriptRoot '.security-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $securityPython)) {
        if ($SkipDependencyInstall) { throw '独立安全测试环境缺失；请先安装 requirements-security.txt，或允许安装依赖。' }
        Invoke-Checked $Python @('-m', 'venv', '.security-venv')
    }
    if (-not $SkipDependencyInstall) {
        Invoke-Checked $securityPython @('-m', 'pip', 'install', '-r', 'requirements-build.txt', '-r', 'requirements-security.txt')
    }
    Invoke-Checked $venvPython @('tests\run_unit_tests.py', '--security-python', $securityPython)
}
Invoke-Checked $venvPython @('tools\prepare_browser_notices.py')
& (Join-Path $PSScriptRoot 'scripts\build-recycle.ps1')
if ($LASTEXITCODE -ne 0) { throw '回收站辅助程序构建失败。' }
& (Join-Path $PSScriptRoot 'scripts\build-webview2.ps1')
if ($LASTEXITCODE -ne 0) { throw 'WebView2 原生宿主构建失败。' }
Invoke-Checked $venvPython @('scripts\stamp_build.py')
Invoke-Checked $venvPython @('-m', 'PyInstaller', '--noconfirm', '--distpath', $distributionRoot, 'MediaFlow.spec')
$packagingReport = Join-Path $PSScriptRoot ('verification\packaging-' + [Guid]::NewGuid().ToString('N') + '.json')
New-Item -ItemType Directory -Path (Split-Path -Parent $packagingReport) -Force | Out-Null
Invoke-Checked $venvPython @('scripts\check_packaging_compatibility.py', '--release-dir', $distributionRoot, '--report', $packagingReport)
$toolTarget = Join-Path $distributionRoot 'tools'
New-Item -ItemType Directory -Path $toolTarget -Force | Out-Null
foreach ($toolName in @('yt-dlp.exe', 'ffmpeg.exe', 'ffprobe.exe', 'FFmpeg-LICENSE', 'FFmpeg-README.txt',
                        'yt-dlp-LICENSE.txt', 'yt-dlp-THIRD_PARTY_LICENSES.txt', 'provenance.json', 'MediaFlow.Recycle.exe')) {
    $toolSource = Join-Path $PSScriptRoot "tools\$toolName"
    if (Test-Path -LiteralPath $toolSource) {
        Copy-Item -LiteralPath $toolSource -Destination $toolTarget -Force
    }
}
$webviewTarget = Join-Path $toolTarget 'webview2'
New-Item -ItemType Directory -Path $webviewTarget -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'tools\webview2') -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $webviewTarget -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'scripts\MediaFlow-Qt.cmd') -Destination $distributionRoot -Force
foreach ($documentName in @('README.md', 'MEDIAFLOW_DASH_FIX.md', 'MEDIAFLOW_BROWSER_LAYER.md', 'MEDIAFLOW_WEBVIEW2.md', 'MEDIAFLOW_BROWSER_ENVIRONMENT.md', 'MEDIAFLOW_DETAILS_QUALITY.md', 'MEDIAFLOW_BRAND_THUMBNAILS.md', 'PROTOCOL_REFACTOR.md', 'MEDIA_DISCOVERY.md', 'MEDIAFLOW_CONFIDENCE.md', 'RESOLVER_UPGRADE.md', 'ROU_CURRENT_PROTOCOL.md', 'ROU_PIPELINE_FIX.md', 'BATCH_DOWNLOAD.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $documentName) -Destination $distributionRoot -Force
}
$identityFile = Join-Path $distributionRoot 'build-info.json'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_DASH_RELEASE_FIX.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_RECOVERY.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_DOUYIN_FIX.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_TOOLS.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_LIBRARY.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_BATCH_IMPORT.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_TRACKS.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_ORGANIZATION.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_STORAGE.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_MIGRATION.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs\MIGRATION_AUDIT.md') -Destination (Join-Path $distributionRoot 'MIGRATION_AUDIT.md') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'MEDIAFLOW_UPDATE.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'PUBLISHING.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'PRODUCTION_RELEASE.md') -Destination $distributionRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs\UPDATE_AUDIT.md') -Destination (Join-Path $distributionRoot 'UPDATE_AUDIT.md') -Force
$identityProcess = Start-Process -FilePath (Join-Path $distributionRoot 'MediaFlow.exe') -ArgumentList @('--build-info', ('"' + $identityFile + '"')) -PassThru -Wait -WindowStyle Hidden
if ($identityProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $identityFile)) {
    throw '发布 EXE 的构建标识或 DASH 模块加载检查失败。'
}
$identity = Get-Content -LiteralPath $identityFile -Raw | ConvertFrom-Json
if (-not $identity.packaged -or -not $identity.dash_module_loaded -or $identity.dash_feature_revision -lt 2) {
    throw '发布 EXE 未包含本次 DASH API / HTML 内嵌解析能力。'
}
if (-not $identity.track_module_loaded -or $identity.track_feature_revision -lt 1 -or -not $identity.migration_module_loaded -or -not $identity.update_module_loaded -or $identity.app_version -ne '2.1.0' -or $identity.update_feature_revision -lt 4) {
    throw '发布 EXE 的版本号或音轨、迁移、更新模块检查失败。'
}
if (-not $identity.organization_module_loaded -or $identity.organization_feature_revision -lt 1) {
    throw '发布 EXE 未包含本次文件命名与整理模块。'
}
if (-not $identity.storage_module_loaded -or $identity.storage_feature_revision -lt 1) {
    throw '发布 EXE 未包含完整哈希和安全回收模块。'
}
# The real EXE report above is verified before producing the portable stamp.
# Machine-specific runtime paths belong only in private acceptance reports.
Invoke-Checked $venvPython @('-c', 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); d=json.loads(p.read_text(encoding="utf-8")); d.pop("executable",None); d.pop("runtime_root",None); p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")', $identityFile)
$licenseSource = (& $venvPython -c 'import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).resolve().parent.parent)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $licenseSource)) { throw '无法定位实际 Qt 依赖许可目录。' }
$licenseTarget = Join-Path $distributionRoot 'licenses'
New-Item -ItemType Directory -Path $licenseTarget -Force | Out-Null
$pythonLicense = (& $venvPython -c 'import pathlib,sys; print(pathlib.Path(sys.base_prefix) / "LICENSE.txt")').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonLicense)) { throw '缺少实际 CPython 运行时许可证。' }
$pythonLicenseTarget = Join-Path $licenseTarget 'Python'
New-Item -ItemType Directory -Path $pythonLicenseTarget -Force | Out-Null
Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $pythonLicenseTarget 'LICENSE.txt') -Force
$pyinstallerLicense = (& $venvPython -c 'import importlib.metadata as m; d=m.distribution("pyinstaller"); print(d.locate_file(next(p for p in d.files if str(p).endswith("licenses/COPYING.txt"))))').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pyinstallerLicense)) { throw '缺少实际 PyInstaller 分发例外与许可。' }
$pyinstallerLicenseTarget = Join-Path $licenseTarget 'PyInstaller'
New-Item -ItemType Directory -Path $pyinstallerLicenseTarget -Force | Out-Null
Copy-Item -LiteralPath $pyinstallerLicense -Destination (Join-Path $pyinstallerLicenseTarget 'COPYING.txt') -Force
foreach ($sourceDocument in @('THIRD_PARTY_SOURCES.md', 'third-party-sources.json')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('docs\' + $sourceDocument)) -Destination $licenseTarget -Force
}
$browserNotices = Join-Path $PSScriptRoot 'verification\browser-licenses'
$browserLicenseTarget = Join-Path $licenseTarget 'QtWebEngine'
New-Item -ItemType Directory -Path $browserLicenseTarget -Force | Out-Null
Get-ChildItem -LiteralPath $browserNotices -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $browserLicenseTarget -Force
}
Get-ChildItem -LiteralPath $licenseSource -Directory -Filter '*.dist-info' | Where-Object {
    $_.Name -like '*side6*' -or $_.Name -like '*shiboken6*'
} | ForEach-Object {
    $packageLicense = Join-Path $_.FullName 'licenses'
    if (Test-Path -LiteralPath $packageLicense) {
        Copy-Item -LiteralPath $packageLicense -Destination (Join-Path $licenseTarget $_.Name) -Recurse -Force
    }
}
$cryptoLicenseSource = (& $venvPython -c 'import pathlib, cryptography; print(pathlib.Path(cryptography.__file__).resolve().parent.parent)').Trim()
Get-ChildItem -LiteralPath $cryptoLicenseSource -Directory -Filter '*.dist-info' | Where-Object {
    $_.Name -like 'cryptography-*' -or $_.Name -like 'cffi-*' -or $_.Name -like 'pycparser-*'
} | ForEach-Object {
    $dependencyLicense = Join-Path $_.FullName 'licenses'
    if (Test-Path -LiteralPath $dependencyLicense) {
        Copy-Item -LiteralPath $dependencyLicense -Destination (Join-Path $licenseTarget $_.Name) -Recurse -Force
    }
}
if (-not $SkipTests -and (Test-Path -LiteralPath (Join-Path $toolTarget 'yt-dlp.exe')) -and
    (Test-Path -LiteralPath (Join-Path $toolTarget 'ffmpeg.exe'))) {
    $smokeReport = Join-Path $PSScriptRoot 'verification\packaged'
    $exePath = Join-Path $distributionRoot 'MediaFlow.exe'
    $process = Start-Process -FilePath $exePath -ArgumentList @('--smoke-test', ('"' + $smokeReport + '"')) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "打包后的 GUI 下载验收失败，请查看 $smokeReport\gui-results.json" }
    $nativeReport = Join-Path $PSScriptRoot 'verification\packaged-native'
    $nativeProcess = Start-Process -FilePath $exePath -ArgumentList @('--smoke-test', ('"' + $nativeReport + '"'), '--native-smoke') -PassThru -Wait -WindowStyle Hidden
    if ($nativeProcess.ExitCode -ne 0) { throw "Windows 原生 GUI 验收失败，请查看 $nativeReport\gui-results.json" }
    foreach ($discoveryMode in @('packaged-discovery', 'packaged-discovery-native')) {
        $discoveryReport = Join-Path $PSScriptRoot "verification\$discoveryMode"
        $discoveryArguments = @('--discovery-smoke', ('"' + $discoveryReport + '"'))
        if ($discoveryMode -eq 'packaged-discovery-native') { $discoveryArguments += '--native-smoke' }
        $discoveryProcess = Start-Process -FilePath $exePath -ArgumentList $discoveryArguments -PassThru -Wait -WindowStyle Hidden
        if ($discoveryProcess.ExitCode -ne 0) { throw "自动查找 Chromium/下载验收失败，请查看 $discoveryReport\discovery-results.json" }
    }
    foreach ($resolverMode in @('packaged-resolver', 'packaged-resolver-native')) {
        $resolverReport = Join-Path $PSScriptRoot "verification\$resolverMode"
        $resolverArguments = @('--resolver-smoke', ('"' + $resolverReport + '"'))
        if ($resolverMode -eq 'packaged-resolver-native') { $resolverArguments += '--native-smoke' }
        $resolverProcess = Start-Process -FilePath $exePath -ArgumentList $resolverArguments -PassThru -Wait -WindowStyle Hidden
        if ($resolverProcess.ExitCode -ne 0) { throw "Resolver/刷新续传验收失败，请查看 $resolverReport\resolver-results.json" }
    }
    foreach ($batchMode in @('packaged-batch', 'packaged-batch-native')) {
        $batchReport = Join-Path $PSScriptRoot "verification\$batchMode"
        $batchArguments = @('--batch-smoke', ('"' + $batchReport + '"'))
        if ($batchMode -eq 'packaged-batch-native') { $batchArguments += '--native-smoke' }
        $batchProcess = Start-Process -FilePath $exePath -ArgumentList $batchArguments -PassThru -Wait -WindowStyle Hidden
        if ($batchProcess.ExitCode -ne 0) { throw "批量下载验收失败，请查看 $batchReport\batch-results.json" }
    }
    foreach ($rouMode in @('packaged-rou-current', 'packaged-rou-current-native')) {
        $rouReport = Join-Path $PSScriptRoot "verification\$rouMode"
        $rouArguments = @('--rou-current-smoke', ('"' + $rouReport + '"'))
        if ($rouMode -eq 'packaged-rou-current-native') { $rouArguments += '--native-smoke' }
        $rouProcess = Start-Process -FilePath $exePath -ArgumentList $rouArguments -PassThru -Wait -WindowStyle Hidden
        if ($rouProcess.ExitCode -ne 0) { throw "当前 Rou 协议验收失败，请查看 $rouReport\rou-current-results.json" }
    }
    foreach ($mediaflowMode in @('packaged-mediaflow', 'packaged-mediaflow-native')) {
        $mediaflowReport = Join-Path $PSScriptRoot "verification\$mediaflowMode"
        $mediaflowArguments = @('--mediaflow-smoke', ('"' + $mediaflowReport + '"'))
        if ($mediaflowMode -eq 'packaged-mediaflow-native') { $mediaflowArguments += '--native-smoke' }
        $mediaflowProcess = Start-Process -FilePath $exePath -ArgumentList $mediaflowArguments -PassThru -Wait -WindowStyle Hidden
        if ($mediaflowProcess.ExitCode -ne 0) { throw "MediaFlow真实性验收失败，请查看 $mediaflowReport\mediaflow-results.json" }
    }
    $environmentReport = Join-Path $PSScriptRoot 'verification\packaged-browser-environment-native'
    $environmentProcess = Start-Process -FilePath $exePath -ArgumentList @('--browser-environment-smoke', ('"' + $environmentReport + '"'), '--native-smoke') -PassThru -Wait -WindowStyle Hidden
    if ($environmentProcess.ExitCode -ne 0) { throw "浏览器环境验收失败，请查看 $environmentReport\browser-environment-results.json" }
    $webviewReport = Join-Path $PSScriptRoot 'verification\packaged-webview2-native'
    $webviewProcess = Start-Process -FilePath $exePath -ArgumentList @('--webview2-smoke', ('"' + $webviewReport + '"'), '--native-smoke') -PassThru -Wait -WindowStyle Hidden
    if ($webviewProcess.ExitCode -ne 0) { throw "WebView2 原生验收失败，请查看 $webviewReport\webview2-results.json" }
}
Write-Host "打包完成：$distributionRoot\MediaFlow.exe（请连同 tools 一起携带）" -ForegroundColor Green
