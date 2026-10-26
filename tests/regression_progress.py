"""Live, disclosure-safe unittest timing; no arguments, locals or environment."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import unittest


def descendants(parent_pid):
    """Only names and IDs belonging to this worker, never command lines."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        row = Entry(); row.dwSize = ctypes.sizeof(row)
        more = kernel.Process32FirstW(handle, ctypes.byref(row)); entries = []
        while more:
            entries.append(dict(pid=row.th32ProcessID, parent_pid=row.th32ParentProcessID,
                                executable=Path(row.szExeFile).name))
            more = kernel.Process32NextW(handle, ctypes.byref(row))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the only normal end.
            raise ctypes.WinError(ctypes.get_last_error())
        owned = {parent_pid}; found = []
        while True:
            new = [r for r in entries if r['parent_pid'] in owned and r['pid'] not in owned]
            if not new:
                return found
            found += new; owned.update(r['pid'] for r in new)
    finally:
        kernel.CloseHandle(handle)


def wait_for_descendants_exit(parent_pid, *, timeout=3):
    """Observe asynchronous Windows job teardown, without killing other processes."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = descendants(parent_pid)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(.05)


class ProgressLog:
    def __init__(self, path, root, *, interval=15):
        self.path, self.root = Path(path), Path(root)
        self.stream = self.path.open('x', encoding='utf-8', buffering=1)
        self.started = time.monotonic(); self.current = None
        self.main_thread = threading.get_ident(); self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, args=(interval,), daemon=True)
        self.thread.start()

    def event(self, phase, **values):
        row = dict(phase=phase, utc=datetime.now(timezone.utc).isoformat(),
                   elapsed_seconds=round(time.monotonic()-self.started, 4), **values)
        with self.lock:
            self.stream.write(json.dumps(row, ensure_ascii=False)+'\n'); self.stream.flush()

    def _heartbeat(self, interval):
        while not self.stopping.wait(interval):
            frame = sys._current_frames().get(self.main_thread)
            frames = []
            for item in traceback.extract_stack(frame, limit=18):
                path = Path(item.filename)
                name = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.name
                frames.append(dict(file=name, line=item.lineno, function=item.name))
            try:
                children = descendants(os.getpid())
                error = None
            except OSError as failure:
                children = None
                error = dict(type=type(failure).__name__, winerror=getattr(failure, 'winerror', None))
            self.event('heartbeat', test=self.current, stack=frames,
                       owned_children=children, owned_children_error=error)

    def close(self):
        self.stopping.set(); self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('The test progress heartbeat did not stop within its bounded shutdown.')
        with self.lock:
            self.stream.close()


def result_type(progress):
    class Result(unittest.TextTestResult):
        def startTest(self, test):
            self.case_start = time.monotonic(); self.case_outcome = 'passed'
            progress.current = test.id(); progress.event('test_start', test=test.id())
            super().startTest(test)

        def addError(self, test, err):
            self.case_outcome = 'error'; super().addError(test, err)

        def addFailure(self, test, err):
            if self.case_outcome != 'error': self.case_outcome = 'failure'
            super().addFailure(test, err)

        def addSkip(self, test, reason):
            if self.case_outcome == 'passed': self.case_outcome = 'skipped'
            super().addSkip(test, reason)

        def addSubTest(self, test, subtest, err):
            if err is not None and self.case_outcome != 'error':
                self.case_outcome = 'failure'
            super().addSubTest(test, subtest, err)

        def addUnexpectedSuccess(self, test):
            if self.case_outcome not in ('error', 'failure'): self.case_outcome = 'unexpected_success'
            super().addUnexpectedSuccess(test)

        def addExpectedFailure(self, test, err):
            if self.case_outcome == 'passed': self.case_outcome = 'expected_failure'
            super().addExpectedFailure(test, err)

        def stopTest(self, test):
            progress.event('test_end', test=test.id(), outcome=self.case_outcome,
                case_seconds=round(time.monotonic()-self.case_start, 4),
                result_code=0 if self.case_outcome == 'passed' else None if self.case_outcome in ('skipped', 'expected_failure') else 1)
            progress.current = None; super().stopTest(test)
    return Result


def progress_summary(path):
    result = dict(tests_started=0, tests_finished=0, last_test=None)
    path = Path(path)
    if not path.is_file():
        return result
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # A killed writer may leave only its final line partial.
            if row.get('phase') == 'test_start':
                result['tests_started'] += 1; result['last_test'] = row['test']
            elif row.get('phase') == 'test_end':
                result['tests_finished'] += 1
    return result
