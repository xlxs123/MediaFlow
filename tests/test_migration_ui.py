"""真实 Qt 后台生命周期和确认边界；引擎替身只验证界面编排，不冒充迁移验收。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from ui.migration_dialog import (MigrationDialog, MigrationRecoveryDialog, known_migration_paths,
                                 should_offer_migration)


class MigrationCancelled(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


class FakeEngine:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.threads = []
        self.gates = {}
        self.entered = {}
        self.failure = {}
        self.sources = [{'path': str(root / 'old-data'), 'version': '1.7.0',
                         'profile_path': str(root / 'old-profile'),
                         'counts': {'library': 4, 'history': 5, 'tasks_recoverable': 2},
                         'types': ['settings', 'library', 'history', 'tasks'],
                         'issues': [], 'fingerprint': 'a' * 64}]
        self.runs = []
        self.closed = False

    def _call(self, operation, payload=None, cancel_event=None):
        self.calls.append((operation, deepcopy(payload)))
        self.threads.append(threading.current_thread())
        entered = self.entered.get(operation)
        if entered:
            entered.set()
        gate = self.gates.get(operation)
        if gate:
            deadline = time.monotonic() + 10
            while not gate.wait(.01):
                if cancel_event and cancel_event.is_set():
                    raise MigrationCancelled('测试迁移已安全取消。')
                if time.monotonic() > deadline:
                    raise MigrationError('测试闸门超时。')
        if operation in self.failure:
            raise MigrationError(self.failure[operation])

    def discover(self, paths=None):
        self._call('discover', paths)
        return deepcopy(self.sources)

    def recover_interrupted(self):
        self._call('recover')
        return []

    def list_runs(self):
        self._call('runs')
        return deepcopy(self.runs)

    def preview(self, source, selections=None):
        self._call('preview', {'source': source, 'selections': selections})
        return {'source': source, 'target': str(self.root / 'target'),
                'version': '1.7.0', 'target_version': '1.8.0',
                'counts': {'settings': 1, 'library': 4, 'history': 5},
                'items': [{'kind': 'library', 'source': source + '/history.sqlite3',
                           'target': str(self.root / 'target/history.sqlite3'),
                           'action': '合并且保留原件', 'count': 4}],
                'issues': [], 'estimated_bytes': 8192, 'files_to_check': 4, 'plan_id': 'preview-token'}

    def apply(self, plan, confirmed=False, browser_confirmed=False, cancel_event=None, progress=None):
        if progress:
            progress({'stage': 'copy', 'current': 1, 'total': 4, 'message': '后台测试进度'})
        self._call('apply', {'plan': plan, 'confirmed': confirmed,
                             'browser_confirmed': browser_confirmed}, cancel_event)
        report = {'id': 'test-run', 'status': 'completed', 'counts': {'library': 4},
                  'issues': [], 'report_path': str(self.root / 'migration-report.json')}
        self.runs.append(report)
        return report

    def rollback(self, run_id, confirmed=False, cancel_event=None, progress=None):
        self._call('rollback', {'id': run_id, 'confirmed': confirmed}, cancel_event)
        return {'id': run_id, 'status': 'rolled_back', 'counts': {'library': 4},
                'issues': [], 'report_path': str(self.root / 'rollback-report.json')}

    def close(self):
        self.closed = True


class MigrationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = FakeEngine(self.root)
        self.factory_threads = []
        def factory(target, profile):
            self.factory_threads.append(threading.current_thread())
            return self.engine
        self.factory = patch('ui.migration_dialog._migration_engine', factory)
        self.factory.start()
        self.dialogs = []

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.003)
        self.fail('迁移界面后台操作超时')

    def tearDown(self):
        for gate in self.engine.gates.values():
            gate.set()
        for dialog in self.dialogs:
            dialog.reject()
            self.wait(lambda current=dialog: current.is_closed)
            dialog.deleteLater()
        self.factory.stop()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def dialog(self, **fields):
        dialog = MigrationDialog(self.root / 'target', target_profile=self.root / 'new-profile', **fields)
        self.dialogs.append(dialog)
        dialog.showMinimized()
        self.wait(lambda: bool(dialog.sources) and not dialog.worker_busy)
        return dialog

    def preview(self, dialog):
        dialog.preview_button.click()
        self.wait(lambda: dialog.plan is not None and not dialog.worker_busy)
        return dialog.plan

    def calls(self, operation):
        return [payload for name, payload in self.engine.calls if name == operation]

    def test_discovery_and_preview_use_background_thread_and_do_not_apply(self):
        dialog = self.dialog()
        self.assertFalse(dialog.selections['browser'].isChecked())
        self.assertTrue(all(dialog.selections[key].isChecked() for key in ('settings', 'library', 'history')))
        self.assertTrue(all(not dialog.selections[key].isChecked() for key in ('organization', 'batches', 'tasks')))
        self.assertFalse(dialog.apply_plan())
        self.preview(dialog)
        self.assertFalse(dialog.confirm_check.isChecked())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn(str(self.root / 'old-data'), dialog.preview_table.item(0, 1).toolTip())
        self.assertFalse(dialog.apply_plan())
        self.assertEqual(self.calls('apply'), [])
        self.assertTrue(all(thread is not threading.main_thread()
                            for thread in self.engine.threads + self.factory_threads))
        self.assertEqual(self.calls('preview')[0]['selections']['settings_policy'], 'keep')
        self.assertLess([name for name, _ in self.engine.calls].index('recover'),
                        [name for name, _ in self.engine.calls].index('discover'))

    def test_actual_checkbox_and_button_pass_confirmed_plan(self):
        dialog = self.dialog()
        plan = self.preview(dialog)
        dialog.confirm_check.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.apply_button.click()
        self.wait(lambda: dialog.last_report is not None and not dialog.worker_busy)
        calls = self.calls('apply')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['confirmed'])
        self.assertFalse(calls[0]['browser_confirmed'])
        self.assertEqual(calls[0]['plan'], plan)
        self.assertIsNone(dialog.plan)
        self.assertFalse(dialog.confirm_check.isChecked())
        self.assertEqual(dialog.last_report['status'], 'completed')
        self.assertIn('migration-report.json', dialog.report_summary.text())

    def test_browser_requires_separate_confirmation_and_is_never_implicit(self):
        dialog = self.dialog()
        dialog.selections['browser'].setChecked(True)
        self.preview(dialog)
        dialog.confirm_check.setChecked(True)
        self.assertFalse(dialog.apply_plan())
        self.assertEqual(self.calls('apply'), [])
        dialog.browser_confirm_check.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.apply_button.click()
        self.wait(lambda: bool(self.calls('apply')) and not dialog.worker_busy)
        self.assertTrue(self.calls('apply')[0]['browser_confirmed'])
        self.assertTrue(self.calls('preview')[0]['selections']['browser'])

    def test_source_or_policy_changes_invalidate_plan_and_both_confirmations(self):
        dialog = self.dialog()
        self.preview(dialog)
        dialog.confirm_check.setChecked(True)
        dialog.settings_policy.setCurrentIndex(1)
        self.assertIsNone(dialog.plan)
        self.assertFalse(dialog.confirm_check.isChecked())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.preview(dialog)
        self.assertEqual(self.calls('preview')[-1]['selections']['settings_policy'], 'source')
        dialog.selections['tasks'].setChecked(True)
        self.assertIsNone(dialog.plan)
        self.assertEqual(self.calls('apply'), [])

    def test_multiple_sources_require_explicit_selection(self):
        self.engine.sources.append({**self.engine.sources[0], 'path': str(self.root / 'old-two'), 'version': '1.6.0'})
        dialog = self.dialog()
        self.assertIsNone(dialog.selected_source())
        self.assertFalse(dialog.preview_button.isEnabled())
        dialog.sources_table.selectRow(1)
        self.preview(dialog)
        self.assertEqual(self.calls('preview')[0]['source'], str(self.root / 'old-two'))

    def test_slow_apply_keeps_ui_timers_alive_and_cancel_is_bounded(self):
        dialog = self.dialog()
        self.preview(dialog)
        entered, release = threading.Event(), threading.Event()
        self.engine.entered['apply'] = entered
        self.engine.gates['apply'] = release
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        try:
            dialog.confirm_check.setChecked(True)
            dialog.apply_button.click()
            self.wait(entered.is_set)
            self.wait(lambda: len(ticks) >= 15)
            self.assertTrue(dialog.worker_busy)
            self.assertFalse(dialog.discover_sources())
            self.assertFalse(dialog.apply_plan())
            self.assertEqual(dialog.progress_bar.value(), 250)
            dialog.cancel_button.click()
            self.wait(lambda: not dialog.worker_busy)
            self.assertIn('安全取消', dialog.status.text())
            self.assertIsNone(dialog.plan)
            self.assertEqual(len(self.calls('apply')), 1)
        finally:
            timer.stop()
            release.set()

    def test_close_during_apply_waits_for_safe_cancel_then_marks_assistant(self):
        dialog = self.dialog(first_run=True)
        self.preview(dialog)
        entered, release = threading.Event(), threading.Event()
        self.engine.entered['apply'] = entered
        self.engine.gates['apply'] = release
        dialog.confirm_check.setChecked(True)
        dialog.apply_button.click()
        self.wait(entered.is_set)
        dialog.close()
        self.assertFalse(dialog.is_closed)
        self.wait(lambda: dialog.is_closed)
        self.assertTrue(self.engine.closed)
        state = json.loads((self.root / 'target/migration-assistant.json').read_text())
        self.assertTrue(state['handled'])
        self.assertNotIn('source', state)

    def test_preview_failure_allows_retry_without_running_apply(self):
        dialog = self.dialog()
        self.engine.failure['preview'] = '目标数据库正被其他实例使用，请关闭后重试。'
        dialog.preview_button.click()
        self.wait(lambda: not dialog.worker_busy)
        self.assertIn('其他实例', dialog.issues.toPlainText())
        self.assertTrue(dialog.preview_button.isEnabled())
        self.assertFalse(dialog.apply_button.isEnabled())
        del self.engine.failure['preview']
        self.preview(dialog)
        self.assertEqual(self.calls('apply'), [])

    def test_rollback_requires_selected_run_and_its_own_confirmation(self):
        self.engine.runs = [{'id': 'prior-run', 'status': 'completed'}]
        dialog = self.dialog()
        self.assertFalse(dialog.request_rollback())
        dialog.runs_combo.setCurrentIndex(1)
        self.assertFalse(dialog.request_rollback())
        dialog.rollback_confirm_check.setChecked(True)
        self.assertTrue(dialog.rollback_button.isEnabled())
        dialog.rollback_button.click()
        self.wait(lambda: dialog.last_report is not None and not dialog.worker_busy)
        self.assertEqual(self.calls('rollback'), [{'id': 'prior-run', 'confirmed': True}])
        self.assertEqual(dialog.last_report['status'], 'rolled_back')
        self.assertFalse(dialog.rollback_confirm_check.isChecked())

    def test_preview_large_item_list_has_bounded_page_and_full_path_tooltip(self):
        dialog = self.dialog()
        plan = self.preview(dialog)
        dialog.plan = dict(plan, items=[{**plan['items'][0], 'source': f'C:/旧版/{index}.mp4'} for index in range(10005)])
        dialog._render_preview()
        self.assertEqual(dialog.preview_table.rowCount(), dialog.PAGE_SIZE)
        dialog.next_button.click()
        self.assertEqual(dialog.preview_table.item(0, 1).toolTip(), 'C:/旧版/100.mp4')
        self.assertIn('10005', dialog.page_label.text())
        self.assertEqual(self.calls('apply'), [])

    def test_startup_detection_only_known_data_and_skip_prevents_repeated_prompt(self):
        with patch.dict(os.environ, {'APPDATA': str(self.root)}):
            target = self.root / 'MediaFlow/1.8.0'
            self.assertFalse(should_offer_migration(target))
            source = self.root / 'MediaFlow/1.7.0'
            source.mkdir(parents=True)
            original = b'{"window_width":1040}'
            (source / 'config.json').write_bytes(original)
            self.assertTrue(should_offer_migration(target))
            self.assertIn(source, known_migration_paths(target))
            dialog = MigrationDialog(target, first_run=True)
            self.dialogs.append(dialog)
            dialog.showMinimized()
            self.wait(lambda: bool(dialog.sources) and not dialog.worker_busy)
            dialog.close_button.click()
            self.wait(lambda: dialog.is_closed)
            self.assertFalse(should_offer_migration(target))
            self.assertEqual((source / 'config.json').read_bytes(), original)

    def test_recovery_gate_closes_worker_before_allowing_database_startup(self):
        recovery = MigrationRecoveryDialog(self.root / 'target')
        recovery.showMinimized()
        self.wait(lambda: recovery.is_closed)
        self.assertTrue(recovery.passed)
        self.assertTrue(self.engine.closed)
        self.assertEqual(self.calls('discover'), [])
        recovery.deleteLater()


class RealMigrationDialogTests(unittest.TestCase):
    """本地真实 SQLite/config 导入及回滚，不接触用户数据或浏览器。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.003)
        self.fail('真实迁移 UI 操作超时')

    def test_real_store_preview_confirm_import_rollback_and_source_preservation(self):
        from core.media_history import HistoryStore
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / '1.7.0', root / '1.8.0'
            source.mkdir()
            (source / 'config.json').write_text(json.dumps({'window_width': 1230,
                'output_directory': str(root / 'media')}), encoding='utf-8')
            (source / 'data-manifest.json').write_text(json.dumps({'application': 'MediaFlow',
                'version': '1.7.0'}), encoding='utf-8')
            media = root / 'media/本地文件.mp4'
            media.parent.mkdir()
            media.write_bytes(b'local bytes; UI history test, not media validation')
            with HistoryStore(source / 'history.sqlite3') as store:
                store.record({'task_key': 'local-completed', 'title': '本地记录',
                              'task_state': 'completed', 'files': [{'path': str(media)}]})
            proofs = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir() if path.is_file()}
            dialog = MigrationDialog(target, source_paths=(source,))
            dialog.showMinimized()
            try:
                self.wait(lambda: bool(dialog.sources) and not dialog.worker_busy)
                dialog.preview_button.click()
                self.wait(lambda: dialog.plan is not None and not dialog.worker_busy)
                self.assertEqual(dialog.preview_table.rowCount(), 7)
                self.assertFalse(dialog.apply_plan())
                self.assertFalse((target / 'history.sqlite3').exists())
                dialog.confirm_check.setChecked(True)
                dialog.apply_button.click()
                self.wait(lambda: dialog.last_report is not None and not dialog.worker_busy)
                self.assertEqual(dialog.last_report['status'], 'completed')
                with HistoryStore(target / 'history.sqlite3') as store:
                    self.assertEqual(store.query()['total'], 1)
                self.wait(lambda: dialog.runs_combo.findData(dialog.last_report['id']) >= 0
                          and not dialog.worker_busy)
                dialog.runs_combo.setCurrentIndex(dialog.runs_combo.findData(dialog.last_report['id']))
                dialog.rollback_confirm_check.setChecked(True)
                dialog.rollback_button.click()
                self.wait(lambda: dialog.last_report.get('status') == 'rolled_back' and not dialog.worker_busy)
                with HistoryStore(target / 'history.sqlite3') as store:
                    self.assertEqual(store.query()['total'], 0)
                self.assertEqual(proofs, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in source.iterdir() if path.is_file()})
                self.assertTrue(media.is_file())
            finally:
                dialog.reject()
                self.wait(lambda: dialog.is_closed)
                dialog.deleteLater()
                self.app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class MigrationApplicationLifecycleTests(unittest.TestCase):
    def test_main_releases_data_lease_before_migration_then_reloads_window(self):
        from main import _run_interactive
        log = []
        class Lease:
            active = False
            def __init__(self, path):
                self.path = path
            def acquire(self):
                self.assert_idle()
                Lease.active = True
                log.append('lease-acquire')
            def assert_idle(self):
                if Lease.active:
                    raise AssertionError('lease overlaps')
            def close(self):
                Lease.active = False
                log.append('lease-close')
        class Window:
            count = 0
            def __init__(self, config):
                if not Lease.active:
                    raise AssertionError('window without data lease')
                Window.count += 1
                self._migration_requested = Window.count == 1
                log.append('window-' + str(Window.count))
            def show(self):
                log.append('show')
            def deleteLater(self):
                log.append('delete-window')
        class App:
            def setQuitOnLastWindowClosed(self, value):
                pass
            def exec(self):
                log.append('app-exec')
                return 0
        class Dialog:
            def __init__(self, *args, **kwargs):
                if Lease.active:
                    raise AssertionError('migration overlaps active services')
                log.append('migration')
            def exec(self):
                log.append('migration-exec')
            def deleteLater(self):
                pass
        fake = SimpleNamespace(DataDirectoryLease=Lease, MigrationError=MigrationError)
        config = SimpleNamespace(path=Path('C:/test-target/config.json'), legacy_path=None)
        with patch.dict('sys.modules', {'core.data_migration': fake}), \
             patch('ui.migration_dialog.should_offer_migration', return_value=False), \
             patch('ui.migration_dialog.MigrationDialog', Dialog), \
             patch('ui.webview2_browser.browser_profile_path', return_value=Path('C:/test-profile')):
            result = _run_interactive(App(), Window, config_store=config)
        self.assertEqual(result, 0)
        self.assertEqual([item for item in log if item in ('lease-acquire', 'lease-close', 'migration')],
                         ['lease-acquire', 'lease-close', 'migration', 'lease-acquire', 'lease-close'])
        self.assertEqual(Window.count, 2)

    def test_mainwindow_requires_confirmation_before_requesting_migration(self):
        from ui.main_window import MainWindow
        class Window:
            _migration_requested = False
            status_label = SimpleNamespace(setText=lambda value: None)
            enabled = True
            close_called = False
            def setEnabled(self, enabled):
                self.enabled = enabled
            def close(self):
                self.close_called = True
        window = Window()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            MainWindow.request_data_migration(window)
        self.assertFalse(window._migration_requested)
        self.assertFalse(window.close_called)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            MainWindow.request_data_migration(window)
        self.assertTrue(window._migration_requested)
        self.assertTrue(window.close_called)
        self.assertFalse(window.enabled)


if __name__ == '__main__':
    unittest.main()
