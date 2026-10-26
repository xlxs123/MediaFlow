// Fixed Windows Authenticode policy, no UI, no network, no trust-store changes.
// WinVerifyTrust returns a LONG status: only zero means formally trusted.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.Pkcs;

namespace MediaFlow.Authenticode
{
    public sealed class TimestampEvidence
    {
        public string SignerThumbprint { get; set; }
        public string TsaThumbprint { get; set; }
        public DateTimeOffset Timestamp { get; set; }
        public string MessageImprintAlgorithm { get; set; }
    }

    public static class NativeTrust
    {
        // RFC3161 Appendix A binds the messageImprint to SignerInfo's signature,
        // not merely to the file or the presence of an unsigned attribute OID.
        // This verifies cryptography and time bounds; Windows chain trust remains
        // an additional, mandatory production check in authenticode.ps1.
        public static TimestampEvidence[] VerifyTimestamps(SignerInfo signer, DateTimeOffset now)
        {
            if (signer == null || signer.Certificate == null)
                throw new CryptographicException("An RFC3161 signer certificate is required.");
            var verified = new List<TimestampEvidence>();
            foreach (CryptographicAttributeObject attribute in signer.UnsignedAttributes) {
                if (attribute.Oid.Value != "1.3.6.1.4.1.311.3.3.1") continue;
                if (attribute.Values.Count == 0)
                    throw new CryptographicException("An RFC3161 attribute cannot be empty.");
                foreach (AsnEncodedData value in attribute.Values) {
                    Rfc3161TimestampToken token;
                    int consumed;
                    if (!Rfc3161TimestampToken.TryDecode(value.RawData, out token, out consumed)
                        || consumed != value.RawData.Length)
                        throw new CryptographicException("The complete RFC3161 token must decode.");
                    System.Security.Cryptography.X509Certificates.X509Certificate2 tsa;
                    if (!token.VerifySignatureForSignerInfo(signer, out tsa) || tsa == null)
                        throw new CryptographicException("RFC3161 signature or messageImprint binding failed.");
                    using (tsa) {
                        var time = token.TokenInfo.Timestamp.ToUniversalTime();
                        if (time > now.ToUniversalTime().AddMinutes(5)
                            || time < new DateTimeOffset(signer.Certificate.NotBefore.ToUniversalTime())
                            || time > new DateTimeOffset(signer.Certificate.NotAfter.ToUniversalTime())
                            || time < new DateTimeOffset(tsa.NotBefore.ToUniversalTime())
                            || time > new DateTimeOffset(tsa.NotAfter.ToUniversalTime()))
                            throw new CryptographicException("RFC3161 timestamp violates certificate validity or clock bounds.");
                        verified.Add(new TimestampEvidence {
                            SignerThumbprint = signer.Certificate.Thumbprint,
                            TsaThumbprint = tsa.Thumbprint,
                            Timestamp = time,
                            MessageImprintAlgorithm = token.TokenInfo.HashAlgorithmId.Value
                        });
                    }
                }
            }
            return verified.ToArray();
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct FileInfo
        {
            public uint Size;
            [MarshalAs(UnmanagedType.LPWStr)] public string Path;
            public IntPtr Handle;
            public IntPtr KnownSubject;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TrustData
        {
            public uint Size;
            public IntPtr PolicyCallback;
            public IntPtr SipClient;
            public uint UiChoice;
            public uint RevocationChecks;
            public uint UnionChoice;
            public IntPtr File;
            public uint StateAction;
            public IntPtr State;
            public IntPtr UrlReference;
            public uint ProviderFlags;
            public uint UiContext;
            public IntPtr SignatureSettings;
        }

        [DllImport("wintrust.dll", ExactSpelling = true, CharSet = CharSet.Unicode)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        private static extern int WinVerifyTrust(IntPtr window, ref Guid action, ref TrustData data);

        public static uint Verify(string path)
        {
            var file = new FileInfo { Size = (uint)Marshal.SizeOf<FileInfo>(), Path = path };
            var pointer = Marshal.AllocHGlobal(Marshal.SizeOf<FileInfo>());
            Marshal.StructureToPtr(file, pointer, false);
            var data = new TrustData {
                Size = (uint)Marshal.SizeOf<TrustData>(),
                UiChoice = 2,                 // WTD_UI_NONE
                UnionChoice = 1,              // WTD_CHOICE_FILE: embedded, not a catalog
                File = pointer,
                StateAction = 1,              // WTD_STATEACTION_VERIFY
                ProviderFlags = 0x1000        // WTD_CACHE_ONLY_URL_RETRIEVAL
            };
            var action = new Guid("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");
            try { return unchecked((uint)WinVerifyTrust(new IntPtr(-1), ref action, ref data)); }
            finally {
                data.StateAction = 2;          // WTD_STATEACTION_CLOSE, even on failure
                WinVerifyTrust(new IntPtr(-1), ref action, ref data);
                Marshal.DestroyStructure<FileInfo>(pointer);
                Marshal.FreeHGlobal(pointer);
            }
        }
    }
}
