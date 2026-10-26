"""Windows trust integration contracts; formal certificate acceptance is separate."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/authenticode.ps1"
FIRST_PARTY = (
    "MediaFlow.exe", "tools/MediaFlow.Recycle.exe",
    "tools/webview2/MediaFlow.WebView2Host.exe",
)


class AuthenticodePolicyTests(unittest.TestCase):
    def test_reviewed_actual_build_outputs_and_separate_ci_production_states(self):
        policy = json.loads((ROOT / "security/authenticode-policy.json").read_text("utf-8"))
        self.assertEqual(policy["first_party_pe"], list(FIRST_PARTY))
        self.assertTrue(policy["production_requires_trusted_chain"])
        self.assertTrue(policy["production_requires_timestamp"])
        self.assertEqual(policy["timestamp_protocol"], "RFC3161")
        self.assertEqual(policy["configuration_state"], "not_configured")
        self.assertEqual(policy["ci_operation"], "Inspect")
        self.assertTrue(policy["ci_allows_unsigned"])
        build = (ROOT / "build.ps1").read_text("utf-8-sig")
        self.assertIn("MediaFlow.Recycle.exe", build)
        self.assertIn("MediaFlow.WebView2Host.exe", (ROOT / "scripts/build-webview2.ps1").read_text("utf-8-sig"))


@unittest.skipUnless(sys.platform == "win32", "Windows native trust APIs required")
class AuthenticodeWindowsTests(unittest.TestCase):
    """Actual SDK/process/read-only checks without generating any CI signing key."""
    @classmethod
    def setUpClass(cls):
        cls.pwsh = os.environ.get("MEDIAFLOW_PWSH") or shutil.which("pwsh")
        if not cls.pwsh:
            raise unittest.SkipTest("PowerShell 7 unavailable")
        if os.environ.get("MEDIAFLOW_AUTH_TEST_ROOT"):
            root = Path(os.environ["MEDIAFLOW_AUTH_TEST_ROOT"])
        elif os.environ.get("GITHUB_ACTIONS") == "true":
            root = Path(os.environ["RUNNER_TEMP"]) / "mediaflow-auth-tests"
        else:
            root = ROOT.parent / ".mediaflow-authenticode-sbom-task/auth-tests"
        root.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="unit-", dir=root))
        source = cls.work / "fixture.cs"
        source.write_text('internal class Program { public static void Main() {} }', encoding="utf-8")
        executable = cls.work / "fixture.exe"
        csc = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        result = subprocess.run([str(csc), "/nologo", "/target:winexe", "/optimize+", "/out:" + str(executable), str(source)], capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError("Controlled PE fixture compilation failed")
        cls.executable = executable
        cls.sdk = os.environ.get("MEDIAFLOW_SIGNTOOL", "")
        cls.records = []

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "work"):
            # Keep passing and failed unit evidence; no automatic deletion.
            (cls.work / "unit-case-index.json").write_text(json.dumps(cls.records, indent=2), encoding="utf-8")

    def fixture(self):
        root = self.work / ("release-" + uuid.uuid4().hex)
        for relative in FIRST_PARTY:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.executable, path)
        return root

    def run_operation(self, *, release=None, operation="Inspect", mode="CI", report=None, extra=()):
        report = report or self.work / ("report-" + uuid.uuid4().hex + ".json")
        command = [self.pwsh, "-NoLogo", "-NoProfile", "-File", str(SCRIPT), "-Operation", operation,
                   "-Mode", mode, "-Report", str(report)]
        if release is not None:
            command += ["-ReleaseDirectory", str(release)]
        if self.sdk:
            command += ["-SignToolPath", self.sdk]
        command += list(extra)
        completed = subprocess.run(command, capture_output=True, timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (report.parent / (report.name + ".stdout.log")).write_bytes(completed.stdout)
        (report.parent / (report.name + ".stderr.log")).write_bytes(completed.stderr)
        value = json.loads(report.read_text("utf-8-sig")) if report.is_file() else None
        self.records.append({"case": self.id(), "returncode": completed.returncode,
                             "report": report.name, "passed": value.get("passed") if isinstance(value, dict) else None})
        return completed, value

    def assert_failed(self, completed, value, code=None):
        self.assertNotEqual(completed.returncode, 0)
        if value is not None:
            self.assertFalse(value["passed"])
            self.assertFalse(value["formal_verified"])
            if code:
                self.assertIn(code, [item["code"] for item in value["errors"]])

    def test_actual_unsigned_ci_inspection_is_not_formal_signing(self):
        release = self.fixture()
        original = {p: hashlib.sha256((release / p).read_bytes()).hexdigest() for p in FIRST_PARTY}
        completed, value = self.run_operation(release=release)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(value["passed"])
        self.assertFalse(value["formal_verified"])
        self.assertTrue(value["source_unchanged"])
        self.assertEqual(len(value["files"]), 3)
        for row in value["files"]:
            self.assertEqual(row["status"], "NotSigned")
            self.assertEqual(row["ownership"], "first_party")
            self.assertEqual(row["before_sha256"], original[row["path"]])
            self.assertEqual(row["after_sha256"], row["before_sha256"])
        text = json.dumps(value)
        self.assertNotIn(str(self.work), text)
        self.assertNotIn(str(self.work).replace("\\", "\\\\"), text)

    def test_ci_configuration_probe_never_claims_files_are_signed(self):
        completed, value = self.run_operation()
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(value["configuration_only"])
        self.assertEqual(value["files"], [])
        self.assertFalse(value["formal_verified"])

    def test_public_wrappers_default_to_production_and_fail_closed(self):
        release = self.fixture()
        for name in ("sign_windows.ps1", "verify_windows_signature.ps1"):
            report = self.work / ("wrapper-" + uuid.uuid4().hex + ".json")
            command = [self.pwsh, "-NoLogo", "-NoProfile", "-File", str(ROOT / "scripts" / name),
                       "-ReleaseDirectory", str(release), "-Report", str(report)]
            if name == "sign_windows.ps1":
                command += ["-OutputDirectory", str(self.work / ("new-wrapper-" + uuid.uuid4().hex))]
            if self.sdk:
                command += ["-SignToolPath", self.sdk]
            completed = subprocess.run(command, capture_output=True, timeout=120)
            value = json.loads(report.read_text("utf-8-sig"))
            self.assert_failed(completed, value)
            self.assertEqual(value["mode"], "Production")
            self.assertEqual(value["configuration_state"], "not_configured")

    def test_production_unconfigured_fails_without_creating_output(self):
        output = self.work / ("must-not-create-" + uuid.uuid4().hex)
        completed, value = self.run_operation(release=self.fixture(), operation="Sign", mode="Production",
                                              extra=("-OutputDirectory", str(output)))
        self.assert_failed(completed, value)
        self.assertEqual(value["configuration_state"], "not_configured")
        self.assertFalse(output.exists())

    def test_production_configuration_probe_rejects_missing_certificate(self):
        completed, value = self.run_operation(mode="Production")
        self.assert_failed(completed, value)
        self.assertEqual(value["files"], [])
        self.assertEqual(value["configuration_state"], "not_configured")

    def test_missing_wrong_and_malformed_thumbprints_never_substitute_identity(self):
        for thumb in ("A" * 40, "bad-thumbprint", "A" * 40 + ";exit 0"):
            with self.subTest(thumb_format=len(thumb)):
                completed, value = self.run_operation(mode="Production", extra=("-CertificateThumbprint", thumb))
                self.assert_failed(completed, value)
                self.assertEqual(value["files"], [])

    def test_ci_cannot_sign_or_verify_as_production(self):
        for operation in ("Sign", "Verify"):
            completed, value = self.run_operation(release=self.fixture(), operation=operation)
            self.assert_failed(completed, value, "ci_inspect_only")

    def test_required_first_party_file_must_exist(self):
        release = self.fixture()
        # Removing a disposable synthetic fixture is part of this negative test.
        (release / FIRST_PARTY[-1]).rename(release / "synthetic-missing-pe.bin")
        completed, value = self.run_operation(release=release)
        self.assert_failed(completed, value, "first_party_missing")

    def test_invalid_pe_is_not_accepted_by_extension(self):
        release = self.fixture()
        (release / FIRST_PARTY[-1]).write_bytes(b"synthetic non-PE file")
        completed, value = self.run_operation(release=release)
        self.assert_failed(completed, value, "invalid_pe")

    def test_user_data_entry_is_rejected_before_its_contents_are_read(self):
        release = self.fixture()
        forbidden = release / "BrowserProfile"
        forbidden.mkdir()
        marker = forbidden / "synthetic-protected-marker.txt"
        marker.write_text("synthetic test marker only", encoding="utf-8")
        completed, value = self.run_operation(release=release)
        self.assert_failed(completed, value, "user_data_present")
        self.assertNotIn(marker.name, json.dumps(value))
        self.assertEqual(marker.read_text("utf-8"), "synthetic test marker only")

    def test_pfx_containers_are_rejected_without_inspection(self):
        release = self.fixture()
        (release / "synthetic-container.pfx").write_bytes(b"not a certificate")
        completed, value = self.run_operation(release=release)
        self.assert_failed(completed, value, "private_material")

    def test_existing_report_is_preserved(self):
        path = self.work / ("existing-" + uuid.uuid4().hex + ".json")
        path.write_text('{"sentinel":"preserve"}', encoding="utf-8")
        completed, _ = self.run_operation(release=self.fixture(), report=path)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(path.read_text("utf-8"), '{"sentinel":"preserve"}')

    def test_report_cannot_modify_release_or_create_signing_output(self):
        release = self.fixture()
        report = release / "must-not-create-report.json"
        completed, value = self.run_operation(release=release, report=report)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(value)
        self.assertFalse(report.exists())

    def test_traversal_unc_and_alternate_stream_paths_are_rejected(self):
        release = self.fixture()
        bad = (str(release) + "\\..\\" + release.name, "\\\\untrusted.invalid\\release", str(release) + ":stream")
        for path in bad:
            completed, value = self.run_operation(release=path)
            self.assert_failed(completed, value)

    def test_third_party_file_is_only_recorded_and_preserved(self):
        release = self.fixture()
        third = release / "tools/third-party-fixture.dll"
        shutil.copyfile(self.executable, third)
        completed, value = self.run_operation(release=release)
        self.assertEqual(completed.returncode, 0)
        row = next(r for r in value["files"] if r["path"] == "tools/third-party-fixture.dll")
        self.assertEqual(row["ownership"], "third_party_preserve")
        self.assertEqual(row["before_sha256"], row["after_sha256"])

    def test_explicit_untrusted_tool_cannot_execute_arbitrary_program(self):
        tool_root = self.work / ("untrusted-tool-" + uuid.uuid4().hex)
        tool_root.mkdir()
        impostor = tool_root / "signtool.exe"
        shutil.copyfile(self.executable, impostor)
        # Invoke directly, avoiding a duplicate SignToolPath in the shared helper.
        report = self.work / ("untrusted-tool-" + uuid.uuid4().hex + ".json")
        command = [self.pwsh, "-NoLogo", "-NoProfile", "-File", str(SCRIPT),
                   "-Operation", "Inspect", "-Mode", "CI", "-Report", str(report),
                   "-SignToolPath", str(impostor)]
        completed = subprocess.run(command, capture_output=True, timeout=120)
        value = json.loads(report.read_text("utf-8-sig"))
        self.assert_failed(completed, value, "untrusted_signtool")

    def test_integration_work_root_cannot_contain_the_original_source(self):
        release = self.fixture()
        output = self.work / ("invalid-integration-" + uuid.uuid4().hex)
        command = [self.pwsh, "-NoLogo", "-NoProfile", "-File",
                   str(ROOT / "tests/authenticode_local_integration.ps1"),
                   "-WorkRoot", str(self.work), "-WorkDirectory", str(output),
                   "-SourceDirectory", str(release), "-SignToolPath", self.sdk or "signtool.exe"]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_rfc3161_tokens_bind_signer_signature_and_reject_forgery(self):
        """Ephemeral in-memory synthetic TSA; no Root store or network access."""
        probe = self.work / ("rfc3161-" + uuid.uuid4().hex + ".ps1")
        probe.write_text(r'''param([string]$NativeSource)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Security.Cryptography.Pkcs
$references=@(Get-ChildItem -LiteralPath (Join-Path $PSHOME 'ref') -Filter '*.dll' -File|ForEach-Object FullName)+[Security.Cryptography.Pkcs.SignedCms].Assembly.Location
$fixture=@'
namespace MediaFlow.Authenticode.Tests {
using System;
using System.Collections.Generic;
using System.Formats.Asn1;
using System.Security.Cryptography;
using System.Security.Cryptography.Pkcs;
using System.Security.Cryptography.X509Certificates;
public static class TimestampFixture {
    public static string Phase="start";
    private const string RfcOid="1.3.6.1.4.1.311.3.3.1";
    private static SignedCms Cms(X509Certificate2 certificate, byte payload) {
        var cms=new SignedCms(new ContentInfo(new byte[]{payload}),false);
        var signer=new CmsSigner(certificate);signer.IncludeOption=X509IncludeOption.EndCertOnly;
        cms.ComputeSignature(signer);return cms;
    }
    private static byte[] Token(SignerInfo signer,X509Certificate2 tsa,DateTimeOffset time) {
        Phase="token-construction";
        var request=Rfc3161TimestampRequest.CreateFromSignerInfo(signer,HashAlgorithmName.SHA256);
        var info=new Rfc3161TimestampTokenInfo(new Oid("1.2.3.4.5"),new Oid("2.16.840.1.101.3.4.2.1"),request.GetMessageHash(),new byte[]{1},time,null,false,null,null,null);
        var timestamp=new SignedCms(new ContentInfo(new Oid("1.2.840.113549.1.9.16.1.4"),info.Encode()),false);
        var cmsSigner=new CmsSigner(tsa);cmsSigner.IncludeOption=X509IncludeOption.EndCertOnly;
        var ess=new AsnWriter(AsnEncodingRules.DER);
        ess.PushSequence();ess.PushSequence();ess.PushSequence();
        ess.WriteOctetString(SHA256.HashData(tsa.RawData));
        ess.PopSequence();ess.PopSequence();ess.PopSequence();
        cmsSigner.SignedAttributes.Add(new AsnEncodedData(new Oid("1.2.840.113549.1.9.16.2.47"),ess.Encode()));
        Phase="token-signature";timestamp.ComputeSignature(cmsSigner);return timestamp.Encode();
    }
    private static void Attach(SignedCms cms,byte[] token) { Phase="attach-token";cms.SignerInfos[0].AddUnsignedAttribute(new AsnEncodedData(new Oid(RfcOid),token)); }
    private static bool Rejected(SignedCms cms,DateTimeOffset now) {
        try { NativeTrust.VerifyTimestamps(cms.SignerInfos[0],now);return false; }
        catch(CryptographicException) { return true; }
    }
    public static Dictionary<string,bool> Run() {
        var now=DateTimeOffset.UtcNow;
        using(var signerKey=RSA.Create(2048)) using(var tsaKey=RSA.Create(2048)) {
            var signerRequest=new CertificateRequest("CN=MediaFlow RFC3161 SYNTHETIC TEST ONLY",signerKey,HashAlgorithmName.SHA256,RSASignaturePadding.Pkcs1);
            var tsaRequest=new CertificateRequest("CN=MediaFlow RFC3161 SYNTHETIC TSA TEST ONLY",tsaKey,HashAlgorithmName.SHA256,RSASignaturePadding.Pkcs1);
            tsaRequest.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(new OidCollection {new Oid("1.3.6.1.5.5.7.3.8")},true));
            using(var signer=signerRequest.CreateSelfSigned(now.AddMinutes(-5),now.AddHours(2)))
            using(var tsa=tsaRequest.CreateSelfSigned(now.AddDays(-2),now.AddDays(2))) {
                var results=new Dictionary<string,bool>();var original=Cms(signer,1);
                results["absent_is_zero"]=NativeTrust.VerifyTimestamps(original.SignerInfos[0],now).Length==0;
                var validToken=Token(original.SignerInfos[0],tsa,now);Attach(original,validToken);
                Phase="verify-valid-token";var evidence=NativeTrust.VerifyTimestamps(original.SignerInfos[0],now);
                results["valid_synthetic_crypto_only"]=evidence.Length==1 && evidence[0].TsaThumbprint==tsa.Thumbprint && evidence[0].SignerThumbprint==signer.Thumbprint;
                var wrongSigner=Cms(signer,2);Attach(wrongSigner,validToken);
                results["wrong_signer_message_imprint_rejected"]=Rejected(wrongSigner,now);
                var malformed=Cms(signer,1);Attach(malformed,new byte[]{0x30,0});
                results["malformed_oid_value_rejected"]=Rejected(malformed,now);
                var tokenCms=new SignedCms();tokenCms.Decode(validToken);
                var signature=tokenCms.SignerInfos[0].GetSignature();var damaged=(byte[])validToken.Clone();int signatureOffset=-1;
                for(int i=0;i<=damaged.Length-signature.Length;i++) { if(damaged.AsSpan(i,signature.Length).SequenceEqual(signature)) { signatureOffset=i;break; } }
                if(signatureOffset<0) throw new CryptographicException("Synthetic signature location was not found.");
                damaged[signatureOffset+signature.Length/2]^=1;
                var damagedCms=new SignedCms();damagedCms.Decode(damaged);
                results["damaged_cms_structure_valid"]=damagedCms.SignerInfos.Count==1;
                var badSignature=Cms(signer,1);Attach(badSignature,damaged);
                results["damaged_token_signature_rejected"]=Rejected(badSignature,now);
                var future=Cms(signer,1);Attach(future,Token(future.SignerInfos[0],tsa,now.AddHours(1)));
                results["future_timestamp_rejected"]=Rejected(future,now);
                var early=Cms(signer,1);Attach(early,Token(early.SignerInfos[0],tsa,now.AddHours(-1)));
                results["outside_signer_validity_rejected"]=Rejected(early,now);
                return results;
            }
        }
    }
}
}
'@
Add-Type -TypeDefinition ([IO.File]::ReadAllText($NativeSource)+[Environment]::NewLine+$fixture) -ReferencedAssemblies $references
try{$result=[MediaFlow.Authenticode.Tests.TimestampFixture]::Run()}catch{Write-Error ('Synthetic RFC3161 phase: '+[MediaFlow.Authenticode.Tests.TimestampFixture]::Phase+'; '+$_.Exception.InnerException.ToString());exit 1}
$result|ConvertTo-Json -Compress
if(@($result.Values|Where-Object {-not $_}).Count){exit 1}
''', encoding="utf-8")
        completed = subprocess.run([self.pwsh, "-NoLogo", "-NoProfile", "-File", str(probe),
                                    "-NativeSource", str(ROOT / "scripts/authenticode_native.cs")],
                                   capture_output=True, timeout=120)
        (probe.parent / (probe.name + ".stdout.log")).write_bytes(completed.stdout)
        (probe.parent / (probe.name + ".stderr.log")).write_bytes(completed.stderr)
        self.assertEqual(completed.returncode, 0, "Synthetic RFC3161 crypto fixture failed; retained diagnostics identify the cause")
        evidence = json.loads(completed.stdout.decode("utf-8-sig"))
        self.assertTrue(evidence["valid_synthetic_crypto_only"])
        self.assertTrue(evidence["wrong_signer_message_imprint_rejected"])
        self.assertTrue(evidence["damaged_cms_structure_valid"])
        self.assertTrue(evidence["damaged_token_signature_rejected"])
        self.assertTrue(all(evidence.values()))

    def test_linked_members_are_rejected_and_target_is_untouched(self):
        release = self.fixture()
        outside = self.work / ("outside-" + uuid.uuid4().hex)
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("synthetic external target", encoding="utf-8")
        junction = release / "linked-member"
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(junction) + " -Target " + quote(outside) + " | Out-Null"
        created = subprocess.run([self.pwsh, "-NoProfile", "-Command", command], capture_output=True, timeout=30)
        self.assertEqual(created.returncode, 0, "Controlled junction fixture creation failed")
        completed, value = self.run_operation(release=release)
        self.assert_failed(completed, value, "linked_member")
        self.assertEqual(marker.read_text("utf-8"), "synthetic external target")


if __name__ == "__main__":
    unittest.main(verbosity=2)
