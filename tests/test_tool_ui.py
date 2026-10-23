import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from core.tool_manager import ToolManager, ToolInfo
from core.downloader import build_arguments
from tests.tool_fixture import compile_fixture, copy_fixture
from ui.main_window import MainWindow
from utils.config import ConfigStore


@unittest.skipUnless(os.name == 'nt', 'Windows tool UI tests')
class ToolUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.compiled_dir = tempfile.TemporaryDirectory()
        cls.compiled = compile_fixture(cls.compiled_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.compiled_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('yt-dlp', 'ffmpeg', 'ffprobe'):
            copy_fixture(self.compiled, self.root / 'tools' / (name + '.exe'))
        self.manager = ToolManager(self.root, run_timeout=0.5, total_timeout=1.5)
        self.patches = [patch('core.tool_manager._MANAGER', self.manager),
                        patch('core.tool_manager.shutil.which', return_value=None)]
        for item in self.patches:
            item.start()
        self.store = ConfigStore(self.root / 'settings.json')
        self.window = MainWindow(self.store)

    def wait_for(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while not predicate():
            self.app.processEvents()
            time.sleep(0.005)
            if time.monotonic() > deadline:
                self.fail('tool UI operation did not complete within its bound')
        self.app.processEvents()

    def detect(self):
        self.window.detect_tools()
        self.wait_for(lambda: self.window.detector is None)

    def tearDown(self):
        if self.window.detector:
            self.window.detector.cancel()
            self.wait_for(lambda: self.window.detector is None)
        self.window.close()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_three_rows_and_no_duplicate_detection(self):
        self.window.detect_tools()
        first = self.window.detector
        self.window.detect_tools()
        self.assertIs(self.window.detector, first)
        self.wait_for(lambda: self.window.detector is None)
        self.assertTrue(all(info.found for info in (self.window.yt_info, self.window.ff_info, self.window.probe_info)))
        self.assertEqual(self.window.tool_table.rowCount(), 3)
        self.assertEqual(self.window.tool_table.item(2, 2).text(), '可用')
        self.assertEqual(self.store.load()['yt_dlp_path'], '')

    def test_timeout_keeps_ui_alive_and_does_not_block_queue_editing(self):
        (self.root / 'tools/yt-dlp.exe.mode').write_text('hang')
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        self.detect()
        timer.stop()
        self.assertEqual(self.window.yt_info.status, 'timeout')
        self.assertGreater(len(ticks), 8)
        self.window.url.setText('https://example.com/video.mp4')
        self.window.output_directory.setText(str(self.root / 'output'))
        request = self.window._collect_request()
        job = self.window.queue.add(request)
        self.assertEqual(job.state, 'queued')
        with self.assertRaises(ValueError):
            build_arguments(request)  # Actual launch still requires the backend tool.

    def test_manual_ffprobe_persists_then_reset_restores_auto(self):
        self.detect()
        custom = copy_fixture(self.compiled, self.root / 'custom/ffprobe.exe')
        with patch('ui.main_window.QFileDialog.getOpenFileName', return_value=(str(custom), '')):
            self.window.select_tool('ffprobe')
        self.wait_for(lambda: self.window.detector is None)
        settings = self.store.load()
        self.assertEqual(settings['ffprobe_path'], str(custom))
        self.assertEqual(settings['ffprobe_mode'], 'custom')
        self.window.tool_choice.setCurrentIndex(2)
        self.window.reset_tool_path()
        self.wait_for(lambda: self.window.detector is None)
        self.assertEqual(self.window.probe_info.path, str(self.root / 'tools/ffprobe.exe'))
        self.assertEqual(self.store.load()['ffprobe_mode'], 'auto')

    def test_close_cancels_detection_without_waiting_for_execution_timeout(self):
        (self.root / 'tools/yt-dlp.exe.mode').write_text('hang')
        self.manager.run_timeout = 60
        self.window.detect_tools()
        started = time.monotonic()
        self.window.close()
        self.wait_for(lambda: self.window.detector is None, 4)
        self.assertLess(time.monotonic() - started, 3)

    def test_invalid_custom_path_is_cleared_only_after_successful_fallback(self):
        self.window.settings.update(yt_dlp_path=str(self.root / 'missing/yt-dlp.exe'), yt_dlp_mode='custom')
        self.detect()
        self.assertTrue(self.window.yt_info.found)
        self.assertEqual(self.store.load()['yt_dlp_mode'], 'auto')
        self.assertIn('替代路径', self.window.tool_table.item(0, 4).text())
