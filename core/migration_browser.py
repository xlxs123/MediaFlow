"""明确选择后的本机离线 WebView2 配置复制；不导出凭据或承诺网站登录仍有效。"""
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from core.data_migration import MigrationCancelled, MigrationError


_STRUCTURE = '浏览器配置结构不兼容或不完整，暂不迁移；请保持浏览器选项未选中，原配置会保留。'
_BUSY = '浏览器配置正在使用，不能复制活动数据库；请关闭使用该配置的 MediaFlow 和 WebView2 后重试。'
_PROCESS = '无法确认浏览器进程的安全状态，暂不复制配置；请关闭其他 MediaFlow 和 WebView2 后重试。'
_LINK = '浏览器配置或目标路径包含链接、重解析点或非常规文件，不能安全迁移。'
_VERSION = '来源浏览器配置与当前 WebView2 Runtime 版本不一致或无法确认，暂不迁移登录配置；原配置会保留。'
_CHANGED = '浏览器配置在检查期间发生变化，已停止复制；源配置未修改，目标的部分副本会保留。'
_COPY_ERROR = '浏览器配置复制未完成；请检查文件占用、权限和磁盘空间。源配置未修改，目标的部分副本会保留。'
_CHUNK = 1024 * 1024


def _cancel(event):
    if event is not None and event.is_set():
        raise MigrationCancelled('浏览器配置迁移已安全取消；源配置未修改，目标的部分副本会保留。')


def _path(value):
    try:
        path = Path(value).expanduser().absolute()
        for candidate in (path, *path.parents):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or candidate.is_junction()):
                raise MigrationError(_LINK)
        return path
    except MigrationError:
        raise
    except (OSError, TypeError, ValueError):
        raise MigrationError(_LINK) from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _inventory(root, event=None):
    _cancel(event)
    root = _path(root)
    if not root.is_dir():
        raise MigrationError(_STRUCTURE)
    entries = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        _cancel(event)
        with os.scandir(directory) as children:
            for child in children:
                _cancel(event)
                path = Path(child.path)
                # Windows DirEntry 缓存不总包含 inode/链接数；使用实际 lstat 建立身份。
                info = path.lstat()
                if child.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise MigrationError(_LINK)
                kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else ''
                if not kind or kind == 'file' and info.st_nlink > 1:
                    raise MigrationError(_LINK)
                relative = path.relative_to(root).as_posix()
                entries[relative] = {'kind': kind, 'identity': _identity(info), 'info': info}
                if kind == 'directory':
                    pending.append(path)
                if len(entries) > 200000:
                    raise MigrationError('浏览器配置文件过多，已停止迁移；请检查所选目录是否为独立 MediaFlow BrowserProfile。')
    return entries


class _FrozenFile:
    """原生句柄避免 CRT 文件描述符上限；整个复制期拒绝其他写入和删除。"""
    def __init__(self, path, directory=False):
        self.path = path
        self.directory = directory
        self.handle = None

    def __enter__(self):
        if os.name != 'nt':
            raise MigrationError('浏览器配置安全迁移仅支持本机 Windows，当前平台未执行复制。')
        from core.recovery_workspace import _windows_file_api
        self.api, create, self._close = _windows_file_api()
        flags = 0x80 | 0x00200000 | (0x02000000 if self.directory else 0x08000000)
        self.handle = create(str(self.path), 0x80000000, 1, None, 3, flags, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise MigrationError(_BUSY)
        # FILE_FLAG_OPEN_REPARSE_POINT 防止检查和打开之间被替换为链接。
        class AttributeTag(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]
        info = AttributeTag()
        query = self.api.GetFileInformationByHandleEx
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or info.attributes & 0x400:
            self.__exit__(None, None, None)
            raise MigrationError(_LINK)
        self._read = self.api.ReadFile
        self._read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self._read.restype = wintypes.BOOL
        self._seek = self.api.SetFilePointerEx
        self._seek.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        self._seek.restype = wintypes.BOOL
        return self

    def read(self, size):
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not self._read(self.handle, buffer, size, ctypes.byref(read), None):
            raise MigrationError(_BUSY)
        return buffer.raw[:read.value]

    def rewind(self):
        if not self._seek(self.handle, 0, None, 0):
            raise MigrationError(_COPY_ERROR)

    def __exit__(self, *_):
        if self.handle is not None:
            self._close(self.handle)
            self.handle = None


def _freeze(stack, root, entries, event=None):
    stack.enter_context(_FrozenFile(root, directory=True))
    readers = {}
    for relative, item in sorted(entries.items(), key=lambda pair: (pair[1]['kind'] != 'directory', pair[0])):
        _cancel(event)
        reader = stack.enter_context(_FrozenFile(root / relative, directory=item['kind'] == 'directory'))
        if _identity((root / relative).lstat()) != item['identity']:
            raise MigrationError(_CHANGED)
        if item['kind'] == 'file':
            readers[relative] = reader
    return readers


def _windows_arguments(command):
    """使用系统命令行解析，路径匹配采用完整参数，不做字符串子串匹配。"""
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    parse = shell.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    values = parse(command, ctypes.byref(count))
    if not values:
        raise MigrationError(_PROCESS)
    try:
        return [values[index] for index in range(count.value)]
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(values)


def _browser_processes():
    if os.name != 'nt':
        raise MigrationError(_PROCESS)
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # 固定脚本，无用户路径拼接。命令行只在内存中用于关联，不写日志或普通报告。
    script = """$ErrorActionPreference='Stop';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);$p=@(Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe' OR Name='MediaFlow.WebView2Host.exe' OR Name='MediaFlow.exe'\" | Select-Object -First 2049 ProcessId,Name,CommandLine);ConvertTo-Json -InputObject $p -Compress -Depth 3"""
    try:
        value = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if value.returncode or len(value.stdout) > 8 * 1024 * 1024:
            raise MigrationError(_PROCESS)
        records = json.loads(value.stdout.decode('utf-8-sig'))
        if not isinstance(records, list) or len(records) > 2048:
            raise MigrationError(_PROCESS)
        return records
    except MigrationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise MigrationError(_PROCESS) from None


def _process_guard(root):
    canonical = lambda value: os.path.normcase(os.path.abspath(value)).rstrip('\\/')
    expected = {canonical(root), canonical(root / 'EBWebView')}
    for record in _browser_processes():
        command = record.get('CommandLine') if isinstance(record, dict) else None
        if not command:
            raise MigrationError(_PROCESS)
        arguments = _windows_arguments(command)
        for index, argument in enumerate(arguments):
            name, separator, value = argument.partition('=')
            if name.casefold() not in ('--user-data-dir', '--user-data-folder', '--userdatafolder'):
                continue
            if not separator:
                value = arguments[index + 1] if index + 1 < len(arguments) else ''
            if not value or not os.path.isabs(value):
                raise MigrationError(_PROCESS)
            if canonical(value) in expected:
                raise MigrationError(_BUSY)


def _runtime_version():
    if os.name != 'nt':
        raise MigrationError(_VERSION)
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    try:
        loader = ctypes.WinDLL(str(root / 'tools/webview2/WebView2Loader.dll'))
        get_version = loader.GetAvailableCoreWebView2BrowserVersionString
        get_version.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_wchar_p)]
        get_version.restype = ctypes.c_long
        pointer = ctypes.c_wchar_p()
        if get_version(None, ctypes.byref(pointer)) != 0 or not pointer.value:
            raise MigrationError(_VERSION)
        try:
            version = pointer.value
        finally:
            free = ctypes.OleDLL('ole32').CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            free(ctypes.cast(pointer, ctypes.c_void_p))
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
            raise MigrationError(_VERSION)
        return version
    except MigrationError:
        raise
    except (OSError, AttributeError, ValueError):
        raise MigrationError(_VERSION) from None


def _structure(entries, readers):
    required = ('EBWebView/Local State', 'EBWebView/Default/Preferences', 'EBWebView/Last Version')
    if any(name not in readers for name in required):
        raise MigrationError(_STRUCTURE)
    if any(Path(name).name == 'Last Browser' for name in entries):
        raise MigrationError(_VERSION)
    for name in required[:2]:
        if entries[name]['info'].st_size > 16 * 1024 * 1024:
            raise MigrationError(_STRUCTURE)
        reader = readers[name]
        reader.rewind()
        try:
            value = json.loads(reader.read(entries[name]['info'].st_size + 1).decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            raise MigrationError(_STRUCTURE) from None
        if not isinstance(value, dict):
            raise MigrationError(_STRUCTURE)
    version_reader = readers[required[2]]
    version_reader.rewind()
    try:
        version = version_reader.read(256).decode('utf-8-sig').strip()
    except UnicodeError:
        raise MigrationError(_VERSION) from None
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version) or version != _runtime_version():
        raise MigrationError(_VERSION)
    databases = [name for name in ('EBWebView/Default/History', 'EBWebView/Default/Network/Cookies') if name in readers]
    if not databases:
        raise MigrationError(_STRUCTURE)
    for name in databases:
        readers[name].rewind()
        if readers[name].read(16) != b'SQLite format 3\0':
            raise MigrationError(_STRUCTURE)


def _same_tree(root, original, event=None):
    current = _inventory(root, event)
    if current.keys() != original.keys() or any(current[name]['identity'] != item['identity'] for name, item in original.items()):
        raise MigrationError(_CHANGED)


def inspect_profile(path):
    try:
        root = _path(path)
        entries = _inventory(root)
        _process_guard(root)
        with ExitStack() as stack:
            readers = _freeze(stack, root, entries)
            _structure(entries, readers)
            _same_tree(root, entries)
            _process_guard(root)
        return {'available': True, 'bytes': sum(item['info'].st_size for item in entries.values() if item['kind'] == 'file'),
                'files_count': len(readers), 'reason': '已确认本机相同 WebView2 Runtime 的离线配置结构；网站登录状态仍需实际打开后验证。'}
    except MigrationError as error:
        return {'available': False, 'bytes': 0, 'files_count': 0, 'reason': str(error)}
    except (OSError, ValueError, TypeError):
        return {'available': False, 'bytes': 0, 'files_count': 0, 'reason': _COPY_ERROR}


def copy_profile(source, dest, cancel_event=None, progress=None):
    try:
        _cancel(cancel_event)
        source, dest = _path(source), _path(dest)
        if source == dest or source.is_relative_to(dest) or dest.is_relative_to(source):
            raise MigrationError('浏览器配置来源和目标必须为相互独立的目录，原配置未修改。')
        if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            raise MigrationError('目标浏览器配置目录已有数据，不能覆盖；请选择空的独立目标。')
        entries = _inventory(source, cancel_event)
        _process_guard(source)
        _process_guard(dest)
        total = sum(item['info'].st_size for item in entries.values() if item['kind'] == 'file')
        hashes = {}
        with ExitStack() as stack:
            readers = _freeze(stack, source, entries, cancel_event)
            _structure(entries, readers)
            _same_tree(source, entries, cancel_event)
            if not dest.exists():
                dest.mkdir(parents=True, exist_ok=False)
            stack.enter_context(_FrozenFile(dest, directory=True))
            if any(dest.iterdir()):
                raise MigrationError('目标浏览器配置目录已变化，未覆盖已有数据。')
            for name, item in sorted(entries.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
                _cancel(cancel_event)
                if item['kind'] == 'directory':
                    (dest / name).mkdir(exist_ok=False)
            done = 0
            for name, reader in readers.items():
                _cancel(cancel_event)
                reader.rewind()
                digest = hashlib.sha256()
                with (dest / name).open('xb') as output:
                    while True:
                        _cancel(cancel_event)
                        block = reader.read(_CHUNK)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        done += len(block)
                        if progress:
                            progress({'stage': 'browser-copy', 'current': done, 'total': total,
                                      'message': '正在完整复制已确认的离线浏览器配置…'})
                    output.flush()
                    os.fsync(output.fileno())
                hashes[name] = digest.hexdigest()
                info = entries[name]['info']
                os.utime(dest / name, ns=(info.st_atime_ns, info.st_mtime_ns))
            # 复制完成前所有源文件句柄仍保持拒写/删；再完整读取源和目标验证内容。
            for stage, table, expected in (('browser-source-check', readers, hashes),):
                verified = 0
                for name, reader in table.items():
                    reader.rewind()
                    digest = hashlib.sha256()
                    while True:
                        _cancel(cancel_event)
                        block = reader.read(_CHUNK)
                        if not block:
                            break
                        digest.update(block)
                        verified += len(block)
                        if progress:
                            progress({'stage': stage, 'current': verified, 'total': total,
                                      'message': '正在重新验证源配置完整内容，保持离线读锁…'})
                    if digest.hexdigest() != expected[name]:
                        raise MigrationError(_CHANGED)
            _same_tree(source, entries, cancel_event)
            destination = _inventory(dest, cancel_event)
            if destination.keys() != entries.keys():
                raise MigrationError(_CHANGED)
            for name in entries:
                if destination[name]['kind'] != entries[name]['kind']:
                    raise MigrationError(_CHANGED)
            target_readers = _freeze(stack, dest, destination, cancel_event)
            _structure(destination, target_readers)
            verified = 0
            for name, reader in target_readers.items():
                reader.rewind()
                digest = hashlib.sha256()
                while True:
                    _cancel(cancel_event)
                    block = reader.read(_CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    verified += len(block)
                    if progress:
                        progress({'stage': 'browser-target-check', 'current': verified, 'total': total,
                                  'message': '正在验证新配置的完整内容和离线结构…'})
                if digest.hexdigest() != hashes[name]:
                    raise MigrationError(_COPY_ERROR)
            _same_tree(dest, destination, cancel_event)
            _process_guard(source)
            _process_guard(dest)
            _cancel(cancel_event)
        return {'hashes': hashes, 'bytes': total, 'files_count': len(hashes)}
    except (MigrationError, MigrationCancelled):
        raise
    except (OSError, ValueError, TypeError):
        raise MigrationError(_COPY_ERROR) from None
