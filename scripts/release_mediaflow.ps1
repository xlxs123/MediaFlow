#requires -Version 7.2
<# One explicit publisher entry. DryRun performs no GitHub/source-tag writes.
   Production signing remains in the existing external DPAPI vault wrapper.
#>
[CmdletBinding(DefaultParameterSetName='DryRun')]
param(
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory,ParameterSetName='DryRun')][switch]$DryRun,
    [Parameter(Mandatory,ParameterSetName='Publish')][switch]$Publish,
    [Parameter(Mandatory,ParameterSetName='TestRelease')][switch]$TestRelease,
    [Parameter(Mandatory,ParameterSetName='TestRelease')][string]$TestRepository,
    [Parameter(Mandatory,ParameterSetName='TestRelease')][string]$TestTag,
    [Parameter(Mandatory,ParameterSetName='TestRelease')][string]$SignedArtifactsDirectory,
    [Parameter(ParameterSetName='TestRelease')][switch]$TestDraftOnly,
    [Parameter(ParameterSetName='TestRelease')][switch]$TestCreateOnly,
    [string]$Python = '',
    [string]$Gh = '',
    [string]$VaultDirectory = '',
    [string]$KeyId = '',
    [string]$RunDirectory = '',
    [switch]$Resume,
    [string]$ExistingReleaseDirectory = '',
    [string[]]$AcceptanceReports = @(),
    [string]$OldReleaseDirectory = '',
    [string]$CiRunId = '',
    [ValidateRange(1,3600)][int]$CiWaitSeconds = 1200,
    [string]$ReleaseNotesFile = '',
    [string[]]$AdditionalAssets = @(),
    [string]$MinimumAppVersion = '2.0.1',
    [string]$CertificateThumbprint = '',
    [ValidateSet('CurrentUser','LocalMachine')][string]$CertificateStore = 'CurrentUser',
    [string]$TimestampUrl = '',
    [string]$SignToolPath = '',
    [string]$SecurityPython = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'MediaFlow publishing requires Windows.' }
if (-not $DryRun -and -not $Publish -and -not $TestRelease) { throw 'Choose exactly one of -DryRun, -Publish or -TestRelease.' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python environment is unavailable; specify -Python.' }
if ($TestRelease) {
    if ($Resume -or $VaultDirectory -or $KeyId -or $ExistingReleaseDirectory -or $AcceptanceReports.Count -or $AdditionalAssets.Count -or $CertificateThumbprint -or $TimestampUrl -or $SignToolPath) {
        throw 'TestRelease mirrors existing signed content only. Resume a remote draft with the same TestTag and a new RunDirectory.'
    }
    $testArguments = @('-X','utf8','-B',(Join-Path $PSScriptRoot 'release_test_transaction.py'),'publish',
        '--version',$Version,'--repository',$TestRepository,'--tag',$TestTag,'--artifacts',$SignedArtifactsDirectory)
    foreach ($pair in @(@('--gh',$Gh),@('--run-directory',$RunDirectory),@('--notes',$ReleaseNotesFile))) {
        if ($pair[1]) { $testArguments += @($pair[0],$pair[1]) }
    }
    if ($TestDraftOnly) { $testArguments += '--draft-only' }
    if ($TestCreateOnly) { $testArguments += '--create-only' }
    & $Python @testArguments
    if ($LASTEXITCODE -ne 0) { throw 'Isolated test publication stopped; existing releases and assets are preserved.' }
    return
}
$arguments = @('-X','utf8','-B',(Join-Path $PSScriptRoot 'release_pipeline.py'),'run',
    '--version',$Version,'--mode',$(if ($DryRun) {'dry-run'} else {'publish'}),
    '--pwsh',(Join-Path $PSHOME 'pwsh.exe'),'--ci-wait-seconds',[string]$CiWaitSeconds,
    '--minimum-app-version',$MinimumAppVersion,'--certificate-store',$CertificateStore)
foreach ($pair in @(
    @('--gh',$Gh), @('--vault-directory',$VaultDirectory), @('--key-id',$KeyId),
    @('--run-directory',$RunDirectory), @('--existing-release-dir',$ExistingReleaseDirectory),
    @('--old-release-dir',$OldReleaseDirectory), @('--ci-run-id',$CiRunId),
    @('--release-notes-file',$ReleaseNotesFile), @('--certificate-thumbprint',$CertificateThumbprint),
    @('--timestamp-url',$TimestampUrl), @('--sign-tool-path',$SignToolPath), @('--security-python',$SecurityPython))) {
    if ($pair[1]) { $arguments += @($pair[0],$pair[1]) }
}
foreach ($report in $AcceptanceReports) { $arguments += @('--acceptance-report',$report) }
foreach ($asset in $AdditionalAssets) { $arguments += @('--additional-asset',$asset) }
if ($Resume) { $arguments += '--resume' }
# Never print environment, command arguments, key data or an authentication token.
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Release pipeline did not complete; inspect its stage report. Prior versions and assets are preserved.' }
