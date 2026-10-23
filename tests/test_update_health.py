"""健康检查验证真实 SQLite/DPAPI 快照，不触碰任何用户目录。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.data_migration import DataDirectoryLease
from core.downloader import DownloadRequest
from core.media_history import HistoryStore
from core.batch_store import BatchStore
from core.task_store import TaskStore, _canonical
from core.update_health import health_report, run_health_cli, _write_report
from core.update_manifest import REQUIRED_FILES


def proofs(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


class UpdateHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-update-health-test-')
        self.root = Path(self.temporary.name)
        self.release = self.root / 'release'
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / '.mediaflow-data.lock').write_bytes(b'\0')
        self.runtime = self.root / 'runtime'
        (self.runtime / 'assets').mkdir(parents=True)
        (self.runtime / 'assets/app.ico').write_bytes(b'icon fixture')
        self.identity = {'app_version': '1.9.0', 'build_id': 'health-fixture-1',
                         'source_sha256': '1' * 64, 'packaged': True,
                         'runtime_root': str(self.runtime),
                         'executable': str(self.release / 'MediaFlow.exe')}
        for name in ('dash', 'track', 'organization', 'storage', 'migration'):
            self.identity[name + '_module_loaded'] = True
        for name in REQUIRED_FILES:
            file = self.release / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'MZ fixture file for structure check')
        (self.release / 'build-info.json').write_text(json.dumps(self.identity), encoding='utf-8')
        self.identity_patch = patch('core.update_health.runtime_identity', return_value=self.identity)
        self.identity_patch.start()

    def tearDown(self):
        self.identity_patch.stop()
        self.temporary.cleanup()

    def report(self, **kwargs):
        return health_report(self.release, self.data, '1.9.0', 'health-fixture-1', **kwargs)

    def existing(self):
        with HistoryStore(self.data / 'history.sqlite3'):
            pass
        with closing(BatchStore(self.data / 'batches.sqlite3')):
            pass
        store = TaskStore(self.data / 'tasks.json')
        self.assertTrue(store.save([]), store.last_error)
        (self.data / 'config.json').write_text('{"output_directory":"G:\\\\Media"}', encoding='utf-8')
        (self.data / 'BrowserProfile').mkdir()
        (self.data / 'BrowserProfile/keep.dat').write_bytes(b'original opaque browser data')
        (self.data / 'keep.mp4').write_bytes(b'original user media')

    def task(self):
        request = DownloadRequest('', 'https://example.test/video.mp4?signature=private-test-value',
                                  str(self.data / 'downloads'), filename='fixture',
                                  download_protocol='Direct', headers={'Cookie': 'private-test-cookie'})
        job = SimpleNamespace(id=7, request=request, name='暂停任务', state='paused',
                              progress={'downloaded_bytes': 100}, retry_count=0,
                              created_at='2026-10-21T00:00:00+00:00', updated_at='2026-10-21T00:00:00+00:00',
                              result={}, context_error='')
        store = TaskStore(self.data / 'tasks.json')
        self.assertTrue(store.save([job]), store.last_error)

    def test_existing_data_has_identical_hashes_and_no_new_store_files(self):
        self.existing()
        before = proofs(self.data)
        report = self.report()
        self.assertTrue(report['passed'], report)
        self.assertEqual(before, proofs(self.data))
        self.assertFalse(list(self.data.glob('.mediaflow-health-*')))
        self.assertIn('未在本检查中验证', report['message'])
        self.assertEqual({item['name'] for item in report['checks']}, {
            'runtime_identity', 'release_resources', 'data_permissions', 'history_database',
            'batch_database', 'task_snapshots', 'store_initialization', 'user_data_access'})

    def test_new_directory_initializes_only_private_stores(self):
        target = self.data / 'new version'
        report = health_report(self.release, target, '1.9.0', 'health-fixture-1')
        self.assertTrue(report['passed'], report)
        self.assertEqual([path.name for path in target.iterdir()], ['.mediaflow-data.lock'])

    def test_missing_tool_and_asset_fail_with_specific_reason(self):
        (self.release / 'tools/ffprobe.exe').unlink()
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(any('tools/ffprobe.exe' in error for error in report['errors']))
        (self.release / 'tools/ffprobe.exe').write_bytes(b'MZ')
        (self.runtime / 'assets/app.ico').unlink()
        self.assertTrue(any('图标资源' in error for error in self.report()['errors']))

    def test_runtime_and_release_identity_must_match(self):
        for kwargs in ({'expected_version': '1.10.0'}, {'expected_build': 'wrong'}):
            with self.subTest(kwargs=kwargs):
                report = health_report(self.release, self.data, **kwargs)
                self.assertFalse(report['passed'])
        self.identity['executable'] = str(self.root / 'another/MediaFlow.exe')
        self.assertTrue(any('不是已部署目录' in error for error in self.report()['errors']))
        self.identity['executable'] = str(self.release / 'MediaFlow.exe')
        (self.release / 'build-info.json').write_text('{"app_version":"1.8.0"}', encoding='utf-8')
        self.assertTrue(any('构建标识与实际' in error for error in self.report()['errors']))

    def test_wrong_database_schema_and_corrupt_bytes_preserve_original(self):
        path = self.data / 'history.sqlite3'
        for kind in ('foreign', 'missing_columns', 'corrupt'):
            with self.subTest(kind=kind):
                if path.exists():
                    path.unlink()
                if kind == 'corrupt':
                    path.write_bytes(b'not sqlite')
                else:
                    with closing(sqlite3.connect(path)) as database:
                        database.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY)')
                        if kind == 'missing_columns':
                            database.execute('PRAGMA application_id=1296451633')
                            database.execute('PRAGMA user_version=1')
                        database.commit()
                before = proofs(self.data)
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertFalse(next(item for item in report['checks'] if item['name'] == 'history_database')['passed'])
                self.assertEqual(before, proofs(self.data))

    def test_broken_optional_history_table_is_not_silently_rebuilt(self):
        self.existing()
        with closing(sqlite3.connect(self.data / 'history.sqlite3')) as database:
            database.execute('DROP TABLE migration_visibility')
            database.execute('CREATE TABLE migration_visibility (wrong INTEGER)')
            database.commit()
        before = proofs(self.data)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(any('附属记录结构' in error for error in report['errors']))
        self.assertEqual(before, proofs(self.data))

    def test_wrong_batch_schema_is_reported_without_mutating(self):
        path = self.data / 'batches.sqlite3'
        with closing(sqlite3.connect(path)) as database:
            database.execute('CREATE TABLE unknown (id INTEGER)')
            database.commit()
        before = proofs(self.data)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertEqual(before, proofs(self.data))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_encrypted_task_is_checked_without_exposing_secrets(self):
        self.task()
        before = proofs(self.data)
        report = self.report()
        self.assertTrue(report['passed'], report)
        task = next(item for item in report['checks'] if item['name'] == 'task_snapshots')['details']
        self.assertEqual(task['records'], 1)
        self.assertEqual(task['needs_reanalysis'], 0)
        self.assertNotIn('private-test', json.dumps(report))
        self.assertEqual(before, proofs(self.data))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_undecryptable_context_is_warning_and_preserves_ciphertext(self):
        self.task()
        path = self.data / 'tasks.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        document.pop('checksum')
        document['tasks'][0]['request_dpapi'] = 'invalid-envelope'
        document['checksum'] = hashlib.sha256(_canonical(document)).hexdigest()
        path.write_bytes(_canonical(document))
        before = proofs(self.data)
        report = self.report()
        self.assertTrue(report['passed'], report)
        task = next(item for item in report['checks'] if item['name'] == 'task_snapshots')['details']
        self.assertEqual(task['needs_reanalysis'], 1)
        self.assertTrue(any('重新分析' in warning for warning in report['warnings']))
        self.assertEqual(before, proofs(self.data))

    def test_valid_backup_is_read_without_restoring_main_snapshot(self):
        self.existing()
        (self.data / 'tasks.json').write_bytes(b'corrupted main')
        before = proofs(self.data)
        report = self.report()
        self.assertTrue(report['passed'], report)
        self.assertTrue(report['warnings'])
        self.assertEqual(before, proofs(self.data))
        (self.data / 'tasks.json.bak').write_bytes(b'corrupted backup')
        before = proofs(self.data)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertEqual(before, proofs(self.data))

    def test_incompatible_task_format_is_rejected_without_backup_overwrite(self):
        document = {'version': 999, 'tasks': []}
        document['checksum'] = hashlib.sha256(_canonical(document)).hexdigest()
        (self.data / 'tasks.json').write_bytes(_canonical(document))
        before = proofs(self.data)
        self.assertFalse(self.report()['passed'])
        self.assertEqual(before, proofs(self.data))

    def test_existing_application_lease_blocks_data_checks(self):
        self.existing()
        before = proofs(self.data)
        with DataDirectoryLease(self.data):
            report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(any('占用' in error for error in report['errors']))
        self.assertEqual(before, proofs(self.data))

    @unittest.skipUnless(os.name == 'nt', 'Windows shared-file access rules')
    def test_open_wal_database_is_not_copied_or_recovered_in_place(self):
        self.existing()
        with closing(sqlite3.connect(self.data / 'history.sqlite3')) as database:
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('PRAGMA wal_autocheckpoint=0')
            database.execute('CREATE TABLE health_fixture (value TEXT)')
            database.execute("INSERT INTO health_fixture VALUES ('committed WAL record')")
            database.commit()
            self.assertTrue((self.data / 'history.sqlite3-wal').is_file())
            before = proofs(self.data)
            report = self.report()
            self.assertFalse(report['passed'])
            self.assertTrue(any('数据文件正在使用或无法读取' in error for error in report['errors']))
            self.assertEqual(before, proofs(self.data))

    def test_empty_existing_lock_is_not_changed(self):
        (self.data / '.mediaflow-data.lock').write_bytes(b'')
        before = proofs(self.data)
        report = self.report()
        self.assertTrue(report['passed'], report)
        self.assertEqual(before, proofs(self.data))

    def test_permission_failure_is_reported_and_existing_files_untouched(self):
        self.existing()
        before = proofs(self.data)
        with patch('core.update_health.tempfile.TemporaryDirectory', side_effect=PermissionError('secret path')):
            report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(any('权限不足' in error for error in report['errors']))
        self.assertNotIn('secret path', json.dumps(report))
        self.assertEqual(before, proofs(self.data))

    def test_stray_journal_does_not_trigger_recovery_or_delete(self):
        self.existing()
        (self.data / 'history.sqlite3-journal').write_bytes(b'pending transaction')
        before = proofs(self.data)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertTrue(any('待恢复事务日志' in error for error in report['errors']))
        self.assertEqual(before, proofs(self.data))

    def test_cli_atomic_report_and_exit_status(self):
        path = self.root / 'report.json'
        actual = self.report()
        with patch('core.update_health.health_report', return_value=actual) as check:
            code = run_health_cli(['MediaFlow.exe', '--update-health', str(path),
                '--expected-version', '1.9.0', '--expected-build', 'health-fixture-1',
                '--health-data-directory', str(self.data)])
        self.assertEqual(code, 0)
        self.assertEqual(check.call_args.args[1:], (str(self.data), '1.9.0', 'health-fixture-1'))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), actual)
        self.assertFalse(list(path.parent.glob('.mediaflow-health-report-*')))
        actual['passed'] = False
        with patch('core.update_health.health_report', return_value=actual):
            self.assertEqual(run_health_cli(['--update-health', str(path), '--expected-version', '1.9.0',
                                            '--expected-build', 'health-fixture-1']), 1)

    def test_report_failure_preserves_existing_file(self):
        path = self.root / 'user-file.json'
        path.write_bytes(b'{"important":"keep"}')
        before = path.read_bytes()
        with self.assertRaises(RuntimeError):
            _write_report(path, self.report())
        self.assertEqual(before, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
