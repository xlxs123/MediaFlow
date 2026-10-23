import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest


class ControlledWorker(QObject):
    finished = Signal()
    completed = Signal(dict)
    log = Signal(str)
    progress = Signal(dict)
    status = Signal(str)
    diagnostic = Signal(dict)

    def __init__(self, request, parent):
        super().__init__(parent)
        self.request = request
        self.started = False
        self.stop_requested = False

    def start(self):
        self.started = True

    def request_stop(self):
        self.stop_requested = True

    def complete(self, success=True, cancelled=False, **extra):
        result = {'success': success, 'cancelled': cancelled, 'exit_code': 0,
                  'files': ['output.mp4'] if success else [], 'error': '',
                  'error_code': '', 'diagnostic': {}}
        result.update(extra)
        self.completed.emit(result)

    def finish(self, success=True, cancelled=False, **extra):
        self.complete(success, cancelled, **extra)
        self.finished.emit()


class WorkerFactory:
    def __init__(self):
        self.workers = []

    def __call__(self, request, parent):
        worker = ControlledWorker(request, parent)
        self.workers.append(worker)
        return worker


class DownloadQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.factory = WorkerFactory()
        self.queue = DownloadQueue(worker_factory=self.factory)

    def tearDown(self):
        self.queue.stop_all()
        for worker in tuple(self.queue._workers):
            worker.finish(success=False, cancelled=True)
        self.pump()
        self.queue.deleteLater()
        self.pump()
        self.temp.cleanup()

    def request(self, **overrides):
        fields = dict(yt_dlp_path='yt-dlp.exe', url='https://media.example/index.m3u8',
                      output_directory=self.temp.name, filename='')
        fields.update(overrides)
        return DownloadRequest(**fields)

    def add(self, count=1):
        return [self.queue.add(self.request()) for _ in range(count)]

    def pump(self):
        for _ in range(6):
            self.app.processEvents()

    def wait_for(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.001)
        self.fail('Qt 事件未在限定时间内完成。')

    def test_default_paused_three_jobs_and_four_fragments(self):
        job = self.add()[0]
        self.assertTrue(self.queue.paused)
        self.assertEqual(self.queue.max_concurrent, 3)
        self.assertEqual(self.queue.active_count, 0)
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.request.concurrent_fragments, 4)
        self.assertIsNone(job.worker)

    def test_invalid_concurrency_limits(self):
        for value in (0, 11, -1, True, 1.5, '3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DownloadQueue(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.queue.set_max_concurrent(value)
        self.assertEqual(self.queue.max_concurrent, 3)

    def test_add_validates_request_before_allocating_id(self):
        for overrides in ({'filename': 'CON'}, {'url': 'file://private'},
                          {'concurrent_fragments': 17}, {'headers': {'Cookie': 'unsafe\nheader'}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.queue.add(self.request(**overrides))
        self.assertEqual(self.queue.jobs, [])
        self.assertEqual(self.add()[0].id, 1)

    def test_fifo_starts_only_three_and_finished_refills(self):
        jobs = self.add(5)
        self.queue.start()
        self.assertEqual([job.state for job in jobs], ['running'] * 3 + ['queued'] * 2)
        self.assertEqual(self.queue.active_count, 3)
        self.factory.workers[1].finish()
        self.pump()
        self.assertEqual(jobs[1].state, 'completed')
        self.assertIsNone(jobs[1].worker)
        self.assertEqual(jobs[3].state, 'running')
        self.assertEqual(jobs[4].state, 'queued')
        self.assertEqual([worker.request.filename for worker in self.factory.workers],
                         ['video001', 'video002', 'video003', 'video004'])
        self.assertEqual(self.queue.active_count, 3)

    def test_completed_does_not_release_worker_or_start_next(self):
        self.queue.set_max_concurrent(1)
        first, second = self.add(2)
        self.queue.start()
        worker = first.worker
        worker.complete()
        self.pump()
        self.assertEqual(first.state, 'completed')
        self.assertIs(first.worker, worker)
        self.assertEqual(self.queue.active_count, 1)
        self.assertEqual(second.state, 'queued')
        worker.finished.emit()
        self.pump()
        self.assertIsNone(first.worker)
        self.assertEqual(second.state, 'running')

    def test_add_to_running_queue_dispatches_available_slot(self):
        first = self.add()[0]
        self.queue.start()
        second = self.add()[0]
        self.assertEqual(first.state, 'running')
        self.assertEqual(second.state, 'running')
        self.assertEqual(self.queue.active_count, 2)

    def test_pause_preserves_active_downloads_and_waiters(self):
        first, second, third, fourth = self.add(4)
        self.queue.start()
        self.queue.pause()
        first.worker.finish()
        self.pump()
        self.assertTrue(self.queue.paused)
        self.assertEqual(fourth.state, 'queued')
        self.assertEqual(self.queue.active_count, 2)
        self.assertFalse(second.worker.stop_requested)
        self.assertFalse(third.worker.stop_requested)
        self.queue.start()
        self.assertEqual(fourth.state, 'running')

    def test_idle_emitted_after_last_finished_when_paused(self):
        count = []
        self.queue.idle.connect(lambda: count.append(self.queue.active_count))
        job = self.add()[0]
        self.queue.start()
        self.queue.pause()
        worker = job.worker
        worker.complete()
        self.pump()
        self.assertEqual(count, [])
        worker.finished.emit()
        self.pump()
        self.assertEqual(count, [0])
        self.queue.pause()
        self.queue.set_max_concurrent(2)
        self.assertEqual(count, [0])

    def test_lower_limit_does_not_cancel_and_waits_for_room(self):
        jobs = self.add(6)
        self.queue.start()
        self.queue.set_max_concurrent(1)
        self.assertEqual(self.queue.active_count, 3)
        self.assertFalse(any(job.worker.stop_requested for job in jobs[:3]))
        jobs[0].worker.finish()
        self.pump()
        jobs[1].worker.finish()
        self.pump()
        self.assertEqual(self.queue.active_count, 1)
        self.assertEqual(jobs[3].state, 'queued')
        jobs[2].worker.finish()
        self.pump()
        self.assertEqual(jobs[3].state, 'running')
        self.assertEqual(self.queue.active_count, 1)

    def test_raise_limit_dispatches_more_in_fifo_order(self):
        self.queue.set_max_concurrent(1)
        jobs = self.add(5)
        self.queue.start()
        self.queue.set_max_concurrent(4)
        self.assertEqual([job.state for job in jobs], ['running'] * 4 + ['queued'])
        self.assertEqual(self.queue.active_count, 4)

    def test_cancel_queued_is_terminal_without_worker(self):
        job = self.add()[0]
        results = []
        self.queue.completed.connect(lambda job_id, result: results.append((job_id, result)))
        self.assertTrue(self.queue.cancel(job.id))
        self.assertEqual(job.state, 'cancelled')
        self.assertTrue(job.result['cancelled'])
        self.assertIsNone(job.worker)
        self.assertEqual(len(results), 1)
        self.queue.start()
        self.assertEqual(self.queue.active_count, 0)

    def test_cancel_one_worker_keeps_other_workers_and_refills(self):
        jobs = self.add(4)
        self.queue.start()
        worker = jobs[1].worker
        self.queue.cancel(jobs[1].id)
        self.assertEqual(jobs[1].state, 'stopping')
        self.assertTrue(worker.stop_requested)
        self.assertFalse(jobs[0].worker.stop_requested)
        self.assertFalse(jobs[2].worker.stop_requested)
        self.assertEqual(jobs[3].state, 'queued')
        worker.finish(success=False, cancelled=True)
        self.pump()
        self.assertEqual(jobs[1].state, 'cancelled')
        self.assertEqual(jobs[3].state, 'running')

    def test_cancel_stopping_and_unknown_jobs_are_safe(self):
        job = self.add()[0]
        self.assertFalse(self.queue.cancel(999))
        self.queue.start()
        self.assertTrue(self.queue.cancel(job.id))
        self.assertTrue(self.queue.cancel(job.id))
        job.worker.finish(success=False, cancelled=True)
        self.pump()
        self.assertFalse(self.queue.cancel(job.id))

    def test_stop_all_pauses_before_cancelling_and_cannot_start_waiters(self):
        jobs = self.add(7)
        self.queue.start()
        self.queue.stop_all()
        self.assertTrue(self.queue.paused)
        self.assertEqual([job.state for job in jobs], ['stopping'] * 3 + ['cancelled'] * 4)
        for job in jobs[:3]:
            self.assertTrue(job.worker.stop_requested)
            job.worker.finish(success=False, cancelled=True)
        self.pump()
        self.assertEqual(self.queue.active_count, 0)
        self.assertTrue(all(job.state == 'cancelled' for job in jobs))
        self.assertEqual(len(self.factory.workers), 3)

    def test_retry_preserves_snapshot_and_partial_and_resets_display(self):
        request = self.request(filename='retry', url='https://media.example/index?token=private',
                               headers={'range': 'bytes=0-', 'referer': 'https://page.example'},
                               inherit_range=True, concurrent_fragments=8)
        job = self.queue.add(request)
        partial = Path(self.temp.name, 'retry.mp4.part')
        partial.write_bytes(b'keep partial')
        self.queue.start()
        job.worker.progress.emit({'percent': 35.0, 'fragment_count': 8})
        job.worker.diagnostic.emit({'drm': False, 'http_status': 403})
        job.worker.log.emit('failed attempt')
        job.worker.finish(success=False, error='test error')
        self.pump()
        self.queue.pause()
        self.queue.retry(job.id)
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.progress, {})
        self.assertEqual(job.diagnostic, {})
        self.assertEqual(list(job.logs), [])
        self.assertIsNone(job.result)
        self.assertEqual(partial.read_bytes(), b'keep partial')
        self.assertEqual(job.request.filename, 'retry')
        self.assertEqual(job.request.headers['range'], 'bytes=0-')
        self.assertEqual(job.request.concurrent_fragments, 8)
        self.assertTrue(job.request.inherit_range)

        self.queue.start()
        self.assertEqual(job.state, 'running')
        self.assertEqual(len(self.factory.workers), 2)

    def test_update_context_keeps_partial_job_name_and_resume_proof(self):
        from core.request_context import RequestContext
        job = self.queue.add(self.request(filename='continue-same', url='https://cdn.test/index?token=old'))
        proof = {'layout_sha256': 'proof'}
        job.request.resume_identity = proof
        partial = Path(self.temp.name, 'continue-same.ts.part')
        partial.write_bytes(b'finished-fragments')
        self.queue.start()
        job.worker.finish(success=False)
        self.pump()
        self.queue.pause()
        context = RequestContext('https://cdn.test/index?token=new', headers={'User-Agent': 'Real browser', 'Range': 'bytes=0-'}, inherit_range=True)
        self.queue.retry_with_context(job.id, context)
        self.assertEqual(job.request.resume_identity, proof)
        self.assertEqual(job.request.filename, 'continue-same')
        self.assertEqual(partial.read_bytes(), b'finished-fragments')
        self.assertEqual(job.request.context_snapshot, context)
        self.assertNotIn('cookie', job.request.headers)

    def test_update_rejects_different_resolver_video_without_mutation(self):
        from core.request_context import RequestContext
        job = self.queue.add(self.request(media_metadata={'resolver_id': 'rou.video', 'video_id': 'one'}))
        self.queue.cancel(job.id)
        before = job.request
        with self.assertRaises(ValueError):
            self.queue.update_context(job.id, RequestContext('https://cdn.test/new'), {'resolver_id': 'rou.video', 'video_id': 'two'})
        self.assertIs(job.request, before)

    def test_retry_cancelled_queued_task_when_running_dispatches(self):
        job = self.add()[0]
        self.queue.cancel(job.id)
        self.queue.start()
        self.queue.retry(job.id)
        self.assertEqual(job.state, 'running')

    def test_retry_rejects_running_success_unknown_or_pending_finished(self):
        job = self.add()[0]
        for job_id in (job.id, 999):
            with self.assertRaises(ValueError):
                self.queue.retry(job_id)
        self.queue.start()
        with self.assertRaises(ValueError):
            self.queue.retry(job.id)
        worker = job.worker
        worker.complete(success=False)
        self.pump()
        with self.assertRaises(ValueError):
            self.queue.retry(job.id)
        worker.finished.emit()
        self.pump()
        self.queue.retry(job.id)
        job.worker.finish()
        self.pump()
        with self.assertRaises(ValueError):
            self.queue.retry(job.id)

    def test_remove_finished_only_without_worker_releases_credentials(self):
        first, second, third = self.add(3)
        self.queue.cancel(first.id)
        self.queue.set_max_concurrent(1)
        self.queue.start()
        second.worker.complete()
        self.pump()
        self.assertEqual(self.queue.remove_finished(), 1)
        self.assertIsNone(self.queue.get(first.id))
        self.assertNotIn(first.id, self.queue._redactors)
        self.assertIsNotNone(self.queue.get(second.id))
        second.worker.finished.emit()
        self.pump()
        self.assertEqual(self.queue.remove_finished(), 1)
        self.assertIsNone(self.queue.get(second.id))
        self.assertEqual(third.state, 'running')
        new = self.add()[0]
        self.assertGreater(new.id, third.id)

    def test_request_headers_body_and_options_are_deep_snapshots(self):
        request = self.request(method='POST', body=b'payload=body-private',
                               headers={'Referer': 'https://a.example/', 'Cookie': 'session=own-cookie',
                                        'Range': 'bytes=0-'}, inherit_range=True,
                               inherit_browser_metadata=True, concurrent_fragments=6)
        first = self.queue.add(request)
        request.url = 'https://b.example/'
        request.headers['Cookie'] = 'session=new-cookie'
        request.headers['Range'] = 'bytes=10-'
        request.body = b'new-body'
        request.concurrent_fragments = 1
        second = self.queue.add(request)
        self.queue.start()
        self.assertEqual(first.request.url, 'https://media.example/index.m3u8')
        self.assertEqual(first.request.headers['Cookie'], 'session=own-cookie')
        self.assertEqual(first.request.headers['Range'], 'bytes=0-')
        self.assertEqual(first.request.body, b'payload=body-private')
        self.assertEqual(first.request.concurrent_fragments, 6)
        self.assertTrue(first.request.inherit_range)
        self.assertTrue(first.request.inherit_browser_metadata)
        self.assertEqual(second.request.headers['Cookie'], 'session=new-cookie')
        self.assertIs(first.worker.request, first.request)
        self.assertNotIn('own-cookie', repr(first))
        self.assertNotIn('body-private', repr(first))

    def test_each_jobs_redactor_is_independent_and_result_types_preserved(self):
        first = self.queue.add(self.request(url='https://a.example/index?sign=secret-A',
                                headers={'Cookie': 'session=cookie-A', 'Authorization': 'Bearer auth-A'},
                                body=b'payload=body-A'))
        second = self.queue.add(self.request(url='https://b.example/index?sign=secret-B',
                                 headers={'Cookie': 'session=cookie-B'}, body=b'{"payload":"body-B"}'))
        logs = []
        self.queue.log.connect(lambda job_id, line: logs.append((job_id, line)))
        self.queue.start()
        first.worker.log.emit('cookie-A auth-A body-A secret-A https://a.example/?sign=secret-A')
        second.worker.log.emit('cookie-B body-B secret-B')
        first.worker.status.emit('fetching secret-A')
        first.worker.progress.emit({'percent': 50.0, 'fragment_count': 4})
        first.worker.diagnostic.emit({'http_status': 206, 'drm': False, 'encryption': 'AES-128',
                                      'final_url': 'https://a.example/index?sign=secret-A',
                                      'request_range': 'bytes=0-'})
        self.pump()
        self.assertEqual([job_id for job_id, _ in logs], [first.id, second.id])
        self.assertNotIn('secret-A', first.status)
        self.assertIs(first.diagnostic['drm'], False)
        self.assertEqual(first.diagnostic['http_status'], 206)
        self.assertEqual(first.diagnostic['encryption'], 'AES-128')
        self.assertEqual(first.diagnostic['request_range'], 'bytes=0-')
        self.assertEqual(first.progress['percent'], 50.0)
        for job, secrets in ((first, ('cookie-A', 'auth-A', 'body-A', 'secret-A')),
                             (second, ('cookie-B', 'body-B', 'secret-B'))):
            for secret in secrets:
                self.assertNotIn(secret, '\n'.join(job.logs))
        first.worker.finish(success=False, error='cookie-A auth-A body-A secret-A',
                            diagnostic={'http_status': 403, 'drm': False})
        self.pump()
        self.assertIs(first.result['success'], False)
        self.assertEqual(first.result['exit_code'], 0)
        for secret in ('cookie-A', 'auth-A', 'body-A', 'secret-A'):
            self.assertNotIn(secret, repr(first.result))

    def test_signal_payload_snapshot_cannot_be_mutated_after_delivery(self):
        job = self.add()[0]
        self.queue.start()
        result = {'success': True, 'files': ['one.mp4'], 'diagnostic': {'drm': False}}
        delivered = []
        self.queue.completed.connect(lambda job_id, value: delivered.append(value))
        job.worker.completed.emit(result)
        self.pump()
        result['files'].append('two.mp4')
        delivered[0]['files'].append('three.mp4')
        self.assertEqual(job.result['files'], ['one.mp4'])

    def test_header_normalization_also_applies_to_per_job_redactor(self):
        job = self.queue.add(self.request(headers={' Authorization ': 'Bearer isolated-auth'}))
        self.queue.start()
        job.worker.log.emit('isolated-auth')
        self.pump()
        self.assertEqual(list(job.logs), ['********'])

    def test_same_thread_worker_signals_are_still_queued(self):
        job = self.add()[0]
        self.queue.start()
        job.worker.progress.emit({'percent': 75.0})
        job.worker.status.emit('later status')
        self.assertEqual(job.progress, {})
        self.assertEqual(job.status, '正在启动…')
        self.pump()
        self.assertEqual(job.progress['percent'], 75.0)
        self.assertEqual(job.status, 'later status')

    def test_missing_fragment_counters_keep_known_values_while_other_progress_updates(self):
        job = self.add()[0]
        self.queue.start()
        job.worker.progress.emit({'percent': 25.0, 'fragment_count': 8,
                                  'fragment_index': 2, 'speed': '1.0 MiB/s', 'eta': '00:06'})
        job.worker.progress.emit({'percent': 100.0, 'fragment_count': None,
                                  'fragment_index': None, 'speed': '--', 'eta': '--'})
        self.pump()
        self.assertEqual(job.progress['fragment_count'], 8)
        self.assertEqual(job.progress['fragment_index'], 2)
        self.assertEqual(job.progress['percent'], 100.0)
        self.assertEqual(job.progress['speed'], '--')
        self.assertEqual(job.progress['eta'], '--')
        job.worker.progress.emit({'percent': None, 'fragment_index': 0})
        self.pump()
        self.assertEqual(job.progress['fragment_count'], 8)
        self.assertEqual(job.progress['fragment_index'], 0)
        self.assertIsNone(job.progress['percent'])

    def test_late_progress_and_duplicate_completion_cannot_overwrite_result(self):
        job = self.add()[0]
        self.queue.start()
        worker = job.worker
        worker.complete()
        worker.progress.emit({'percent': 3.0})
        worker.status.emit('stale status')
        worker.diagnostic.emit({'drm': True})
        worker.complete(success=False, error='duplicate completion')
        self.pump()
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.status, '下载完成')
        self.assertEqual(job.progress, {})
        self.assertEqual(job.diagnostic, {})
        self.assertTrue(job.result['success'])

    def test_cancelled_retry_rejoins_fifo_behind_existing_waiter(self):
        self.queue.set_max_concurrent(1)
        first, second, third = self.add(3)
        self.queue.start()
        self.queue.cancel(second.id)
        self.queue.retry(second.id)
        first.worker.finish()
        self.pump()
        self.assertEqual(third.state, 'running')
        self.assertEqual(second.state, 'queued')
        self.assertEqual([job.id for job in self.queue.jobs], [first.id, second.id, third.id])

    def test_logs_bounded_to_two_thousand_and_only_log_signal_changes(self):
        job = self.add()[0]
        self.queue.start()
        changes = []
        self.queue.changed.connect(changes.append)
        for index in range(2010):
            job.worker.log.emit(f'line {index}')
        self.pump()
        self.assertEqual(len(job.logs), 2000)
        self.assertEqual(job.logs[0], 'line 10')
        self.assertEqual(job.logs[-1], 'line 2009')
        self.assertEqual(changes, [])

    def test_stopping_status_not_overwritten_by_late_worker_status(self):
        job = self.add()[0]
        self.queue.start()
        self.queue.cancel(job.id)
        job.worker.status.emit('正在下载…')
        self.pump()
        self.assertEqual(job.state, 'stopping')
        self.assertIn('停止', job.status)

    def test_default_names_skip_existing_mp4_part_and_case_insensitive_names(self):
        Path(self.temp.name, 'VIDEO001.mp4').write_bytes(b'existing')
        Path(self.temp.name, 'video002.mp4.part').write_bytes(b'partial')
        Path(self.temp.name, 'video003.ts.part-Frag1').write_bytes(b'fragment')
        self.queue.add(self.request(filename='VIDEO004.mp4'))
        first, second = self.add(2)
        self.assertEqual(first.name, 'video005')
        self.assertEqual(second.name, 'video006')
        self.assertEqual(Path(self.temp.name, 'VIDEO001.mp4').read_bytes(), b'existing')

    def test_explicit_existing_file_keeps_resume_name_and_duplicate_is_renamed(self):
        Path(self.temp.name, 'resume.mp4.part').write_bytes(b'partial')
        first = self.queue.add(self.request(filename='resume.mp4'))
        second = self.queue.add(self.request(filename='Resume.MP4'))
        third = self.queue.add(self.request(filename='resume'))
        self.assertEqual([first.name, second.name, third.name], ['resume', 'Resume (2)', 'resume (3)'])

    def test_same_names_different_directories_and_path_aliases(self):
        first = self.queue.add(self.request(filename='same'))
        other = self.queue.add(self.request(filename='same', output_directory=str(Path(self.temp.name, 'other'))))
        alias = self.queue.add(self.request(filename='SAME', output_directory=str(Path(self.temp.name, '.'))))
        self.assertEqual(first.name, other.name)
        self.assertEqual(alias.name, 'SAME (2)')

    def test_collision_suffix_keeps_windows_name_length(self):
        base = '长' * 180
        self.queue.add(self.request(filename=base))
        second = self.queue.add(self.request(filename=base))
        third = self.queue.add(self.request(filename=base))
        self.assertEqual(len(second.name), 180)
        self.assertEqual(len(third.name), 180)
        self.assertTrue(second.name.endswith(' (2)'))
        self.assertTrue(third.name.endswith(' (3)'))

    def test_factory_failure_does_not_block_fifo_and_is_redacted(self):
        failing = self.queue.add(self.request(filename='fail', headers={'Cookie': 'session=private-factory'}))
        next_job = self.add()[0]
        self.queue.set_max_concurrent(1)
        normal_factory = self.factory

        def factory(request, parent):
            if request.filename == 'fail':
                raise RuntimeError('private-factory')
            return normal_factory(request, parent)

        self.queue._worker_factory = factory
        self.queue.start()
        self.assertEqual(failing.state, 'failed')
        self.assertIsNone(failing.worker)
        self.assertNotIn('private-factory', failing.result['error'])
        self.assertEqual(next_job.state, 'running')
        self.assertEqual(self.queue.active_count, 1)

    def test_start_failure_deletes_unstarted_worker_and_refills(self):
        first, second = self.add(2)
        self.queue.set_max_concurrent(1)

        class FailedStartWorker(ControlledWorker):
            def start(worker):
                raise RuntimeError('start failed')

        def factory(request, parent):
            if request.filename == first.name:
                return FailedStartWorker(request, parent)
            return self.factory(request, parent)

        self.queue._worker_factory = factory
        self.queue.start()
        self.assertEqual(first.state, 'failed')
        self.assertIsNone(first.worker)
        self.assertEqual(second.state, 'running')
        self.assertEqual(self.queue.active_count, 1)

    def test_finished_without_completed_marks_failure_and_refills(self):
        self.queue.set_max_concurrent(1)
        first, second = self.add(2)
        self.queue.start()
        first.worker.finished.emit()
        self.pump()
        self.assertEqual(first.state, 'failed')
        self.assertIn('未返回', first.result['error'])
        self.assertIsNone(first.worker)
        self.assertEqual(second.state, 'running')

    def test_completed_changed_is_sent_before_completed_signal(self):
        job = self.add()[0]
        self.queue.start()
        events = []
        self.queue.changed.connect(lambda job_id: events.append(('changed', job_id, job.state)))
        self.queue.completed.connect(lambda job_id, value: events.append(('completed', job_id, job.state)))
        job.worker.complete()
        self.pump()
        self.assertEqual(events, [('changed', job.id, 'completed'), ('completed', job.id, 'completed')])

    def test_true_qthread_callbacks_execute_on_queue_owner_and_finished_race(self):
        emitter_threads = []
        receiver_threads = []
        gate = threading.Event()

        class ThreadedWorker(QThread):
            completed = Signal(dict)
            log = Signal(str)
            progress = Signal(dict)
            status = Signal(str)
            diagnostic = Signal(dict)

            def __init__(worker, request, parent):
                super().__init__(parent)

            def request_stop(worker):
                gate.set()

            def run(worker):
                emitter_threads.append(QThread.currentThread())
                gate.wait(2)
                worker.log.emit('thread log')
                worker.progress.emit({'percent': 100.0})
                worker.diagnostic.emit({'drm': False, 'http_status': 200})
                worker.completed.emit({'success': True, 'cancelled': False, 'files': []})

        self.queue._worker_factory = ThreadedWorker
        job = self.add()[0]
        self.queue.changed.connect(lambda job_id: receiver_threads.append(QThread.currentThread()))
        self.queue.log.connect(lambda job_id, line: receiver_threads.append(QThread.currentThread()))
        self.queue.completed.connect(lambda job_id, result: receiver_threads.append(QThread.currentThread()))
        self.queue.start()
        gate.set()
        self.wait_for(lambda: job.worker is None)
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.progress['percent'], 100.0)
        self.assertIs(job.diagnostic['drm'], False)
        self.assertEqual(list(job.logs), ['thread log'])
        self.assertEqual(self.queue.active_count, 0)
        self.assertTrue(receiver_threads)
        self.assertTrue(all(thread == self.queue.thread() for thread in receiver_threads))
        self.assertTrue(all(thread != self.queue.thread() for thread in emitter_threads))


if __name__ == '__main__':
    unittest.main()
