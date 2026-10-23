import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time
import unittest

from core.process_tree import ProcessTree, NO_WINDOW


def alive(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        running = kernel.WaitForSingleObject(handle, 0) == 0x102
        kernel.CloseHandle(handle)
        return running
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ProcessTreeTests(unittest.TestCase):
    def test_stops_parent_and_child(self):
        code = ('import subprocess,sys,time; '
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                'print(child.pid,flush=True); time.sleep(60)')
        process = subprocess.Popen([sys.executable, '-c', code], shell=False, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
        tree = ProcessTree(process)
        try:
            child_pid = int(process.stdout.readline().strip())
            self.assertTrue(alive(child_pid))
            tree.terminate()
            tree.close()
            deadline = time.monotonic() + 3
            while alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(alive(child_pid), '下载的子进程没有被终止')
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                tree.terminate()
            tree.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
