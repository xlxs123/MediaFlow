"""实际后台扫描、Qt 响应、分页与明确清理确认。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from core.media_history import HistoryStore
from core.storage_cleanup import StorageCleanup
from ui.storage_panel import StoragePanel
from ui.storage_service import StorageService


class StorageUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.sqlite3'
        self.paths = []
        with HistoryStore(self.db) as store:
            for i in range(3):
                path = self.root / ('sample-' + str(i) + '.mp4')
                path.write_bytes(b'complete real repeated bytes')
                self.paths.append(path)
                store.record({'task_key': 'file-' + str(i), 'title': path.stem, 'task_state': 'completed',
                              'files': [{'path': str(path)}]})
        self.service = StorageService(self.db, lambda: {'jobs': []})
        self.panel = StoragePanel(self.service)
        self.panel.show()

    def wait(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.004)
        self.fail('后台空间操作超时')

    def tearDown(self):
        self.service.shutdown()
        self.wait(lambda: self.service.is_closed)
        if self.panel.confirmation_dialog:
            self.panel.confirmation_dialog.close()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def scan(self):
        self.panel.scan_button.click()
        self.wait(lambda: self.service.result is not None and not self.service.busy)
        self.assertTrue(self.service.result['complete'])

    def test_actual_scan_default_scope_and_no_implicit_deletion(self):
        self.scan()
        self.assertEqual(self.panel.extra_dirs(), ())
        self.assertEqual(len(self.service.result['groups']), 1)
        self.assertFalse(self.panel.selected_cleanup_paths())
        self.assertFalse(self.panel.cleanup_button.isEnabled())
        self.assertEqual(self.panel.files_table.rowCount(), 3)
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_confirm_requires_checkbox_and_native_qt_button(self):
        self.scan()
        self.panel.set_cleanup_paths([str(self.paths[1])])
        self.panel.cleanup_button.click()
        self.wait(lambda: self.panel.confirmation_dialog is not None)
        dialog = self.panel.confirmation_dialog
        self.assertIn(str(self.paths[1]), dialog.paths_preview.toPlainText())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertFalse(self.panel.confirm_cleanup(True))
        self.assertTrue(self.paths[1].exists())
        calls, gates = [], []
        self.service.cleanup_guard_changed.connect(gates.append)
        def local_recycler(path, proof, receipt, nonce=None, **kwargs):
            calls.append(path)
            Path(path).rename(self.root / 'unit-recycled-file')
            return {'recycled': True, 'sha256': proof['sha256'], 'nonce': nonce}
        self.service._worker._cleanup.recycler = local_recycler
        dialog.confirm_check.setChecked(True)
        dialog.apply_button.click()
        self.wait(lambda: self.panel.last_cleanup is not None)
        self.wait(lambda: not self.service.busy and self.service.result.get('complete'))
        self.assertEqual(calls, [str(self.paths[1])])
        self.assertEqual(gates, [True, False])
        self.assertTrue(self.paths[0].exists() and self.paths[2].exists())
        self.assertEqual(self.service.result['stats']['missing_files'], 1)

    def test_cancel_scan_returns_ineligible_result(self):
        self.assertTrue(self.service.start_scan())
        self.service.cancel()
        self.wait(lambda: self.service.result is not None and not self.service.busy)
        self.assertFalse(self.service.result['complete'])
        self.assertTrue(self.service.result['cancelled'])
        self.assertFalse(self.service.preview_cleanup([str(self.paths[1])]))

    def test_actual_many_files_scanned_in_background_and_rendered_paginated(self):
        with HistoryStore(self.db) as store:
            for i in range(3, 303):
                path = self.root / ('sample-' + str(i) + '.mp4')
                path.write_bytes(b'content-' + i.to_bytes(4, 'big'))
                store.record({'task_key': 'file-' + str(i), 'title': path.stem, 'task_state': 'completed',
                              'files': [{'path': str(path)}]})
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        self.scan()
        timer.stop()
        self.assertGreater(len(ticks), 2)
        self.assertEqual(self.service.result['stats']['total_files'], 303)
        self.assertLessEqual(self.panel.files_table.rowCount(), self.panel.FILE_PAGE_SIZE)

    def test_latest_protection_provider_blocks_changed_task_state(self):
        self.scan()
        self.service.protection_provider = lambda: {'jobs': [
            {'state': 'recovering', 'paths': [str(self.paths[1])], 'directories': []}]}
        self.panel.set_cleanup_paths([str(self.paths[1])])
        self.panel.cleanup_button.click()
        self.wait(lambda: self.panel.last_preview is not None)
        self.assertFalse(self.panel.last_preview['token'])
        self.assertTrue(self.panel.last_preview['errors'])
        self.assertTrue(self.paths[1].exists())

    def test_rescan_replays_pending_database_recovery(self):
        self.scan()
        calls = []
        original = self.service._worker._cleanup.recover
        self.service._worker._cleanup.recover = lambda store: (calls.append(True) or original(store))
        previous = self.service.result
        self.panel.scan_button.click()
        self.wait(lambda: not self.service.busy and self.service.result is not previous)
        self.assertEqual(calls, [True])

    def test_declining_exit_does_not_shut_down_space_service(self):
        from ui.main_window import MainWindow
        from utils.config import ConfigStore
        with patch.object(MainWindow, 'detect_tools', lambda _self: None):
            window = MainWindow(ConfigStore(self.root / 'separate' / 'config.json'))
            try:
                with patch.object(window, 'is_downloading', return_value=True), \
                        patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                    window.close()
                self.assertFalse(window.storage_service._closing)
                self.assertFalse(window.storage_service.is_closed)
            finally:
                window.close()
                self.wait(lambda: window.storage_service.is_closed and window.library_service.is_closed
                          and window.batch_controller.is_closed)
