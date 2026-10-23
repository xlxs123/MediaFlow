"""Persistent app profile boundaries and explicit, guarded data management."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from ui.webview2_browser import _PipeHost, WebView2Profile, browser_profile_path
from utils.config import ConfigStore


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_path_is_local_not_roaming_or_edge(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': r'C:\LocalTest', 'APPDATA': r'C:\RoamingTest'}):
            self.assertEqual(browser_profile_path(), Path(r'C:\LocalTest') / 'MediaFlow' / 'BrowserProfile')

    def test_custom_config_isolates_test_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(browser_profile_path(ConfigStore(Path(directory) / 'config.json')),
                             Path(directory).resolve() / 'BrowserProfile')

    def test_persistent_cleanup_never_deletes_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'BrowserProfile'
            folder.mkdir()
            marker = folder / 'existing-state'
            marker.write_text('synthetic', encoding='utf-8')
            profile = WebView2Profile(user_data_folder=folder)
            self.assertFalse(profile.isOffTheRecord())
            with patch('ui.webview2_browser.shutil.rmtree') as remove:
                profile.host.shutdown()
                profile.host._cleanup()
            remove.assert_not_called()
            self.assertTrue(marker.is_file())
            profile.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_initialize_explicitly_selects_persistent_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            host = _PipeHost(user_data_folder=Path(directory) / 'BrowserProfile')
            with patch.object(host, '_write') as write:
                host._initialize()
            self.assertEqual(write.call_args.args[1], {'userDataFolder': str(host.user_data_folder), 'persistent': True})
            host.shutdown()
            host.deleteLater()

    def test_temporary_mode_remains_private(self):
        profile = WebView2Profile()
        folder = profile.host.user_data_folder
        self.assertTrue(profile.isOffTheRecord())
        self.assertTrue(folder.is_dir())
        profile.host.shutdown()
        self.assertFalse(folder.exists())
        profile.deleteLater()

    def test_older_helper_cannot_silently_claim_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            host = _PipeHost(user_data_folder=Path(directory) / 'BrowserProfile')
            errors = []
            host.failed.connect(errors.append)
            host._initialized({'runtimeVersion': 'older-host'}, True)
            self.assertFalse(host._ready)
            self.assertTrue(host._closing)
            self.assertIn('持久 Profile', errors[0])
            self.assertTrue(host.user_data_folder.is_dir())
            host.deleteLater()

    def test_arbitrary_profile_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                _PipeHost(user_data_folder=Path(directory) / 'Default')


class DataManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow
        self.directory = tempfile.TemporaryDirectory()
        with patch.object(MainWindow, 'detect_tools'):
            self.window = MainWindow(ConfigStore(Path(self.directory.name) / 'config.json'))
            self.app.processEvents()

    def tearDown(self):
        self.window._discovery_dialog = None
        self.window.batch_controller.shutdown()
        self.window.close()
        deadline = time.monotonic() + 8
        while (not self.window.batch_controller.is_closed or not self.window.library_service.is_closed) and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(self.window.batch_controller.is_closed, '批次后台未在限时内结束')
        self.assertTrue(self.window.library_service.is_closed, '媒体库后台未在限时内结束')
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()

    def test_cancel_default_no_does_not_start_host_or_clear(self):
        with patch('ui.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            self.window.clear_browser_button.click()
        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertIsNone(self.window._webview2_profile)
        self.assertFalse(self.window._clearing_browser_data)

    def test_active_page_prevents_confirmation_and_clear(self):
        self.window._discovery_dialog = object()
        with patch('ui.main_window.QMessageBox.information') as info, patch('ui.main_window.QMessageBox.question') as question:
            self.window.clear_browser_data()
        info.assert_called_once()
        question.assert_not_called()

    def test_confirm_waits_for_native_completion_and_blocks_reopen(self):
        profile = Mock()
        with patch('ui.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch('ui.webview2_browser.select_browser_backend', return_value='webview2'), \
                patch.object(self.window, 'webview2_profile', return_value=profile):
            self.window.clear_browser_data()
        self.assertTrue(self.window._clearing_browser_data)
        self.assertFalse(self.window.clear_browser_button.isEnabled())
        self.assertIsNone(self.window.open_discovery())
        profile.clear_browsing_data.call_args.args[0](True, True)
        self.assertFalse(self.window._clearing_browser_data)
        self.assertTrue(self.window.clear_browser_button.isEnabled())
        self.assertIn('已清除', self.window.status_label.text())

    def test_native_failure_is_not_reported_as_cleared(self):
        self.window._clearing_browser_data = True
        self.window._browser_data_cleared(None, False)
        self.assertIn('未清除', self.window.status_label.text())

