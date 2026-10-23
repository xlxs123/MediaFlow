"""无前台自动化：真实 Qt/SQLite 后台服务，验证预览确认边界和主线程响应。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from core.library_organizer import LibraryOrganizer
from core.media_history import snapshot_job
from tests.test_library_organizer import OrganizerFixture
from tests.test_media_history import HistoryFixture
from ui.library_service import LibraryService
from ui.media_library import MediaLibrary
from ui.organize_dialog import OrganizeDialog


class OriginalTitleMappingTests(HistoryFixture):
    def test_formatted_job_name_does_not_replace_original_title_for_later_organization(self):
        job = self.job(name='Bilibili_真实标题_180p_20261021', media_metadata={
            'organization_values': {'title': '真实标题', 'site': 'Bilibili'}})
        self.assertEqual(snapshot_job(job)['title'], '真实标题')
        job.request.media_metadata['organization_values'] = ['invalid']
        self.assertEqual(snapshot_job(job)['title'], job.name)
        job.request.media_metadata['organization_values'] = {'title': '  '}
        self.assertEqual(snapshot_job(job)['title'], job.name)


class OrganizationServiceTests(OrganizerFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.entry = self.add(sidecar=True)
        self.other = self.add('未选媒体.mp4')
        self.service = LibraryService(self.store.path)
        self.results = []
        self.mappings = []
        self.messages = []
        self.service.action_ready.connect(lambda command, value: self.results.append((command, value)))
        self.service.paths_moved.connect(lambda value: self.mappings.extend(value))
        self.service.message.connect(self.messages.append)
        self.dialogs = []

    def wait(self, condition, timeout=8):
        end = time.monotonic() + timeout
        while not condition():
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.003)
            if time.monotonic() > end:
                self.fail('后台整理测试超时')
        self.app.processEvents()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        self.service.shutdown()
        self.wait(lambda: self.service.is_closed)
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        super().tearDown()

    def dialog(self):
        dialog = OrganizeDialog(self.service, [self.entry])
        dialog.root_edit.setText(str(self.target))
        self.dialogs.append(dialog)
        return dialog

    def preview(self):
        self.results.clear()
        self.service.preview_organization([self.entry['id']], str(self.target), {'classification': 'site'})
        self.wait(lambda: any(command == 'organize_preview' for command, _ in self.results))
        return next(value for command, value in self.results if command == 'organize_preview')

    def test_preview_and_unconfirmed_apply_do_not_move_files(self):
        plan = self.preview()
        self.assertEqual(plan['ready_count'], 1)
        self.assertEqual(plan['items'], plan['rows'])
        self.assertTrue(plan['rows'][0]['requires_directory'])
        self.assertTrue(Path(self.entry['path']).exists())
        self.assertFalse(self.target.exists())
        self.service.apply_organization(plan['token'])
        self.wait(lambda: any(command == 'organize_apply' for command, _ in self.results))
        failure = next(value for command, value in self.results if command == 'organize_apply')
        self.assertIn('明确确认', failure['error'])
        self.assertTrue(Path(self.entry['path']).exists())
        self.assertEqual(self.mappings, [])

    def test_confirmed_apply_emits_real_task_mapping_and_replay_is_persistent(self):
        plan = self.preview()
        self.service.apply_organization(plan['token'], confirmed=True)
        self.wait(lambda: any(command == 'organize_apply' for command, _ in self.results))
        success = next(value for command, value in self.results if command == 'organize_apply')
        self.assertEqual(success['completed'], 1)
        self.assertEqual(len(self.mappings), 2)
        self.assertEqual(self.mappings[0]['task_key'], self.entry['task_key'])
        self.assertTrue(all(Path(item['new']).exists() for item in self.mappings))
        self.assertTrue(Path(self.other['path']).exists())
        first = list(self.mappings)
        self.service.replay_organization_paths()
        self.wait(lambda: len(self.mappings) == 4)
        self.assertEqual(first, self.mappings[2:])

    def test_expensive_copy_runs_off_ui_thread_and_qt_timers_keep_firing(self):
        plan = self.preview()
        entered = threading.Event()
        release = threading.Event()
        threads = []
        original = LibraryOrganizer._copy
        def slow(organizer, item, journal, index):
            threads.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test gate did not open')
            return original(organizer, item, journal, index)
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        try:
            with patch.object(LibraryOrganizer, '_copy', slow):
                self.service.apply_organization(plan['token'], confirmed=True)
                self.wait(entered.is_set)
                self.wait(lambda: len(ticks) >= 15)
                self.assertTrue(Path(self.entry['path']).exists())
                self.assertTrue(all(thread is not threading.main_thread() for thread in threads))
                release.set()
                self.wait(lambda: any(command == 'organize_apply' for command, _ in self.results))
        finally:
            release.set()
            timer.stop()
        self.assertGreaterEqual(len(ticks), 15)

    def test_dialog_requires_preview_and_explicit_checkbox_before_apply(self):
        dialog = self.dialog()
        dialog.apply_plan()
        self.assertTrue(Path(self.entry['path']).exists())
        dialog.request_preview()
        self.wait(lambda: dialog.plan is not None)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.table.item(0, 1).toolTip(), self.entry['path'])
        self.assertTrue(dialog.table.item(0, 2).toolTip().startswith(str(self.target)))
        dialog.apply_plan()
        self.assertTrue(Path(self.entry['path']).exists())
        dialog.confirm_check.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.apply_button.click()
        self.wait(lambda: not dialog._applying)
        self.assertIn('整理完成 1', dialog.status.text())
        self.assertFalse(Path(self.entry['path']).exists())
        self.assertTrue(Path(self.other['path']).exists())

    def test_cancel_after_preview_preserves_originals(self):
        dialog = self.dialog()
        dialog.request_preview()
        self.wait(lambda: dialog.plan is not None)
        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertTrue(Path(self.entry['path']).exists())
        self.assertFalse(self.target.exists())

    def test_editing_options_invalidates_preview_and_requires_new_confirmation(self):
        dialog = self.dialog()
        dialog.request_preview()
        self.wait(lambda: dialog.plan is not None)
        dialog.confirm_check.setChecked(True)
        dialog.filename_template.setText('{site}_{title}.{ext}')
        self.assertIsNone(dialog.plan)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertFalse(dialog.confirm_check.isChecked())
        self.assertTrue(Path(self.entry['path']).exists())

    def test_backend_preview_error_reenables_controls_and_does_not_freeze_dialog(self):
        dialog = self.dialog()
        dialog.root_edit.setText('relative-folder')
        dialog.request_preview()
        self.wait(lambda: not dialog._waiting_preview)
        self.assertIn('绝对路径', dialog.status.text())
        self.assertTrue(dialog.preview_button.isEnabled())
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_library_multiple_selection_passes_only_selected_ids(self):
        third = self.add('第三个未选.mp4')
        widget = MediaLibrary(self.service)
        try:
            widget.set_active(True)
            self.wait(lambda: not widget._loading and widget.total == 3)
            widget.table.clearSelection()
            selected = {self.entry['id'], third['id']}
            for row, item in enumerate(widget.items):
                if item['id'] in selected:
                    widget.table.selectionModel().select(widget.table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.assertEqual({entry['id'] for entry in widget.selected_entries()}, selected)
            self.assertTrue(widget.organize_button.isEnabled())
            shown = []
            def cancel(dialog):
                shown.extend(dialog.entry_ids)
                return QDialog.DialogCode.Rejected
            with patch.object(OrganizeDialog, 'exec', cancel):
                widget.organize_selected()
            self.assertEqual(set(shown), selected)
            self.assertTrue(Path(self.other['path']).exists())
            self.assertFalse(self.target.exists())
        finally:
            widget._debounce.stop()
            widget.close()
            widget.deleteLater()

    def test_startup_recovery_replays_completed_commits_after_prior_service_has_closed(self):
        plan = self.preview()
        self.service.apply_organization(plan['token'], confirmed=True)
        self.wait(lambda: any(command == 'organize_apply' for command, _ in self.results))
        expected = list(self.mappings)
        self.service.shutdown()
        self.wait(lambda: self.service.is_closed)
        self.service = LibraryService(self.store.path)
        replay = []
        self.service.paths_moved.connect(replay.extend)
        self.wait(lambda: bool(replay))
        self.assertEqual(replay, expected)
        self.assertTrue(all(Path(item['new']).exists() for item in replay))


if __name__ == '__main__':
    unittest.main()
