import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from core import tool_manager as tools
from tests.tool_fixture import compile_fixture, copy_fixture
from utils.config import ConfigStore


@unittest.skipUnless(os.name == 'nt', 'Windows executable tests')
class ToolManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled_dir = tempfile.TemporaryDirectory()
        cls.compiled = compile_fixture(cls.compiled_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.compiled_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = tools.ToolManager(self.root / 'app', run_timeout=0.3, total_timeout=1.5)
        self.path_patch = patch('core.tool_manager.shutil.which', return_value=None)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def tool(self, relative='app/tools/yt-dlp.exe', mode='ok'):
        return copy_fixture(self.compiled, self.root / relative, mode)

    def test_portable_precedes_legacy_saved_path_and_path(self):
        portable = self.tool()
        old = self.tool('old/yt-dlp.exe', 'hang')
        with patch('core.tool_manager.shutil.which', return_value=str(old)):
            info = self.manager.detect('yt-dlp', str(old))
        self.assertTrue(info.found)
        self.assertEqual(info.path, str(portable))
        self.assertEqual(len(info.attempts), 1)

    def test_working_portable_tool_never_walks_unrelated_system_path(self):
        portable = self.tool()
        with patch('core.tool_manager.shutil.which',side_effect=AssertionError('PATH must be lazy')) as lookup:
            self.assertTrue(self.manager.detect('yt-dlp').found)
            self.assertEqual(self.manager.locate('yt-dlp'),str(portable.resolve()))
            lookup.assert_not_called()

    def test_custom_override_and_persistence(self):
        self.tool()
        custom = self.tool('custom/yt-dlp.exe', 'stderr')
        store = ConfigStore(self.root / 'config.json')
        store.save({'yt_dlp_path': str(custom), 'yt_dlp_mode': 'custom', 'ffmpeg_path': 'X:\\ffmpeg.exe'})
        settings = store.load()
        info = self.manager.detect('yt-dlp', settings['yt_dlp_path'], mode=settings['yt_dlp_mode'])
        self.assertEqual(info.path, str(custom))
        self.assertEqual(info.version, '2026.10.19')
        self.assertEqual(settings['ffmpeg_path'], 'X:\\ffmpeg.exe')

    def test_invalid_old_path_recovers_to_portable(self):
        portable = self.tool()
        info = self.manager.detect('yt-dlp', str(self.root / 'moved/yt-dlp.exe'), mode='custom')
        self.assertEqual(info.path, str(portable))
        self.assertTrue(info.found)
        self.assertIn('替代路径', info.warning)

    def test_missing_and_invalid_directory_are_distinct(self):
        self.assertEqual(self.manager.detect('yt-dlp').status, 'missing')
        self.manager.app_dir.mkdir()
        invalid = self.manager.detect('yt-dlp', str(self.manager.app_dir), mode='custom')
        self.assertEqual(invalid.status, 'invalid_path')

    def test_broken_executable_is_start_failure(self):
        path = self.root / 'broken.exe'
        path.write_bytes(b'not a Windows executable')
        self.assertEqual(self.manager.detect('yt-dlp', str(path), mode='custom').status, 'start_failed')

    def test_nonzero_exit_and_unrelated_version_fail(self):
        for mode in ('exit', 'wrong'):
            path = self.tool('custom/yt-dlp.exe', mode)
            info = self.manager.detect('yt-dlp', str(path), mode='custom')
            self.assertEqual(info.status, 'version_failed')
            self.assertEqual(info.return_code, 7 if mode == 'exit' else 0)

    def test_hung_process_is_bounded_and_fallback_verified(self):
        # 实际.NET夹具首次启动可能包含系统加载/扫描；仍使用严格有界的测试预算。
        self.manager.run_timeout = 0.8
        self.manager.total_timeout = 3.0
        hang = self.tool('custom/yt-dlp.exe', 'hang')
        portable = self.tool()
        started = time.monotonic()
        info = self.manager.detect('yt-dlp', str(hang), mode='custom')
        self.assertTrue(info.found)
        self.assertEqual(info.path, str(portable))
        self.assertEqual(info.attempts[0]['status'], 'timeout')
        self.assertLess(time.monotonic() - started, 3.5)

    def test_child_process_is_terminated_on_timeout(self):
        path = self.tool(mode='child')
        self.manager.run_timeout = 0.7
        self.assertEqual(self.manager.detect('yt-dlp').status, 'timeout')
        pid = int(Path(str(path) + '.pid').read_text())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
            finally:
                kernel.CloseHandle(handle)

    def test_cancel_ends_running_probe(self):
        self.tool(mode='hang')
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        started = time.monotonic()
        self.assertFalse(self.manager.detect('yt-dlp', cancel_event=cancel).found)
        timer.join()
        self.assertLess(time.monotonic() - started, 1.5)

    def test_start_timeout_cleans_up_late_process(self):
        self.tool()
        self.manager.start_timeout = 0.06
        original = subprocess.Popen
        processes = []
        created = threading.Event()
        def delayed(*args, **kwargs):
            time.sleep(0.2)
            process = original(*args, **kwargs)
            processes.append(process)
            created.set()
            return process
        with patch('core.tool_manager.subprocess.Popen', side_effect=delayed):
            self.assertEqual(self.manager.detect('yt-dlp').status, 'timeout')
            # 等待真实启动与后台终止，而非假定0.7秒足够覆盖CreateProcess调度。
            self.assertTrue(created.wait(5), '延迟启动进程没有在有界时间内返回')
            deadline = time.monotonic() + 3
            while processes[0].poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_path_fallback_is_validated(self):
        path = self.tool('system/yt-dlp.exe', 'wrong')
        with patch('core.tool_manager.shutil.which', return_value=str(path)):
            self.assertFalse(self.manager.detect('yt-dlp').found)
            Path(str(path) + '.mode').write_text('ok')
            self.assertTrue(self.manager.detect('yt-dlp').found)

    def test_cwd_and_meipass_do_not_define_root(self):
        path = self.tool()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(self.manager.detect('yt-dlp').path, str(path))
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(self.root / 'app/MediaFlow.exe')), \
                    patch.object(sys, '_MEIPASS', str(self.root / 'unpacked'), create=True):
                self.assertEqual(tools.application_directory(), self.root / 'app')
        finally:
            os.chdir(previous)

    def test_cache_reuses_version_and_rechecks_changed_file(self):
        path = self.tool()
        self.assertTrue(self.manager.detect('yt-dlp').found)
        with patch.object(self.manager, '_probe', wraps=self.manager._probe) as probe:
            self.assertTrue(self.manager.detect('yt-dlp', refresh=False).found)
            self.assertEqual(probe.call_count, 0)
            os.utime(path, ns=(time.time_ns(), time.time_ns()))
            self.assertTrue(self.manager.detect('yt-dlp', refresh=False).found)
            self.assertEqual(probe.call_count, 1)

    def test_all_tools_accept_stderr_version(self):
        for name in ('yt-dlp', 'ffmpeg', 'ffprobe'):
            self.tool('app/tools/' + name + '.exe', 'stderr')
            info = self.manager.detect(name)
            self.assertTrue(info.found, info.error)
            self.assertEqual(info.return_code, 0)
