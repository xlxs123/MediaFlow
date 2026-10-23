// Isolated Windows recycle-only operation. No permanent delete code path.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItem {
    void BindToHandler(IntPtr a, ref Guid b, ref Guid c, out IntPtr d);
    void GetParent(out IShellItem p);
    void GetDisplayName(uint kind, out IntPtr value);
    void GetAttributes(uint mask, out uint attrs);
    void Compare(IShellItem other, uint hint, out int order);
}
[ComImport, Guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFileOperation {
    void Advise(IFileOperationProgressSink sink, out uint cookie);
    void Unadvise(uint cookie);
    void SetOperationFlags(uint flags);
    void SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)] string s);
    void SetProgressDialog(IntPtr p);
    void SetProperties(IntPtr p);
    void SetOwnerWindow(IntPtr hwnd);
    void ApplyPropertiesToItem(IShellItem i);
    void ApplyPropertiesToItems(IntPtr i);
    void RenameItem(IShellItem i, [MarshalAs(UnmanagedType.LPWStr)] string n, IntPtr s);
    void RenameItems(IntPtr i, [MarshalAs(UnmanagedType.LPWStr)] string n);
    void MoveItem(IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n, IntPtr s);
    void MoveItems(IntPtr i, IShellItem dest);
    void CopyItem(IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n, IntPtr s);
    void CopyItems(IntPtr i, IShellItem dest);
    void DeleteItem(IShellItem i, IFileOperationProgressSink sink);
    void DeleteItems(IntPtr i);
    void NewItem(IShellItem dest, uint attr, [MarshalAs(UnmanagedType.LPWStr)] string n, [MarshalAs(UnmanagedType.LPWStr)] string t, IntPtr s);
    void PerformOperations();
    void GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)] out bool aborted);
}
[ComVisible(true), Guid("04B0F1A7-9490-44BC-96E1-4296A31252E2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFileOperationProgressSink {
    [PreserveSig] int StartOperations();
    [PreserveSig] int FinishOperations(int hr);
    [PreserveSig] int PreRenameItem(uint f, IShellItem i, [MarshalAs(UnmanagedType.LPWStr)] string n);
    [PreserveSig] int PostRenameItem(uint f, IShellItem i, [MarshalAs(UnmanagedType.LPWStr)] string n, int hr, IShellItem result);
    [PreserveSig] int PreMoveItem(uint f, IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n);
    [PreserveSig] int PostMoveItem(uint f, IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n, int hr, IShellItem result);
    [PreserveSig] int PreCopyItem(uint f, IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n);
    [PreserveSig] int PostCopyItem(uint f, IShellItem i, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n, int hr, IShellItem result);
    [PreserveSig] int PreDeleteItem(uint f, IShellItem i);
    [PreserveSig] int PostDeleteItem(uint f, IShellItem i, int hr, IShellItem result);
    [PreserveSig] int PreNewItem(uint f, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n);
    [PreserveSig] int PostNewItem(uint f, IShellItem dest, [MarshalAs(UnmanagedType.LPWStr)] string n, [MarshalAs(UnmanagedType.LPWStr)] string t, uint attrs, int hr, IShellItem result);
    [PreserveSig] int UpdateProgress(uint total, uint done);
    [PreserveSig] int ResetTimer();
    [PreserveSig] int PauseTimer();
    [PreserveSig] int ResumeTimer();
}

[StructLayout(LayoutKind.Sequential, Pack=4)]
public struct FileIdentity {
    public uint attrs; public long creation; public long access; public long write;
    public uint volume, sizeHigh, sizeLow, links, idHigh, idLow;
}

[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public sealed class RecycleSink : IFileOperationProgressSink {
    public string Source, Expected, Receipt, Nonce, BinPath;
    public FileStream Guard;
    public bool Recycled;
    public uint PreFlags;
    public const int Abort = unchecked((int)0x80004004);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileIdentity info);
    public static string Hash(Stream stream) {
        stream.Position = 0;
        using (SHA256 h = SHA256.Create()) return BitConverter.ToString(h.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }
    public static string Display(IShellItem item) {
        IntPtr raw; item.GetDisplayName(0x80058000, out raw);
        try { return Marshal.PtrToStringUni(raw); } finally { Marshal.FreeCoTaskMem(raw); }
    }
    public int PreDeleteItem(uint flags, IShellItem item) {
        try {
            PreFlags = flags;
            if ((flags & 0x80) == 0 || !String.Equals(Path.GetFullPath(Display(item)), Source, StringComparison.OrdinalIgnoreCase)) return Abort;
            using (FileStream current = new FileStream(Source, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete)) {
                FileIdentity a, b;
                if (!GetFileInformationByHandle(Guard.SafeFileHandle, out a) || !GetFileInformationByHandle(current.SafeFileHandle, out b)) return Abort;
                if (a.volume != b.volume || a.idHigh != b.idHigh || a.idLow != b.idLow || a.write != b.write || a.sizeHigh != b.sizeHigh || a.sizeLow != b.sizeLow || b.links != 1) return Abort;
            }
            return 0;
        } catch { return Abort; }
    }
    public int PostDeleteItem(uint f, IShellItem item, int hr, IShellItem created) {
        try {
            if (hr < 0 || created == null) return Abort;
            BinPath = Display(created);
            string binRoot = Path.GetPathRoot(Source) + "$Recycle.Bin" + Path.DirectorySeparatorChar;
            if (!BinPath.StartsWith(binRoot, StringComparison.OrdinalIgnoreCase) || File.Exists(Source)) return Abort;
            using (FileStream target = new FileStream(BinPath, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete)) {
                if (Hash(target) != Expected || target.Length != Guard.Length) return Abort;
            }
            var receipt = new { recycled=true, path=Source, sha256=Expected, bin_path=BinPath, nonce=Nonce, size=Guard.Length, pre_flags=PreFlags };
            byte[] bytes = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(receipt));
            using (FileStream output = new FileStream(Receipt, FileMode.CreateNew, FileAccess.Write, FileShare.Read)) {
                output.Write(bytes, 0, bytes.Length); output.Flush(true);
            }
            Recycled = true;
            return 0;
        } catch { return Abort; }
    }
    public int StartOperations(){return 0;} public int FinishOperations(int h){return 0;}
    public int PreRenameItem(uint f,IShellItem i,string n){return Abort;}
    public int PostRenameItem(uint f,IShellItem i,string n,int h,IShellItem r){return Abort;}
    public int PreMoveItem(uint f,IShellItem i,IShellItem d,string n){return Abort;}
    public int PostMoveItem(uint f,IShellItem i,IShellItem d,string n,int h,IShellItem r){return Abort;}
    public int PreCopyItem(uint f,IShellItem i,IShellItem d,string n){return Abort;}
    public int PostCopyItem(uint f,IShellItem i,IShellItem d,string n,int h,IShellItem r){return Abort;}
    public int PreNewItem(uint f,IShellItem d,string n){return Abort;}
    public int PostNewItem(uint f,IShellItem d,string n,string t,uint a,int h,IShellItem r){return Abort;}
    public int UpdateProgress(uint t,uint d){return 0;} public int ResetTimer(){return 0;}
    public int PauseTimer(){return 0;} public int ResumeTimer(){return 0;}
}

class Program {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
    static extern void SHCreateItemFromParsingName(string path, IntPtr context, ref Guid iid, out IShellItem item);
    [STAThread] static int Main(string[] args) {
        // Exactly one previously confirmed file. No enumeration, recursive operation or fallback.
        if (args.Length != 4) return 2;
        IFileOperation operation = null; IShellItem source = null;
        try {
            string path = Path.GetFullPath(args[0]);
            if (new DriveInfo(Path.GetPathRoot(path)).DriveType != DriveType.Fixed) return 3;
            for (string current = path; !String.IsNullOrEmpty(current); current = Path.GetDirectoryName(current))
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) return 4;
            using (FileStream guard = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete)) {
                if (RecycleSink.Hash(guard) != args[1]) return 5;
                var sink = new RecycleSink { Source=path, Expected=args[1], Receipt=Path.GetFullPath(args[2]), Nonce=args[3], Guard=guard };
                operation = (IFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("3AD05575-8857-4850-9277-11B85BDB8E09")));
                // RECYCLEONDELETE + EARLYFAILURE + ADDUNDORECORD. Do not ask the Shell to permanently delete.
                operation.SetOperationFlags(0x00080000 | 0x00100000 | 0x20000000 | 0x0400 | 0x0004 | 0x0010 | 0x1000 | 0x2000);
                Guid iid = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");
                SHCreateItemFromParsingName(path, IntPtr.Zero, ref iid, out source);
                operation.DeleteItem(source, sink); operation.PerformOperations();
                bool aborted; operation.GetAnyOperationsAborted(out aborted);
                GC.KeepAlive(sink);
                return sink.Recycled && !aborted ? 0 : 6;
            }
        } catch { return 7; }
        finally {
            if (source != null) Marshal.FinalReleaseComObject(source);
            if (operation != null) Marshal.FinalReleaseComObject(operation);
        }
    }
}
