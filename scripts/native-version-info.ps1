# Shared compile-time metadata; never patch a previously signed PE.
function Write-MediaFlowNativeVersion([string]$ProjectRoot,[string]$Path){
    $branding=Get-Content -LiteralPath (Join-Path $ProjectRoot 'utils/branding.py') -Raw
    $match=[regex]::Match($branding,"(?m)^APP_VERSION = '(\d+\.\d+\.\d+)'\r?$")
    if(-not $match.Success){throw 'A release APP_VERSION is required for native PE metadata.'}
    $version=$match.Groups[1].Value
    foreach($part in $version.Split('.')){if([int]$part -gt 65535){throw 'Native PE version exceeds UInt16.'}}
    $text=@"
using System.Reflection;
[assembly: AssemblyProduct("MediaFlow")]
[assembly: AssemblyCompany("MediaFlow contributors")]
[assembly: AssemblyVersion("$version.0")]
[assembly: AssemblyFileVersion("$version.0")]
[assembly: AssemblyInformationalVersion("$version")]
"@
    [IO.File]::WriteAllText($Path,$text,[Text.UTF8Encoding]::new($false))
}
