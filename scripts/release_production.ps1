#requires -Version 7.2
<#
Explicit, local production preparation. This script never uploads or initializes
keys. Pending channels fail before tests/build/output creation. Reusing a build
still checks its real EXE and binds every supplied acceptance report to its hash.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExpectedVersion,
    [Parameter(Mandatory)][string]$ChannelConfiguration,
    [Parameter(Mandatory)][string]$TrustedKeysFile,
    [Parameter(Mandatory)][string]$KeyId,
    [Parameter(Mandatory)][string]$PackageUrl,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [Parameter(Mandatory)][string]$VerificationDirectory,
    [Parameter(Mandatory)][string]$PasswordEnvironment,
    [string]$PrivateKeyFile = '',
    [string]$PrivateKeyEnvironment = '',
    [string]$Python = '',
    [string]$BuildDirectory = '',
    [string]$ExistingReleaseDirectory = '',
    [string[]]$AcceptanceReports = @(),
    [string]$PublishedAt = ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')),
    [string]$ReleaseNotesFile = '',
    [string]$ReleasePage = '',
    [string]$MinimumAppVersion = '2.0.0'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Resolve-ProjectPath([string]$Value) {
    if ([IO.Path]::IsPathFullyQualified($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Value))
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments, [string]$Stage,
          [int]$TimeoutSeconds = 600, [string]$LogPrefix = '', [switch]$SigningSecrets)
    $start = [Diagnostics.ProcessStartInfo]::new($Program)
    $start.WorkingDirectory = $projectRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.Encoding]::UTF8
    $start.StandardErrorEncoding = [Text.Encoding]::UTF8
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $start.Environment['PYTHONIOENCODING'] = 'utf-8'
    $start.Environment['PYTHONDONTWRITEBYTECODE'] = '1'
    if (-not $SigningSecrets) {
        foreach ($secretName in @($PrivateKeyEnvironment, $PasswordEnvironment)) {
            if ($secretName) { $start.Environment.Remove($secretName) | Out-Null }
        }
    }
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $start
    $stdout = $stderr = ''
    $started = $false
    try {
        if (-not $child.Start()) { throw "阶段启动失败：$Stage" }
        $started = $true
        $outTask = $child.StandardOutput.ReadToEndAsync()
        $errTask = $child.StandardError.ReadToEndAsync()
        if (-not $child.WaitForExit($TimeoutSeconds * 1000)) {
            $child.Kill($true)
            $child.WaitForExit(10000) | Out-Null
            throw "阶段超过有限等待时间，已停止本次进程树：$Stage"
        }
        if (-not $outTask.Wait(10000) -or -not $errTask.Wait(10000)) {
            throw "阶段输出管道未按时关闭：$Stage"
        }
        $stdout = $outTask.GetAwaiter().GetResult()
        $stderr = $errTask.GetAwaiter().GetResult()
        if ($LogPrefix) {
            [IO.File]::WriteAllText($LogPrefix + '.stdout.txt', $stdout, [Text.UTF8Encoding]::new($false))
            [IO.File]::WriteAllText($LogPrefix + '.stderr.txt', $stderr, [Text.UTF8Encoding]::new($false))
        }
        if ($child.ExitCode -ne 0) {
            # The fixed publisher/gate commands emit sanitized errors. Never
            # print command arguments, inherited environment or private input.
            $reason = $stderr.Trim()
            if ($reason.Length -gt 700) { $reason = $reason.Substring($reason.Length - 700) }
            throw "阶段失败：$Stage；退出码 $($child.ExitCode)。$reason"
        }
        return $stdout
    }
    finally {
        if ($started -and -not $child.HasExited) {
            $child.Kill($true)
            $child.WaitForExit(10000) | Out-Null
        }
        $child.Dispose()
    }
}

if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$Python = Resolve-ProjectPath $Python
$channel = Resolve-ProjectPath $ChannelConfiguration
$keys = Resolve-ProjectPath $TrustedKeysFile
$output = Resolve-ProjectPath $OutputDirectory
$verification = Resolve-ProjectPath $VerificationDirectory
if ($ExistingReleaseDirectory -and $BuildDirectory) { throw '现存已验收目录与新构建目录只能选择一个。' }
if ($ExistingReleaseDirectory) {
    $release = Resolve-ProjectPath $ExistingReleaseDirectory
} else {
    if (-not $BuildDirectory) { $BuildDirectory = 'dist-mediaflow-production-' + $ExpectedVersion }
    $release = Resolve-ProjectPath $BuildDirectory
}
$reports = @($AcceptanceReports | ForEach-Object { Resolve-ProjectPath $_ })
$publisher = Join-Path $projectRoot 'scripts\publish_release.py'
$gates = Join-Path $projectRoot 'release-workflows\release_gates.py'
$preflight = @('-B', $gates, 'preflight', '--expected-version', $ExpectedVersion,
    '--channel-file', $channel, '--trusted-keys-file', $keys, '--key-id', $KeyId,
    '--package-url', $PackageUrl, '--release-dir', $release, '--output-dir', $output,
    '--verification-dir', $verification, '--password-env', $PasswordEnvironment)
if ($PrivateKeyFile) { $preflight += @('--private-key-file', (Resolve-ProjectPath $PrivateKeyFile)) }
if ($PrivateKeyEnvironment) { $preflight += @('--private-key-env', $PrivateKeyEnvironment) }
if ($ExistingReleaseDirectory) { $preflight += '--existing' }
foreach ($report in $reports) { $preflight += @('--report', $report) }

$approved = Invoke-Checked -Program $Python -Arguments $preflight -Stage '发布配置与外部密钥预检' -TimeoutSeconds 45 -SigningSecrets
$null = New-Item -ItemType Directory -Path $verification
[IO.File]::WriteAllText((Join-Path $verification 'preflight.json'), $approved, [Text.UTF8Encoding]::new($false))
$stageName = '相关增量测试'
try {
    Write-Host '阶段：相关增量测试'
    $null = Invoke-Checked -Program $Python -Arguments @('-B',
        (Join-Path $projectRoot 'tests\run_update_tests.py'), '--report-dir', (Join-Path $verification 'unit')) `
        -Stage $stageName -TimeoutSeconds 1200 -LogPrefix (Join-Path $verification 'unit')
    if (-not $ExistingReleaseDirectory) {
        $stageName = '独立目录构建'
        Write-Host '阶段：独立目录构建'
        $shell = Join-Path $PSHOME 'pwsh.exe'
        # Related tests already passed; native acceptance below is mandatory.
        $null = Invoke-Checked -Program $shell -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive',
            '-File', (Join-Path $projectRoot 'build.ps1'), '-Python', $Python,
            '-OutputDirectory', $release, '-SkipTests') -Stage $stageName -TimeoutSeconds 2400 -LogPrefix (Join-Path $verification 'build')
    }
    $stageName = '实际 EXE 身份与固定渠道'
    Write-Host '阶段：实际 EXE 身份与固定渠道'
    $exe = Join-Path $release 'MediaFlow.exe'
    $identityFile = Join-Path $verification 'runtime-identity.json'
    $null = Invoke-Checked -Program $exe -Arguments @('--build-info', $identityFile) -Stage $stageName -TimeoutSeconds 90 -LogPrefix (Join-Path $verification 'identity')
    $null = Invoke-Checked -Program $Python -Arguments @('-B', $gates, 'runtime', '--identity-file',
        $identityFile, '--release-dir', $release, '--channel-file', $channel,
        '--expected-version', $ExpectedVersion) -Stage $stageName -TimeoutSeconds 45 -LogPrefix (Join-Path $verification 'identity-gate')
    if (-not $ExistingReleaseDirectory) {
        $stageName = '真实 EXE 本地下载和媒体库验收'
        Write-Host '阶段：真实 EXE 本地下载和媒体库验收'
        $nativeDirectory = Join-Path $verification 'native-acceptance'
        $null = Invoke-Checked -Program $exe -Arguments @('--tracks-check', $nativeDirectory,
            '--tracks-organization', '--native-smoke') -Stage $stageName -TimeoutSeconds 750 -LogPrefix (Join-Path $verification 'native')
        $null = Invoke-Checked -Program $exe -Arguments @('--tracks-check', $nativeDirectory,
            '--tracks-restart', '--native-smoke') -Stage '真实 EXE 重启验收' -TimeoutSeconds 240 -LogPrefix (Join-Path $verification 'restart')
        $reports = @((Join-Path $nativeDirectory 'result.json'), (Join-Path $nativeDirectory 'restart-result.json'))
    }
    $stageName = '验收收据绑定'
    $receipt = Join-Path $verification 'acceptance-receipt.json'
    $receiptArgs = @('-B', $publisher, 'receipt', '--release-dir', $release, '--output', $receipt)
    foreach ($report in $reports) { $receiptArgs += @('--report', $report) }
    $null = Invoke-Checked -Program $Python -Arguments $receiptArgs -Stage $stageName -TimeoutSeconds 180 -LogPrefix (Join-Path $verification 'receipt')
    $stageName = '独立加密私钥签名与自验'
    Write-Host '阶段：独立加密私钥签名与自验'
    $publishArgs = @('-B', $publisher, 'publish', '--release-dir', $release,
        '--output-dir', $output, '--package-url', $PackageUrl, '--published-at', $PublishedAt,
        '--key-id', $KeyId, '--trusted-keys-file', $keys, '--acceptance-receipt', $receipt,
        '--password-env', $PasswordEnvironment, '--minimum-app-version', $MinimumAppVersion)
    if ($PrivateKeyFile) { $publishArgs += @('--private-key-file', (Resolve-ProjectPath $PrivateKeyFile)) }
    if ($PrivateKeyEnvironment) { $publishArgs += @('--private-key-env', $PrivateKeyEnvironment) }
    if ($ReleasePage) { $publishArgs += @('--release-page', $ReleasePage) }
    if ($ReleaseNotesFile) {
        $notes = [IO.File]::ReadAllText((Resolve-ProjectPath $ReleaseNotesFile), [Text.Encoding]::UTF8)
        $publishArgs += @('--notes', $notes)
    }
    $signed = Invoke-Checked -Program $Python -Arguments $publishArgs -Stage $stageName -TimeoutSeconds 1800 -SigningSecrets -LogPrefix (Join-Path $verification 'publisher')
    $stageName = '最终发布资产再校验'
    $validated = Invoke-Checked -Program $Python -Arguments @('-B', $gates, 'published',
        '--output-dir', $output, '--trusted-keys-file', $keys, '--expected-version', $ExpectedVersion,
        '--package-url', $PackageUrl) -Stage $stageName -TimeoutSeconds 600 -LogPrefix (Join-Path $verification 'final-gate')
    $result = [ordered]@{schema_version=1; product='MediaFlow'; version=$ExpectedVersion;
        stage='local_signed_preparation_complete'; passed=$true; uploaded=$false;
        public_verification='pending'; output_directory=$output; release_directory=$release;
        acceptance_receipt=$receipt; signed_artifacts=($signed | ConvertFrom-Json);
        validation=($validated | ConvertFrom-Json)}
    $result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $verification 'production-preparation.json') -Encoding utf8NoBOM
    Write-Host '本地发布准备完成，尚未上传。真实公网验收待上传至已配置平台后执行 scripts/verify_hosted_release.py。'
    $result | ConvertTo-Json -Depth 12
}
catch {
    [ordered]@{schema_version=1; product='MediaFlow'; passed=$false; stage=$stageName;
        uploaded=$false; public_verification='pending'; old_versions_preserved=$true} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $verification 'production-preparation-failed.json') -Encoding utf8NoBOM
    throw
}
