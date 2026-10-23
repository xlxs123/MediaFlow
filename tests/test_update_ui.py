"""Qt orchestration checks; fake I/O does not claim packaged update acceptance."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from core.update_manager import UpdateCancelled, UpdateError
from ui.update_dialog import UpdateDialog


class FakeManager:
    def __init__(self, root):
        self.root = root
        self.current_identity = {'app_version': '1.9.0', 'build_id': 'test-current'}
        self.settings = {'source_url': '', 'startup_check': False, 'trusted_keys': {},
                         'last_checked': '', 'last_status': '未检查'}
        self.calls = []
        self.threads = []
        self.gates = {}
        self.entered = {}
        self.failures = {}
        self.records = []
        self.record = {'id': 'a' * 32, 'state': 'prepared', 'version': '1.10.0',
            'build_id': 'test-next', 'authenticated': False, 'authentication': '未经过发布者身份认证',
            'package_size': 1234, 'unpacked_size': 4096, 'files_count': 9,
            'target_path': str(root / 'versions' / 'new'), 'current_path': str(root / 'old'),
            'data_directory': str(root / 'data'), 'migration_state': '尚未迁移',
            'is_downgrade': False, 'release_notes': '<b>纯文本</b>', 'release_page': '',
            'message': '完整性已校验；等待部署确认。'}

    def _call(self, name, payload=None, cancel=None):
        self.calls.append((name, deepcopy(payload)))
        self.threads.append(threading.current_thread())
        if name in self.entered:
            self.entered[name].set()
        gate = self.gates.get(name)
        if gate:
            while not gate.wait(.01):
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled('已安全取消。')
        if name in self.failures:
            raise UpdateError(self.failures[name])

    def load_settings(self):
        self._call('load')
        return deepcopy(self.settings)

    def save_settings(self, values):
        self._call('save', values)
        self.settings.update(deepcopy(values))
        return self.load_settings()

    def recover_interrupted(self):
        self._call('recover')
        return []

    def list_versions(self):
        self._call('list')
        return deepcopy(self.records)

    def check_updates(self, cancel=None, progress=None):
        self._call('check', cancel=cancel)
        self.settings['last_checked'] = '2026-10-22T01:02:03+00:00'
        if not self.settings['source_url']:
            return {'status': 'not_configured', 'message': '未配置在线更新源'}
        return {'status': 'update_available', 'message': '发现可用版本',
            'manifest': {'version': '1.10.0', 'build_id': 'next', 'release_notes': '新功能',
                         'package': {'size': 1234}}, 'authenticated': True, 'authentication': '签名通过'}

    def prepare_local(self, path, cancel=None, progress=None):
        if progress:
            progress({'message': '逐文件完整校验', 'completed': 25, 'total': 100})
        self._call('prepare', path, cancel)
        self.records = [deepcopy(self.record)]
        return deepcopy(self.record)

    def download_update(self, manifest, cancel=None, progress=None):
        self._call('download', manifest, cancel)
        self.records = [deepcopy(self.record)]
        return deepcopy(self.record)

    def deploy(self, identifier, confirmed=False, allow_unsigned=False, allow_downgrade=False,
               cancel=None, progress=None):
        self._call('deploy', {'id': identifier, 'confirmed': confirmed,
            'allow_unsigned': allow_unsigned, 'allow_downgrade': allow_downgrade}, cancel)
        self.record.update(state='deployed', message='已部署；未执行 EXE。')
        self.records = [deepcopy(self.record)]
        return deepcopy(self.record)

    def check_health(self, identifier, confirmed=False, cancel=None, progress=None):
        self._call('health', {'id': identifier, 'confirmed': confirmed}, cancel)
        self.record.update(state='healthy', health={'passed': True})
        self.records = [deepcopy(self.record)]
        return {'passed': True, 'message': '实际启动健康检查通过。'}

    def launch_new(self, identifier, confirmed=False):
        self._call('launch_new', {'id': identifier, 'confirmed': confirmed})
        return {'started': True, 'message': '已请求独立打开新版本。'}

    def launch_previous(self, identifier, confirmed=False):
        self._call('launch_previous', {'id': identifier, 'confirmed': confirmed})
        return {'started': True, 'message': '已请求打开旧版本。'}


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-update-ui-')
        self.root = Path(self.temp.name)
        self.manager = FakeManager(self.root)
        self.dialogs = []

    def wait(self, condition, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if condition():
                return
            time.sleep(.003)
        self.fail('Qt 更新操作超时')

    def dialog(self, factory=None):
        result = UpdateDialog(factory or self.manager)
        self.dialogs.append(result)
        self.wait(lambda: not result.busy)
        return result

    def prepare(self, dialog):
        self.assertTrue(dialog.import_local(self.root / 'release.zip'))
        self.wait(lambda: not dialog.busy)
        self.assertIsNotNone(dialog.preview)

    def teardown_dialogs(self):
        for gate in self.manager.gates.values():
            gate.set()
        for dialog in self.dialogs:
            dialog.shutdown()
            self.wait(lambda d=dialog: d.is_closed)
            dialog.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def tearDown(self):
        self.teardown_dialogs()
        self.temp.cleanup()

    def calls(self, name):
        return [payload for operation, payload in self.manager.calls if operation == name]

    def test_defaults_are_truthful_no_source_and_no_auto_download(self):
        dialog = self.dialog()
        self.assertIn('未配置在线更新源', dialog.status_label.text())
        self.assertIn('待配置', dialog.channel_label.text())
        self.assertFalse(dialog.startup_check.isChecked())
        self.assertFalse(dialog.download_button.isEnabled())
        dialog.check_on_startup()
        self.assertFalse(self.calls('check'))
        self.assertFalse(self.calls('download'))
        self.assertFalse(self.calls('deploy'))

    def test_fixed_channel_is_not_presented_as_public_acceptance(self):
        self.manager.settings['channel_state'] = 'initialized'
        dialog = self.dialog()
        self.assertIn('已固定', dialog.channel_label.text())
        self.assertIn('须通过检查确认', dialog.channel_label.text())
        self.assertFalse(self.calls('check'))
        self.assertFalse(self.calls('download'))

    def test_signature_and_integrity_stages_are_separate(self):
        dialog=self.dialog()
        dialog._progress({'stage':'signature_verified','message':'signature done','completed':1,'total':1})
        self.assertIn('已通过',dialog.signature_label.text())
        self.assertIn('尚未',dialog.integrity_label.text())
        dialog._progress({'stage':'package_hash','message':'hashing','completed':10,'total':100})
        self.assertIn('正在校验',dialog.integrity_label.text())
        dialog._failed('download','hash mismatch')
        dialog._refresh_controls()
        self.assertIn('未通过',dialog.integrity_label.text())
        self.assertFalse(dialog.deploy_button.isEnabled())

    def test_constructor_and_all_manager_io_run_in_background(self):
        threads = []
        def factory():
            threads.append(threading.current_thread())
            return self.manager
        dialog = self.dialog(factory)
        dialog.check_updates()
        self.wait(lambda: not dialog.busy)
        self.assertTrue(threads)
        self.assertTrue(all(thread is not threading.current_thread() for thread in threads + self.manager.threads))
        self.assertEqual(dialog.last_result['status'], 'not_configured')
        self.assertIn('2026-10-22', dialog.last_check.text())

    def test_unsigned_deployment_requires_both_confirmations(self):
        dialog = self.dialog()
        self.prepare(dialog)
        self.assertFalse(dialog.deploy_update())
        dialog.deploy_confirm_check.setChecked(True)
        self.assertFalse(dialog.deploy_button.isEnabled())
        self.assertFalse(dialog.deploy_update())
        dialog.unsigned_confirm_check.setChecked(True)
        self.assertTrue(dialog.deploy_button.isEnabled())
        self.assertTrue(dialog.deploy_update())
        self.wait(lambda: not dialog.busy)
        self.assertEqual(self.calls('deploy'), [{'id': 'a' * 32, 'confirmed': True,
            'allow_unsigned': True, 'allow_downgrade': False}])
        self.assertFalse(self.calls('health'))
        self.assertFalse(self.calls('launch_new'))
        self.assertFalse(dialog.execute_confirm_check.isChecked())

    def test_downgrade_has_separate_confirmation(self):
        self.manager.record.update(version='1.8.0', is_downgrade=True)
        dialog = self.dialog()
        self.prepare(dialog)
        dialog.deploy_confirm_check.setChecked(True)
        dialog.unsigned_confirm_check.setChecked(True)
        self.assertFalse(dialog.deploy_update())
        dialog.downgrade_confirm_check.setChecked(True)
        self.assertTrue(dialog.deploy_update())
        self.wait(lambda: not dialog.busy)
        self.assertTrue(self.calls('deploy')[0]['allow_downgrade'])

    def test_new_preview_resets_trust_and_run_confirmations(self):
        dialog = self.dialog()
        self.prepare(dialog)
        for box in (dialog.deploy_confirm_check, dialog.unsigned_confirm_check,
                    dialog.downgrade_confirm_check, dialog.execute_confirm_check):
            box.setChecked(True)
        self.prepare(dialog)
        self.assertFalse(any(box.isChecked() for box in (dialog.deploy_confirm_check,
            dialog.unsigned_confirm_check, dialog.downgrade_confirm_check, dialog.execute_confirm_check)))
        self.assertEqual(dialog.release_notes.toPlainText(), '<b>纯文本</b>')
        self.assertIn(str(self.root / 'versions' / 'new'), dialog.preview_text.toPlainText())

    def test_health_execution_requires_permission_and_launch_separate_confirmation(self):
        self.manager.record['state'] = 'deployed'
        dialog = self.dialog()
        self.prepare(dialog)
        self.assertFalse(dialog.check_health())
        dialog.execute_confirm_check.setChecked(True)
        self.assertTrue(dialog.check_health())
        self.wait(lambda: not dialog.busy)
        self.assertEqual(self.calls('health')[0]['confirmed'], True)
        self.assertFalse(self.calls('launch_new'))
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.assertFalse(dialog.launch_new())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes) as question:
            self.assertTrue(dialog.launch_new())
        self.wait(lambda: not dialog.busy)
        self.assertIn(str(self.root / 'versions' / 'new' / 'MediaFlow.exe'), question.call_args.args[2])
        self.assertEqual(len(self.calls('launch_new')), 1)

    def test_health_failure_keeps_previous_available(self):
        self.manager.record['state'] = 'health_failed'
        dialog = self.dialog()
        self.prepare(dialog)
        self.assertFalse(dialog.launch_new_button.isEnabled())
        self.assertTrue(dialog.launch_previous_button.isEnabled())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(dialog.launch_previous())
        self.wait(lambda: not dialog.busy)
        self.assertEqual(self.calls('launch_previous')[0]['confirmed'], True)

    def test_busy_rejects_duplicates_and_cancel_keeps_ui_responsive(self):
        self.manager.gates['prepare'] = threading.Event()
        self.manager.entered['prepare'] = threading.Event()
        dialog = self.dialog()
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        try:
            self.assertTrue(dialog.import_local('one.zip'))
            self.wait(self.manager.entered['prepare'].is_set)
            self.assertFalse(dialog.import_local('two.zip'))
            self.wait(lambda: len(ticks) >= 4)
            dialog.cancel()
            self.wait(lambda: not dialog.busy)
            self.assertEqual(len(self.calls('prepare')), 1)
            self.assertIn('已安全取消', dialog.status_label.text())
        finally:
            timer.stop()

    def test_shutdown_waits_for_owned_worker(self):
        self.manager.gates['prepare'] = threading.Event()
        self.manager.entered['prepare'] = threading.Event()
        dialog = self.dialog()
        dialog.import_local('one.zip')
        self.wait(self.manager.entered['prepare'].is_set)
        closed = []
        dialog.shutdown_complete.connect(lambda: closed.append(True))
        dialog.shutdown()
        self.assertFalse(dialog.is_closed)
        self.wait(lambda: dialog.is_closed)
        self.assertIsNone(dialog._worker)
        self.assertFalse(dialog.busy)
        self.assertEqual(closed, [True])

    def test_startup_enabled_checks_only_and_source_change_invalidates_download(self):
        self.manager.settings.update(source_url='https://updates.example.invalid/release.json', startup_check=True)
        dialog = self.dialog()
        dialog.check_on_startup()
        self.wait(lambda: not dialog.busy)
        self.assertEqual(len(self.calls('check')), 1)
        self.assertFalse(self.calls('download'))
        self.assertFalse(self.calls('deploy'))
        self.assertTrue(dialog.download_button.isEnabled())
        dialog.source_edit.setText('https://different.example.invalid/release.json')
        self.assertFalse(dialog.download_button.isEnabled())

    def test_key_import_cancel_does_not_read_or_trust_file(self):
        dialog = self.dialog()
        path = str(self.root / 'absent-key.json')
        with (patch('ui.update_dialog.QFileDialog.getOpenFileName', return_value=(path, '')),
              patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No)):
            dialog._choose_key()
        self.assertFalse(dialog.busy)
        self.assertEqual(self.manager.settings['trusted_keys'], {})
        self.assertFalse(self.calls('save'))

    def test_key_import_explicit_confirmation_is_persisted(self):
        dialog = self.dialog()
        path = self.root / 'key.json'
        keys = {'publisher': {'algorithm': 'test-only', 'modulus': 'not-real'}}
        path.write_text(json.dumps({'trusted_keys': keys}), encoding='utf-8')
        with (patch('ui.update_dialog.QFileDialog.getOpenFileName', return_value=(str(path), '')),
              patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes)):
            dialog._choose_key()
        self.wait(lambda: not dialog.busy)
        self.assertEqual(self.manager.settings['trusted_keys'], keys)
        self.assertIn('1 个', dialog.trust_label.text())

    def test_manager_error_shown_without_success_and_can_retry(self):
        self.manager.failures['prepare'] = '发布文件 SHA-256 不匹配'
        dialog = self.dialog()
        dialog.import_local('bad.zip')
        self.wait(lambda: not dialog.busy)
        self.assertIsNone(dialog.preview)
        self.assertIn('SHA-256 不匹配', dialog.status_label.text())
        self.assertFalse(dialog.deploy_button.isEnabled())
        self.manager.failures.clear()
        self.prepare(dialog)

    def test_versions_selection_does_not_reuse_previous_confirmation(self):
        self.manager.records = [deepcopy(self.manager.record)]
        dialog = self.dialog()
        self.assertEqual(dialog.versions_table.rowCount(), 1)
        dialog.versions_table.selectRow(0)
        self.assertEqual(dialog.preview['id'], 'a' * 32)
        self.assertFalse(dialog.deploy_confirm_check.isChecked())

    def test_main_window_entry_uses_private_state_and_closes_update_worker(self):
        from ui.main_window import MainWindow
        from utils.config import ConfigStore
        with patch.object(MainWindow, 'detect_tools', return_value=None):
            window = MainWindow(ConfigStore(self.root / 'config.json'))
            window.showMinimized()
            try:
                self.app.processEvents()
                self.assertIsNone(window._update_dialog)
                window.update_button.click()
                self.wait(lambda: window._update_dialog is not None and not window._update_dialog.busy)
                dialog = window._update_dialog
                self.assertEqual(dialog.manager.state_dir, (self.root / 'Updates').resolve())
                self.assertEqual(window.update_button.text(), '更新与版本管理')
                self.assertTrue(dialog.isVisible())
                window.update_button.click()
                self.assertIs(window._update_dialog, dialog)
                self.assertTrue(dialog.isVisible())
                entered = threading.Event()
                def work(cancel, progress):
                    entered.set()
                    while not cancel.wait(.01):
                        pass
                    raise UpdateCancelled('主窗口关闭时取消')
                dialog._start('private_test', work)
                self.wait(entered.is_set)
                window.close()
                self.wait(lambda: window._update_dialog is None and window.library_service.is_closed
                          and window.storage_service.is_closed and window.batch_controller.is_closed)
                self.assertTrue(dialog.is_closed)
            finally:
                window.close()
                self.wait(lambda: window.library_service.is_closed and window.storage_service.is_closed
                          and window.batch_controller.is_closed)
                window.deleteLater()


if __name__ == '__main__':
    unittest.main()
