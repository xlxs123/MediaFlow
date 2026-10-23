import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from ui.thumbnail_service import ThumbnailService


VIDEO = {'format_name': 'mov,mp4', 'duration': 10, 'video_streams': [{'index': 0, 'codec_type': 'video'}]}


class ThumbnailServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = ThumbnailService()

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('thumbnail worker did not finish')

    def tearDown(self):
        self.service.cancel()
        self.pump(lambda: not self.service.busy)
        self.service.deleteLater()
        self.app.processEvents()

    def test_serial_workers_run_off_gui_thread_and_preserve_validation_input(self):
        calls = []
        original_thread = threading.get_ident()

        def make(path, **kwargs):
            calls.append((path, threading.get_ident()))
            kwargs['validation']['duration'] = 999
            time.sleep(.03)
            return {'status': 'generated', 'path': path + '.jpg', 'message': 'ok', 'attempts': [1]}

        with patch('ui.thumbnail_service.generate_thumbnail', side_effect=make):
            for number in range(3):
                self.assertTrue(self.service.submit(number, [str(number)], [VIDEO], 'ffmpeg'))
            self.assertFalse(self.service.submit(0, ['0'], [VIDEO], 'ffmpeg'))
            self.pump(lambda: not self.service.busy)
        self.assertEqual([call[0] for call in calls], ['0', '1', '2'])
        self.assertTrue(all(thread != original_thread for _, thread in calls))
        self.assertEqual(VIDEO['duration'], 10)
        self.assertTrue(all(result['status'] == 'generated' for result in self.service.results.values()))

    def test_missing_or_mismatched_validation_never_starts_ffmpeg(self):
        with patch('ui.thumbnail_service.generate_thumbnail') as make:
            for validations in (None, [], [{}], [VIDEO, VIDEO]):
                self.assertFalse(self.service.submit(0, ['video.mp4'], validations, 'ffmpeg'))
            make.assert_not_called()

    def test_unexpected_thumbnail_exception_is_contained(self):
        with patch('ui.thumbnail_service.generate_thumbnail', side_effect=OSError('decoder failure')):
            self.service.submit(0, ['video.mp4'], [VIDEO], 'ffmpeg')
            self.pump(lambda: not self.service.busy)
        self.assertEqual(self.service.results[0]['status'], 'failed')

    def test_shutdown_cancels_running_worker_and_discards_waiting_cover(self):
        started = threading.Event()
        calls = []

        def make(path, **kwargs):
            calls.append(path)
            started.set()
            kwargs['cancel_event'].wait(3)
            return {'status': 'cancelled', 'path': '', 'message': 'cancelled', 'attempts': []}

        with patch('ui.thumbnail_service.generate_thumbnail', side_effect=make):
            self.service.submit(0, ['active.mp4'], [VIDEO], 'ffmpeg')
            self.service.submit(1, ['pending.mp4'], [VIDEO], 'ffmpeg')
            self.pump(started.is_set)
            self.service.cancel()
            self.pump(lambda: not self.service.busy)
        self.assertEqual(calls, ['active.mp4'])
        self.assertTrue(all(result['status'] == 'cancelled' for result in self.service.results.values()))
