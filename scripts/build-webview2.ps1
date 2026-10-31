param([switch]$Offline,[string]$OutputDirectory='')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-version-info.ps1')
$sourceRoot = Join-Path $projectRoot 'native\webview2'
$cacheRoot = Join-Path $sourceRoot '.sdk'
$outputRoot = Join-Path $projectRoot 'tools\webview2'
if($OutputDirectory){$outputRoot=$OutputDirectory}
$sdkVersion = '1.0.4258.31'
$sdkSha256 = '56F7F4B8BF9AEE4B8EFEFBBDD4F67D5F74EBD1B100ED0806DA71BF76AF481AA9'
$packagePath = Join-Path $cacheRoot "microsoft.web.webview2.$sdkVersion.nupkg"
New-Item -ItemType Directory -Force -Path $cacheRoot, $outputRoot | Out-Null
if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
    if ($Offline) { throw 'The pinned WebView2 SDK package is not cached.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$sdkVersion/microsoft.web.webview2.$sdkVersion.nupkg" -OutFile $packagePath
}
if ((Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash -ne $sdkSha256) {
    throw 'WebView2 SDK SHA256 mismatch; refusing to compile.'
}
$cscPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $cscPath -PathType Leaf)) { throw '.NET Framework x64 C# compiler is unavailable.' }
$extractionRoot = Join-Path $cacheRoot ('extract-' + [Guid]::NewGuid().ToString('N'))
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($packagePath, $extractionRoot)
    $coreDll = Join-Path $extractionRoot 'lib\net462\Microsoft.Web.WebView2.Core.dll'
    $loaderDll = Join-Path $extractionRoot 'runtimes\win-x64\native\WebView2Loader.dll'
    $hostExe = Join-Path $outputRoot 'MediaFlow.WebView2Host.exe'
    $versionFile=Join-Path $extractionRoot 'MediaFlow.VersionInfo.cs'
    Write-MediaFlowNativeVersion $projectRoot $versionFile
    & $cscPath /nologo /target:winexe /platform:x64 /optimize+ /utf8output /langversion:5 `
        "/out:$hostExe" "/win32manifest:$(Join-Path $sourceRoot 'host.manifest')" `
        /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll `
        /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll `
        "/reference:$coreDll" (Join-Path $sourceRoot 'Host.cs') $versionFile
    if ($LASTEXITCODE -ne 0) { throw 'WebView2 host compilation failed.' }
    Copy-Item -LiteralPath $coreDll -Destination (Join-Path $outputRoot 'Microsoft.Web.WebView2.Core.dll') -Force
    Copy-Item -LiteralPath $loaderDll -Destination (Join-Path $outputRoot 'WebView2Loader.dll') -Force
    Copy-Item -LiteralPath (Join-Path $extractionRoot 'LICENSE.txt') -Destination (Join-Path $outputRoot 'LICENSE.txt') -Force
    Copy-Item -LiteralPath (Join-Path $extractionRoot 'NOTICE.txt') -Destination (Join-Path $outputRoot 'NOTICE.txt') -Force
    $configuration = '<?xml version="1.0" encoding="utf-8"?><configuration><startup><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.6.2"/></startup></configuration>'
    [IO.File]::WriteAllText($hostExe + '.config', $configuration, [Text.UTF8Encoding]::new($false))
    Write-Output "Built $hostExe (WebView2 SDK $sdkVersion, SHA256 verified)."
}
finally {
    if (Test-Path -LiteralPath $extractionRoot) {
        $resolvedCache = [IO.Path]::GetFullPath($cacheRoot).TrimEnd('\') + '\'
        $resolvedExtraction = [IO.Path]::GetFullPath($extractionRoot)
        if (-not $resolvedExtraction.StartsWith($resolvedCache, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing cleanup outside the SDK cache.'
        }
        Remove-Item -LiteralPath $resolvedExtraction -Recurse -Force
    }
}
