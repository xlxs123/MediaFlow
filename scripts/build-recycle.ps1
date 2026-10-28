param([string]$OutputDirectory='')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-version-info.ps1')
$cscPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $cscPath -PathType Leaf)) { throw 'Existing .NET Framework compiler is required.' }
if(-not $OutputDirectory){$OutputDirectory=Join-Path $projectRoot 'tools'}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$target = Join-Path $OutputDirectory 'MediaFlow.Recycle.exe'
$versionFile=Join-Path ([IO.Path]::GetTempPath()) ('MediaFlow-version-'+[Guid]::NewGuid().ToString('N')+'.cs')
try{
Write-MediaFlowNativeVersion $projectRoot $versionFile
& $cscPath /nologo /target:exe /platform:x64 /optimize+ /utf8output /langversion:5 `
    "/out:$target" /reference:System.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll `
    (Join-Path $projectRoot 'native\recycle\Recycle.cs') $versionFile
if ($LASTEXITCODE -ne 0) { throw 'Recycle-only helper compilation failed.' }
}finally{if(Test-Path -LiteralPath $versionFile){Remove-Item -LiteralPath $versionFile -ErrorAction Stop}}
Write-Output "Built $target"
