[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$Report,
    [ValidateSet('Test','Production')][string]$Mode='Production',
    [string]$CertificateThumbprint='',
    [ValidateSet('CurrentUser','LocalMachine')][string]$CertificateStore='CurrentUser',
    [string]$TimestampUrl='',
    [string]$SignToolPath=''
)
$forward=@{}+$PSBoundParameters;$forward['Mode']=$Mode
& (Join-Path $PSScriptRoot 'authenticode.ps1') -Operation Sign @forward
exit $LASTEXITCODE
