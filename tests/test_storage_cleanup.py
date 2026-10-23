"""清理事务故障注入使用本地隔离回收替身；原生回收站另有正式EXE验收。"""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.downloader import DownloadRequest
from core.download_queue import DownloadJob, DownloadQueue
from core.media_history import HistoryStore
from core.media_storage_scan import StorageScanner
from core.storage_cleanup import StorageCleanup, StorageCleanupError
from core.library_organizer import OrganizationError
from core.storage_safety import ProtectionPolicy, queue_snapshot
from core.task_store import TaskStore


class StorageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = HistoryStore(self.root / 'history.sqlite3')
        self.a, self.b = self.root / 'first.mp4', self.root / 'copy.webm'
        for index, path in enumerate((self.a, self.b)):
            path.write_bytes(b'actual complete identical bytes')
            self.store.record({'task_key': 'file-' + str(index), 'queue_id': index + 1,
                'title': 'different title ' + str(index), 'task_state': 'completed',
                'files': [{'path': str(path), 'file_state': 'exists'}]})
        self.calls = []
        self.cleanup = StorageCleanup(self.root / 'cleanup', self.recycle)
        self.policy = ProtectionPolicy({'jobs': []})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def recycle(self, path, proof, receipt, nonce=None, **kwargs):
        self.calls.append(path)
        destination = self.root / ('unit-recycled-' + nonce)
        Path(path).rename(destination)
        result = {'recycled': True, 'path': path, 'sha256': proof['sha256'], 'size': proof['size'],
                  'nonce': nonce, 'pre_flags': 0x80, 'bin_path': str(Path(path).anchor) + '$Recycle.Bin' + os.sep + nonce}
        Path(receipt).write_text(json.dumps(result), encoding='utf-8')
        return result

    def preview(self, paths=None, policy=None):
        scan = StorageScanner().scan(self.store.storage_entries())
        return self.cleanup.preview(self.store, scan, paths or [str(self.b)], policy or self.policy)

    def test_preview_and_false_confirmation_never_modify_file(self):
        preview = self.preview()
        self.assertEqual(preview['count'], 1)
        for value in (False, None, 'true', 1):
            with self.assertRaises(StorageCleanupError):
                self.cleanup.apply(self.store, preview['token'], confirmed=value, protection=self.policy)
            self.assertTrue(self.b.exists())
        self.assertEqual(self.calls, [])

    def test_last_copy_cannot_be_selected(self):
        preview = self.preview([str(self.a), str(self.b)])
        self.assertFalse(preview['token'])
        self.assertTrue(preview['errors'])
        self.assertTrue(self.a.exists() and self.b.exists())

    def test_same_file_multiple_histories_all_updated_unrelated_preserved(self):
        self.store.record({'task_key': 'extra', 'title': 'shared', 'task_state': 'completed', 'files': [{'path': str(self.b)}]})
        preview = self.preview()
        self.assertEqual(len(preview['rows'][0]['entry_ids']), 2)
        result = self.cleanup.apply(self.store, preview['token'], True, self.policy)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(self.a.exists())
        self.assertFalse(self.b.exists())
        rows = self.store.storage_entries()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['file_state'] == 'missing' for row in rows if row['path'] == str(self.b)))
        self.assertNotEqual(next(row for row in rows if row['path'] == str(self.a))['file_state'], 'missing')
        self.assertIn(str(self.a), result['retained_verified'])

    def test_target_change_after_preview_aborts(self):
        plan = self.preview()
        self.b.write_bytes(b'a different file entirely')
        with self.assertRaises(StorageCleanupError):
            self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.b.exists())

    def test_retained_copy_missing_or_changed_aborts(self):
        plan = self.preview()
        self.a.rename(self.root / 'user-moved.mp4')
        with self.assertRaises(OrganizationError):
            self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertTrue(self.b.exists())

    def test_recycle_failure_never_unlinks_or_marks_deleted(self):
        plan = self.preview()
        self.cleanup.recycler = lambda *a, **k: (_ for _ in ()).throw(PermissionError())
        result = self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertEqual(result['completed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertTrue(self.b.exists())
        self.assertTrue(all(row['file_state'] != 'missing' for row in self.store.storage_entries()))

    def test_database_failure_after_recycle_has_recoverable_receipt(self):
        plan = self.preview()
        with patch.object(self.store, 'mark_storage_removed', side_effect=OSError('injected database disk failure')):
            result = self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['database_pending'], 1)
        self.assertFalse(self.b.exists())
        self.store.close()
        self.store = HistoryStore(self.root / 'history.sqlite3')
        restored = StorageCleanup(self.root / 'cleanup').recover(self.store)
        self.assertEqual(restored['recovered'], 1)
        self.assertFalse(restored['errors'])
        self.assertEqual(next(row for row in self.store.storage_entries() if row['path'] == str(self.b))['file_state'], 'missing')

    def test_missing_without_receipt_is_not_inferred_as_success(self):
        def no_receipt(path, *args, **kwargs):
            Path(path).rename(self.root / 'externally-moved.mp4')
            raise OSError('operation outcome unknown')
        self.cleanup.recycler = no_receipt
        plan = self.preview()
        result = self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertEqual(result['completed'], 0)
        restored = self.cleanup.recover(self.store)
        self.assertTrue(restored['errors'])
        self.assertNotEqual(next(row for row in self.store.storage_entries() if row['path'] == str(self.b))['file_state'], 'missing')

    def test_active_and_unknown_recovery_tasks_block_cleanup(self):
        for state in ('running', 'paused', 'failed', 'cancelled', 'recovering', 'merging', 'unknown'):
            policy = ProtectionPolicy({'jobs': [{'state': state, 'paths': [str(self.b)], 'directories': []}]})
            plan = self.preview(policy=policy)
            self.assertFalse(plan['token'], state)
            self.assertTrue(self.b.exists())

    def test_protection_rechecked_after_preview(self):
        plan = self.preview()
        policy = ProtectionPolicy({'jobs': [{'state': 'running', 'paths': [str(self.b)], 'directories': []}]})
        with self.assertRaises(StorageCleanupError):
            self.cleanup.apply(self.store, plan['token'], True, policy)
        self.assertTrue(self.b.exists())

    def test_corrupt_or_backup_only_task_snapshot_blocks_cleanup(self):
        tasks = self.root / 'tasks.json'
        tasks.write_text('{}', encoding='utf-8')
        self.assertTrue(ProtectionPolicy({'jobs': []}, tasks).reason(self.a))
        tasks.rename(self.root / 'tasks.json.bak')
        self.assertTrue(ProtectionPolicy({'jobs': []}, tasks).reason(self.a))

    def test_real_taskstore_safe_metadata_without_decrypting_secrets(self):
        store = TaskStore(self.root / 'tasks.json')
        request = DownloadRequest('', 'https://example.test/private?token=never-output', str(self.root))
        request.output_path = str(self.b)
        job = DownloadJob(1, request, 'resume', state='paused')
        self.assertTrue(store.save([job]))
        with patch.object(TaskStore, '_unprotect', side_effect=AssertionError('must not decrypt')):
            policy = ProtectionPolicy({'jobs': []}, store.path)
        self.assertIn('任务', policy.reason(self.b))
        self.assertNotIn('never-output', policy.reason(self.b))

    def test_workspace_and_sensitive_files_always_protected(self):
        self.assertTrue(self.policy.reason(self.root / '.stream-downloads/abc/output.mp4'))
        self.assertTrue(self.policy.reason(self.root / 'BrowserProfile/Cookies'))
        self.assertTrue(self.policy.reason(self.root / 'download.mp4.part'))

    def test_manual_delete_still_requires_explicit_preview(self):
        plan = self.cleanup.preview_files(self.store, [str(self.a)], self.policy)
        self.assertTrue(plan['token'])
        result = self.cleanup.apply(self.store, plan['token'], True, self.policy)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(self.b.exists())

    def test_queue_cleanup_lease_protects_new_and_resume_tasks(self):
        queue = DownloadQueue()
        queue.set_storage_cleanup_guard(True)
        queue.set_storage_cleanup_guard(True)
        with self.assertRaises(ValueError):
            queue.add(DownloadRequest('', 'http://localhost/video.mp4', str(self.root)))
        queue.set_storage_cleanup_guard(False)
        with self.assertRaises(ValueError):
            queue.start()
        queue.set_storage_cleanup_guard(False)
        queue.pause()
        snapshot = queue_snapshot(queue)
        self.assertEqual(snapshot['jobs'], [])
