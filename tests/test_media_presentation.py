from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QLabel

from core.discovery_models import MediaCandidate
from core.request_context import RequestContext
from ui.media_presentation import (display_text, duration_text, confidence_status,
                                   confidence_diagnostics, confidence_reasons, diagnostic_text)
from ui.main_window import MainWindow
from core.yt_dlp_manager import ToolInfo
from utils.config import ConfigStore


class MediaPresentationTests(unittest.TestCase):
    def test_duration_boundaries(self):
        for value, expected in ((.2, '0秒'), (19.5, '19秒'), (59, '59秒'), (60, '1分'),
                                (344, '5分44秒'), (3599, '59分59秒'), (3600, '1小时'),
                                (3608, '1小时8秒'), (3660, '1小时1分'),
                                (3661, '1小时1分1秒'), (3668, '1小时1分8秒'), (86400, '24小时')):
            with self.subTest(value=value):
                self.assertEqual(duration_text(value), expected)

    def test_invalid_duration_does_not_show_internal_values(self):
        for value in (None, '344', 0, -1, True, float('nan'), float('inf'), 10 ** 400):
            self.assertEqual(duration_text(value), '未知')

    def test_unknown_values_and_protocol_text(self):
        for value in (None, 'None', ' null ', 'Unknown', 'UNKNOWN', '--', ''):
            self.assertEqual(display_text(value), '未知')
        self.assertEqual(display_text('HLS_AES128'), 'HLS_AES128')
        self.assertEqual(display_text('none.mp4'), 'none.mp4')

    def test_display_thresholds_do_not_use_automatic_selection_threshold(self):
        for score, status in ((0, '低可信度'), (39, '低可信度'), (40, '需确认'),
                              (69, '需确认'), (70, '需确认'), (79, '需确认'), (80, '推荐'), (100, '推荐')):
            self.assertEqual(confidence_status(score), status)

    def test_diagnostics_read_evidence_without_mutating_candidate(self):
        candidate = MediaCandidate('bili', RequestContext('https://cdn.example/media'),
            content_type='video/mp4', confidenceScore=10, duration=344,
            metadata={'confidence_evidence': {'current_player': False},
                      'confidence_details': [{'points': 20, 'reason': '时长有效：344 秒'}]})
        before = deepcopy(candidate)
        reasons = confidence_reasons(candidate)
        self.assertIn('⚠ 非当前播放器', reasons)
        self.assertIn('⚠ 缺少页面上下文（未关联播放器）', reasons)
        self.assertIn('✓ 时长检测通过', reasons)
        self.assertFalse(any('344' in reason or '时长有效' in reason for reason in reasons))
        self.assertEqual(candidate, before)

    def test_duration_mismatch_requires_explicit_evidence(self):
        candidate = MediaCandidate('unknown', RequestContext('https://cdn.example/media'))
        self.assertNotIn('时长不匹配', confidence_diagnostics(candidate))
        candidate.metadata['duration_mismatch'] = True
        self.assertIn('时长不匹配', confidence_diagnostics(candidate))

    def test_verified_resolver_does_not_claim_missing_page_association(self):
        candidate = MediaCandidate('rou', RequestContext('https://cdn.example/index.png'),
            media_type='HLS', metadata={'resolver_verified': True})
        self.assertFalse(any('播放器' in text or '页面上下文' in text for text in confidence_diagnostics(candidate)))

    def test_inherited_current_player_evidence_is_not_claimed_missing(self):
        candidate = MediaCandidate('child', RequestContext('https://cdn.example/child.m3u8'),
            media_type='HLS', metadata={'confidence_details': [
                {'points': 40, 'reason': '当前主播放器 video.currentSrc'}]})
        self.assertFalse(any('播放器' in text or '页面上下文' in text for text in confidence_diagnostics(candidate)))
        candidate.metadata['confidence_evidence'] = {'current_player': False}
        self.assertIn('非当前播放器', confidence_diagnostics(candidate))

    def test_diagnostic_unknown_and_verified_duration(self):
        self.assertEqual(diagnostic_text('DRM：Unknown\nEncryption：None\n时长（秒）：344.0\nOrigin：--\nMETHOD：NONE'),
                         'DRM：未知\nEncryption：无\n时长：5分44秒\nOrigin：未知\nMETHOD：NONE')


class CompactHomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.detector_patch = patch.object(MainWindow, 'detect_tools')
        self.detector_patch.start()
        self.window = MainWindow(ConfigStore(Path(self.folder.name) / 'settings.json'))
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.queue.stop_all()
        self.window.close()
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (self.window.batch_controller.is_closed and
                                                    self.window.library_service.is_closed):
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(self.window.batch_controller.is_closed and self.window.library_service.is_closed)
        self.window.deleteLater()
        self.app.processEvents()
        self.detector_patch.stop()
        self.folder.cleanup()

    def test_home_uses_input_hint_without_duplicate_empty_area(self):
        self.assertEqual(self.window.url.placeholderText(), '请输入媒体URL，或使用自动查找。')
        self.assertFalse(any(label.text() == '粘贴媒体网址开始分析'
                             for label in self.window.findChildren(QLabel)))
        self.assertTrue(self.window.task_scroll.isHidden())
        self.window.input_tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(self.window.pause_queue_button.isVisible())
        self.assertTrue(self.window.remove_finished_button.isVisible())
        self.assertTrue(self.window.queue_summary.isVisible())
        self.assertEqual(self.window.input_tabs.tabText(1), '任务队列 (0)')

    def test_task_cards_expand_only_with_tasks_and_collapse_after_removal(self):
        self.window.yt_info = ToolInfo(path='unit-yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://media.example/ordinary-video')
        self.assertTrue(self.window.queue.paused)
        job = self.window.add_current_to_queue()
        self.assertIsNotNone(job)
        self.assertIn(job.id, self.window._task_cards)
        self.assertFalse(self.window.task_scroll.isHidden())
        self.assertIsNone(job.worker)
        self.window.queue.stop_all()
        self.window.remove_finished_jobs()
        self.app.processEvents()
        self.assertFalse(self.window.queue.jobs)
        self.assertFalse(self.window._task_cards)
        self.assertTrue(self.window.task_scroll.isHidden())
        self.assertEqual(self.window.input_tabs.tabText(1), '任务队列 (0)')


if __name__ == '__main__':
    unittest.main()
