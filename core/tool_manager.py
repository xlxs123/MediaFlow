"""Portable tool paths and bounded, cancellable probes; call off the UI thread."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from core.process_tree import NO_WINDOW, ProcessTree


def application_directory():
    # Frozen __file__ / _MEIPASS are unpacked resources, not the release directory.
    return (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1])


APP_DIR = application_directory()
TOOL_FLAGS = {'yt-dlp': '--version', 'ffmpeg': '-version', 'ffprobe': '-version'}
STATUS_LABELS = {'unchecked': '未检测', 'checking': '正在检测', 'available': '可用',
                 'missing': '文件不存在', 'start_failed': '启动失败', 'timeout': '检测超时',
                 'version_failed': '版本检测失败', 'invalid_path': '路径无效'}
START_TIMEOUT, RUN_TIMEOUT, TOTAL_TIMEOUT = 3.0, 8.0, 20.0


@dataclass
class ToolInfo:
    path: str = ''
    version: str = ''
    error: str = ''
    status: str = 'unchecked'
    source: str = ''
    warning: str = ''
    return_code: int | None = None
    attempts: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.status == 'unchecked' and self.path and self.version and not self.error:
            self.status = 'available'

    @property
    def found(self):
        return self.status == 'available' and bool(self.path and self.version and not self.error)

    @property
    def label(self):
        return STATUS_LABELS[self.status]


def _stop(process, tree):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                pass
        tree.terminate()  # Also tear down PyInstaller launcher children.
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
    finally:
        tree.close()


def _version(name, stdout, stderr):
    for line in (stdout + '\n' + stderr).splitlines():
        line = line.strip()
        if name == 'yt-dlp':
            if re.fullmatch(r'\d{4}\.\d{2}\.\d{2}(?:[.\w+-]*)', line):
                return line
        else:
            match = re.match(re.escape(name) + r' version (\S+)', line)
            if match:
                return match[1]
    return ''


class ToolManager:
    def __init__(self, app_dir=None, *, start_timeout=START_TIMEOUT, run_timeout=RUN_TIMEOUT,
                 total_timeout=TOTAL_TIMEOUT):
        self.app_dir = Path(app_dir) if app_dir is not None else application_directory()
        self.start_timeout, self.run_timeout, self.total_timeout = start_timeout, run_timeout, total_timeout
        self._locks = {name: threading.Lock() for name in TOOL_FLAGS}
        self._cache = {}

    def candidates(self, name, configured='', *, mode='auto', sibling='', include_path=True):
        if name not in TOOL_FLAGS:
            raise ValueError('未知工具。')
        filename = name + ('.exe' if os.name == 'nt' else '')
        portable = [(self.app_dir / 'tools' / filename, 'portable')]
        custom = [(Path(configured).expanduser(), 'custom')] if configured else []
        # Only an explicit manual selection overrides the portable default. Older
        # auto-saved absolute paths remain fallbacks instead of pinning an old EXE.
        paths = custom + portable if mode == 'custom' else portable + custom
        if sibling:
            paths.append((Path(sibling).parent / filename, 'sibling'))
        paths.append((self.app_dir / filename, 'application'))
        if not getattr(sys, 'frozen', False):
            paths.append((self.app_dir / '.venv' / 'Scripts' / filename, 'environment'))
        system = shutil.which(name) if include_path else None
        if system:
            paths.append((Path(system), 'PATH'))
        unique = {}
        for path, source in paths:
            if not path.is_absolute():
                path = self.app_dir / path
            unique.setdefault(os.path.normcase(str(path)), (path, source))
        return list(unique.values())

    def _iter_candidates(self, name, configured='', *, mode='auto', sibling=''):
        known = self.candidates(name, configured, mode=mode, sibling=sibling, include_path=False)
        yield from known
        # Looking through PATH can touch slow or disconnected directories. A
        # working portable/custom tool must return before that fallback runs.
        system = shutil.which(name)
        if system:
            path = Path(system)
            if not path.is_absolute():
                path = self.app_dir / path
            if os.path.normcase(str(path)) not in {os.path.normcase(str(p)) for p, _ in known}:
                yield path, 'PATH'

    @staticmethod
    def path_error(path):
        try:
            if not path.exists():
                return 'missing'
            if not path.is_file() or (os.name == 'nt' and path.suffix.lower() != '.exe'):
                return 'invalid_path'
        except (OSError, ValueError):
            return 'invalid_path'
        return ''

    def locate(self, name, configured='', *, mode='auto', sibling=''):
        """Locate a file only; existence does not claim availability."""
        for path, source in self._iter_candidates(name, configured, mode=mode, sibling=sibling):
            if not self.path_error(path):
                return str(path.resolve())
        return ''

    def _probe(self, name, path, source, cancel, budget):
        info = ToolInfo(str(path), status='checking', source=source)
        done, released, ownership = threading.Event(), threading.Event(), threading.Lock()
        holder = {'abandoned': False}
        stdout, stderr = tempfile.TemporaryFile(), tempfile.TemporaryFile()

        def start():
            process, tree = None, None
            try:
                process = subprocess.Popen([str(path), TOOL_FLAGS[name]], shell=False,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
                tree = ProcessTree(process)
            except (OSError, ValueError) as error:
                holder['error'] = type(error).__name__
                holder['error_code'] = getattr(error, 'winerror', None) or getattr(error, 'errno', None)
            with ownership:
                abandoned = holder['abandoned']
                holder.update(process=process, tree=tree)
                done.set()
            if abandoned:
                if process:
                    _stop(process, tree)
                stdout.close()
                stderr.close()
                released.set()

        # OS process creation can stall independently of the tool. A late creation
        # stays owned by this daemon and is immediately terminated on return.
        threading.Thread(target=start, daemon=True, name='MediaFlow-tool-start').start()
        started = time.monotonic()
        while not done.wait(0.03):
            if cancel.is_set() or time.monotonic() - started >= min(self.start_timeout, budget):
                with ownership:
                    if done.is_set():
                        break
                    holder['abandoned'] = True
                if cancel.is_set():
                    released.wait(1.0)
                return replace(info, status='unchecked' if cancel.is_set() else 'timeout',
                    error='检测已取消。' if cancel.is_set() else f'{path.name} 启动阶段超时，请检查安全软件或改用随包工具。')
        process, tree = holder['process'], holder['tree']
        try:
            if 'error' in holder:
                code = holder.get('error_code')
                return replace(info, status='start_failed',
                    error=f'{path.name} 存在，但进程启动失败（系统错误 {code}），请检查文件是否损坏或被安全软件拦截。')
            execution_deadline = min(started + budget, time.monotonic() + self.run_timeout)
            while process.poll() is None:
                if cancel.wait(0.03) or time.monotonic() >= execution_deadline:
                    _stop(process, tree)
                    return replace(info, status='unchecked' if cancel.is_set() else 'timeout',
                        error='检测已取消。' if cancel.is_set() else f'{path.name} 已启动，但版本检测超时；可重新检测或恢复自动查找。')
            stdout.seek(0)
            stderr.seek(0)
            version = _version(name, stdout.read(65536).decode('utf-8', errors='replace'),
                               stderr.read(65536).decode('utf-8', errors='replace'))
            if process.returncode != 0 or not version:
                return replace(info, status='version_failed', return_code=process.returncode,
                    error=f'{path.name} 版本检测失败（退出码 {process.returncode}），请选择对应工具的可执行文件。')
            return replace(info, version=version, status='available', return_code=0)
        finally:
            if tree:
                tree.close()
            stdout.close()
            stderr.close()

    def detect(self, name, configured='', *, mode='auto', sibling='', cancel_event=None, refresh=True):
        cancel = cancel_event or threading.Event()
        lock = self._locks[name]
        while not lock.acquire(timeout=0.05):
            if cancel.is_set():
                return ToolInfo(error='检测已取消。')
        try:
            attempts, failures = [], []
            started = time.monotonic()
            for path, source in self._iter_candidates(name, configured, mode=mode, sibling=sibling):
                if cancel.is_set():
                    return ToolInfo(error='检测已取消。', attempts=attempts)
                status = self.path_error(path)
                if status:
                    if source in ('custom', 'portable'):
                        message = (f'{path.name} 不存在，请检查发布目录中的 tools 文件夹。' if status == 'missing'
                                   else f'{path.name} 路径无效，请选择可执行文件。')
                        failures.append(ToolInfo(str(path), error=message, status=status, source=source))
                    continue
                stat = path.stat()
                key = (name, str(path), stat.st_size, stat.st_mtime_ns)
                cached = self._cache.get(key)
                budget = self.total_timeout - (time.monotonic() - started)
                if budget <= 0:
                    break
                info = (replace(cached, source=source) if cached and not refresh else
                        self._probe(name, path, source, cancel, budget))
                attempts.append({'path': str(path), 'source': source, 'status': info.status,
                                 'error': info.error, 'return_code': info.return_code})
                if info.found:
                    self._cache[key] = replace(info, attempts=[])
                    warning = ''
                    if failures or any(a['status'] != 'available' for a in attempts):
                        previous = next((f.error for f in failures if f.source == 'custom'),
                                        failures[0].error if failures else '')
                        warning = '已自动使用可用的替代路径。' + previous
                    return replace(info, warning=warning, attempts=attempts)
                failures.append(info)
            failed = next((f for f in failures if f.status not in ('missing', 'invalid_path')),
                          failures[0] if failures else ToolInfo(error=f'未检测到 {name}，请检查 tools 文件夹。', status='missing'))
            return replace(failed, attempts=attempts)
        except (OSError, ValueError):
            return ToolInfo(str(configured), error='工具路径无效或无法读取，请恢复自动查找。', status='invalid_path')
        finally:
            lock.release()


_MANAGER = ToolManager()


def get_tool_manager():
    return _MANAGER
