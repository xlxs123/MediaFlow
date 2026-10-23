"""只验证候选展示；使用实际 Qt 卡片和既有选择入口，不启动网络或修改评分。"""
from copy import deepcopy
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from core.discovery_models import MediaCandidate
from core.request_context import RequestContext
from tests.test_mediaflow_ui import media
from tests.test_rou_current_ui import DialogHarness, verified


class CandidateDisplayUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DialogHarness()
        self.dialog._candidate_cards = {}
        self.card_content = QWidget(self.dialog)
        self.dialog.candidate_card_layout = QVBoxLayout(self.card_content)
        self.dialog.candidate_empty_state = QLabel('没有候选', self.card_content)
        self.dialog.candidate_card_layout.addWidget(self.dialog.candidate_empty_state)
        self.dialog.candidate_card_layout.addStretch()
        self.dialog.include_low.toggled.connect(self.dialog._render_candidates)

    def tearDown(self):
        self.dialog._closing = True
        self.dialog.deleteLater()
        self.app.processEvents()

    def display(self, *candidates):
        self.dialog._update_candidates(list(candidates), 'BLOB_MSE', self.dialog.worker.generation)

    def select(self, name):
        self.dialog._select_card(name)

    def test_bilibili_low_candidates_retained_and_expandable(self):
        # 当前真实 B站 MSE 捕获的分数分布；不伪造播放器关联证据。
        candidates = [MediaCandidate(f'bili-{i}', RequestContext(f'https://cdn.example/opaque-{i}'),
            media_type='Direct', confirmed=True, content_type='video/mp4', confidenceScore=score,
            metadata={'confidence_evidence': {'current_player': False}})
            for i, score in enumerate((40, 10, 0, 0))]
        self.display(*candidates)
        self.assertEqual(len(self.dialog._candidates), 4)
        self.assertEqual(len(self.dialog._candidate_cards), 4)
        self.assertEqual(len(self.dialog._visible_candidates()), 1)
        self.assertIn('低可信度（3）', self.dialog.include_low.text())
        self.assertIn('找到 4 个候选', self.dialog.status_label.text())
        low_card = self.dialog._candidate_cards['bili-1']
        self.assertTrue(low_card.isHidden())
        self.dialog.include_low.setChecked(True)
        self.assertEqual(len(self.dialog._visible_candidates()), 4)
        self.assertIs(self.dialog._candidate_cards['bili-1'], low_card)
        self.assertFalse(low_card.isHidden())
        self.assertIn('非当前播放器', low_card.reasons_label.text())
        self.assertIn('缺少页面上下文', low_card.reasons_label.text())
        self.select('bili-1')
        self.assertIn('状态：低可信度', self.dialog.details.toPlainText())
        self.assertTrue(self.dialog.confirm_button.isEnabled())

    def test_douyin_current_keeps_original_selection_and_preloads_can_be_inspected(self):
        main, preload = media('douyin-current', current=True), media('douyin-preload', visible=False)
        preload.confidenceScore = 10
        payload = deepcopy(main.download_payload())
        self.display(preload, main)
        self.assertEqual(self.dialog.selected_candidate().id, main.id)
        self.assertEqual(self.dialog.selected_candidate().download_payload(), payload)
        self.assertIn('推荐', self.dialog._candidate_cards[main.id].confidence_label.text())
        self.assertTrue(self.dialog._candidate_cards[preload.id].isHidden())
        self.dialog.include_low.setChecked(True)
        self.assertEqual(self.dialog.selected_candidate().id, main.id)
        self.assertEqual([g.selected_candidate.id for g in self.dialog._groups if g.selected], [main.id])

    def test_three_sections_have_exact_boundaries(self):
        candidates = []
        for score in (0, 39, 40, 79, 80, 100):
            candidate = media(str(score))
            candidate.confidenceScore = score
            candidates.append(candidate)
        self.display(*candidates)
        self.assertEqual(self.dialog._confidence_headers[0].text(), '推荐（2）')
        self.assertEqual(self.dialog._confidence_headers[1].text(), '可确认（2）')
        self.assertIn('低可信度（2）', self.dialog.include_low.text())
        self.assertEqual(self.dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole), '100')
        for name, expected in (('39', '低可信度'), ('40', '需确认'), ('79', '需确认'), ('80', '推荐')):
            self.assertIn(expected, self.dialog._candidate_cards[name].confidence_label.text())
        self.assertEqual(len(self.dialog._visible_candidates()), 4)

    def test_normal_hls_keeps_decoded_manifest_and_chinese_duration(self):
        main = verified()
        main.duration = 344
        payload = deepcopy(main.download_payload())
        self.display(main)
        self.assertEqual(self.dialog.table.item(0, 3).text(), '5分44秒')
        self.assertIn('时长：5分44秒', self.dialog.details.toPlainText())
        self.assertEqual(self.dialog.selected_candidate().download_payload(), payload)
        self.assertEqual(self.dialog.selected_candidate().media_type, 'HLS')
        self.assertTrue(self.dialog.confirm_button.isEnabled())

    def test_same_group_low_variant_not_lost_by_quality_filter(self):
        high = media('high', current=True, metadata={'video_id': 'same'})
        low = media('low', height=480, metadata={'video_id': 'same'})
        low.confidenceScore = 10
        self.display(high, low)
        self.assertEqual(len(self.dialog._groups), 1)
        self.assertIn(low.id, self.dialog._candidate_cards)
        self.assertTrue(self.dialog._candidate_cards[low.id].isHidden())
        self.dialog.include_low.setChecked(True)
        self.select(low.id)
        self.assertEqual(self.dialog.selected_candidate().id, low.id)
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertEqual([g.selected_candidate.id for g in self.dialog._groups if g.selected], [high.id])

    def test_protocol_excluded_candidate_is_retained_as_diagnostic_without_download(self):
        thumbnail = MediaCandidate('thumbnail', RequestContext('https://cdn.example/cover'),
            media_type='Direct', content_type='image/png', confidenceScore=0, confirmed=False,
            metadata={'resource_role': 'thumbnail'})
        self.display(thumbnail)
        self.assertIn(thumbnail.id, self.dialog._candidate_cards)
        self.assertFalse(self.dialog._groups)
        self.dialog.include_low.setChecked(True)
        self.select(thumbnail.id)
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        self.assertFalse(self.dialog._candidate_cards[thumbnail.id].download_button.isEnabled())

    def test_all_low_is_not_reported_as_no_media(self):
        candidate = media('low')
        candidate.confidenceScore = 0
        self.display(candidate)
        self.assertTrue(self.dialog.candidate_empty_state.isHidden())
        self.assertIn('找到 1 个候选', self.dialog.status_label.text())
        self.assertIn('展开查看', self.dialog.status_label.text())
        self.assertFalse(self.dialog.include_low.isHidden())
        self.assertFalse(self.dialog.include_low.isChecked())

    def test_context_missing_reason_does_not_fake_duration_mismatch(self):
        candidate = media('uncertain', context_complete=False)
        self.display(candidate)
        self.select(candidate.id)
        reasons = self.dialog._candidate_cards[candidate.id].reasons_label.toolTip()
        self.assertIn('缺少可复现的请求上下文', reasons)
        self.assertNotIn('时长不匹配', reasons)

    def test_display_never_changes_confidence_evidence_or_download_payload(self):
        candidates = [media('main', current=True), media('weak', visible=False)]
        snapshots = [(c.confidenceScore, deepcopy(c.confidenceReasons),
                      deepcopy(c.metadata['confidence_details']), deepcopy(c.download_payload())) for c in candidates]
        self.display(*candidates)
        self.dialog.include_low.setChecked(True)
        self.dialog.include_low.setChecked(False)
        self.assertEqual([(c.confidenceScore, c.confidenceReasons, c.metadata['confidence_details'], c.download_payload())
                          for c in candidates], snapshots)

    def test_more_than_old_twelve_and_128_caps_are_not_dropped_by_display(self):
        candidates = [MediaCandidate(str(i), RequestContext(f'https://cdn.example/{i}'),
                                     confidenceScore=10, confirmed=True, media_type='Direct') for i in range(130)]
        self.display(*candidates)
        self.assertEqual(len(self.dialog._candidate_cards), 130)
        self.dialog.include_low.setChecked(True)
        self.assertEqual(len(self.dialog._visible_candidates()), 130)

    def test_removing_old_generation_removes_stale_cards(self):
        self.display(media('previous'))
        self.display(media('next', current=True))
        self.assertEqual(set(self.dialog._candidate_cards), {'next'})
        self.assertEqual(self.dialog._confidence_headers[0].text(), '推荐（1）')

    def test_candidate_reason_tooltip_redacts_request_credentials(self):
        candidate = MediaCandidate('private', RequestContext('https://cdn.example/video?token=SIGNEDSECRET',
            headers={'Cookie': 'session=COOKIETESTSECRET'}), media_type='Direct', confirmed=True,
            confidenceScore=40, confidenceReasons=['会话 COOKIETESTSECRET，签名 SIGNEDSECRET'])
        self.display(candidate)
        card = self.dialog._candidate_cards[candidate.id]
        rendered = card.reasons_label.text() + card.reasons_label.toolTip() + card.confidence_label.toolTip()
        self.assertNotIn('COOKIETESTSECRET', rendered)
        self.assertNotIn('SIGNEDSECRET', rendered)


if __name__ == '__main__':
    unittest.main()
