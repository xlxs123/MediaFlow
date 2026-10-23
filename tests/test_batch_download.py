import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtWidgets import QApplication

from core.concurrency import GlobalConcurrencyController
from core.discovery_models import MediaCandidate
from core.download_manager import DownloadManager, output_extension, quality_sources
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest, build_arguments
from core.errors import MediaCheckCancelled
from core.media_groups import group_media
from core.request_context import RequestContext
from tests.test_download_queue import WorkerFactory


def candidate(number, height=1080, title='Video', **overrides):
    metadata = {'video_id': str(number), 'title': title, 'source_page': 'https://page.example/videos',
                'discovered_at': time.time()}
    metadata.update(overrides.pop('metadata', {}))
    fields = dict(id=str(number) + '-' + str(height), request_context=RequestContext(
        f'https://cdn.example/{number}/{height}.m3u8?token=current', headers={
            'referer': 'https://page.example/videos', 'user-agent': 'BatchFixture', 'range': 'bytes=0-'}, inherit_range=True),
        media_type='HLS', manifest_type='Media', resolution=(height * 16 // 9, height),
        score=80, duration=180, drm=False, confirmed=True, http_status=200, metadata=metadata)
    fields.update(overrides)
    return MediaCandidate(**fields)


class BatchDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.factory = WorkerFactory()
        self.queue = DownloadQueue(worker_factory=self.factory)
        self.manager = DownloadManager(self.queue)
        self.template = DownloadRequest('yt-dlp.exe', 'https://unused.example', self.temp.name,
                                        ffmpeg_path='ffmpeg.exe', headers={'cookie': 'wrong-shared-cookie'})

    def tearDown(self):
        self.queue.stop_all()
        for worker in tuple(self.queue._workers):
            worker.finish(success=False, cancelled=True)
        self.pump()
        self.manager.deleteLater()
        self.queue.deleteLater()
        self.pump()
        self.temp.cleanup()

    def pump(self):
        for _ in range(12):
            self.app.processEvents()

    def add(self, candidates, **options):
        return self.manager.add_batch(group_media(candidates), self.template, **options)

    def test_three_independent_videos_create_three_jobs_and_start(self):
        jobs = self.add([candidate(n) for n in range(3)])
        self.assertEqual(len(jobs), 3)
        self.assertEqual(self.queue.active_count, 3)
        self.assertEqual({j.state for j in jobs}, {'running'})
        self.assertEqual(len({j.batch_id for j in jobs}), 1)

    def test_three_qualities_default_one_highest(self):
        jobs = self.add([candidate('a', h) for h in (480, 1080, 720)])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].media_candidate.resolution[1], 1080)
        self.assertNotIn('[1080p]', jobs[0].name)

    def test_three_identical_titles_allocate_browser_style_numbers(self):
        jobs = self.add([candidate(n) for n in range(3)])
        self.assertEqual([Path(j.output_path).name for j in jobs], ['Video.mp4', 'Video (1).mp4', 'Video (2).mp4'])

    def test_disk_existing_files_are_never_overwritten(self):
        for filename in ('Video.mp4', 'Video (1).mp4'):
            Path(self.temp.name, filename).write_bytes(b'keep')
        jobs = self.add([candidate('new')])
        self.assertEqual(Path(jobs[0].output_path).name, 'Video (2).mp4')
        self.assertEqual(Path(self.temp.name, 'Video.mp4').read_bytes(), b'keep')

    def test_twenty_same_title_tasks_reserved_before_workers_run(self):
        jobs = self.add([candidate(n) for n in range(20)])
        self.assertEqual(len({j.output_path.casefold() for j in jobs}), 20)
        self.assertEqual(self.queue.active_count, 3)
        self.assertEqual(sum(j.state == 'queued' for j in jobs), 17)
        self.assertEqual(Path(jobs[-1].output_path).name, 'Video (19).mp4')

    def test_vast_ad_plus_five_main_only_five_jobs(self):
        ad = candidate('ad', is_ad=True, metadata={'vast_ad': True})
        jobs = self.add([ad] + [candidate(n) for n in range(5)])
        self.assertEqual(len(jobs), 5)
        self.assertTrue(all(not j.media_candidate.is_ad for j in jobs))

    def test_signed_urls_same_video_only_one_latest_context(self):
        old = candidate('a', media_type='Direct', content_type='video/mp4', metadata={'discovered_at': 1})
        old.request_context = RequestContext('https://cdn.example/movie.mp4?token=A')
        fresh = deepcopy(old)
        fresh.id = 'fresh'
        fresh.request_context = RequestContext('https://cdn.example/movie.mp4?token=B')
        fresh.metadata['discovered_at'] = 2
        jobs = self.add([old, fresh])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].request.url.endswith('token=B'))

    def test_one_failure_does_not_stop_other_batch_tasks(self):
        jobs = self.add([candidate(n) for n in range(4)])
        jobs[0].worker.finish(success=False, error_code='NETWORK_ERROR')
        self.pump()
        self.assertEqual(jobs[0].state, 'failed')
        self.assertEqual(self.queue.active_count, 3)
        for job in jobs[1:]:
            job.worker.finish()
        self.pump()
        self.assertEqual(self.manager.summary()['completed'], 3)
        self.assertEqual(self.manager.summary()['failed'], 1)

    def test_expired_signed_url_only_current_waits_for_context(self):
        jobs = self.add([candidate(n) for n in range(4)])
        jobs[0].worker.finish(success=False, error_code='POSSIBLE_URL_EXPIRED')
        self.pump()
        self.assertEqual(jobs[0].state, 'waiting_context')
        self.assertEqual([j.state for j in jobs[1:]], ['running'] * 3)

    def test_all_qualities_have_quality_label_and_one_per_quality(self):
        jobs = self.add([candidate('a', h) for h in (480, 1080, 720)], all_qualities=True)
        self.assertEqual([Path(j.output_path).name for j in jobs],
                         ['Video [1080p].mp4', 'Video [720p].mp4', 'Video [480p].mp4'])

    def test_long_title_all_qualities_keep_labels_in_second_batch_too(self):
        groups = group_media([candidate('a', h) for h in (1080, 720)])
        groups[0].title = '视😀' * 200
        jobs = self.manager.add_batch(groups, self.template, all_qualities=True)
        jobs += self.manager.add_batch(groups, self.template, all_qualities=True)
        self.assertEqual(len({job.output_path.casefold() for job in jobs}), 4)
        for job in jobs:
            self.assertIn('[' + job.media_candidate.resolution_label + ']', Path(job.output_path).name)
            self.assertLessEqual(len(Path(job.output_path).name.encode('utf-16-le')) // 2, 180)

    def test_output_extension_preserves_actual_direct_container(self):
        for mime, ext in [('video/webm', 'webm'), ('video/x-matroska', 'mkv'),
                          ('video/mp4', 'mp4'), ('audio/mp4', 'm4a'), ('audio/mpeg', 'mp3'), ('audio/opus', 'opus')]:
            with self.subTest(mime=mime):
                media = candidate(mime, media_type='Direct', content_type=mime)
                job = self.add([media])[0]
                self.assertEqual(Path(job.output_path).suffix, '.' + ext)

    def test_batch_contexts_ignore_form_headers_and_preserve_range_no_origin(self):
        job = self.add([candidate('a')])[0]
        self.assertNotIn('cookie', job.request.headers)
        self.assertNotIn('origin', job.request.headers)
        self.assertEqual(job.request.headers['range'], 'bytes=0-')
        self.assertTrue(job.request.inherit_range)
        self.assertIs(job.request.concurrency, self.queue.concurrency)

    def test_pause_and_continue_keeps_same_path_and_partial_identity(self):
        jobs = self.add([candidate(n) for n in range(4)])
        job = jobs[0]
        path, staging = job.output_path, job.request.temporary_directory
        job.request.resume_identity = {'identity': 'same-video'}
        self.queue.pause_job(job.id)
        job.worker.finish(success=False, cancelled=True)
        self.pump()
        self.assertEqual(job.state, 'paused')
        self.assertEqual(jobs[3].state, 'running')
        self.queue.resume_job(job.id)
        self.assertEqual(job.output_path, path)
        self.assertEqual(job.request.temporary_directory, staging)
        self.assertEqual(job.request.resume_identity, {'identity': 'same-video'})

    def test_pause_waiting_and_cancel_are_separate_states(self):
        jobs = self.add([candidate(n) for n in range(4)])
        queued = jobs[3]
        self.queue.pause_job(queued.id)
        self.assertEqual(queued.state, 'paused')
        self.queue.cancel(queued.id)
        self.assertEqual(queued.state, 'cancelled')

    def test_fallback_default_prompts_and_does_not_silently_switch(self):
        offered = []
        self.manager.quality_fallback_required.connect(lambda *args: offered.append(args))
        job = self.add([candidate('a', h) for h in (1080, 720, 480)])[0]
        job.worker.finish(success=False, error_code='NETWORK_ERROR')
        self.pump()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.media_candidate.resolution[1], 1080)
        self.assertEqual(offered, [(job.id, '1080p', '720p')])

    def test_opt_in_fallback_uses_separate_partial_files_same_output(self):
        job = self.add([candidate('a', h) for h in (1080, 720)], auto_fallback=True)[0]
        output, staging = job.output_path, job.request.temporary_directory
        job.request.resume_identity = {'old': True}
        job.worker.finish(success=False, error_code='NETWORK_ERROR')
        self.pump()
        self.assertEqual(job.state, 'running')
        self.assertEqual(job.media_candidate.resolution[1], 720)
        self.assertEqual(job.output_path, output)
        self.assertNotEqual(job.request.temporary_directory, staging)
        self.assertIsNone(job.request.resume_identity)
        self.assertTrue(any('改用 720p' in line for line in job.logs))

    def test_all_qualities_failure_does_not_fallback_to_another_planned_quality(self):
        offered = []
        self.manager.quality_fallback_required.connect(lambda *args: offered.append(args))
        jobs = self.add([candidate('a', h) for h in (1080, 720)], all_qualities=True, auto_fallback=True)
        jobs[0].worker.finish(success=False, error_code='NETWORK_ERROR')
        self.pump()
        self.assertEqual(jobs[0].state, 'failed')
        self.assertEqual(jobs[1].state, 'running')
        self.assertFalse(offered)

    def test_drm_failure_never_enters_fallback(self):
        job = self.add([candidate('a', h) for h in (1080, 720)], auto_fallback=True)[0]
        job.worker.finish(success=False, error_code='PROTECTED_MEDIA')
        self.pump()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.media_candidate.resolution[1], 1080)

    def test_permanent_local_and_integrity_errors_never_switch_quality(self):
        for index, code in enumerate(('DISK_FULL', 'FILE_PERMISSION', 'RESUME_MISMATCH',
                                      'INCOMPLETE_MEDIA_RESPONSE')):
            job = self.add([candidate(str(index), h) for h in (1080, 720)], auto_fallback=True)[0]
            job.worker.finish(success=False, error_code=code)
            self.pump()
            self.assertEqual(job.state, 'failed')
            self.assertEqual(job.media_candidate.resolution[1], 1080)

    def test_retry_failed_does_not_retry_expired_or_completed(self):
        first, second, third = self.add([candidate(n) for n in range(3)])
        first.worker.finish(success=False, error_code='NETWORK_ERROR')
        second.worker.finish(success=False, error_code='EXPIRED_URL')
        third.worker.finish()
        self.pump()
        self.assertEqual(self.manager.retry_failed(), 1)
        self.assertEqual(first.state, 'running')
        self.assertEqual(second.state, 'waiting_context')
        self.assertEqual(third.state, 'completed')

    def test_unchecked_group_is_not_downloaded(self):
        groups = group_media([candidate(n) for n in range(3)])
        groups[1].selected = False
        jobs = self.manager.add_batch(groups, self.template)
        self.assertEqual(len(jobs), 2)

    def test_dash_representations_all_qualities_keep_mpd_context(self):
        media = candidate('dash', media_type='DASH', manifest_type='MPD', metadata={'representations': [
            {'id': 'v1', 'width': 1920, 'height': 1080, 'bandwidth': 5000000},
            {'id': 'v2', 'width': 1280, 'height': 720, 'bandwidth': 2000000},
            {'id': 'a', 'width': 0, 'height': 0, 'bandwidth': 128000}]})
        jobs = self.add([media], all_qualities=True)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j.request.url == media.url for j in jobs))
        self.assertIn('[height=1080]', jobs[0].request.format_selector)
        self.assertIn('[height=720]', jobs[1].request.format_selector)

    def test_bad_output_container_rejected_before_batch_mutation(self):
        requests = [replace(self.template, filename='good', output_extension='mp4'),
                    replace(self.template, filename='bad', output_extension='exe')]
        with self.assertRaises(ValueError):
            self.queue.add_batch(requests)
        self.assertFalse(self.queue.jobs)

    def test_allocated_output_path_must_match_container_and_directory(self):
        request = replace(self.template, filename='Video', output_extension='webm',
                          output_path=str(Path(self.temp.name, 'Video.mp4')))
        with self.assertRaises(ValueError):
            build_arguments(request)


class ConcurrencyTests(unittest.TestCase):
    def test_combined_requests_never_exceed_global_limit(self):
        controller = GlobalConcurrencyController(3)
        errors = []
        def request():
            try:
                lease = controller.acquire()
                time.sleep(0.015)
                lease.release()
                lease.release()
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=request) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertFalse(errors)
        self.assertEqual(controller.peak_count, 3)
        self.assertEqual(controller.active_count, 0)
        self.assertIs(deepcopy(controller), controller)

    def test_waiting_request_can_be_cancelled_without_consuming_slot(self):
        controller = GlobalConcurrencyController(1)
        lease = controller.acquire()
        cancelled = threading.Event()
        errors = []
        def request():
            try:
                controller.acquire(cancelled)
            except MediaCheckCancelled:
                errors.append('cancelled')
        thread = threading.Thread(target=request)
        thread.start()
        cancelled.set()
        thread.join(1)
        lease.release()
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(controller.active_count, 0)
