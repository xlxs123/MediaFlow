[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
    [Parameter(Mandatory=$true)][string]$Report,
    [ValidateSet('CI','Test','Production')][string]$Mode='Production',
    [string]$CertificateThumbprint='',
    [ValidateSet('CurrentUser','LocalMachine')][string]$CertificateStore='CurrentUser',
    [string]$SignToolPath=''
)
if($Mode -eq 'CI'){$operation='Inspect'}else{$operation='Verify'}
$forward=@{}+$PSBoundParameters;$forward['Mode']=$Mode
& (Join-Path $PSScriptRoot 'authenticode.ps1') -Operation $operation @forward
exit $LASTEXITCODE
