"""串行网页租约、代际回调与原有候选确认的离线隔离验证。"""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from core.discovery_models import MediaCandidate
from core.media_groups import group_media
from core.request_context import RequestContext
from ui.batch_discovery import BatchCandidateDialog, SerialBatchDiscovery
from ui.discovery_dialog import DiscoveryDialog


def media(identity, *, confidence=95, **changes):
    metadata = {'video_id': identity, 'title': '正片 ' + identity,
                'confidence_evidence': {'current_player': True}}
    metadata.update(changes.pop('metadata', {}))
    fields = dict(id=identity, request_context=RequestContext(
        'https://cdn.example.test/' + identity + '.mp4?token=private-test-token'),
        media_type='Direct', content_type='video/mp4', score=confidence, duration=19,
        confirmed=True, validated=True, drm=False, http_status=200, resolution=(1280, 720), metadata=metadata)
    fields.update(changes)
    return MediaCandidate(**fields)


class FakeWorker(QObject):
    batch_ready = Signal(object, int)

    def __init__(self, parent):
        super().__init__(parent)
        self.generation = 1
        self.freeze_count = 0

    def freeze(self):
        self.freeze_count += 1


class FakeDiscoveryDialog(QObject):
    media_selected = Signal(object)
    shutdown_complete = Signal()
    instances = []

    def __init__(self, owner, page_url='', listen_seconds=60):
        super().__init__(owner)
        self.instances.append(self)
        self.source_url, self.current_url, self.listen_seconds = page_url, page_url, listen_seconds
        self._closing = False
        self._document_ready = False
        self._listening = True
        self._resolver_epoch = 0
        self._groups, self._candidates, self._fetch_queue = [], [], ['pending-previous-fetch']
        self.excluded_ids = set()
        self.deep_count = self.shutdown_count = 0
        self.observer_stops = 0
        self.worker = FakeWorker(self)
        self.listen_timer, self.resolver_timer = QTimer(self), QTimer(self)
        self.page = SimpleNamespace(url=lambda: QUrl(self.current_url))
        self.page_url = SimpleNamespace(setReadOnly=Mock())
        self.download_all_button = SimpleNamespace(hide=Mock())
        self.find_button = SimpleNamespace(setEnabled=Mock())
        self.deep_find_button = SimpleNamespace(setEnabled=Mock())
        self.observer = SimpleNamespace(stop=self._stop_observer)

    def _stop_observer(self):
        self.observer_stops += 1

    def setWindowTitle(self, *_):
        pass

    def setAttribute(self, *_):
        pass

    def setWindowModality(self, *_):
        pass

    def showMinimized(self):
        pass

    def deep_find(self):
        self.deep_count += 1
        self.worker.generation += 1

    def shutdown(self):
        self.shutdown_count += 1
        self._closing = True

    def _candidate_allowed(self, candidate):
        return candidate.id not in self.excluded_ids

    def _render_candidates(self):
        self._groups = group_media(self._candidates)

    def _empty_diagnostic(self):
        return 'PLAYER_NOT_STARTED：未发现可下载完整媒体。'


class Owner(QObject):
    def __init__(self):
        super().__init__()
        self.remembered = []

    def _remember_resolver_session(self, candidate, dialog):
        self.remembered.append((candidate.id, dialog))


class SerialBatchDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patch = patch('ui.batch_discovery.DiscoveryDialog', FakeDiscoveryDialog)
        self.patch.start()
        FakeDiscoveryDialog.instances = []
        self.owner = Owner()
        self.driver = SerialBatchDiscovery(self.owner)
        self.results = []
        self.driver.finished.connect(lambda item, payload, error: self.results.append((item, payload, error)))

    def tearDown(self):
        if self.driver.dialog:
            dialog = self.driver.dialog
            self.driver.cancel()
            dialog.shutdown_complete.emit()
        self.driver._timer.stop()
        self.patch.stop()
        self.owner.deleteLater()
        self.app.processEvents()

    def start(self, item='first', url='https://pages.example.test/first'):
        self.driver.start(item, url, {'quality': 'balanced', 'discovery_seconds': 10})
        self.assertTrue(self.driver._timer.isActive())
        self.driver._timer.stop()  # 用可控时间驱动 poll，避免依赖真实等待。
        return self.driver.dialog

    def freeze(self, dialog, candidates=()):
        dialog._groups = group_media(candidates)
        dialog._document_ready = True
        dialog._listening = False
        with patch('ui.batch_discovery.time.monotonic', return_value=100.0):
            self.driver._poll()
        self.assertEqual(dialog.worker.freeze_count, 1)

    def test_every_url_gets_new_dialog_deep_find_and_exclusive_page_lease(self):
        first = self.start()
        self.assertEqual(first.deep_count, 1)
        self.assertEqual(first.source_url, 'https://pages.example.test/first')
        self.assertEqual(first.listen_seconds, 10)
        self.assertEqual(first._quality_preference, 'balanced')
        first.page_url.setReadOnly.assert_called_once_with(True)
        first.download_all_button.hide.assert_called_once()
        self.assertFalse(self.driver.can_start())
        with self.assertRaises(ValueError):
            self.driver.start('second', 'https://pages.example.test/second', {})
        self.driver.cancel()
        self.assertTrue(self.driver.busy)
        first.shutdown_complete.emit()
        second = self.start('second', 'https://pages.example.test/second')
        self.assertIsNot(first, second)
        self.assertEqual(second.deep_count, 1)
        self.assertEqual(len(FakeDiscoveryDialog.instances), 2)

    def test_freeze_handoff_waits_for_shutdown_complete_before_emitting_finished(self):
        dialog = self.start()
        candidate = media('first')
        self.freeze(dialog, [candidate])
        self.assertEqual(dialog._fetch_queue, [])
        self.assertEqual(dialog._resolver_epoch, 1)
        self.assertEqual(dialog.observer_stops, 1)
        self.assertEqual(self.results, [])
        dialog.worker.batch_ready.emit([candidate], dialog.worker.generation)
        self.assertEqual(dialog.shutdown_count, 1)
        self.assertTrue(self.driver.busy)
        self.assertEqual(self.results, [])
        dialog.shutdown_complete.emit()
        self.assertFalse(self.driver.busy)
        self.assertEqual(self.results[0][0], 'first')
        self.assertEqual(self.results[0][2], '')
        self.assertEqual(self.results[0][1]['groups'][0].candidates[0].id, 'first')

    def test_wrong_generation_cannot_publish_candidates(self):
        dialog = self.start()
        self.freeze(dialog)
        dialog.worker.batch_ready.emit([media('stale')], dialog.worker.generation - 1)
        self.assertEqual(dialog.shutdown_count, 0)
        self.assertEqual(self.driver._payload, {})
        dialog.worker.batch_ready.emit([media('current')], dialog.worker.generation)
        dialog.shutdown_complete.emit()
        ids = {candidate.id for group in self.results[0][1]['groups'] for candidate in group.candidates}
        self.assertEqual(ids, {'current'})

    def test_previous_dialog_callbacks_cannot_release_or_pollute_current_page(self):
        first = self.start()
        self.driver.cancel()
        first.shutdown_complete.emit()
        second = self.start('second', 'https://pages.example.test/second')
        first.worker.batch_ready.emit([media('old')], first.worker.generation)
        first.media_selected.emit(media('old-manual'))
        first.shutdown_complete.emit()
        self.assertIs(self.driver.dialog, second)
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.driver._payload, {})
        self.assertEqual(self.owner.remembered, [])

    def test_cancel_clears_already_cached_manual_selection_and_rejects_late_batch(self):
        dialog = self.start()
        candidate = media('first')
        dialog._groups = group_media([candidate])
        dialog.media_selected.emit(candidate)
        self.assertTrue(self.driver._payload)
        self.driver.cancel()
        dialog.worker.batch_ready.emit([candidate], dialog.worker.generation)
        self.assertTrue(self.driver.busy)
        dialog.shutdown_complete.emit()
        self.assertEqual(self.results[0][1], {})
        self.assertIn('暂停或取消', self.results[0][2])

    def test_deadline_freezes_and_silent_worker_timeout_closes_page(self):
        dialog = self.start()
        with patch('ui.batch_discovery.time.monotonic', return_value=self.driver._deadline + 1):
            self.driver._poll()
        self.assertEqual(dialog.worker.freeze_count, 1)
        with patch('ui.batch_discovery.time.monotonic', return_value=self.driver._freeze_at + 21):
            self.driver._poll()
        self.assertTrue(dialog._closing)
        self.assertEqual(self.results, [])
        dialog.shutdown_complete.emit()
        self.assertFalse(self.driver.busy)
        self.assertIn('超时', self.results[0][2])
        self.assertEqual(self.results[0][1], {})

    def test_automatic_choice_waits_for_stable_current_player_evidence(self):
        dialog = self.start()
        dialog._groups = group_media([media('first')])
        dialog._document_ready = True
        with patch('ui.batch_discovery.time.monotonic', return_value=100):
            self.driver._poll()
        with patch('ui.batch_discovery.time.monotonic', return_value=101):
            self.driver._poll()
        self.assertEqual(dialog.worker.freeze_count, 0)
        with patch('ui.batch_discovery.time.monotonic', return_value=103):
            self.driver._poll()
        self.assertEqual(dialog.worker.freeze_count, 1)

    def test_page_switch_rejects_another_work_without_returning_candidates(self):
        dialog = self.start(url='https://www.douyin.com/user/self?modal_id=7526879737634950451')
        dialog._document_ready = True
        self.driver._poll()
        dialog.current_url = 'https://www.douyin.com/user/self?modal_id=7657489271422549747'
        dialog._groups = group_media([media('wrong-work')])
        self.driver._poll()
        self.assertTrue(dialog._closing)
        self.assertEqual(dialog.worker.freeze_count, 0)
        dialog.shutdown_complete.emit()
        self.assertEqual(self.results[0][1], {})
        self.assertIn('页面已切换', self.results[0][2])

    def test_navigation_between_poll_and_frozen_publish_cannot_handoff_another_page(self):
        dialog = self.start(url='https://pages.example.test/first')
        self.freeze(dialog, [media('first')])
        # 原页已提交并请求 freeze，URL 刚改变而下一轮定时检查尚未执行。
        dialog.current_url = 'https://pages.example.test/unrelated'
        dialog.worker.batch_ready.emit([media('unrelated')], dialog.worker.generation)
        dialog.shutdown_complete.emit()
        self.assertFalse(self.results[0][1].get('groups'))
        self.assertIn('页面已切换', self.results[0][2])

    def test_manual_selection_after_page_switch_cannot_bypass_url_ownership(self):
        dialog = self.start(url='https://pages.example.test/first')
        dialog._document_ready = True
        self.driver._poll()
        dialog.current_url = 'https://pages.example.test/unrelated'
        candidate = media('unrelated')
        dialog._groups = group_media([candidate])
        dialog.media_selected.emit(candidate)
        dialog.shutdown()
        dialog.shutdown_complete.emit()
        self.assertFalse(self.results[0][1].get('groups'))
        self.assertNotIn('manual_candidate', self.results[0][1])
        self.assertIn('页面已切换', self.results[0][2])

    def test_frozen_filters_excluded_resources_and_copies_remaining_groups(self):
        dialog = self.start()
        allowed, excluded = media('allowed'), media('excluded')
        self.freeze(dialog, [allowed, excluded])
        dialog.excluded_ids.add('excluded')
        dialog.worker.batch_ready.emit([allowed, excluded], dialog.worker.generation)
        dialog._groups.clear()
        dialog.shutdown_complete.emit()
        ids = {candidate.id for group in self.results[0][1]['groups'] for candidate in group.candidates}
        self.assertEqual(ids, {'allowed'})
        self.assertEqual({identity for identity, _ in self.owner.remembered}, {'allowed'})

    def test_no_media_keeps_existing_empty_diagnostic_and_releases_cleanly(self):
        dialog = self.start()
        self.freeze(dialog)
        dialog.worker.batch_ready.emit([], dialog.worker.generation)
        dialog.shutdown_complete.emit()
        self.assertEqual(self.results[0][1], {'groups': []})
        self.assertIn('PLAYER_NOT_STARTED', self.results[0][2])

    def test_browser_acquire_gate_blocks_dialog_creation(self):
        self.driver.can_acquire = lambda: False
        self.assertFalse(self.driver.can_start())
        with self.assertRaises(ValueError):
            self.driver.start('first', 'https://pages.example.test/first', {})
        self.assertEqual(FakeDiscoveryDialog.instances, [])


class CandidateConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def candidate_dialog(self, candidates):
        dialog = BatchCandidateDialog(group_media(candidates))
        self.dialogs.append(dialog)
        return dialog

    def test_isolated_segments_have_no_selectable_row_or_enabled_confirmation(self):
        dialog = self.candidate_dialog([media('fragment', media_type='Segment', classification='Media Segment')])
        self.assertEqual(dialog.choices.count(), 0)
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        with patch('ui.discovery_dialog.QMessageBox.question') as question:
            dialog._confirm()
        self.assertEqual(dialog.candidate_id, '')
        question.assert_not_called()

    def test_low_confidence_uses_original_default_no_confirmation_then_accepts_explicit_yes(self):
        dialog = self.candidate_dialog([media('low-confidence', confidence=50)])
        self.assertEqual(dialog.choices.count(), 1)
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            dialog._confirm()
        self.assertEqual(dialog.candidate_id, '')
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            dialog._confirm()
        self.assertEqual(dialog.candidate_id, 'low-confidence')
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_verified_high_confidence_does_not_invent_an_extra_confirmation(self):
        dialog = self.candidate_dialog([media('verified')])
        with patch('ui.discovery_dialog.QMessageBox.question') as question:
            dialog._confirm()
        question.assert_not_called()
        self.assertEqual(dialog.candidate_id, 'verified')
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_revalidation_at_click_rejects_candidate_that_lost_access(self):
        dialog = self.candidate_dialog([media('expired')])
        dialog._choices[0].http_status = 403
        with patch('ui.discovery_dialog.QMessageBox.question') as question:
            dialog._confirm()
        self.assertEqual(dialog.candidate_id, '')
        question.assert_not_called()

    def test_original_discovery_confirm_does_not_emit_segment_even_if_confirmation_is_true(self):
        selected = media('isolated', media_type='Segment', classification='Media Segment')
        messages, emitted = [], []
        proxy = SimpleNamespace(selected_candidate=lambda: selected,
            status_label=SimpleNamespace(setText=messages.append), _confirm_medium=lambda _: True,
            media_selected=SimpleNamespace(emit=emitted.append), setResult=Mock(), close=Mock())
        DiscoveryDialog.confirm(proxy)
        self.assertEqual(emitted, [])
        self.assertTrue(messages)
        proxy.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
