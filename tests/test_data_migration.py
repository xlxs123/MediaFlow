"""Migration contracts exercised against real SQLite files, DPAPI and partial bytes."""
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from core.data_migration import MigrationEngine, MigrationError, DataDirectoryLease, _hash, _connect
from core.downloader import DownloadRequest
from core.media_history import HistoryStore
from core.recovery_workspace import RecoveryWorkspace
from core.task_store import TaskStore


def history_fixture(directory, cls=HistoryStore):
    directory.mkdir(parents=True, exist_ok=True)
    media = directory / '中文样本.mp4'
    media.write_bytes(b'real media bytes for index migration')
    stat = media.stat()
    snapshot = {'task_key': 'a' * 64, 'queue_id': 4, 'title': '中文样本', 'source_site': 'douyin.com',
                'source_url': 'https://www.douyin.com/user/self?modal_id=7526879737634950451',
                'task_state': 'completed', 'error': '', 'progress': {'percent': 100},
                'started_at': '2026-10-20T10:00:00+00:00', 'finished_at': '2026-10-20T10:01:00+00:00',
                'cancelled_at': '', 'files': [{'path': str(media), 'validation': {'size': stat.st_size,
                  'mtime_ns': stat.st_mtime_ns, 'duration': 19.086, 'format_name': 'mov,mp4',
                  'video_streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 320, 'height': 180}],
                  'audio_streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'language': 'eng', 'channels': 2}]}}]}
    with cls(directory / 'history.sqlite3') as store:
        store.record(snapshot)
        failed = dict(snapshot, task_key='b' * 64, title='失败任务', task_state='failed', error='网络中断', files=[])
        store.record(failed)
        cancelled = dict(snapshot, task_key='c' * 64, title='取消任务', task_state='cancelled', files=[])
        store.record(cancelled)
    return snapshot, media


def task_fixture(directory, *, corrupt=False, workspace=True):
    request = DownloadRequest('', 'https://example.test/video.mp4', str(directory / 'downloads'),
                              filename='video', output_extension='mp4', download_protocol='Direct')
    request.resume_identity = {'protocol': 'Direct', 'layout_sha256': 'fixture'}
    if workspace:
        ws = RecoveryWorkspace(request, request.resume_identity, 'Direct')
        ws.prepare()
        (ws.temporary / 'video.mp4.part').write_bytes(b'a' * 1100000)
        ws.checkpoint(final=True)
        ws.close()
    job = SimpleNamespace(id=9, request=request, name='暂停任务', state='paused', progress={'downloaded_bytes': 1100000} if workspace else {},
           created_at='2026-10-20T10:00:00+00:00', updated_at='2026-10-20T10:01:00+00:00',
           retry_count=0, result={}, context_error='')
    store = TaskStore(directory / 'tasks.json')
    assert store.save([job]), store.last_error
    if corrupt:
        from core.task_store import _canonical
        data = json.loads(store.path.read_text(encoding='utf-8'))
        data['tasks'][0]['request_dpapi'] = 'not-a-dpapi-envelope'
        data.pop('checksum')
        data['checksum'] = _hash(data)
        store.path.write_bytes(_canonical(data))
    return request


def tree_hash(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-migration-test-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.target = self.root / 'target'
        self.engine = MigrationEngine(self.target)
        (self.source / 'config.json').write_text(json.dumps({'output_directory': str(self.root / 'downloads'), 'max_concurrent_videos': 5,
            'Cookie': 'do-not-export', 'yt_dlp_path': str(self.root / 'old/yt-dlp.exe'), 'yt_dlp_mode': 'custom'}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, **choice):
        return self.engine.apply(self.engine.preview(self.source, choice), confirmed=True)

    def test_discovery_manual_multi_unknown_version_not_inferred_from_folder(self):
        other = self.root / '1.7.0'
        other.mkdir()
        (other / 'config.json').write_text('{"window_width":1100}', encoding='utf-8')
        sources = self.engine.discover([self.source, other])
        self.assertEqual(len(sources), 2)
        self.assertIn('未知', sources[1]['version'])
        self.assertEqual(self.engine.discover([self.root / 'absent']), [])
        with patch.dict(os.environ, {'APPDATA': str(self.root / 'empty')}):
            self.assertEqual(self.engine.discover(), [])

    def test_settings_whitelist_and_stale_tool_fallback_preserve_source(self):
        before = tree_hash(self.source)
        report = self.apply()
        self.assertEqual(report['status'], 'completed', report)
        config = json.loads((self.target / 'config.json').read_text())
        self.assertEqual(config['max_concurrent_videos'], 5)
        self.assertEqual(config['yt_dlp_mode'], 'auto')
        self.assertNotIn('Cookie', config)
        self.assertNotIn('do-not-export', Path(report['report_path']).read_text(encoding='utf-8'))
        self.assertEqual(tree_hash(self.source), before)
        self.assertTrue((self.target / 'migration' / report['id'] / 'backup/snapshot.json').is_file())

    def test_conflicting_settings_keep_or_source_and_preview_stale(self):
        self.target.mkdir()
        (self.target / 'config.json').write_text('{"max_concurrent_videos":2}', encoding='utf-8')
        self.apply()
        self.assertEqual(json.loads((self.target / 'config.json').read_text())['max_concurrent_videos'], 2)
        self.apply(settings_policy='source')
        self.assertEqual(json.loads((self.target / 'config.json').read_text())['max_concurrent_videos'], 5)
        plan = self.engine.preview(self.source)
        plan['items']['browser'] = True
        with self.assertRaises(MigrationError):
            self.engine.apply(plan, confirmed=True)

    def test_confirmation_source_changes_and_space_preflight(self):
        plan = self.engine.preview(self.source)
        with self.assertRaises(MigrationError):
            self.engine.apply(plan)
        (self.source / 'config.json').write_text('{"window_width":1200}')
        with self.assertRaises(MigrationError):
            self.engine.apply(plan, confirmed=True)
        with patch('core.data_migration.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaises(MigrationError):
                self.apply()
        self.assertFalse((self.target / 'config.json').exists())

    def test_library_history_missing_duplicate_and_restart(self):
        _, media = history_fixture(self.source)
        media.unlink()
        before = tree_hash(self.source)
        report = self.apply()
        self.assertEqual(report['status'], 'completed', report)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            self.assertEqual(store.query()['total'], 1)
            self.assertEqual(store.query()['items'][0]['file_state'], 'missing')
            self.assertEqual(store.query(view='history')['total'], 3)
        self.engine = MigrationEngine(self.target)
        again = self.apply()
        self.assertEqual(again['counts']['history'], 0)
        self.assertEqual(again['counts']['skipped'], 3)
        self.assertEqual(tree_hash(self.source), before)

    def test_independent_history_library_selections(self):
        history_fixture(self.source)
        self.apply(library=False, history=True)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            self.assertEqual(store.query()['total'], 0)
            self.assertEqual(store.query(view='history')['total'], 3)
        second = MigrationEngine(self.root / 'library-only')
        report = second.apply(second.preview(self.source, {'library': True, 'history': False}), confirmed=True)
        self.assertEqual(report['status'], 'completed', report)
        with HistoryStore(second.target / 'history.sqlite3') as store:
            self.assertEqual(store.query()['total'], 1)
            self.assertEqual(store.query(view='history')['total'], 0)

    def test_conflicting_record_is_not_merged_by_same_path(self):
        snapshot, _ = history_fixture(self.source)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            store.record(dict(snapshot, title='different record'))
        report = self.apply()
        self.assertEqual(report['status'], 'completed', report)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            self.assertEqual(store.query()['total'], 2)
        self.assertEqual(report['counts']['conflicts'], 1)

    def test_rollback_preserves_later_new_records_and_changed_settings(self):
        history_fixture(self.source)
        report = self.apply()
        config = json.loads((self.target / 'config.json').read_text())
        config['max_concurrent_videos'] = 7
        (self.target / 'config.json').write_text(json.dumps(config))
        with HistoryStore(self.target / 'history.sqlite3') as store:
            store.record({'task_key': 'd' * 64, 'title': '后来新增', 'task_state': 'failed', 'files': []})
        result = self.engine.rollback(report['id'], confirmed=True)
        self.assertEqual(result['status'], 'rollback_partial')
        self.assertEqual(json.loads((self.target / 'config.json').read_text())['max_concurrent_videos'], 7)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            self.assertEqual(store.query(view='history')['total'], 1)
            self.assertEqual(store.query(view='history')['items'][0]['title'], '后来新增')
        retried = self.apply()
        self.assertEqual(retried['status'], 'completed', retried)
        self.assertEqual(retried['counts']['history'], 3)
        with HistoryStore(self.target / 'history.sqlite3') as store:
            self.assertEqual(store.query(view='history')['total'], 4)
        self.assertEqual(self.apply()['counts']['history'], 0)

    def test_cancel_failure_and_interrupted_changes_rollback(self):
        history_fixture(self.source)
        with patch.object(self.engine, '_merge_history', side_effect=OSError('private-url-must-not-appear')):
            report = self.apply()
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('private-url', Path(report['report_path']).read_text(encoding='utf-8'))
        self.assertFalse(json.loads((self.target / 'config.json').read_text()))
        event = threading.Event()
        event.set()
        with self.assertRaises(MigrationError):
            self.engine.apply(self.engine.preview(self.source), confirmed=True, cancel_event=event)
        # Real write-ahead state as if power was lost just after a config write.
        with closing(self.engine._ledger()) as db:
            run = uuid.uuid4().hex
            report = {'id': run, 'status': 'running', 'source': str(self.source), 'target': str(self.target)}
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run, 'source', 'running', json.dumps(report)))
            db.commit()
            self.engine._change(db, run, 'config', 'window_width', {'__absent__': True}, 1300)
        recovered = MigrationEngine(self.target).recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertNotIn('window_width', json.loads((self.target / 'config.json').read_text()))

    def test_template_unknown_field_and_traversal_are_repaired(self):
        data = {'organization': {'filename_template': '{nonexistent}.{ext}', 'classification': 'custom', 'directory_template': '../escape'}}
        (self.source / 'config.json').write_text(json.dumps(data))
        plan = self.engine.preview(self.source, {'organization': True})
        self.assertEqual(plan['settings_preview']['organization']['classification'], 'none')
        self.assertTrue(plan['issues'])
        self.assertEqual(self.engine.apply(plan, confirmed=True)['status'], 'completed')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI is a Windows current-user facility')
    def test_safe_task_clones_verified_prefix_and_does_not_share_source_workspace(self):
        request = task_fixture(self.source)
        before = tree_hash(self.source)
        report = self.apply(tasks=True)
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(report['counts']['tasks_recoverable'], 1)
        loaded = TaskStore(self.target / 'tasks.json').load()
        self.assertEqual(loaded[0]['metadata']['state'], 'paused')
        copied = loaded[0]['request']
        self.assertNotEqual(copied.temporary_directory, request.temporary_directory)
        self.assertEqual((Path(copied.temporary_directory) / 'transfer/video.mp4.part').read_bytes(), b'a' * 1100000)
        self.assertEqual(tree_hash(self.source), before)
        workspace = RecoveryWorkspace(copied, copied.resume_identity, 'Direct')
        workspace.prepare()
        workspace.close()

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_undecryptable_and_changed_partial_become_waiting_context(self):
        request = task_fixture(self.source)
        (Path(request.temporary_directory) / 'transfer/video.mp4.part').write_bytes(b'changed')
        report = self.apply(tasks=True)
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(report['counts']['tasks_incompatible'], 1)
        loaded = TaskStore(self.target / 'tasks.json').load()
        self.assertIsNone(loaded[0]['request'])
        self.assertEqual(loaded[0]['metadata']['state'], 'waiting_context')
        # Exactly the fallback constructor used by DownloadQueue.restore_tasks.
        placeholder = DownloadRequest(url='https://mediaflow.invalid/context-required', **loaded[0]['metadata']['request'])
        self.assertFalse(placeholder.yt_dlp_path)
        other = self.root / 'bad-cipher'
        other.mkdir()
        task_fixture(other, corrupt=True)
        plan = self.engine.preview(other, {'tasks': True})
        self.assertEqual(plan['counts']['tasks_incompatible'], 1)

    def test_target_lease_blocks_migration(self):
        with DataDirectoryLease(self.target):
            with self.assertRaises(MigrationError):
                self.apply()

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_batches_preserve_completed_groups_and_pause_unfinished(self):
        from core.batch_store import BatchStore
        batch_id = str(uuid.uuid4())
        store = BatchStore(self.source / 'batches.sqlite3')
        items = [{'id': str(uuid.uuid4()), 'batch_id': batch_id, 'ordinal': n,
                  'url': f'https://www.douyin.com/user/self?modal_id={7526879737634950451+n}',
                  'state': state, 'queue_key': uuid.uuid4().hex, 'job_id': 15}
                 for n, state in enumerate(('completed', 'downloading'))]
        store.create_batch({'id': batch_id, 'title': '旧合集', 'options': {'collection_name': '合集',
            'track_preferences': {'audio_language': 'ja'}}}, items)
        store.close()
        before = tree_hash(self.source)
        report = self.apply(batches=True)
        self.assertEqual(report['status'], 'completed', report)
        loaded_store = BatchStore(self.target / 'batches.sqlite3')
        loaded = loaded_store.load()
        loaded_store.close()
        self.assertTrue(loaded['batches'][0]['paused'])
        self.assertEqual([i['state'] for i in loaded['items']], ['completed', 'paused'])
        self.assertEqual([i['job_id'] for i in loaded['items']], [0, 0])
        self.assertEqual(tree_hash(self.source), before)
        self.assertEqual(self.apply(batches=True)['counts']['batches'], 0)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_retry_after_partial_batch_rollback_checks_child_records(self):
        from core.batch_store import BatchStore
        batch_id = str(uuid.uuid4())
        source = BatchStore(self.source / 'batches.sqlite3')
        source.create_batch({'id': batch_id, 'title': '原批次'}, [
            {'id':str(uuid.uuid4()), 'batch_id':batch_id, 'ordinal':0,
             'url':'https://example.test/page', 'state':'waiting_discovery'}])
        source.close()
        first = self.apply(batches=True)
        target = BatchStore(self.target / 'batches.sqlite3')
        record = target.load()['batches'][0]
        record['title'] = '用户后续修改'
        target.save_batch(record)
        target.close()
        result = self.engine.rollback(first['id'], confirmed=True)
        self.assertEqual(result['status'], 'rollback_partial')
        retried = self.apply(batches=True)
        self.assertEqual(retried['status'], 'completed', retried)
        self.assertEqual(retried['counts']['batches'], 1)
        target = BatchStore(self.target / 'batches.sqlite3')
        data = target.load(); target.close()
        self.assertEqual(len(data['batches']), 2)
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(self.apply(batches=True)['counts']['batches'], 0)

    def test_prepared_but_unwritten_change_is_safely_rolled_back(self):
        self.target.mkdir()
        run = uuid.uuid4().hex
        with closing(self.engine._ledger()) as db:
            report = {'id': run, 'status': 'running', 'source': str(self.source), 'target': str(self.target)}
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run, 'source', 'running', json.dumps(report)))
            db.commit()
            self.engine._prepared(db, run, 'config', 'window_width', {'__absent__': True}, 1300)
        result = self.engine.recover_interrupted()[0]
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(result['rollback']['preserved'], 0)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_same_numeric_task_metadata_with_different_media_identity_is_not_silently_deduplicated(self):
        request = task_fixture(self.source)
        first = self.apply(tasks=True)
        self.assertEqual(first['status'], 'completed')
        # A real checkpoint and encrypted request for another media identity with
        # the same queue ID/name/public progress must be treated as a conflict.
        request.resume_identity = {'protocol': 'Direct', 'layout_sha256': 'another-media'}
        marker = Path(request.temporary_directory) / 'workspace.json'
        value = json.loads(marker.read_text(encoding='utf-8'))
        value['identity'] = hashlib.sha256(json.dumps(request.resume_identity, sort_keys=True).encode()).hexdigest()
        marker.write_text(json.dumps(value), encoding='utf-8')
        document = TaskStore._read_document(self.source / 'tasks.json')[1]
        from core.task_store import _canonical, _encode
        from dataclasses import fields
        import base64
        payload = {f.name: _encode(getattr(request, f.name)) for f in fields(DownloadRequest) if f.name != 'concurrency'}
        document['tasks'][0]['request_dpapi'] = base64.b64encode(TaskStore._protect(_canonical(payload))).decode('ascii')
        document.pop('checksum'); document['checksum'] = _hash(document)
        TaskStore._atomic_write(self.source / 'tasks.json', _canonical(document))
        second = self.apply(tasks=True)
        self.assertEqual(second['status'], 'failed')
        self.assertTrue(any('工作区标识' in s for s in second['issues']))
        self.assertEqual(len(TaskStore(self.target / 'tasks.json').load()), 1)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_copied_prefix_is_rechecked_before_queue_commit(self):
        from core.data_migration import frozen_file
        from contextlib import contextmanager
        request = task_fixture(self.source)
        original = Path(request.temporary_directory) / 'transfer/video.mp4.part'
        @contextmanager
        def changed_before_copy(path):
            if Path(path) == original:
                original.write_bytes(b'changed between inspect and copy')
            with frozen_file(path) as stream:
                yield stream
        plan = self.engine.preview(self.source, {'tasks': True})
        with patch('core.data_migration.frozen_file', changed_before_copy):
            result = self.engine.apply(plan, confirmed=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(TaskStore(self.target / 'tasks.json').load(), [])

    def test_corrupted_rollback_path_never_moves_unowned_directory(self):
        directory = self.root / 'not-owned'
        directory.mkdir(); (directory / 'keep.mp4').write_bytes(b'keep')
        run = uuid.uuid4().hex
        with closing(self.engine._ledger()) as db:
            self.engine._prepared(db, run, 'workspace', str(directory), None,
                {'keep.mp4': hashlib.sha256(b'keep').hexdigest()})
            result = self.engine._rollback(db, run)
        self.assertEqual(result['preserved'], 1)
        self.assertEqual((directory / 'keep.mp4').read_bytes(), b'keep')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_target_backup_only_is_not_replaced_with_a_new_empty_queue(self):
        self.target.mkdir()
        task_fixture(self.target)
        backup = self.target / 'tasks.json.bak'
        before = backup.read_bytes()
        (self.target / 'tasks.json').unlink()
        with self.assertRaises(MigrationError):
            self.apply(tasks=True)
        self.assertFalse((self.target / 'tasks.json').exists())
        self.assertEqual(backup.read_bytes(), before)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI')
    def test_numeric_target_id_reuse_does_not_hide_an_old_different_task(self):
        task_fixture(self.source)
        self.assertEqual(self.apply(tasks=True)['status'], 'completed')
        from core.task_store import _canonical
        document = TaskStore._read_document(self.target / 'tasks.json')[1]
        # Same numeric ID now belongs to a new task, not to the imported workspace.
        document['tasks'][0]['metadata']['created_at'] = '2026-10-21T12:00:00+00:00'
        document['tasks'][0]['metadata']['request']['temporary_directory'] = ''
        document.pop('checksum'); document['checksum'] = _hash(document)
        TaskStore._atomic_write(self.target / 'tasks.json', _canonical(document))
        result = self.apply(tasks=True)
        self.assertEqual(result['status'], 'failed')  # Old orphan workspace is retained, never overwritten.
        self.assertTrue(any('工作区标识' in message for message in result['issues']))
        current = TaskStore._read_document(self.target / 'tasks.json')[1]
        self.assertEqual(current['tasks'][0]['metadata']['created_at'], '2026-10-21T12:00:00+00:00')

    def test_cancelled_browser_copy_never_leaves_a_partial_runtime_profile_and_can_retry(self):
        from core.data_migration import MigrationCancelled
        (self.source / 'BrowserProfile').mkdir()
        def partial_copy(source, dest, **kwargs):
            self.assertNotEqual(Path(dest), self.engine.target_profile)
            Path(dest).mkdir(parents=True)
            (Path(dest) / 'partial-cookie-database').write_bytes(b'partial')
            raise MigrationCancelled('测试取消')
        def complete_copy(source, dest, **kwargs):
            self.assertNotEqual(Path(dest), self.engine.target_profile)
            Path(dest).mkdir(parents=True)
            (Path(dest) / 'complete-fixture').write_bytes(b'complete')
            return {'hashes': {'complete-fixture': hashlib.sha256(b'complete').hexdigest()}, 'bytes': 8, 'files_count': 1}
        with patch('core.migration_browser.inspect_profile', return_value={'available':True,'bytes':8,'files_count':1}):
            plan = self.engine.preview(self.source, {'browser': True})
            with patch('core.migration_browser.copy_profile', side_effect=partial_copy):
                result = self.engine.apply(plan, confirmed=True, browser_confirmed=True)
            self.assertEqual(result['status'], 'cancelled')
            self.assertFalse(self.engine.target_profile.exists())
            retry = self.engine.preview(self.source, {'browser': True})
            with patch('core.migration_browser.copy_profile', side_effect=complete_copy):
                result = self.engine.apply(retry, confirmed=True, browser_confirmed=True)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual((self.engine.target_profile / 'complete-fixture').read_bytes(), b'complete')

    def test_unknown_database_version_is_skipped_and_source_preserved(self):
        history_fixture(self.source)
        with _connect(self.source / 'history.sqlite3') as db:
            db.execute('PRAGMA user_version=999')
        before = tree_hash(self.source)
        plan = self.engine.preview(self.source)
        self.assertEqual(plan['counts']['history'], 0)
        self.assertTrue(plan['issues'])
        self.assertEqual(tree_hash(self.source), before)

    def test_audited_historical_source_schemas(self):
        # Use actual old schema constructors where available; never open real old data for writes.
        repository = Path(__file__).resolve().parents[2]
        for suffix in ('', '-1.5.0', '-1.6.0', '-1.7.0'):
            path = repository / ('m3u8-downloader' + suffix) / 'core/media_history.py'
            if not path.is_file():
                continue
            import types
            module = types.ModuleType('historical_history')
            module.__file__ = str(path)
            # Read/compile only: do not create __pycache__ in protected old sources.
            exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
            source = self.root / ('historical' + suffix)
            history_fixture(source, module.HistoryStore)
            engine = MigrationEngine(self.root / ('imported' + suffix))
            report = engine.apply(engine.preview(source), confirmed=True)
            self.assertEqual(report['status'], 'completed', report)
            with HistoryStore(engine.target / 'history.sqlite3') as store:
                self.assertEqual(store.query(view='history')['total'], 3)


if __name__ == '__main__':
    unittest.main()
