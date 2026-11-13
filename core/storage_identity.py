"""文件身份和不共享写入/删除的 Windows 读取句柄。"""
from contextlib import contextmanager
import os
from pathlib import Path


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _BasicInfo(ctypes.Structure):
        _fields_ = [('created', ctypes.c_int64), ('accessed', ctypes.c_int64),
                    ('written', ctypes.c_int64), ('changed', ctypes.c_int64),
                    ('attributes', wintypes.DWORD)]

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def _open(path, access, share, flags):
        handle = _kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _change_time(handle):
        basic = _BasicInfo()
        if not _kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (basic.changed - 116444736000000000) * 100


def _identity(info, changed):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed]


def identity_for(path, info=None):
    info = info or Path(path).stat()
    changed = info.st_ctime_ns
    if os.name == 'nt':
        handle = _open(path, 0x80, 7, 0x80)  # FILE_READ_ATTRIBUTES, share all
        try:
            changed = _change_time(handle)
        finally:
            _kernel.CloseHandle(handle)
    return _identity(info, changed)


def identity_from_stream(stream, info):
    changed = _change_time(msvcrt.get_osfhandle(stream.fileno())) if os.name == 'nt' else info.st_ctime_ns
    return _identity(info, changed)


@contextmanager
def open_read_locked(path):
    if os.name != 'nt':
        with Path(path).open('rb') as stream:
            yield stream
        return
    # GENERIC_READ, FILE_SHARE_READ, FILE_FLAG_OPEN_REPARSE_POINT.
    handle = _open(path, 0x80000000, 1, 0x00200000)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream
