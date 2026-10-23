"""本地真实文件与 Windows 共享模式验证；不会读取用户的真实浏览器。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.data_migration import MigrationCancelled, MigrationError
from core.migration_browser import copy_profile, inspect_profile, _runtime_version


_VERSION = '155.0.4283.62'


def _proofs(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def _fixture(root):
    web = root / 'EBWebView'
    (web / 'Default/Network').mkdir(parents=True)
    (web / 'Default/Empty Directory').mkdir()
    (web / 'Local State').write_text(json.dumps({'profile': {'last_used': 'Default'}}), encoding='utf-8')
    (web / 'Default/Preferences').write_text(json.dumps({'profile': {'name': 'Offline unit fixture'}}), encoding='utf-8')
    (web / 'Last Version').write_text(_VERSION, encoding='utf-8')
    for name in ('Default/History', 'Default/Network/Cookies'):
        with closing(sqlite3.connect(web / name)) as db:
            db.execute('CREATE TABLE unit_fixture (id INTEGER PRIMARY KEY, note TEXT)')
            db.execute('INSERT INTO unit_fixture(note) VALUES(?)', ('no real accounts',))
            db.commit()
    (web / 'Default/LOCK').write_bytes(b'')
    (web / 'Default/Cache').mkdir()
    (web / 'Default/Cache/缓存.bin').write_bytes(bytes(range(256)) * 4097)
    (web / 'WV2Profile_mediaflow').mkdir()
    (web / 'WV2Profile_mediaflow/Preferences').write_text('{"profile":{"name":"MediaFlow"}}', encoding='utf-8')
    return root


@unittest.skipUnless(os.name == 'nt', 'Windows 原生拒写/删句柄测试')
class MigrationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = _fixture(self.root / 'old BrowserProfile')
        self.target = self.root / 'new BrowserProfile'
        self.processes = patch('core.migration_browser._browser_processes', return_value=[])
        self.version = patch('core.migration_browser._runtime_version', return_value=_VERSION)
        self.processes.start()
        self.version.start()

    def tearDown(self):
        self.processes.stop()
        self.version.stop()
        self.temp.cleanup()

    def test_inspect_requires_actual_offline_structure_without_exposing_payload(self):
        before = _proofs(self.source)
        result = inspect_profile(self.source)
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual(set(result), {'available', 'bytes', 'files_count', 'reason'})
        self.assertEqual(result['files_count'], len(before))
        self.assertEqual(result['bytes'], sum(path.stat().st_size for path in self.source.rglob('*') if path.is_file()))
        self.assertEqual(before, _proofs(self.source))
        self.assertNotIn('no real accounts', json.dumps(result))

    def test_full_copy_preserves_every_byte_empty_directory_and_source(self):
        before = _proofs(self.source)
        progress = []
        copied = copy_profile(self.source, self.target, progress=progress.append)
        self.assertEqual(copied['hashes'], before)
        self.assertEqual(_proofs(self.target), before)
        self.assertEqual(_proofs(self.source), before)
        self.assertTrue((self.target / 'EBWebView/Default/Empty Directory').is_dir())
        self.assertEqual(copied['files_count'], len(before))
        self.assertTrue(inspect_profile(self.target)['available'])
        for stage in ('browser-copy', 'browser-source-check', 'browser-target-check'):
            values = [item['current'] for item in progress if item['stage'] == stage]
            self.assertTrue(values)
            self.assertEqual(values, sorted(values))
            self.assertEqual(values[-1], copied['bytes'])
        self.assertFalse(any('path' in item or 'hashes' in item for item in progress))

    def test_existing_nonempty_target_is_never_overwritten(self):
        self.target.mkdir()
        preserved = self.target / 'existing-user-file'
        preserved.write_bytes(b'keep exactly')
        with self.assertRaisesRegex(MigrationError, '已有数据'):
            copy_profile(self.source, self.target)
        self.assertEqual(preserved.read_bytes(), b'keep exactly')
        self.assertEqual(list(self.target.iterdir()), [preserved])

    def test_same_source_and_nested_targets_are_rejected(self):
        before = _proofs(self.source)
        for target in (self.source, self.source / 'child', self.source.parent):
            with self.subTest(target=target), self.assertRaisesRegex(MigrationError, '独立'):
                copy_profile(self.source, target)
        self.assertEqual(_proofs(self.source), before)

    def test_missing_preferences_invalid_json_and_unrecognizable_sqlite_are_rejected(self):
        preferences = self.source / 'EBWebView/Default/Preferences'
        original = preferences.read_bytes()
        preferences.unlink()
        self.assertFalse(inspect_profile(self.source)['available'])
        preferences.write_bytes(b'not json')
        self.assertFalse(inspect_profile(self.source)['available'])
        preferences.write_bytes(original)
        (self.source / 'EBWebView/Default/History').write_bytes(b'not a SQLite database')
        self.assertFalse(inspect_profile(self.source)['available'])
        with self.assertRaises(MigrationError):
            copy_profile(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_only_confirmed_same_runtime_version_is_accepted(self):
        path = self.source / 'EBWebView/Last Version'
        for version in ('156.0.1.2', '154.0.1.2', '', 'not a version'):
            path.write_text(version)
            result = inspect_profile(self.source)
            self.assertFalse(result['available'])
            self.assertIn('Runtime', result['reason'])
        path.write_text(_VERSION)
        with patch('core.migration_browser._runtime_version', side_effect=MigrationError('无法确认Runtime')):
            self.assertFalse(inspect_profile(self.source)['available'])

    def test_unknown_last_browser_format_is_not_claimed_compatible(self):
        (self.source / 'EBWebView/Last Browser').write_bytes(b'unknown version-specific marker')
        self.assertFalse(inspect_profile(self.source)['available'])
        with self.assertRaisesRegex(MigrationError, '版本'):
            copy_profile(self.source, self.target)

    def test_active_writer_blocks_snapshot_and_copy_using_real_windows_sharing(self):
        file = self.source / 'EBWebView/Default/Network/Cookies'
        before = file.read_bytes()
        with file.open('r+b'):
            result = inspect_profile(self.source)
            self.assertFalse(result['available'])
            self.assertIn('正在使用', result['reason'])
            with self.assertRaisesRegex(MigrationError, '正在使用'):
                copy_profile(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(file.read_bytes(), before)

    def test_all_source_handles_deny_write_and_delete_until_copy_ends(self):
        watched = self.source / 'EBWebView/WV2Profile_mediaflow/Preferences'
        attempts = []
        def verify_locks(progress):
            if attempts or progress['stage'] != 'browser-copy':
                return
            try:
                with watched.open('r+b'):
                    attempts.append('write allowed')
            except PermissionError:
                attempts.append('write denied')
            try:
                watched.unlink()
                attempts.append('delete allowed')
            except PermissionError:
                attempts.append('delete denied')
        before = _proofs(self.source)
        copy_profile(self.source, self.target, progress=verify_locks)
        self.assertEqual(attempts, ['write denied', 'delete denied'])
        self.assertEqual(before, _proofs(self.source))
        with watched.open('r+b') as stream:
            self.assertTrue(stream.read(1))  # 所有锁在函数返回后已释放。

    def test_singleton_and_leveldb_lock_file_writer_is_not_copied_live(self):
        lock = self.source / 'EBWebView/SingletonLock'
        lock.write_bytes(b'unit lock marker')
        with lock.open('r+b'):
            self.assertFalse(inspect_profile(self.source)['available'])
        copied = copy_profile(self.source, self.target)
        self.assertIn('EBWebView/SingletonLock', copied['hashes'])

    def test_process_userdata_path_matches_exactly_including_spaces(self):
        same = str(self.source / 'EBWebView')
        with patch('core.migration_browser._browser_processes', return_value=[{
                'Name': 'msedgewebview2.exe', 'CommandLine': f'"C:\\WebView\\msedgewebview2.exe" --user-data-dir="{same}" --type=browser'}]):
            result = inspect_profile(self.source)
            self.assertFalse(result['available'])
            self.assertNotIn(same, result['reason'])
            self.assertIn('正在使用', result['reason'])
        with patch('core.migration_browser._browser_processes', return_value=[{
                'CommandLine': f'webview.exe --user-data-dir "{self.source}-other\\EBWebView"'}]):
            self.assertTrue(inspect_profile(self.source)['available'])

    def test_unknown_or_inaccessible_process_status_fails_closed_without_cmdline_output(self):
        with patch('core.migration_browser._browser_processes', return_value=[{'CommandLine': None}]):
            result = inspect_profile(self.source)
            self.assertFalse(result['available'])
            self.assertIn('无法确认', result['reason'])
        with patch('core.migration_browser._browser_processes', side_effect=MigrationError('无法确认浏览器进程的安全状态')):
            self.assertFalse(inspect_profile(self.source)['available'])

    def test_cancellation_before_and_during_copy_preserves_source_and_keeps_partial_target(self):
        event = threading.Event()
        before = _proofs(self.source)
        event.set()
        with self.assertRaises(MigrationCancelled):
            copy_profile(self.source, self.target, cancel_event=event)
        self.assertFalse(self.target.exists())
        event.clear()
        def cancel(progress):
            if progress['stage'] == 'browser-copy':
                event.set()
        with self.assertRaises(MigrationCancelled):
            copy_profile(self.source, self.target, cancel_event=event, progress=cancel)
        self.assertTrue(self.target.is_dir())
        self.assertTrue(any(self.target.rglob('*')))
        self.assertEqual(before, _proofs(self.source))

    def test_new_source_file_during_copy_aborts_instead_of_claiming_consistent_snapshot(self):
        changed = []
        def mutate(progress):
            if not changed and progress['stage'] == 'browser-copy':
                (self.source / 'EBWebView/new-during-copy').write_bytes(b'external writer')
                changed.append(True)
        with self.assertRaisesRegex(MigrationError, '变化'):
            copy_profile(self.source, self.target, progress=mutate)
        self.assertTrue((self.source / 'EBWebView/new-during-copy').exists())

    def test_target_injected_file_aborts_and_is_preserved(self):
        changed = []
        injected = self.target / 'external-user-file'
        def mutate(progress):
            if not changed and progress['stage'] == 'browser-copy':
                injected.write_bytes(b'never overwrite or remove')
                changed.append(True)
        with self.assertRaises(MigrationError):
            copy_profile(self.source, self.target, progress=mutate)
        self.assertEqual(injected.read_bytes(), b'never overwrite or remove')

    def test_hardlinked_source_file_is_rejected(self):
        original = self.source / 'EBWebView/Default/Preferences'
        os.link(original, self.root / 'outside-alias')
        result = inspect_profile(self.source)
        self.assertFalse(result['available'])
        self.assertIn('链接', result['reason'])

    def test_junction_is_not_followed(self):
        import _winapi
        if not hasattr(_winapi, 'CreateJunction'):
            self.skipTest('此 Python 没有测试所需的原生 Junction 构造器')
        external = self.root / 'external'
        external.mkdir()
        (external / 'keep.txt').write_text('keep')
        _winapi.CreateJunction(str(external), str(self.source / 'EBWebView/link-outside'))
        self.assertFalse(inspect_profile(self.source)['available'])
        with self.assertRaises(MigrationError):
            copy_profile(self.source, self.target)
        self.assertEqual((external / 'keep.txt').read_text(), 'keep')


@unittest.skipUnless(os.name == 'nt', '本机 WebView2 原生运行时验证')
class SavedBrowserProfileTests(unittest.TestCase):
    def test_saved_localhost_profile_exact_runtime_and_complete_copy(self):
        value = os.environ.get('MEDIAFLOW_MIGRATION_PROFILE_FIXTURE')
        if not value:
            self.skipTest('仅在明确指定已保存的 localhost 专用验收配置时读取该样本')
        source = Path(value)
        self.assertTrue(source.is_dir())
        before = _proofs(source)
        inspected = inspect_profile(source)
        self.assertTrue(inspected['available'], inspected['reason'])
        with tempfile.TemporaryDirectory(prefix='MediaFlow-migration-browser-fixture-') as temporary:
            target = Path(temporary) / 'BrowserProfile'
            result = copy_profile(source, target)
            self.assertEqual(result['hashes'], before)
            self.assertEqual(_proofs(target), before)
            self.assertEqual(_proofs(source), before)
            self.assertTrue(inspect_profile(target)['available'])
            self.assertRegex(_runtime_version(), r'^\d+\.\d+\.\d+\.\d+$')


if __name__ == '__main__':
    unittest.main()
