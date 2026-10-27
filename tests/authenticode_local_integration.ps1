# Opt-in local SDK integration. Never add a test certificate to a trust root.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$WorkRoot,
    [Parameter(Mandatory=$true)][string]$WorkDirectory,
    [Parameter(Mandatory=$true)][string]$SourceDirectory,
    [Parameter(Mandatory=$true)][string]$SignToolPath
)
$ErrorActionPreference='Stop'
if(-not $IsWindows){throw 'This integration requires Windows.'}
function Assert-LocalPlain([string]$Path,[bool]$MustExist=$true){
    if(-not[IO.Path]::IsPathFullyQualified($Path) -or $Path.StartsWith('\\') -or $Path -match '(^|[\\/])\.\.?([\\/]|$)' -or $Path.Substring(2).Contains(':')){throw 'Integration paths must be canonical local paths.'}
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\');$current=$full
    if($MustExist -and -not(Test-Path -LiteralPath $full)){throw 'A required integration input is absent.'}
    while($current){
        if(Test-Path -LiteralPath $current){$item=Get-Item -LiteralPath $current -Force;if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Integration refuses linked paths or ancestors.'}}
        $parent=[IO.Path]::GetDirectoryName($current);if(-not $parent -or $parent -eq $current){break};$current=$parent
    }
    return $full
}
$allowed=Assert-LocalPlain $WorkRoot
if(-not(Test-Path -LiteralPath $allowed -PathType Container)){throw 'WorkRoot must be an existing plain directory.'}
$work=Assert-LocalPlain $WorkDirectory $false
$sourceRoot=Assert-LocalPlain $SourceDirectory
if(-not $work.StartsWith($allowed+'\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $work)){throw 'Integration must create a fresh child below its explicitly selected WorkRoot.'}
if($sourceRoot.Equals($allowed,[StringComparison]::OrdinalIgnoreCase) -or $sourceRoot.StartsWith($allowed+'\',[StringComparison]::OrdinalIgnoreCase) -or $allowed.StartsWith($sourceRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'The test work root and original source must not contain each other.'}
New-Item -ItemType Directory -Path $work -ErrorAction Stop|Out-Null
$script=Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\authenticode.ps1'
$inputTree=Join-Path $work 'input'
New-Item -ItemType Directory -Path $inputTree|Out-Null
$paths=@('MediaFlow.exe','tools/MediaFlow.Recycle.exe','tools/webview2/MediaFlow.WebView2Host.exe')
$original=[Collections.Generic.List[object]]::new()
# Fixed individual files only. Never enumerate/copy a previously launched tree.
foreach($relative in $paths){
    $source=Assert-LocalPlain (Join-Path $sourceRoot $relative);$item=Get-Item -LiteralPath $source -Force
    if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Static PE input is not a plain file.'}
    $target=Join-Path $inputTree $relative;New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force|Out-Null
    $before=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
    if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $before){throw 'Static copy hash mismatch.'}
    $original.Add([pscustomobject]@{path=$relative;sha256=$before.ToLowerInvariant()})
}
# One authentic Microsoft SDK PE records the preserve-third-party behavior.
$third=Join-Path $inputTree 'tools\third-party-preserve-fixture.exe'
Copy-Item -LiteralPath $SignToolPath -Destination $third -ErrorAction Stop
$rootsBefore=@(Get-ChildItem Cert:\CurrentUser\Root,Cert:\LocalMachine\Root|ForEach-Object Thumbprint|Sort-Object)
$label='MediaFlow Authenticode TEST ONLY '+[Guid]::NewGuid().ToString('N')
$certificate=New-SelfSignedCertificate -Subject ('CN='+$label) -FriendlyName $label -Type CodeSigningCert -CertStoreLocation Cert:\CurrentUser\My -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -NotBefore (Get-Date).AddMinutes(-5) -NotAfter (Get-Date).AddDays(2)
$wrongCertificate=New-SelfSignedCertificate -Subject ('CN='+$label+' WRONG EKU') -FriendlyName ($label+' WRONG EKU') -Type Custom -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.2') -KeyUsage DigitalSignature -CertStoreLocation Cert:\CurrentUser\My -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -NotBefore (Get-Date).AddMinutes(-5) -NotAfter (Get-Date).AddDays(2)
$expiredCertificate=New-SelfSignedCertificate -Subject ('CN='+$label+' EXPIRED') -FriendlyName ($label+' EXPIRED') -Type CodeSigningCert -CertStoreLocation Cert:\CurrentUser\My -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -NotBefore (Get-Date).AddDays(-3) -NotAfter (Get-Date).AddDays(-1)
# Persist only public certificate bytes for a real HasPrivateKey=false case.
# The ephemeral RSA key is never exported, persisted or used to sign an EXE.
$rsa=[Security.Cryptography.RSA]::Create(2048)
try{
    $request=[Security.Cryptography.X509Certificates.CertificateRequest]::new(('CN='+$label+' NO PRIVATE KEY'),$rsa,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $usages=[Security.Cryptography.OidCollection]::new();$null=$usages.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usages,$true))
    $ephemeral=$request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5),[DateTimeOffset]::UtcNow.AddDays(2))
    try{$publicOnly=[Security.Cryptography.X509Certificates.X509Certificate2]::new($ephemeral.RawData)}finally{$ephemeral.Dispose()}
    try{
        $store=[Security.Cryptography.X509Certificates.X509Store]::new('My','CurrentUser')
        try{$store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite);$store.Add($publicOnly)}finally{$store.Dispose()}
        $publicThumbprint=$publicOnly.Thumbprint
    }finally{$publicOnly.Dispose()}
}finally{$rsa.Dispose()}
if((Get-Item -LiteralPath ('Cert:\CurrentUser\My\'+$publicThumbprint)).HasPrivateKey){throw 'The public-only fixture unexpectedly has a private key.'}
$private=[Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($certificate)
try{if($private.Key.ExportPolicy -ne [Security.Cryptography.CngExportPolicies]::None){throw 'Test key must remain non-exportable.'}}finally{$private.Dispose()}
$certificateInfo=[ordered]@{test_only=$true;formal_certificate=$false;test_thumbprint=$certificate.Thumbprint;wrong_eku_thumbprint=$wrongCertificate.Thumbprint;expired_thumbprint=$expiredCertificate.Thumbprint;public_only_thumbprint=$publicThumbprint;public_only_has_private_key=$false;store='CurrentUser/My';non_exportable=$true;trusted_root_added=$false;private_key_exported=$false;expires_utc=$certificate.NotAfter.ToUniversalTime().ToString('o')}
$certificateInfo|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $work 'test-certificate-metadata.json') -Encoding utf8
$rows=[Collections.Generic.List[object]]::new()
$script:integrationStage='test-cases'
function Run-Case([string]$Name,[string[]]$Arguments,[bool]$ExpectedPass,[string]$ExpectedCode=''){
    $script:integrationStage=$Name
    $outputIndex=[Array]::IndexOf($Arguments,'-OutputDirectory')
    $newRejectedOutput=if(-not $ExpectedPass -and $outputIndex -ge 0 -and -not(Test-Path -LiteralPath $Arguments[$outputIndex+1])){$Arguments[$outputIndex+1]}else{$null}
    $report=Join-Path $work ($Name+'.json')
    $start=[Diagnostics.ProcessStartInfo]::new();$start.FileName=(Join-Path $PSHOME 'pwsh.exe');$start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    foreach($arg in @('-NoLogo','-NoProfile','-File',$script,'-Report',$report,'-SignToolPath',$SignToolPath)+$Arguments){$start.ArgumentList.Add($arg)}
    $proc=[Diagnostics.Process]::new();$proc.StartInfo=$start
    try{
        if(-not $proc.Start()){throw 'Unable to start integration operation.'}
        $out=$proc.StandardOutput.ReadToEndAsync();$err=$proc.StandardError.ReadToEndAsync()
        if(-not $proc.WaitForExit(600000)){$proc.Kill($true);throw 'Integration operation exceeded its bound.'}
        $out.GetAwaiter().GetResult()|Set-Content -LiteralPath (Join-Path $work ($Name+'.stdout.log')) -Encoding utf8
        $err.GetAwaiter().GetResult()|Set-Content -LiteralPath (Join-Path $work ($Name+'.stderr.log')) -Encoding utf8
        $value=Get-Content -LiteralPath $report -Raw|ConvertFrom-Json
        $good=($value.passed -eq $ExpectedPass -and (($proc.ExitCode -eq 0) -eq $ExpectedPass) -and -not $value.formal_verified)
        if($ExpectedCode -and $ExpectedCode -notin @($value.errors.code)){$good=$false}
        if($newRejectedOutput -and (Test-Path -LiteralPath $newRejectedOutput)){$good=$false}
        $rows.Add([pscustomobject]@{case=$Name;passed=$good;expected_success=$ExpectedPass;exit_code=$proc.ExitCode;reported_passed=$value.passed;formal_verified=$value.formal_verified;errors=@($value.errors.code);rejected_output_absent=$(if($newRejectedOutput){-not(Test-Path -LiteralPath $newRejectedOutput)}else{$null})})
        [pscustomobject]@{case=$Name;passed=$good;errors=@($value.errors.code)}|ConvertTo-Json -Compress|Write-Output
        if(-not $good){throw ('Integration assertion failed: '+$Name)}
        return $value
    }finally{$proc.Dispose()}
}
try{
    $null=Run-Case 'ci-inspect-unsigned' @('-Operation','Inspect','-Mode','CI','-ReleaseDirectory',$inputTree) $true
    $signed=Join-Path $work 'signed-test-only'
    $null=Run-Case 'test-sign' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$inputTree,'-OutputDirectory',$signed,'-CertificateThumbprint',$certificate.Thumbprint) $true
    $null=Run-Case 'test-verify' @('-Operation','Verify','-Mode','Test','-ReleaseDirectory',$signed,'-CertificateThumbprint',$certificate.Thumbprint) $true
    $null=Run-Case 'existing-output-preserved' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$inputTree,'-OutputDirectory',$signed,'-CertificateThumbprint',$certificate.Thumbprint) $false 'output_conflict'
    $null=Run-Case 'existing-signature-preserved' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$signed,'-OutputDirectory',(Join-Path $work 'must-not-resign'),'-CertificateThumbprint',$certificate.Thumbprint) $false 'existing_first_party_signature'
    $null=Run-Case 'wrong-eku' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$inputTree,'-OutputDirectory',(Join-Path $work 'wrong-eku-output'),'-CertificateThumbprint',$wrongCertificate.Thumbprint) $false 'wrong_eku'
    $null=Run-Case 'wrong-thumbprint' @('-Operation','Verify','-Mode','Test','-ReleaseDirectory',$signed,'-CertificateThumbprint',('A'*40)) $false 'certificate_not_found'
    $null=Run-Case 'wrong-existing-signer' @('-Operation','Verify','-Mode','Test','-ReleaseDirectory',$signed,'-CertificateThumbprint',$publicThumbprint) $false 'signer_mismatch'
    $null=Run-Case 'expired-test-certificate' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$inputTree,'-OutputDirectory',(Join-Path $work 'expired-output'),'-CertificateThumbprint',$expiredCertificate.Thumbprint) $false 'certificate_expired_or_not_yet_valid'
    $null=Run-Case 'expired-production-inspect' @('-Operation','Inspect','-Mode','Production','-CertificateThumbprint',$expiredCertificate.Thumbprint) $false 'certificate_expired_or_not_yet_valid'
    $null=Run-Case 'public-only-cannot-sign' @('-Operation','Sign','-Mode','Test','-ReleaseDirectory',$inputTree,'-OutputDirectory',(Join-Path $work 'public-only-output'),'-CertificateThumbprint',$publicThumbprint) $false 'private_key_unavailable'
    $null=Run-Case 'public-only-production-inspect' @('-Operation','Inspect','-Mode','Production','-CertificateThumbprint',$publicThumbprint) $false 'private_key_unavailable'
    $null=Run-Case 'production-unconfigured' @('-Operation','Sign','-Mode','Production','-ReleaseDirectory',$inputTree,'-OutputDirectory',(Join-Path $work 'production-unconfigured-output')) $false 'certificate_not_configured'
    $null=Run-Case 'production-reject-test' @('-Operation','Sign','-Mode','Production','-ReleaseDirectory',$inputTree,'-OutputDirectory',(Join-Path $work 'production-test-output'),'-CertificateThumbprint',$certificate.Thumbprint) $false 'untrusted_production_certificate'
    # Exercise actual final archive bytes without using the production publisher,
    # a production key, a trusted test root or a fabricated formal report.
    $script:integrationStage='final-package-create'
    $package=Join-Path $work 'signed-test-only.zip'
    $expectedFiles=@{}
    foreach($name in @($paths)+@('tools/third-party-preserve-fixture.exe')){
        $file=Get-Item -LiteralPath (Join-Path $signed $name)
        $expectedFiles[$name]=@{size=$file.Length;sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash}
    }
    [IO.Compression.ZipFile]::CreateFromDirectory($signed,$package,[IO.Compression.CompressionLevel]::NoCompression,$false)
    $packageSha=(Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash
    function Expand-TestPackage([string]$ArchivePath,[string]$Output){
        $script:integrationStage='final-package-digest'
        if((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash -ne $packageSha){throw 'Test package digest mismatch.'}
        if(Test-Path -LiteralPath $Output){throw 'Test package extraction must use a fresh directory.'}
        $zip=[IO.Compression.ZipFile]::OpenRead($ArchivePath)
        try{
            $script:integrationStage='final-package-inventory'
            $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            if($zip.Entries.Count -ne $expectedFiles.Count){throw 'Test package member count mismatch.'}
            foreach($entry in $zip.Entries){
                if(-not $expectedFiles.ContainsKey($entry.FullName) -or -not $seen.Add($entry.FullName) -or $entry.Length -ne $expectedFiles[$entry.FullName].size){throw 'Unexpected test archive member.'}
                $stream=$entry.Open()
                try{$hash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($stream))}finally{$stream.Dispose()}
                if($hash -ne $expectedFiles[$entry.FullName].sha256){throw 'Test package member digest mismatch.'}
            }
            $script:integrationStage='final-package-extraction'
            New-Item -ItemType Directory -Path $Output|Out-Null
            foreach($entry in $zip.Entries){
                # Exact fixed-name allowlist above; no arbitrary ZIP paths.
                $target=Join-Path $Output $entry.FullName
                New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force|Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$false)
                if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $expectedFiles[$entry.FullName].sha256){throw 'Extracted test file digest mismatch.'}
            }
        }finally{$zip.Dispose()}
        $script:integrationStage='final-package-stability'
        if((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash -ne $packageSha){throw 'Test package changed during verification.'}
    }
    $roundtrip=Join-Path $work 'zip-roundtrip-test-only'
    Expand-TestPackage $package $roundtrip
    $rows.Add([pscustomobject]@{case='final-package-roundtrip';passed=$true;formal_verified=$false;files_verified=$expectedFiles.Count;package_sha256=$packageSha.ToLowerInvariant()})
    $null=Run-Case 'final-package-test-verify' @('-Operation','Verify','-Mode','Test','-ReleaseDirectory',$roundtrip,'-CertificateThumbprint',$certificate.Thumbprint) $true
    $script:integrationStage='altered-final-package-fixture'
    $alteredPackage=Join-Path $work 'altered-test-only.zip';Copy-Item -LiteralPath $package -Destination $alteredPackage -ErrorAction Stop
    $zip=[IO.Compression.ZipFile]::Open($alteredPackage,[IO.Compression.ZipArchiveMode]::Update)
    try{
        $stream=$zip.GetEntry('tools/MediaFlow.Recycle.exe').Open()
        try{$stream.Position=512;$old=$stream.ReadByte();$stream.Position=512;$stream.WriteByte([byte]($old -bxor 1))}finally{$stream.Dispose()}
    }finally{$zip.Dispose()}
    $rejected=$false;$rejectedOutput=Join-Path $work 'must-not-extract-altered-zip'
    try{Expand-TestPackage $alteredPackage $rejectedOutput}catch{if($_.Exception.Message -ne 'Test package digest mismatch.'){throw};$rejected=$true}
    if(-not $rejected -or (Test-Path -LiteralPath $rejectedOutput)){throw 'Altered ZIP was not rejected before extraction.'}
    $rows.Add([pscustomobject]@{case='altered-final-package-rejected';passed=$true;formal_verified=$false;rejected_before_extraction=$true})
    # Alter a signed, disposable copy; do not modify the original or passing copy.
    $tampered=Join-Path $work 'tampered-test-only';New-Item -ItemType Directory -Path $tampered|Out-Null
    foreach($relative in $paths){$dest=Join-Path $tampered $relative;New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($dest)) -Force|Out-Null;Copy-Item -LiteralPath (Join-Path $signed $relative) -Destination $dest}
    $target=Join-Path $tampered 'tools\MediaFlow.Recycle.exe'
    $stream=[IO.File]::Open($target,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try{$stream.Position=512;$old=$stream.ReadByte();$stream.Position=512;$stream.WriteByte([byte]($old -bxor 1))}finally{$stream.Dispose()}
    $null=Run-Case 'tampered-signature' @('-Operation','Verify','-Mode','Test','-ReleaseDirectory',$tampered,'-CertificateThumbprint',$certificate.Thumbprint) $false 'test_verification_failed'
    foreach($record in $original){if((Get-FileHash -LiteralPath (Join-Path $SourceDirectory $record.path) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256){throw 'An original static source PE changed.'}}
    $rootStoreUnchanged=(@(Compare-Object $rootsBefore @(Get-ChildItem Cert:\CurrentUser\Root,Cert:\LocalMachine\Root|ForEach-Object Thumbprint|Sort-Object)).Count -eq 0)
    if(-not $rootStoreUnchanged){throw 'Trust roots changed during test.'}
    $summary=[ordered]@{passed=$true;scope='Actual local Windows SDK integration with explicitly untrusted, non-exportable TEST ONLY certificates; not formal Authenticode acceptance.';cases=@($rows.ToArray());original_static_pe_unchanged=$true;trust_roots_unchanged=$rootStoreUnchanged;formal_signed=$false;production_ready=$false;published=$false;formal_publisher_certificate='not_configured';private_key_exported=$false;test_certificates_retained_until_expiry=$true;final_package_test_only=$true;final_package_sha256=$packageSha.ToLowerInvariant();final_package_size=(Get-Item -LiteralPath $package).Length;final_package_reextracted_pe_verified=$paths.Count;timestamp_service_protocol_tested=$false}
}catch{
    $summary=[ordered]@{passed=$false;scope='Actual local integration; failed records retained.';cases=@($rows.ToArray());formal_signed=$false;production_ready=$false;published=$false;failed_stage=$script:integrationStage;error_type=$_.Exception.GetType().Name;failure='Local SDK integration assertion failed; per-case reports and the failed stage are retained in the selected work directory.'}
}
$summary|ConvertTo-Json -Depth 9|Set-Content -LiteralPath (Join-Path $work 'integration-results.json') -Encoding utf8
if(-not $summary.passed){throw 'Actual local Authenticode integration failed; reports are retained.'}
