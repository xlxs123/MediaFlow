"""媒体库异步边界与用户文件操作；真实媒体检查另由验证器测试覆盖。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox
from core.media_history import HistoryStore
from ui.library_service import LibraryService
from ui.media_library import MediaLibrary


class MediaLibraryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            time.sleep(.005)
            if time.monotonic() >= end:
                self.fail('媒体库后台操作超时')
        self.app.processEvents()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / '视频示例.mp4'
        self.path.write_bytes(b'unit file, not a media validator fixture')
        self.db = self.root / 'history.sqlite3'
        store = HistoryStore(self.db)
        store.record({'task_key': 'unit-video', 'queue_id': 1, 'title': '视频示例',
            'task_state': 'completed', 'source_site': 'example.com',
            'source_url': 'https://example.com/watch', 'progress': {'percent': 100},
            'finished_at': '2026-10-19T01:00:00+00:00', 'files': [{'path': str(self.path),
                'validation': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'size': self.path.stat().st_size,
                'duration': 19.0, 'video_streams': [{'codec_type': 'video', 'codec_name': 'h264',
                'index': 0, 'width': 576, 'height': 1024}],
                'audio_streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'index': 1}]}}]})
        store.close()
        self.service = LibraryService(self.db)
        self.widget = MediaLibrary(self.service)
        self.widget.set_active(True)
        self.wait(lambda: not self.widget._loading and self.widget.total == 1)

    def tearDown(self):
        self.widget._debounce.stop()
        self.service.shutdown()
        self.wait(lambda: self.service.is_closed)
        self.widget.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_metadata_and_file_operations_available(self):
        item = self.widget.items[0]
        self.assertEqual(item['media_type'], 'video')
        self.assertEqual(item['width'], 576)
        self.assertIn('19', self.widget.table.item(0, 3).text())
        self.assertIn(str(self.path), self.widget.table.item(0, 1).toolTip())
        self.assertTrue(self.widget.buttons['details'].isEnabled())

    def test_search_reset_restores_full_list(self):
        self.widget.search.setText('不存在的名字')
        self.wait(lambda: not self.widget._loading and self.widget.total == 0)
        self.assertTrue(self.widget.empty.isVisibleTo(self.widget))
        self.widget.reset_filters()
        self.wait(lambda: not self.widget._loading and self.widget.total == 1)

    def test_scan_occurs_off_ui_thread(self):
        from ui.library_service import file_status
        calls = []
        def check(path):
            calls.append(threading.current_thread())
            return file_status(path)
        with patch('ui.library_service.file_status', side_effect=check):
            self.widget.refresh()
            self.wait(lambda: not self.widget._loading)
        self.assertTrue(calls)
        self.assertTrue(all(thread is not threading.main_thread() for thread in calls))

    def test_missing_file_keeps_history(self):
        self.path.unlink()
        self.widget.refresh()
        self.wait(lambda: not self.widget._loading and self.widget.items[0]['file_state'] == 'missing')
        self.assertEqual(self.widget.total, 1)
        self.assertEqual(self.widget.items[0]['file_state'], 'missing')

    def test_remove_history_preserves_file(self):
        self.widget.run_action('remove')
        self.wait(lambda: self.widget.total == 0)
        self.assertTrue(self.path.is_file())

    def test_delete_no_keeps_file_and_full_path_is_shown(self):
        shown = []
        def decline(dialog):
            shown.append(dialog.text())
            return QMessageBox.StandardButton.No
        with patch.object(QMessageBox, 'exec', decline):
            self.widget.run_action('delete_preview')
            self.wait(lambda: bool(shown))
        self.assertIn(str(self.path), shown[0])
        self.assertTrue(self.path.is_file())

    def test_delete_yes_removes_only_file_and_keeps_record(self):
        with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Yes):
            self.widget.run_action('delete_preview')
            self.wait(lambda: not self.path.exists())
        self.widget.refresh()
        self.wait(lambda: not self.widget._loading and self.widget.items[0]['file_state'] == 'missing')
        self.assertEqual(self.widget.total, 1)
        self.assertEqual(self.widget.items[0]['file_state'], 'missing')

    def test_file_replaced_during_confirmation_is_preserved(self):
        def replace(dialog):
            self.path.write_bytes(b'changed after confirmation preview')
            return QMessageBox.StandardButton.Yes
        with patch.object(QMessageBox, 'exec', replace):
            self.widget.run_action('delete_preview')
            self.wait(lambda: '发生变化' in self.widget.notice.text())
        self.assertTrue(self.path.is_file())

    def test_relocate_clears_previous_media_validation(self):
        moved = self.path.with_name('移动后的文件.mp4')
        self.path.rename(moved)
        with patch('ui.media_library.QFileDialog.getOpenFileName', return_value=(str(moved), '')):
            self.widget.run_action('relocate')
        self.wait(lambda: self.widget.items[0]['path'] == str(moved))
        self.assertEqual(self.widget.items[0]['validation_state'], 'unverified')
        self.assertEqual(self.widget.items[0]['width'], 0)

    def test_source_open_failure_is_actionable(self):
        with patch('ui.media_library.QDesktopServices.openUrl', return_value=False):
            self.widget.run_action('source')
        self.assertIn('重新分析来源', self.widget.notice.text())

    def test_same_size_change_after_manual_verification_requires_recheck(self):
        store = HistoryStore(self.db)
        entry = self.widget.items[0]
        validation = dict(entry['validation'], mtime_ns=self.path.stat().st_mtime_ns)
        store.update(entry['id'], {'validation': validation})
        store.close()
        timestamp = self.path.stat().st_mtime_ns
        self.path.write_bytes(b'X' * self.path.stat().st_size)
        os.utime(self.path, ns=(timestamp + 5_000_000, timestamp + 5_000_000))
        self.widget.refresh()
        self.wait(lambda: not self.widget._loading and self.widget.items[0]['validation_state'] == 'unverified')

    def test_changed_file_during_incomplete_check_invalidates_previous_result(self):
        def changed(path, **kwargs):
            return {'validation_state': 'unverified', 'validation_reason': '文件发生变化',
                    'file_state': 'exists', 'file_changed': True, 'validation': {}}
        with patch('ui.library_service.revalidate_media', side_effect=changed):
            self.widget.run_action('validate')
            self.wait(lambda: self.widget.items[0]['validation_state'] == 'unverified')

    def test_cancel_validation_keeps_previous_valid_result(self):
        started = threading.Event()
        def incomplete(path, cancel_event=None, **kwargs):
            started.set()
            cancel_event.wait(3)
            return {'validation_state': 'unverified', 'validation_reason': '验证已取消',
                    'file_state': 'exists', 'validation': {}}
        with patch('ui.library_service.revalidate_media', side_effect=incomplete):
            self.widget.run_action('validate')
            self.wait(started.is_set)
            self.assertFalse(self.service.validate(self.widget.items[0]['id']))
            self.service.cancel_validation()
            self.wait(lambda: self.service.validator is None)
            self.widget.refresh()
            self.wait(lambda: not self.widget._loading)
        self.assertEqual(self.widget.items[0]['validation_state'], 'valid')


if __name__ == '__main__':
    unittest.main()
