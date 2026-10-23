"""候选卡片的实际 Qt 离屏展示；不启动浏览器或调用下载器。"""
from copy import deepcopy
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from core.discovery_models import MediaCandidate
from core.request_context import RequestContext
from ui.media_cards import CandidateCard
from ui.media_presentation import confidence_reasons


class MediaCardsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.card = CandidateCard()

    def tearDown(self):
        self.card.deleteLater()
        self.app.processEvents()

    def columns(self, score=80):
        return ['', '当前正片', '1080p', '19秒', 'HLS', 'AES-128 / No',
                f'{score}/100 · 旧状态', 'Network', 'https://media.example/index.png']

    def render(self, columns=None, reasons=(), **kwargs):
        self.card.update_display(columns or self.columns(), reasons,
            kwargs.pop('choices', [('1080p', 'main')]), kwargs.pop('candidate_id', 'main'),
            kwargs.pop('checked', False), kwargs.pop('checkable', True),
            kwargs.pop('downloadable', True), **kwargs)

    def test_status_thresholds_and_tones_are_independent_of_download_permissions(self):
        for score, status, tone in ((0, '低可信度', 'low'), (39, '低可信度', 'low'),
                (40, '需确认', 'medium'), (70, '需确认', 'medium'),
                (79, '需确认', 'medium'), (80, '推荐', 'high'), (100, '推荐', 'high')):
            with self.subTest(score=score):
                self.render(self.columns(score), downloadable=False, checkable=False)
                self.assertEqual(self.card.confidence_label.text(), f'可信度 {score}/100 · {status}')
                self.assertEqual(self.card.confidence_label.property('tone'), tone)
                self.assertFalse(self.card.download_button.isEnabled())
                self.assertFalse(self.card.checked_box.isEnabled())

    def test_diagnostics_and_duration_evidence_are_visible_without_mutating_candidate(self):
        candidate = MediaCandidate('main', RequestContext('https://media.example/index.png'),
            media_type='HLS', duration=19.5, confidenceScore=50,
            metadata={'confidence_evidence': {'current_player': False},
                      'confidence_details': [{'reason': '时长有效：19.5 秒', 'points': 20}]})
        before = deepcopy(candidate)
        self.render(self.columns(50), confidence_reasons(candidate))
        self.assertIn('⚠ 非当前播放器', self.card.reasons_label.text())
        self.assertIn('⚠ 缺少页面上下文', self.card.reasons_label.text())
        self.assertIn('✓ 时长检测通过', self.card.reasons_label.toolTip())
        self.assertIn('✓ 时长检测通过', self.card.confidence_label.toolTip())
        self.assertIn('时长：19秒', self.card.meta_label.text())
        self.assertNotIn('时长有效', self.card.reasons_label.toolTip())
        self.assertEqual(candidate, before)

    def test_legacy_reasons_placeholders_and_sensitive_diagnostics(self):
        columns = self.columns()
        columns[1], columns[2], columns[5], columns[7] = None, 'Unknown', 'null', 'None'
        columns[8] = 'https://media.example/v?token=UNITSECRET'
        reasons = ['时长有效：19.5 秒', '✓ 时长有效：19.5 秒',
                   '⚠ 请求签名 UNITSECRET', 'Cookie: session=UNITPRIVATE']
        original = deepcopy((columns, reasons))
        self.render(columns, reasons, quality_summary='Unknown',
                    quality_reasons=['https://media.example/v?token=UNITSECRET'])
        self.assertEqual(self.card.title_label.text(), '未知')
        self.assertEqual(self.card.source_label.text(), '来源：未知')
        self.assertNotIn('Unknown', self.card.meta_label.text())
        self.assertNotIn('null', self.card.meta_label.text())
        self.assertEqual(self.card.reasons_label.text().count('✓ 时长检测通过'), 2)
        rendered = '\n'.join(label.text() + label.toolTip() for label in (
            self.card.title_label, self.card.source_label, self.card.meta_label,
            self.card.reasons_label, self.card.confidence_label))
        self.assertNotIn('UNITSECRET', rendered)
        self.assertNotIn('UNITPRIVATE', rendered)
        self.assertEqual((columns, reasons), original)

    def test_refresh_keeps_selection_and_download_signals_unchanged(self):
        checked, quality, downloads = [], [], []
        self.card.checked.connect(checked.append)
        self.card.quality_changed.connect(quality.append)
        self.card.download_requested.connect(lambda: downloads.append(True))
        self.render(choices=[('720p', 'small'), ('1080p', 'main')], checked=True)
        self.assertTrue(self.card.checked_box.isChecked())
        self.assertEqual(self.card.quality_combo.currentData(), 'main')
        self.assertFalse(self.card.quality_combo.isHidden())
        self.assertEqual((checked, quality, downloads), ([], [], []))
        self.card.quality_combo.setCurrentIndex(0)
        self.card.checked_box.setChecked(False)
        self.card.download_button.click()
        self.assertEqual((checked, quality, downloads), ([False], ['small'], [True]))


if __name__ == '__main__':
    unittest.main()
