$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$cscPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $cscPath -PathType Leaf)) { throw 'Existing .NET Framework compiler is required.' }
$target = Join-Path $projectRoot 'tools\MediaFlow.Recycle.exe'
& $cscPath /nologo /target:exe /platform:x64 /optimize+ /utf8output /langversion:5 `
    "/out:$target" /reference:System.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll `
    (Join-Path $projectRoot 'native\recycle\Recycle.cs')
if ($LASTEXITCODE -ne 0) { throw 'Recycle-only helper compilation failed.' }
Write-Output "Built $target"
