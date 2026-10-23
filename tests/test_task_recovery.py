import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest
from core.request_context import RequestContext
from core.task_store import TaskStore, TaskStoreSecurityError, _dpapi
from tests.test_download_queue import WorkerFactory


class TaskRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, 'state', 'tasks.json')
        self.queues = []

    def tearDown(self):
        for queue in self.queues:
            queue._paused = True
            for worker in tuple(queue._workers):
                worker.finish(success=False, cancelled=True)
            self.pump()
            queue.deleteLater()
        self.pump()
        self.temp.cleanup()

    def pump(self):
        for _ in range(6):
            self.app.processEvents()

    def queue(self, store=None):
        queue = DownloadQueue(worker_factory=WorkerFactory(), task_store=store or TaskStore(self.path))
        self.queues.append(queue)
        return queue

    def request(self, **overrides):
        arguments = dict(yt_dlp_path='yt-dlp.exe', url='https://media.example/video.mp4',
                         output_directory=str(Path(self.temp.name, 'media')), filename='large',
                         download_protocol='Direct', output_extension='mp4')
        arguments.update(overrides)
        return DownloadRequest(**arguments)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_current_user_dpapi_roundtrip_and_no_plaintext_secrets(self):
        queue = self.queue()
        url = 'https://media.example/private-route?signature=secret-signature'
        context = RequestContext(url, method='POST', headers={'Cookie': 'session=secret-cookie',
                                  'X-Custom-Key': 'secret-header'}, body=b'secret-body',
                                  original_url='https://page.example/private-page?auth=secret-page')
        job = queue.add(self.request(url=url, method='POST', headers=context.headers, body=context.body,
                                    context_snapshot=context, manifest_text='secret-manifest',
                                    media_metadata={'private': 'secret-metadata'},
                                    resume_identity={'private': 'secret-resume'}))
        job.progress = {'percent': 42.5, 'downloaded_bytes': 2048,
                        'url': 'secret-progress', 'eta': 15, 'recovery_state': 'retrying'}
        job.diagnostic = {'secret': 'secret-diagnostic'}
        job.logs.append('secret-log')
        job.result = {'error': 'secret-error', 'error_code': 'NETWORK_ERROR'}
        self.assertTrue(queue.save_tasks())
        for file in (self.path, queue.task_store.backup_path):
            raw = file.read_text('utf-8')
            for secret in ('private-route', 'secret-signature', 'secret-cookie', 'secret-header',
                           'secret-body', 'secret-page', 'secret-manifest', 'secret-metadata',
                           'secret-resume', 'secret-progress', 'secret-diagnostic', 'secret-log', 'secret-error'):
                self.assertNotIn(secret, raw)
        restored = self.queue().restore_tasks()[0]
        self.assertEqual(restored.request.url, url)
        self.assertEqual(restored.request.context_snapshot, context)
        self.assertEqual(restored.request.body, b'secret-body')
        self.assertEqual(restored.request.manifest_text, 'secret-manifest')
        self.assertEqual(restored.request.resume_identity, {'private': 'secret-resume'})
        self.assertEqual(restored.progress, {'percent': 42.5, 'downloaded_bytes': 2048,
                                           'eta': 15, 'recovery_state': 'retrying'})
        self.assertEqual(restored.created_at, job.created_at)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_running_recovery_preserves_identity_workspace_and_requires_manual_resume(self):
        queue = self.queue()
        job = queue.add(self.request(resume_identity={'etag': 'strong-proof'}))
        queue.start()
        job.worker.progress.emit({'percent': 37, 'downloaded_bytes': 370})
        self.pump()
        queue.save_tasks()
        restored_queue = self.queue()
        restored = restored_queue.restore_tasks()[0]
        self.assertEqual(restored.state, 'paused')
        self.assertEqual(restored.id, job.id)
        self.assertEqual(restored.output_path, job.output_path)
        self.assertEqual(restored.request.temporary_directory, job.request.temporary_directory)
        self.assertEqual(restored.request.resume_identity, job.request.resume_identity)
        self.assertEqual(restored.progress['downloaded_bytes'], 370)
        self.assertEqual(restored_queue.active_count, 0)
        restored_queue.start()
        self.assertEqual(restored_queue.active_count, 0)
        restored_queue.resume_job(restored.id)
        self.assertEqual(restored.state, 'running')
        self.assertEqual(restored.progress['percent'], 37)
        added = restored_queue.add(self.request(filename='second'))
        self.assertGreater(added.id, restored.id)

    def test_manual_url_has_fixed_unique_workspace_without_output_extension(self):
        queue = self.queue()
        first = queue.add(self.request(output_extension=''))
        second = queue.add(self.request(output_extension=''))
        self.assertNotEqual(first.request.temporary_directory, second.request.temporary_directory)
        self.assertEqual(Path(first.request.temporary_directory).parent.name, '.stream-downloads')
        first_workspace = first.request.temporary_directory
        queue.cancel(first.id)
        queue.retry(first.id)
        self.assertEqual(first.request.temporary_directory, first_workspace)

    def test_dpapi_failure_saves_metadata_and_requires_new_context_across_resaves(self):
        store = TaskStore(self.path)
        with patch.object(store, '_protect', side_effect=TaskStoreSecurityError('unavailable')):
            queue = self.queue(store)
            job = queue.add(self.request(url='https://media.example/?token=very-secret'))
        self.assertTrue(self.path.exists())
        self.assertIn('DPAPI', queue.persistence_error)
        self.assertNotIn('very-secret', self.path.read_text('utf-8'))
        recovered = self.queue()
        restored = recovered.restore_tasks()[0]
        self.assertEqual(restored.id, job.id)
        self.assertEqual(restored.state, 'waiting_context')
        self.assertTrue(restored.context_error)
        with self.assertRaises(ValueError):
            recovered.retry(restored.id)
        self.assertFalse(recovered.save_tasks())
        self.assertEqual(self.queue().restore_tasks()[0].state, 'waiting_context')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_decryption_failure_keeps_visible_task_and_original_ciphertext(self):
        queue = self.queue()
        original = queue.add(self.request(headers={'Cookie': 'private-cookie'}))
        store = TaskStore(self.path)
        recovered = self.queue(store)
        with patch.object(store, '_unprotect', side_effect=TaskStoreSecurityError('wrong-user')):
            restored = recovered.restore_tasks()[0]
        self.assertEqual(restored.state, 'waiting_context')
        self.assertEqual(restored.request.temporary_directory, original.request.temporary_directory)
        self.assertFalse(recovered.save_tasks())
        readable_again = self.queue().restore_tasks()[0]
        self.assertEqual(readable_again.request.headers['Cookie'], 'private-cookie')

    def test_non_windows_protection_fails_closed(self):
        with patch('core.task_store.os.name', 'posix'):
            with self.assertRaises(TaskStoreSecurityError):
                _dpapi(b'private-cookie')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_shutdown_pauses_active_and_queued_preserving_completed_and_cancelled(self):
        queue = self.queue()
        queue.set_max_concurrent(1)
        done = queue.add(self.request(filename='done'))
        cancelled = queue.add(self.request(filename='cancelled'))
        running = queue.add(self.request(filename='running'))
        pending = queue.add(self.request(filename='pending'))
        queue.cancel(cancelled.id)
        queue.start()
        done.worker.finish()
        self.pump()
        self.assertEqual(running.state, 'running')
        queue.shutdown()
        self.assertEqual(pending.state, 'paused')
        self.assertTrue(running.worker.stop_requested)
        running.worker.finish(success=False, cancelled=True)
        self.pump()
        restored = self.queue().restore_tasks()
        self.assertEqual([job.state for job in restored], ['completed', 'cancelled', 'paused', 'paused'])
        self.assertTrue(restored[0].result['success'])

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_corrupt_primary_falls_back_and_preserves_corrupt_original(self):
        queue = self.queue()
        job = queue.add(self.request())
        job.progress = {'percent': 12, 'downloaded_bytes': 12}
        queue.save_tasks()
        job.progress = {'percent': 24, 'downloaded_bytes': 24}
        queue.save_tasks()
        damaged = b'{interrupted write'
        self.path.write_bytes(damaged)
        recovered = self.queue()
        restored = recovered.restore_tasks()[0]
        self.assertEqual(restored.progress['percent'], 12)
        self.assertIn('备份', recovered.persistence_warning)
        self.assertTrue(recovered.save_tasks())
        quarantined = list(self.path.parent.glob(self.path.name + '.corrupt-*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), damaged)
        self.assertEqual(len(self.queue().restore_tasks()), 1)

    def test_both_corrupt_snapshots_are_never_replaced_by_empty_queue(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'invalid original')
        backup = self.path.with_name(self.path.name + '.bak')
        backup.write_bytes(b'invalid backup')
        queue = self.queue()
        self.assertEqual(queue.restore_tasks(), [])
        self.assertTrue(queue.persistence_error)
        self.assertFalse(queue.save_tasks())
        self.assertEqual(self.path.read_bytes(), b'invalid original')
        self.assertEqual(backup.read_bytes(), b'invalid backup')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_atomic_replace_failure_retains_last_valid_snapshot(self):
        queue = self.queue()
        job = queue.add(self.request())
        previous = self.path.read_bytes()
        job.progress = {'percent': 88}
        with patch('core.task_store.os.replace', side_effect=PermissionError('locked')):
            self.assertFalse(queue.save_tasks())
        self.assertTrue(queue.persistence_error)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len(self.queue().restore_tasks()), 1)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_progress_is_throttled_and_unchanged_request_is_not_reencrypted(self):
        store = TaskStore(self.path)
        with patch.object(store, '_protect', wraps=store._protect) as protect:
            queue = self.queue(store)
            job = queue.add(self.request(manifest_text='large manifest data'))
            queue.start()
            count = protect.call_count
            with patch.object(store, 'save', wraps=store.save) as save:
                for value in range(20):
                    job.worker.progress.emit({'percent': value, 'downloaded_bytes': value})
                self.pump()
                self.assertEqual(save.call_count, 0)
                queue.save_tasks()
                self.assertEqual(protect.call_count, count)
                job.request.resume_identity = {'new-layout': 'proof'}
                job.worker.diagnostic.emit({'protocol': 'Direct'})
                self.pump()
                self.assertEqual(protect.call_count, count + 1)

    def test_retry_progress_displays_substate_without_changing_running_contract(self):
        queue = self.queue()
        job = queue.add(self.request())
        queue.start()
        job.worker.progress.emit({'recovery_state': 'waiting_network', 'retry_attempt': 2,
                                  'retry_countdown': 4, 'percent': 36})
        self.pump()
        self.assertEqual(job.state, 'running')
        self.assertIn('等待网络', job.status)
        self.assertIn('2', job.status)
        self.assertIn('4', job.status)
        self.assertEqual(job.progress['percent'], 36)

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_restored_job_cannot_overwrite_newly_existing_final_file(self):
        queue = self.queue()
        job = queue.add(self.request())
        Path(job.output_path).parent.mkdir(parents=True)
        Path(job.output_path).write_bytes(b'user-final-file')
        restored_queue = self.queue()
        restored = restored_queue.restore_tasks()[0]
        restored_queue.resume_job(restored.id)
        restored_queue.start()
        self.assertEqual(restored.state, 'failed')
        self.assertEqual(restored.result['error_code'], 'FILE_EXISTS')
        self.assertEqual(restored_queue.active_count, 0)
        self.assertEqual(Path(job.output_path).read_bytes(), b'user-final-file')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI 仅在 Windows 上可用')
    def test_waiting_context_state_is_not_automatically_resumed(self):
        queue = self.queue()
        job = queue.add(self.request())
        queue.start()
        job.worker.finish(success=False, error_code='EXPIRED_URL')
        self.pump()
        recovered = self.queue()
        restored = recovered.restore_tasks()[0]
        self.assertEqual(restored.state, 'waiting_context')
        recovered.start()
        self.assertEqual(recovered.active_count, 0)
        with self.assertRaises(ValueError):
            recovered.restore_tasks()


if __name__ == '__main__':
    unittest.main()
