"""不通过 Shell 管理子进程，Windows 优先使用 Job Object。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import subprocess

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class ProcessTree:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._job = None
        self._kernel = None
        if os.name == 'nt':
            self._assign_job()

    def _assign_job(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_ulonglong) for field in ('ReadOperationCount', 'WriteOperationCount',
                       'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(job)
            return
        if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(self.process._handle))):
            kernel.CloseHandle(job)
            return
        self._job, self._kernel = job, kernel

    def terminate(self):
        if self._job:
            self._kernel.TerminateJobObject(self._job, 1)
        elif os.name == 'nt' and self.process.poll() is None:
            taskkill = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32' / 'taskkill.exe'
            try:
                subprocess.run([str(taskkill), '/PID', str(self.process.pid), '/T', '/F'], shell=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        elif os.name != 'nt' and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
            self.process.wait(timeout=3)

    def close(self):
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
