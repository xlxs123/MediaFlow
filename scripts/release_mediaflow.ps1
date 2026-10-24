#requires -Version 7.2
<# One explicit publisher entry. DryRun performs no GitHub/source-tag writes.
   Production signing remains in the existing external DPAPI vault wrapper.
#>
[CmdletBinding(DefaultParameterSetName='DryRun')]
param(
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory,ParameterSetName='DryRun')][switch]$DryRun,
    [Parameter(Mandatory,ParameterSetName='Publish')][switch]$Publish,
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
    [string]$MinimumAppVersion = '2.0.1'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'MediaFlow publishing requires Windows.' }
if (-not $DryRun -and -not $Publish) { throw 'Choose exactly one of -DryRun or -Publish.' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python environment is unavailable; specify -Python.' }
$arguments = @('-X','utf8','-B',(Join-Path $PSScriptRoot 'release_pipeline.py'),'run',
    '--version',$Version,'--mode',$(if ($DryRun) {'dry-run'} else {'publish'}),
    '--pwsh',(Join-Path $PSHOME 'pwsh.exe'),'--ci-wait-seconds',[string]$CiWaitSeconds,
    '--minimum-app-version',$MinimumAppVersion)
foreach ($pair in @(
    @('--gh',$Gh), @('--vault-directory',$VaultDirectory), @('--key-id',$KeyId),
    @('--run-directory',$RunDirectory), @('--existing-release-dir',$ExistingReleaseDirectory),
    @('--old-release-dir',$OldReleaseDirectory), @('--ci-run-id',$CiRunId),
    @('--release-notes-file',$ReleaseNotesFile))) {
    if ($pair[1]) { $arguments += @($pair[0],$pair[1]) }
}
foreach ($report in $AcceptanceReports) { $arguments += @('--acceptance-report',$report) }
foreach ($asset in $AdditionalAssets) { $arguments += @('--additional-asset',$asset) }
if ($Resume) { $arguments += '--resume' }
# Never print environment, command arguments, key data or an authentication token.
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Release pipeline did not complete; inspect its stage report. Prior versions and assets are preserved.' }
