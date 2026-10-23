"""真实性分数的 Qt 选择/确认回归；浏览器实测另见 mediaflow_smoke。"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from core.media_confidence import score_candidate
from tests.test_rou_current_ui import DialogHarness, direct


def media(name, *, current=False, visible=True, duration=19, height=720, **fields):
    candidate = direct(name, url=f'https://cdn.example/{name}.mp4', duration=duration,
                       resolution=(height * 16 // 9, height), content_type='video/mp4', **fields)
    evidence = {'current_player': current, 'associated': True, 'visible': visible, 'hidden': not visible}
    candidate.metadata['confidence_evidence'] = evidence
    rating = score_candidate(candidate, evidence)
    candidate.confidenceScore, candidate.confidenceReasons = rating.score, rating.reasons
    candidate.metadata['confidence_details'] = rating.details
    return candidate


class MediaFlowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DialogHarness()
        self.imported, self.batches = [], []
        self.dialog.media_selected.connect(self.imported.append)
        self.dialog.batch_requested.connect(lambda groups, options: self.batches.append(groups))

    def tearDown(self):
        self.dialog._closing = True
        self.dialog.deleteLater()
        self.app.processEvents()

    def display(self, *candidates):
        self.dialog._update_candidates(list(candidates), 'HTML5_VIDEO', self.dialog.worker.generation)

    def select(self, name):
        row = next(row for row in range(self.dialog.table.rowCount())
                   if self.dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == name)
        self.dialog.table.selectRow(row)
        self.dialog._selection_changed(user=True)

    def test_current_720p_wins_visible_preloaded_4k_and_only_main_is_checked(self):
        main, preload = media('main', current=True), media('preload', height=2160)
        self.display(preload, main)
        self.assertEqual(self.dialog.selected_candidate().id, 'main')
        self.assertEqual(self.dialog.table.item(0, 6).text(), '100/100 · 推荐')
        self.assertEqual([g.selected_candidate.id for g in self.dialog._groups if g.selected], ['main'])
        self.assertIn('✓ 当前主播放器 video.currentSrc (+40)', self.dialog.details.toPlainText())
        self.assertIn('时长：19秒', self.dialog.details.toPlainText())

    def test_equal_visible_players_require_choice_and_are_not_batch_preselected(self):
        self.display(media('one'), media('two'))
        self.assertIsNone(self.dialog.selected_candidate())
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        self.assertFalse(any(group.selected for group in self.dialog._groups))
        self.select('two')
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertEqual(self.dialog.selected_candidate().id, 'two')

    def test_two_frames_with_equal_current_players_do_not_batch_select_first(self):
        self.display(media('frame-one', current=True), media('frame-two', current=True))
        self.assertIsNone(self.dialog.selected_candidate())
        self.assertFalse(any(group.selected for group in self.dialog._groups))
        self.assertFalse(self.dialog.download_all_button.isEnabled())

    def test_score_40_requires_explicit_confirmation_and_preserves_payload(self):
        candidate = media('uncertain', visible=False)
        candidate.confidenceScore = 40
        original = deepcopy(candidate.download_payload())
        self.display(candidate)
        self.assertIsNone(self.dialog.selected_candidate())
        self.select('uncertain')
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            self.dialog.confirm()
            question.assert_called_once()
        self.assertEqual(self.imported, [])
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog.confirm()
        self.assertEqual(len(self.imported), 1)
        self.assertEqual(self.imported[0].download_payload(), original)

    def test_score_69_batch_is_confirmed_and_cancel_does_not_enqueue(self):
        candidate = media('uncertain')
        candidate.confidenceScore = 69
        self.display(candidate)
        self.dialog._group_checks[self.dialog._groups[0].id] = True
        self.dialog._batch_pending = True
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            self.dialog._batch_ready([candidate], self.dialog.worker.generation)
        self.assertEqual(self.batches, [])
        self.assertFalse(self.dialog._batch_pending)
        self.dialog._batch_pending = True
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog._batch_ready([candidate], self.dialog.worker.generation)
        self.assertEqual(len(self.batches), 1)

    def test_score_39_can_select_and_import_after_explicit_confirmation(self):
        candidate = media('weak')
        candidate.confidenceScore = 39
        self.display(candidate)
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertTrue(self.dialog.table.isRowHidden(0))
        self.dialog.include_low.setChecked(True)
        self.dialog._render_candidates()
        self.select('weak')
        self.assertIn('低可信度', self.dialog.table.item(0, 6).text())
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertTrue(self.dialog.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            self.dialog.confirm()
        self.assertEqual(self.imported, [])
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog.confirm()
        self.assertEqual([candidate.id for candidate in self.imported], ['weak'])

    def test_all_qualities_also_confirms_medium_variant_under_high_score_parent(self):
        high = media('master', current=True, metadata={'video_id': 'same'})
        medium = media('variant', metadata={'video_id': 'same'})
        medium.confidenceScore = 60
        self.dialog.all_qualities.setChecked(True)
        self.display(high, medium)
        self.assertEqual(len(self.dialog._groups), 1)
        self.assertTrue(self.dialog._groups[0].selected)
        self.dialog._batch_pending = True
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            self.dialog._batch_ready([high, medium], self.dialog.worker.generation)
            question.assert_called_once()
        self.assertEqual(self.batches, [])

    def test_high_score_does_not_allow_drm_or_ads(self):
        protected = media('protected', current=True, drm=True)
        ad = media('advert', current=True, is_ad=True)
        self.dialog.include_low.setChecked(True)
        self.display(protected, ad)
        self.assertIsNone(self.dialog.selected_candidate())
        for name in ('protected', 'advert'):
            self.select(name)
            self.assertFalse(self.dialog.confirm_button.isEnabled())
            self.dialog.confirm()
        self.assertEqual(self.imported, [])

    def test_manual_selection_survives_score_refresh(self):
        first, second = media('one', current=True), media('two')
        self.display(first, second)
        self.select('two')
        self.display(first, second)
        self.assertEqual(self.dialog.selected_candidate().id, 'two')

    def test_70_is_suggested_but_still_requires_confirmation(self):
        candidate = media('main')
        self.assertEqual(candidate.confidence, 70)
        self.display(candidate)
        self.assertEqual(self.dialog.selected_candidate().id, 'main')
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as question:
            self.dialog.confirm()
            question.assert_called_once()
        self.assertEqual(len(self.imported), 1)


if __name__ == '__main__':
    unittest.main()
