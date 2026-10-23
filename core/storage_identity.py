"""Windows 的变更时间采用 FileBasicInfo.ChangeTime，避免 stat/fstat 的 ctime 语义差异。"""
import os
from pathlib import Path


def identity_for(path, info=None):
    info = info or Path(path).stat()
    changed = info.st_ctime_ns
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class BasicInfo(ctypes.Structure):
            _fields_ = [('created', ctypes.c_int64), ('accessed', ctypes.c_int64),
                        ('written', ctypes.c_int64), ('changed', ctypes.c_int64), ('attributes', wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                      wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            basic = BasicInfo()
            if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise ctypes.WinError(ctypes.get_last_error())
            changed = (basic.changed - 116444736000000000) * 100
        finally:
            kernel.CloseHandle(handle)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed]
