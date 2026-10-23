#requires -Version 7.2
<#
Explicit one-time initialization only. The external vault receives a restricted
Windows ACL before the existing Ed25519 generator creates any private bytes.
Never run this on every build. No secret is accepted as a command-line value.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExternalDirectory,
    [Parameter(Mandatory)][string]$KeyId,
    [Parameter(Mandatory)][string]$PasswordEnvironment,
    [string]$Python = '',
    [switch]$ConfirmInitialize
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
if (-not $ConfirmInitialize) { throw '密钥尚未初始化。只有明确传入 -ConfirmInitialize 才会创建新密钥。' }
if (-not $IsWindows) { throw '此初始化包装器要求 Windows ACL；其他系统请采用受保护目录并使用现有显式 init-key。' }
if ($PasswordEnvironment -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') { throw '密码环境变量名无效。' }
$passphrase = [Environment]::GetEnvironmentVariable($PasswordEnvironment)
if (-not $passphrase -or $passphrase.Length -lt 20) {
    throw '请通过指定环境变量提供至少 20 字符的独立强密码；不要将密码写入参数、脚本或日志。'
}
$passphrase = $null
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$gates = Join-Path $projectRoot 'release-workflows\release_gates.py'
$approvalText = & $Python -X utf8 -B $gates key-destination --directory $ExternalDirectory
if ($LASTEXITCODE -ne 0) { throw '外部新密钥目录预检未通过，未创建密钥。' }
$approval = $approvalText | ConvertFrom-Json
$vault = $approval.vault

function Assert-PrivateAcl([string]$Path, [string[]]$AllowedSids) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '密钥路径包含重解析点。' }
    # Only read the discretionary ACL, never the privileged audit/SACL section.
    $section = [Security.AccessControl.AccessControlSections]::Access
    if ($item.PSIsContainer) {
        $security = [IO.FileSystemAclExtensions]::GetAccessControl([IO.DirectoryInfo]::new($Path), $section)
    } else {
        $security = [IO.FileSystemAclExtensions]::GetAccessControl([IO.FileInfo]::new($Path), $section)
    }
    $rules = $security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin $AllowedSids -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw ('密钥目录包含未允许的访问规则：' + $rule.IdentityReference.Value +
                ' (' + $rule.AccessControlType + ')；未将其认定为安全初始化。')
        }
    }
}

function Set-PrivateAcl([string]$Path, [Security.Principal.SecurityIdentifier]$OwnerSid,
                        [string[]]$AllowedSids) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) { $security = [Security.AccessControl.DirectorySecurity]::new() }
    else { $security = [Security.AccessControl.FileSecurity]::new() }
    # Newly created paths already belong to the publisher. Change only DACL.
    $security.SetAccessRuleProtection($true, $false)
    foreach ($identifier in $AllowedSids) {
        $sid = [Security.Principal.SecurityIdentifier]::new($identifier)
        if ($item.PSIsContainer) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                 [Security.AccessControl.InheritanceFlags]::ObjectInherit),
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow)
        } else {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow)
        }
        $security.AddAccessRule($rule)
    }
    if ($item.PSIsContainer) {
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($Path), $security)
    } else {
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($Path), $security)
    }
}

$null = New-Item -ItemType Directory -Path $vault
try {
    $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $allowed = @($ownerSid.Value, $systemSid.Value) | Select-Object -Unique
    Set-PrivateAcl $vault $ownerSid $allowed
    Assert-PrivateAcl $vault $allowed
    # The generator's temporary directory inherits the restricted vault ACL.
    $keyDirectory = Join-Path $vault 'keys'
    $resultText = & $Python -X utf8 -B (Join-Path $projectRoot 'scripts\publish_release.py') init-key `
        --directory $keyDirectory --key-id $KeyId --password-env $PasswordEnvironment
    if ($LASTEXITCODE -ne 0) { throw '加密密钥生成未完成；外部保险目录保留，未自动重试或更换密钥。' }
    Assert-PrivateAcl $vault $allowed
    foreach ($item in (Get-ChildItem -LiteralPath $vault -Recurse -Force)) {
        # Python's Windows chmod may add OWNER RIGHTS. Replace child DACLs
        # explicitly with the same approved user/SYSTEM rules before success.
        Set-PrivateAcl $item.FullName $ownerSid $allowed
        Assert-PrivateAcl $item.FullName $allowed
    }
    $result = $resultText | ConvertFrom-Json
    $result | Add-Member -NotePropertyName acl_restricted -NotePropertyValue $true
    $result | Add-Member -NotePropertyName production_trust_configured -NotePropertyValue $false
    $result | ConvertTo-Json -Depth 6
    Write-Host '已创建外部加密密钥。尚未固定客户端信任根或配置托管；请离线核对公钥指纹并备份保险目录。'
}
catch {
    throw ('密钥初始化未完成；外部保险目录及已生成的加密文件保留，勿覆盖或重新生成。原因：' + $_.Exception.Message)
}
