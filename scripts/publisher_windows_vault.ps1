#requires -Version 7.2
<# Offline publisher only. Formal private bytes never enter client or CI.
   A CSPRNG passphrase is protected by Windows DPAPI CurrentUser and a restricted
   user/SYSTEM DACL. It is passed only in a short-lived child environment.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Initialize','Publish')][string]$Operation,
    [Parameter(Mandatory)][string]$VaultDirectory,
    [Parameter(Mandatory)][string]$KeyId,
    [string]$Python = '',
    [switch]$ConfirmInitialize,
    [string]$ReleaseDirectory,
    [string]$OutputDirectory,
    [string]$AcceptanceReceipt,
    [string]$PackageUrl,
    [string]$PublishedAt,
    [string]$ReleasePage,
    [string]$ReleaseNotesFile,
    [string]$MinimumAppVersion = '2.0.1'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Publisher vault requires Windows DPAPI CurrentUser.' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
$vault = [IO.Path]::GetFullPath($VaultDirectory)
$passwordFile = Join-Path $vault 'password.current-user.dpapi'
$keyFile = Join-Path $vault 'keys\private-key.encrypted.pem'
$publicFile = Join-Path $vault 'keys\trusted-public-keys.json'
$scope = [Security.Cryptography.DataProtectionScope]::CurrentUser
$entropy = [Text.Encoding]::UTF8.GetBytes('MediaFlow.PublisherVault.v1')
$envName = 'MEDIAFLOW_PROTECTED_PUBLISHER_PASSWORD'
$prior = [Environment]::GetEnvironmentVariable($envName)
$plain = $null
function Assert-VaultAcl([string]$Path) {
    $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
    foreach ($entry in @((Get-Item -LiteralPath $Path -Force)) + @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Vault must not contain reparse points.' }
        $acl = [IO.FileSystemAclExtensions]::GetAccessControl($entry, [Security.AccessControl.AccessControlSections]::Access)
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.IdentityReference.Value -notin $allowed -or $rule.AccessControlType -ne 'Allow') {
                throw 'Vault access exceeds current user and SYSTEM; refusing to load secrets.'
            }
        }
    }
}
try {
    if ($Operation -eq 'Initialize') {
        if (-not $ConfirmInitialize) { throw 'Explicit -ConfirmInitialize is required; no automatic key replacement.' }
        if (Test-Path -LiteralPath $vault) { throw 'Vault already exists; preserving it without rekeying.' }
        $random = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
        $secret = [Convert]::ToBase64String($random)
        [Environment]::SetEnvironmentVariable($envName,$secret)
        $plain = [Text.Encoding]::UTF8.GetBytes($secret)
        $protected = [Security.Cryptography.ProtectedData]::Protect($plain,$entropy,$scope)
        & (Join-Path $PSScriptRoot 'initialize_release_key.ps1') -ExternalDirectory $vault `
            -KeyId $KeyId -PasswordEnvironment $envName -Python $Python -ConfirmInitialize
        if ($LASTEXITCODE -ne 0) { throw 'Encrypted key initialization did not complete.' }
        $stream = [IO.File]::Open($passwordFile,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $stream.Write($protected); $stream.Flush($true) } finally { $stream.Dispose() }
        Assert-VaultAcl $vault
        [Security.Cryptography.CryptographicOperations]::ZeroMemory($random)
        Write-Output '{"windows_current_user_protection":true,"initialized":true,"secret_logged":false}'
    } else {
        foreach ($required in @($ReleaseDirectory,$OutputDirectory,$AcceptanceReceipt,$PackageUrl,$PublishedAt,$ReleasePage,$ReleaseNotesFile)) {
            if (-not $required) { throw 'Publishing requires explicit accepted release, receipt and actual hosted asset URLs.' }
        }
        Assert-VaultAcl $vault
        $plain = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($passwordFile),$entropy,$scope)
        [Environment]::SetEnvironmentVariable($envName,[Text.Encoding]::UTF8.GetString($plain))
        $notes = [IO.File]::ReadAllText([IO.Path]::GetFullPath($ReleaseNotesFile),[Text.Encoding]::UTF8)
        & $Python -X utf8 -B (Join-Path $PSScriptRoot 'publish_release.py') publish `
            --release-dir $ReleaseDirectory --output-dir $OutputDirectory --package-url $PackageUrl `
            --published-at $PublishedAt --key-id $KeyId --trusted-keys-file $publicFile `
            --acceptance-receipt $AcceptanceReceipt --private-key-file $keyFile --password-env $envName `
            --notes $notes --release-page $ReleasePage --minimum-app-version $MinimumAppVersion
        if ($LASTEXITCODE -ne 0) { throw 'Signed publication failed; old application and external key were preserved.' }
    }
} finally {
    [Environment]::SetEnvironmentVariable($envName,$prior)
    if ($null -ne $plain) { [Security.Cryptography.CryptographicOperations]::ZeroMemory($plain) }
    $secret = $null
    $prior = $null
}
