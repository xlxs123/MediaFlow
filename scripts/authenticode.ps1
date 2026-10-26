# Requires PowerShell 7.4+ on Windows. Production keys never enter this script or CI.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Inspect','Sign','Verify')][string]$Operation,
    [string]$ReleaseDirectory='',
    [Parameter(Mandatory=$true)][string]$Report,
    [ValidateSet('CI','Test','Production')][string]$Mode='CI',
    [string]$OutputDirectory='',
    [string]$CertificateThumbprint='',
    [ValidateSet('CurrentUser','LocalMachine')][string]$CertificateStore='CurrentUser',
    [string]$TimestampUrl='',
    [string]$SignToolPath=''
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$script:stage='parameters'
$script:reportAllowed=$false
$script:inspectionRows=[Collections.Generic.List[object]]::new()
$script:currentFile=$null
$script:result=[ordered]@{
    schema_version=1;kind='mediaflow-authenticode';product='MediaFlow';operation=$Operation;mode=$Mode;
    passed=$false;formal_verified=$false;configuration_state='not_configured';
    production_ready=$false;configuration_only=$false;test_only=($Mode -eq 'Test');created_at=[DateTime]::UtcNow.ToString('o');
    tool=$null;certificate=$null;files=@();errors=@();source_unchanged=$false;
    static_files_verified=0;input_tree_sha256=$null;output_tree_sha256=$null;
    private_key_exported=$false;trust_store_modified=$false;
    scope='Authenticode only; no Ed25519 replacement, release publication, user-data migration or SmartScreen reputation claim.'
}
function Stop-Auth([string]$Code,[string]$Message){throw "[MF_AUTH:$Code]$Message"}
function Plain-Path([string]$Path,[bool]$Exists=$true){
    if(-not [IO.Path]::IsPathFullyQualified($Path) -or $Path.StartsWith('\\') -or $Path -match '[\x00-\x1f]' -or $Path -match '(^|[\\/])\.\.?([\\/]|$)' -or $Path.Substring(2).Contains(':')){Stop-Auth 'invalid_path' 'Only fully qualified, local, canonical paths are accepted.'}
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\')
    if($Exists -and -not(Test-Path -LiteralPath $full)){Stop-Auth 'missing_path' 'A required input does not exist.'}
    $current=$full
    while($current){
        if(Test-Path -LiteralPath $current){$item=Get-Item -LiteralPath $current -Force;if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){Stop-Auth 'linked_path' 'Reparse points and linked ancestors are not accepted.'}}
        $parent=[IO.Path]::GetDirectoryName($current);if(-not $parent -or $parent -eq $current){break};$current=$parent
    }
    return $full
}
function File-Digest([string]$Path){
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{$sha=[Security.Cryptography.SHA256]::Create();try{return [Convert]::ToHexString($sha.ComputeHash($stream)).ToLowerInvariant()}finally{$sha.Dispose()}}finally{$stream.Dispose()}
}
function Public-Digest([string]$Text){
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Text))).ToLowerInvariant()
}
function Static-Tree([string]$Root){
    $stack=[Collections.Generic.Stack[string]]::new();$stack.Push($Root);$items=[Collections.Generic.List[object]]::new()
    while($stack.Count){
        foreach($entry in Get-ChildItem -LiteralPath $stack.Pop() -Force){
            if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){Stop-Auth 'linked_member' 'The static release contains a linked member.'}
            if($entry.Name -match '^(BrowserProfiles?|MediaFlow-WebView2|EBWebView|UserData|User Data|Data|Cookies|Network|Login Data|vault|keys|\.git|\.venv|verification|downloads?|media.library)$'){Stop-Auth 'user_data_present' 'A non-static or protected entry was encountered; its contents were not inspected.'}
            if($entry.PSIsContainer){$stack.Push($entry.FullName);continue}
            if($entry.Extension -match '^\.(pfx|p12|pem|key|sqlite|sqlite3|db)$'){Stop-Auth 'private_material' 'Certificate containers, user databases and private material cannot enter a signing tree.'}
            $rel=$entry.FullName.Substring($Root.Length+1).Replace('\','/')
            if($rel -match '[\x00-\x1f]' -or $rel.StartsWith('-') -or $rel.Contains(':')){Stop-Auth 'unsafe_member' 'An invalid static member path was encountered.'}
            $before=[long]$entry.Length;$mtime=$entry.LastWriteTimeUtc.Ticks;$hash=File-Digest $entry.FullName;$after=Get-Item -LiteralPath $entry.FullName -Force
            if($after.Length -ne $before -or $after.LastWriteTimeUtc.Ticks -ne $mtime){Stop-Auth 'source_changed' 'A static input changed while being read.'}
            $items.Add([pscustomobject]@{path=$rel;bytes=$before;sha256=$hash})
            if($items.Count -gt 20000){Stop-Auth 'too_many_files' 'The static release exceeds the bounded file count.'}
        }
    }
    return @($items.ToArray() | Sort-Object path)
}
function Tree-Digest($Tree){return Public-Digest ($Tree|ConvertTo-Json -Depth 5 -Compress)}
function Discover-SignTool {
    $choices=[Collections.Generic.List[string]]::new()
    if($SignToolPath){$choices.Add((Plain-Path $SignToolPath))}else{
        $roots=[Collections.Generic.List[string]]::new()
        foreach($reg in @('HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots')){
            $value=Get-ItemProperty -LiteralPath $reg -Name KitsRoot10 -ErrorAction SilentlyContinue;if($value -and $value.KitsRoot10){$roots.Add($value.KitsRoot10)}
        }
        foreach($envRoot in @(${env:ProgramFiles(x86)},$env:ProgramFiles)){if($envRoot){$roots.Add((Join-Path $envRoot 'Windows Kits\10'))}}
        foreach($root in @($roots|Select-Object -Unique)){
            $bin=Join-Path $root 'bin';if(Test-Path -LiteralPath $bin){foreach($version in Get-ChildItem -LiteralPath $bin -Directory | Where-Object Name -Match '^10\.0\.\d+\.\d+$' | Sort-Object {[version]$_.Name} -Descending){$candidate=Join-Path $version.FullName 'x64\signtool.exe';if(Test-Path -LiteralPath $candidate){$choices.Add($candidate)}}}
        }
        $command=Get-Command signtool.exe -CommandType Application -ErrorAction SilentlyContinue;if($command){$choices.Add($command.Source)}
    }
    foreach($choice in $choices){
        $path=Plain-Path $choice;$item=Get-Item -LiteralPath $path -Force
        $sig=Get-AuthenticodeSignature -LiteralPath $path
        if($item.Name -ieq 'signtool.exe' -and $item.VersionInfo.OriginalFilename -ieq 'signtool.exe' -and $item.VersionInfo.CompanyName -eq 'Microsoft Corporation' -and $sig.Status.ToString() -eq 'Valid' -and $sig.SignerCertificate.Subject -match '(^|, )O=Microsoft Corporation(,|$)'){
            $script:tool=$path;$script:result.tool=[ordered]@{available=$true;name='signtool.exe';version=$item.VersionInfo.ProductVersion;sha256=(File-Digest $path);windows_signature='Valid';publisher='Microsoft Corporation'};return
        }
        if($SignToolPath){Stop-Auth 'untrusted_signtool' 'The explicitly selected executable is not a Windows-trusted Microsoft SDK SignTool.'}
    }
    $script:result.tool=@{available=$false;name='signtool.exe';version=$null}
    if($Operation -ne 'Inspect' -or $Mode -ne 'CI'){Stop-Auth 'signtool_missing' 'A verified Windows SDK SignTool is required for signing or verification.'}
}
function Native-Tool([string[]]$Arguments){
    $start=[Diagnostics.ProcessStartInfo]::new();$start.FileName=$script:tool;$start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    foreach($name in @($start.Environment.Keys)){if($name -match '^(GH_|GITHUB_)' -or $name -match '(PASSWORD|TOKEN|PRIVATE_?KEY|SECRET)'){$null=$start.Environment.Remove($name)}}
    foreach($arg in $Arguments){$start.ArgumentList.Add($arg)}
    $proc=[Diagnostics.Process]::new();$proc.StartInfo=$start
    try{
        if(-not $proc.Start()){Stop-Auth 'tool_start_failed' 'SignTool could not be started.'}
        $stdout=$proc.StandardOutput.ReadToEndAsync();$stderr=$proc.StandardError.ReadToEndAsync()
        if(-not $proc.WaitForExit(120000)){$proc.Kill($true);$proc.WaitForExit();Stop-Auth 'tool_timeout' 'SignTool exceeded its bounded execution time.'}
        $out=$stdout.GetAwaiter().GetResult();$err=$stderr.GetAwaiter().GetResult()
        return [pscustomobject]@{exit_code=$proc.ExitCode;stdout_sha256=(Public-Digest $out);stderr_sha256=(Public-Digest $err);diagnostic_codes=@([regex]::Matches($out+' '+$err,'(?i)0x[0-9a-f]{8}')|ForEach-Object {$_.Value.ToLowerInvariant()}|Sort-Object -Unique)}
    }finally{$proc.Dispose()}
}
function Certificate-Chain($Certificate,[bool]$Online){
    $chain=[Security.Cryptography.X509Certificates.X509Chain]::new()
    try{
        $chain.ChainPolicy.RevocationMode=if($Online){[Security.Cryptography.X509Certificates.X509RevocationMode]::Online}else{[Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck}
        $chain.ChainPolicy.RevocationFlag=[Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
        $chain.ChainPolicy.UrlRetrievalTimeout=[TimeSpan]::FromSeconds(15)
        $chain.ChainPolicy.VerificationFlags=[Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
        $valid=$chain.Build($Certificate)
        return [pscustomobject]@{valid=$valid;status=@($chain.ChainStatus|ForEach-Object {$_.Status.ToString()}|Sort-Object -Unique);revocation_checked=$Online}
    }finally{$chain.Dispose()}
}
function Certificate-Eku($Certificate,[string]$Oid){
    foreach($extension in $Certificate.Extensions){if($extension.Oid.Value -eq '2.5.29.37'){return $Oid -in @($extension.EnhancedKeyUsages|ForEach-Object Value)}};return $false
}
function Select-Certificate {
    if(-not $CertificateThumbprint){if($Mode -ne 'CI'){Stop-Auth 'certificate_not_configured' 'Authenticode 未配置：需要明确指定正式代码签名证书。'};return $null}
    if($CertificateThumbprint -notmatch '^[a-fA-F0-9]{40}$'){Stop-Auth 'invalid_thumbprint' 'The certificate thumbprint must contain exactly 40 hexadecimal characters.'}
    $thumb=$CertificateThumbprint.ToUpperInvariant();$cert=Get-Item -LiteralPath ('Cert:\'+$CertificateStore+'\My\'+$thumb) -ErrorAction SilentlyContinue
    if(-not $cert){Stop-Auth 'certificate_not_found' 'The explicitly selected certificate was not found; no other certificate will be substituted.'}
    $script:result.configuration_state='invalid_certificate'
    $chain=Certificate-Chain $cert ($Mode -eq 'Production')
    $script:result.certificate=[ordered]@{thumbprint=$cert.Thumbprint;subject=$cert.Subject;issuer=$cert.Issuer;not_before=$cert.NotBefore.ToUniversalTime().ToString('o');not_after=$cert.NotAfter.ToUniversalTime().ToString('o');has_private_key=$cert.HasPrivateKey;code_signing_eku=(Certificate-Eku $cert '1.3.6.1.5.5.7.3.3');chain=$chain;self_signed=($cert.Subject -eq $cert.Issuer);store=$CertificateStore}
    $now=Get-Date
    if($cert.NotBefore -gt $now -or $cert.NotAfter -le $now){Stop-Auth 'certificate_expired_or_not_yet_valid' 'The selected certificate is outside its validity period.'}
    if(-not $script:result.certificate.code_signing_eku){Stop-Auth 'wrong_eku' 'The certificate does not explicitly allow code signing.'}
    if(($Operation -eq 'Sign' -or ($Operation -eq 'Inspect' -and $Mode -eq 'Production')) -and -not $cert.HasPrivateKey){Stop-Auth 'private_key_unavailable' 'The selected certificate has no accessible signing key in its Windows store.'}
    if($Mode -eq 'Production'){
        if($cert.Subject -eq $cert.Issuer -or $cert.Subject -match '(?i)TEST ONLY|MediaFlow Authenticode TEST' -or -not $chain.valid){Stop-Auth 'untrusted_production_certificate' 'Production refuses self-signed, test, revoked, untrusted or unverifiable certificates.'}
        $script:result.configuration_state='configured'
    }elseif($Mode -eq 'Test'){
        if(-not $cert.Subject.StartsWith($script:policy.test_subject_prefix,[StringComparison]::Ordinal) -or $cert.Subject -ne $cert.Issuer){Stop-Auth 'not_a_test_certificate' 'Test mode requires the explicitly named, self-signed MediaFlow TEST ONLY certificate.'}
        if($chain.valid -or @($chain.status|Where-Object {$_ -ne 'UntrustedRoot'}).Count){Stop-Auth 'test_certificate_trust_changed' 'Test certificates must remain untrusted and must not have additional chain errors.'}
        $script:result.configuration_state='test_only_untrusted'
    }
    return $cert
}
function Pe-Metadata([string]$Path){
    $stream=[IO.File]::OpenRead($Path);$reader=[IO.BinaryReader]::new($stream)
    try{
        if($stream.Length -lt 128 -or $reader.ReadUInt16() -ne 0x5a4d){Stop-Auth 'invalid_pe' 'A declared PE file has no valid DOS header.'}
        $stream.Position=0x3c;$nt=$reader.ReadUInt32();if($nt -gt $stream.Length-24){Stop-Auth 'invalid_pe' 'A PE header offset is out of bounds.'}
        $stream.Position=$nt;if($reader.ReadUInt32() -ne 0x4550){Stop-Auth 'invalid_pe' 'A declared PE file has no valid PE header.'}
        $stream.Position=$nt+20;$optionalSize=$reader.ReadUInt16();$optional=$nt+24;$stream.Position=$optional;$magic=$reader.ReadUInt16()
        $directory=if($magic -eq 0x10b){96}elseif($magic -eq 0x20b){112}else{Stop-Auth 'invalid_pe' 'The PE optional header format is unsupported.'}
        if($optionalSize -lt $directory+40 -or $optional+$optionalSize -gt $stream.Length){Stop-Auth 'invalid_pe' 'The PE optional header is truncated.'}
        $stream.Position=$optional+$directory+32;$offset=$reader.ReadUInt32();$size=$reader.ReadUInt32();$count=0;$tokens=[Collections.Generic.List[object]]::new();$thumbs=[Collections.Generic.List[string]]::new()
        if($offset -or $size){
            if(-not $offset -or $size -lt 8 -or [long]$offset+$size -gt $stream.Length -or $size -gt 16MB){Stop-Auth 'invalid_pe_certificate' 'The embedded signature table is invalid or too large.'}
            $end=[long]$offset+$size;$stream.Position=$offset
            while($stream.Position -lt $end){
                $start=$stream.Position;if($end-$start -lt 8){Stop-Auth 'invalid_pe_certificate' 'A signature table member is truncated.'}
                $length=$reader.ReadUInt32();$revision=$reader.ReadUInt16();$type=$reader.ReadUInt16()
                if($length -lt 8 -or $start+$length -gt $end -or $revision -ne 0x200 -or $type -ne 2){Stop-Auth 'invalid_pe_certificate' 'An unsupported embedded signature entry was found.'}
                $cms=[Security.Cryptography.Pkcs.SignedCms]::new();$cms.Decode($reader.ReadBytes([int]$length-8));$cms.CheckSignature($true);$count++
                foreach($signer in $cms.SignerInfos){
                    if($signer.Certificate){$thumbs.Add($signer.Certificate.Thumbprint)}
                    try{$verified=[MediaFlow.Authenticode.NativeTrust]::VerifyTimestamps($signer,[DateTimeOffset]::UtcNow)}catch{Stop-Auth 'invalid_rfc3161_token' 'A declared RFC3161 timestamp failed token signature, signer binding, certificate validity or clock validation.'}
                    foreach($token in $verified){$tokens.Add([pscustomobject]@{signer_thumbprint=$token.SignerThumbprint;tsa_thumbprint=$token.TsaThumbprint;timestamp_utc=$token.Timestamp.ToString('o');message_imprint_algorithm=$token.MessageImprintAlgorithm;cryptographically_verified=$true})}
                }
                $stream.Position=$start+[long]([Math]::Ceiling($length/8.0)*8)
            }
        }
        return [pscustomobject]@{embedded_signatures=$count;rfc3161_timestamps=$tokens.Count;rfc3161_tokens=@($tokens.ToArray());embedded_signer_thumbprints=@($thumbs.ToArray())}
    }finally{$reader.Dispose();$stream.Dispose()}
}
function Assert-Timestamp {
    $uri=$null
    if(-not[Uri]::TryCreate($TimestampUrl,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.HostNameType -ne [UriHostNameType]::Dns -or $uri.IsLoopback -or $uri.Host -notmatch '\.' -or $uri.Host -match '(?i)\.(local|internal|localhost)$'){Stop-Auth 'timestamp_required' 'Production signing requires an explicitly configured HTTPS RFC3161 service without embedded credentials.'}
}
function Inspect-Pe([string]$Root,$Item,[bool]$FirstParty){
    $path=Join-Path $Root $Item.path;$pe=Pe-Metadata $path;$signature=Get-AuthenticodeSignature -LiteralPath $path
    $signer=$signature.SignerCertificate;$timestamp=$signature.TimeStamperCertificate
    $row=[ordered]@{path=$Item.path;ownership=$(if($FirstParty){'first_party'}else{'third_party_preserve'});before_sha256=$Item.sha256;after_sha256=(File-Digest $path);status=$signature.Status.ToString();signature_type=$signature.SignatureType.ToString();signer_thumbprint=$(if($signer){$signer.Thumbprint}else{$null});signer_subject=$(if($signer){$signer.Subject}else{$null});timestamp_thumbprint=$(if($timestamp){$timestamp.Thumbprint}else{$null});embedded_signatures=$pe.embedded_signatures;embedded_signer_thumbprints=$pe.embedded_signer_thumbprints;rfc3161_timestamps=$pe.rfc3161_timestamps;rfc3161_tokens=$pe.rfc3161_tokens;chain_valid=$false;timestamp_chain_valid=$false;native_verify_exit_code=$null;native_verification=$null;windows_trust_status=$null;formal_verified=$false;test_verified=$false;policy=$(if($FirstParty){'Sign only the fixed first-party list in a new tree.'}else{'Preserve original bytes and existing signature; never re-sign.'})}
    $script:inspectionRows.Add($row)
    if($signer){$row.chain_valid=(Certificate-Chain $signer ($Mode -eq 'Production' -and $FirstParty)).valid}
    if($timestamp){$row.timestamp_chain_valid=(Certificate-Chain $timestamp ($Mode -eq 'Production' -and $FirstParty)).valid}
    if($Operation -eq 'Inspect' -and $Mode -eq 'CI'){
        if($row.status -eq 'HashMismatch'){Stop-Auth 'invalid_existing_signature' 'An existing embedded signature reports modified file content.'}
        return [pscustomobject]$row
    }
    if(-not $FirstParty){return [pscustomobject]$row}
    if(-not $signer -or $signer.Thumbprint -ne $CertificateThumbprint.ToUpperInvariant() -or $pe.embedded_signatures -lt 1 -or $signer.Thumbprint -notin $pe.embedded_signer_thumbprints -or $row.signature_type -ne 'Authenticode'){Stop-Auth 'signer_mismatch' 'A first-party PE must contain an embedded Authenticode signature matching the explicitly selected identity; catalog signatures are insufficient.'}
    $native=Native-Tool @('verify','/pa','/all','/v','/tw',$path);$row.native_verify_exit_code=$native.exit_code;$row.native_verification=$native
    $row.windows_trust_status='0x'+[MediaFlow.Authenticode.NativeTrust]::Verify($path).ToString('x8')
    if($Mode -eq 'Production'){
        $boundTokens=@($pe.rfc3161_tokens|Where-Object {$timestamp -and $_.cryptographically_verified -and $_.signer_thumbprint -eq $signer.Thumbprint -and $_.tsa_thumbprint -eq $timestamp.Thumbprint -and $_.message_imprint_algorithm -eq '2.16.840.1.101.3.4.2.1'})
        if($row.status -ne 'Valid' -or -not $row.chain_valid -or $native.exit_code -ne 0 -or $row.windows_trust_status -ne '0x00000000' -or -not $timestamp -or -not $row.timestamp_chain_valid -or $boundTokens.Count -lt 1 -or -not(Certificate-Eku $timestamp '1.3.6.1.5.5.7.3.8')){Stop-Auth 'formal_verification_failed' 'Windows trust, native verification, bound RFC3161 timestamp or signer identity verification failed.'}
        $row.formal_verified=$true
    }elseif($Mode -eq 'Test'){
        if($row.status -notin @('UnknownError','NotTrusted') -or $native.exit_code -ne 1 -or $pe.embedded_signatures -ne 1 -or $row.windows_trust_status -ne '0x800b0109'){Stop-Auth 'test_verification_failed' 'The test signature must verify its content while remaining rejected only for the untrusted test root.'}
        $row.test_verified=$true
    }
    return [pscustomobject]$row
}
try{
    if(-not $IsWindows -or $PSVersionTable.PSVersion -lt [version]'7.4'){Stop-Auth 'unsupported_platform' 'PowerShell 7.4 or later on Windows is required.'}
    $reportPath=Plain-Path $Report $false
    if(Test-Path -LiteralPath $reportPath){Stop-Auth 'existing_report' 'An existing report will never be overwritten.'}
    if($ReleaseDirectory){
        $root=Plain-Path $ReleaseDirectory
        if(-not(Test-Path -LiteralPath $root -PathType Container)){Stop-Auth 'not_a_release_directory' 'The release input must be a directory.'}
        if($reportPath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){Stop-Auth 'report_inside_release' 'Reports must remain outside the static release tree.'}
    }elseif($Operation -ne 'Inspect'){Stop-Auth 'release_directory_required' 'Signing and file verification require the complete static release input.'}
    if($OutputDirectory){$proposed=Plain-Path $OutputDirectory $false;if($reportPath.StartsWith($proposed+'\',[StringComparison]::OrdinalIgnoreCase)){Stop-Auth 'report_inside_output' 'Reports must remain outside the derived static release tree.'}}
    $script:reportAllowed=$true
    if($Mode -eq 'CI' -and $Operation -ne 'Inspect'){Stop-Auth 'ci_inspect_only' 'Ordinary CI is read-only and can only inspect Authenticode state.'}
    $policyPath=Join-Path (Split-Path -Parent $PSScriptRoot) 'security\authenticode-policy.json'
    $script:policy=Get-Content -LiteralPath $policyPath -Raw|ConvertFrom-Json
    $expected=@('MediaFlow.exe','tools/MediaFlow.Recycle.exe','tools/webview2/MediaFlow.WebView2Host.exe')
    if($script:policy.schema_version -ne 1 -or $script:policy.product -ne 'MediaFlow' -or (@($script:policy.first_party_pe|Sort-Object)|ConvertTo-Json -Compress) -ne (@($expected|Sort-Object)|ConvertTo-Json -Compress) -or -not $script:policy.production_requires_trusted_chain -or -not $script:policy.production_requires_timestamp){Stop-Auth 'invalid_policy' 'The reviewed first-party paths and production trust requirements cannot be relaxed.'}
    Add-Type -AssemblyName System.Security.Cryptography.Pkcs
    $references=@(Get-ChildItem -LiteralPath (Join-Path $PSHOME 'ref') -Filter '*.dll' -File|ForEach-Object FullName)+[Security.Cryptography.Pkcs.SignedCms].Assembly.Location
    Add-Type -Path (Join-Path $PSScriptRoot 'authenticode_native.cs') -ReferencedAssemblies $references
    if(-not $ReleaseDirectory){
        $script:stage='tool';Discover-SignTool
        $script:stage='certificate';$certificate=Select-Certificate
        if($Mode -eq 'Production'){Assert-Timestamp}
        $script:result.configuration_only=$true;$script:result.passed=$true
    }else{
    foreach($rel in $expected){if(-not(Test-Path -LiteralPath (Join-Path $root $rel) -PathType Leaf)){Stop-Auth 'first_party_missing' 'A required first-party PE is absent; unrelated directories will not be scanned.'}}
    $script:stage='static_tree';$before=@(Static-Tree $root)
    foreach($rel in $expected){if($rel -notin @($before.path)){Stop-Auth 'first_party_missing' 'A required first-party PE is absent.'}}
    $script:result.input_tree_sha256=Tree-Digest $before
    $script:stage='tool';Discover-SignTool
    $script:stage='certificate';$certificate=Select-Certificate
    if($Operation -eq 'Sign'){
        if(-not $OutputDirectory){Stop-Auth 'new_output_required' 'Signing requires a new independent output directory.'}
        $output=Plain-Path $OutputDirectory $false
        if((Test-Path -LiteralPath $output) -or $output.Equals($root,[StringComparison]::OrdinalIgnoreCase) -or $output.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase) -or $root.StartsWith($output+'\',[StringComparison]::OrdinalIgnoreCase) -or $reportPath.StartsWith($output+'\',[StringComparison]::OrdinalIgnoreCase)){Stop-Auth 'output_conflict' 'Signing output must be a new, non-overlapping tree; no source or existing output is overwritten.'}
        if($Mode -eq 'Production'){
            Assert-Timestamp
        }elseif($TimestampUrl){Stop-Auth 'test_no_network_timestamp' 'Local untrusted test signing does not contact timestamp services.'}
        foreach($rel in $expected){$sig=Get-AuthenticodeSignature -LiteralPath (Join-Path $root $rel);if($sig.Status.ToString() -ne 'NotSigned'){Stop-Auth 'existing_first_party_signature' 'A first-party PE already has a signature; it will be preserved, not overwritten or appended.'}}
        $script:stage='new_tree_copy';New-Item -ItemType Directory -Path $output -ErrorAction Stop|Out-Null
        foreach($item in $before){
            $destination=Join-Path $output $item.path;New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destination)) -Force|Out-Null
            $input=[IO.File]::Open((Join-Path $root $item.path),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
            try{$copy=[IO.File]::Open($destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None);try{$input.CopyTo($copy)}finally{$copy.Dispose()}}finally{$input.Dispose()}
            if((File-Digest $destination) -ne $item.sha256){Stop-Auth 'copy_integrity_failed' 'The new static copy differs from the verified source bytes.'}
        }
        $script:stage='sign';foreach($rel in $expected){
            $script:currentFile=$rel
            $arguments=@('sign','/fd','SHA256','/sha1',$certificate.Thumbprint,'/s','My','/u','1.3.6.1.5.5.7.3.3')
            if($CertificateStore -eq 'LocalMachine'){$arguments+='/sm'}
            if($Mode -eq 'Production'){$arguments+=@('/tr',$TimestampUrl,'/td','SHA256')}
            $arguments+=(Join-Path $output $rel);$signed=Native-Tool $arguments
            if($signed.exit_code -ne 0){Stop-Auth 'sign_failed' 'SignTool signing or timestamping failed; the source and diagnostic output tree are preserved.'}
        }
        $inspectRoot=$output
    }else{$inspectRoot=$root}
    $script:stage='verify';$rows=[Collections.Generic.List[object]]::new()
    foreach($item in $before){
        $script:currentFile=$item.path
        $first=$item.path -in $expected
        if($item.path -match '(?i)\.(exe|dll|pyd)$'){$rows.Add((Inspect-Pe $inspectRoot $item $first))}
        elseif((File-Digest (Join-Path $inspectRoot $item.path)) -ne $item.sha256){Stop-Auth 'non_pe_changed' 'A non-PE static file unexpectedly changed.'}
    }
    $script:result.files=@($rows.ToArray())
    if(@($script:result.files|Where-Object {$_.ownership -eq 'third_party_preserve' -and $_.before_sha256 -ne $_.after_sha256}).Count){Stop-Auth 'third_party_changed' 'Third-party binary bytes changed; publication is blocked.'}
    if((Tree-Digest @(Static-Tree $root)) -ne $script:result.input_tree_sha256){Stop-Auth 'source_changed' 'The original release source changed; publication is blocked.'}
    $script:result.source_unchanged=$true;$script:result.output_tree_sha256=Tree-Digest @(Static-Tree $inspectRoot);$script:result.static_files_verified=$before.Count
    $script:result.formal_verified=($Mode -eq 'Production' -and @($script:result.files|Where-Object {$_.ownership -eq 'first_party' -and $_.formal_verified}).Count -eq $expected.Count)
    $script:result.production_ready=$script:result.formal_verified
    $script:result.passed=($Mode -eq 'CI' -or $Mode -eq 'Test' -or $script:result.formal_verified)
    }
}catch{
    if(-not $script:result.files.Count -and $script:inspectionRows.Count){$script:result.files=@($script:inspectionRows.ToArray())}
    $message=$_.Exception.Message;$code='operation_failed';$public='The operation failed; original inputs and any newly created diagnostic output were preserved.'
    if($message -match '^\[MF_AUTH:([a-z0-9_]+)\](.*)$'){$code=$Matches[1];$public=$Matches[2]}
    $script:result.errors=@([ordered]@{code=$code;stage=$script:stage;file=$script:currentFile;message=$public;exception_type=$_.Exception.GetType().Name})
}
try{
    if(-not $script:reportAllowed){throw 'Report destination was not approved.'}
    if(-not $reportPath){$reportPath=Plain-Path $Report $false}
    if(Test-Path -LiteralPath $reportPath){throw 'Report already exists.'}
    $parent=Plain-Path ([IO.Path]::GetDirectoryName($reportPath)) $false;New-Item -ItemType Directory -Path $parent -Force|Out-Null;Plain-Path $parent|Out-Null
    $stream=[IO.File]::Open($reportPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$writer=[IO.StreamWriter]::new($stream,[Text.UTF8Encoding]::new($false));try{$writer.Write(($script:result|ConvertTo-Json -Depth 15))}finally{$writer.Dispose()}}finally{$stream.Dispose()}
}catch{Write-Output '{"passed":false,"formal_verified":false,"error":"report_not_written_no_overwrite"}';exit 2}
[ordered]@{passed=$script:result.passed;formal_verified=$script:result.formal_verified;mode=$Mode;configuration_state=$script:result.configuration_state;files=$script:result.files.Count;errors=@($script:result.errors|ForEach-Object code)}|ConvertTo-Json -Compress|Write-Output
if($script:result.passed){exit 0}else{exit 1}
