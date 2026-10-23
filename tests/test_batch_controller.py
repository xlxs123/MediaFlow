"""批次编排的真实队列/DPAPI 边界，网页与下载执行由可控替身驱动。"""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core.batch_controller import BatchController, automatic_choice, queue_key
from core.batch_store import BatchStore, BatchStoreError
from core.discovery_models import MediaCandidate
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest
from core.media_groups import group_media
from core.request_context import RequestContext
from core.task_store import TaskStore
from tests.test_download_queue import WorkerFactory


class SerialDriver(QObject):
    finished = Signal(str, object, str)

    def __init__(self):
        super().__init__()
        self.busy = False
        self.allowed = True
        self.current = ''
        self.started = []
        self.cancelled = []
        self.on_start = None

    def can_start(self):
        return self.allowed

    def start(self, item_id, url, options):
        if self.busy:
            raise AssertionError('同一页面不能并行发现两个网址')
        self.busy, self.current = True, item_id
        self.started.append((item_id, url, deepcopy(options)))
        if self.on_start:
            self.on_start(item_id, url)

    def finish(self, groups=(), error=''):
        item_id = self.current
        self.busy, self.current = False, ''
        self.finished.emit(item_id, {'groups': list(groups)}, error)

    def cancel(self):
        if self.busy:
            self.cancelled.append(self.current)
            self.finish(error='发现已取消')


def media(name, *, associated=True, confidence=95, **changes):
    metadata = {'video_id': name, 'title': '视频 ' + name,
                'page_url': 'https://pages.example.test/' + name,
                'confidence_evidence': {'current_player': associated}}
    metadata.update(changes.pop('metadata', {}))
    fields = dict(id=name, request_context=RequestContext(
        'https://cdn.example.test/' + name + '.mp4?token=private-media-token'),
        media_type='Direct', content_type='video/mp4', resolution=(1280, 720), duration=19,
        http_status=200, confirmed=True, validated=True, drm=False, score=confidence,
        metadata=metadata)
    fields.update(changes)
    return MediaCandidate(**fields)


class AutomaticChoiceTests(unittest.TestCase):
    def test_only_current_player_verified_resolver_or_exact_direct_input_is_automatic(self):
        candidate = media('current')
        self.assertIsNotNone(automatic_choice(group_media([candidate]), 'https://pages.example.test/current'))
        unknown = media('unknown', associated=False)
        self.assertIsNone(automatic_choice(group_media([unknown]), 'https://pages.example.test/unknown'))
        verified = media('verified', associated=False, metadata={'resolver_verified': True})
        self.assertIsNotNone(automatic_choice(group_media([verified]), 'https://pages.example.test/verified'))
        self.assertIsNotNone(automatic_choice(group_media([unknown]), unknown.url))

    def test_confirmation_required_never_becomes_automatic_even_with_current_player(self):
        candidate = media('low-confidence', confidence=55)
        self.assertTrue(candidate.requires_confirmation)
        self.assertIsNone(automatic_choice(group_media([candidate]), candidate.url))

    def test_multiple_media_and_ineligible_segments_ad_or_preview_require_safety(self):
        self.assertIsNone(automatic_choice(group_media([media('first'), media('second')]),
                                           'https://pages.example.test/list'))
        for candidate in (media('segment', media_type='Segment', classification='Media Segment'),
                          media('advert', is_ad=True), media('clip', metadata={'preview': True}),
                          media('denied', http_status=403)):
            with self.subTest(candidate=candidate.id):
                self.assertIsNone(automatic_choice(group_media([candidate]), candidate.url))


class BatchControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'batches.sqlite'
        self.tasks = self.root / 'tasks.json'
        self.controllers = []
        self.queues = []
        self.drivers = []
        self.controller, self.queue, self.driver, self.factory = self.make_controller()

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        for queue in self.queues:
            for worker in tuple(queue._workers):
                worker.finish(success=False, cancelled=True)
        for controller in self.controllers:
            self.wait_for(lambda controller=controller: controller.is_closed)
            controller._db._thread.join(timeout=2)
            self.assertFalse(controller._db._thread.is_alive())
            controller.deleteLater()
        for queue in self.queues:
            queue.deleteLater()
        self.pump()
        self.temporary.cleanup()

    def pump(self):
        for _ in range(8):
            self.app.processEvents()

    def wait_for(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('批次后台操作未在限定时间内完成。')

    def make_controller(self, restore=False):
        factory, driver = WorkerFactory(), SerialDriver()
        queue = DownloadQueue(worker_factory=factory, task_store=TaskStore(self.tasks))
        if restore:
            queue.restore_tasks()
        controller = BatchController(queue, driver, self.path, self.template)
        self.queues.append(queue)
        self.controllers.append(controller)
        self.drivers.append(driver)
        self.wait_for(lambda: controller.ready)
        return controller, queue, driver, factory

    def template(self, options):
        return DownloadRequest('yt-dlp.exe', 'https://unused.example.test',
                               options.get('output_directory') or str(self.root / 'output'),
                               ffmpeg_path='ffmpeg.exe', ffprobe_path='ffprobe.exe')

    def create(self, names=('first', 'second')):
        batch_id = self.controller.create(['https://pages.example.test/' + name for name in names],
                                          {'output_directory': str(self.root / 'output')})
        self.wait_for(lambda: len(self.driver.started) > 0)
        return batch_id, [item for item in self.controller.items.values() if item['batch_id'] == batch_id]

    def finish_job(self, job, **values):
        if values.get('success', True):
            Path(job.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(job.output_path).write_bytes(b'controlled-worker-output')
        job.worker.finish(files=[job.output_path] if values.get('success', True) else [], **values)
        self.pump()

    def close_controller(self, controller, queue):
        controller.shutdown()
        for worker in tuple(queue._workers):
            worker.finish(success=False, cancelled=True)
        self.wait_for(lambda: controller.is_closed)
        controller._db._thread.join(timeout=2)

    def test_discovery_starts_only_after_atomic_initial_records_are_committed(self):
        entered, release = threading.Event(), threading.Event()
        original = BatchStore.create_batch
        durable = []

        def delayed(store, batch, items):
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放保存屏障')
            return original(store, batch, items)

        def inspect_start(item_id, url):
            store = BatchStore(self.path)
            try:
                durable.append(next(item for item in store.load()['items'] if item['id'] == item_id)['url'])
            finally:
                store.close()

        self.driver.on_start = inspect_start
        with patch.object(BatchStore, 'create_batch', delayed):
            try:
                self.controller.create(['https://pages.example.test/first'], {})
                self.wait_for(entered.is_set)
                self.controller._pump()
                self.assertEqual(self.driver.started, [])
                self.assertFalse(self.queue.jobs)
            finally:
                release.set()
            self.wait_for(lambda: len(self.driver.started) == 1)
        self.assertEqual(durable, ['https://pages.example.test/first'])

    def test_sequential_discovery_rejects_late_previous_results_and_keeps_candidates_independent(self):
        _, items = self.create()
        first_id, second_id = (item['id'] for item in items)
        self.assertEqual(len(self.driver.started), 1)
        self.driver.finish(group_media([media('first-choice', confidence=50)]))
        self.wait_for(lambda: len(self.driver.started) == 2)
        self.assertEqual(items[0]['state'], 'waiting_selection')
        self.driver.finished.emit(first_id, {'groups': group_media([media('late-unrelated')])}, '')
        self.assertEqual(self.controller.active_item_id, second_id)
        self.assertEqual(self.queue.jobs, [])
        self.driver.finish(group_media([media('second-choice', confidence=50)]))
        first = self.controller.candidate_groups(first_id)
        second = self.controller.candidate_groups(second_id)
        self.assertEqual({candidate.id for group in first for candidate in group.candidates}, {'first-choice'})
        self.assertEqual({candidate.id for group in second for candidate in group.candidates}, {'second-choice'})
        first[0].candidates.clear()
        self.assertEqual(len(self.controller.candidate_groups(first_id)[0].candidates), 1)

    def test_one_failure_records_history_and_continues_to_next_url(self):
        events = []
        self.controller.history_event.connect(events.append)
        _, items = self.create()
        self.driver.finish(error='页面加载失败 token=private-failure-token')
        self.wait_for(lambda: len(self.driver.started) == 2)
        self.assertEqual(items[0]['state'], 'failed')
        self.assertNotIn('private-failure-token', items[0]['error'])
        self.assertEqual(events[0]['task_state'], 'failed')
        self.driver.finish(group_media([media('second')]))
        self.assertEqual(items[1]['state'], 'downloading')
        self.assertEqual(len(self.queue.jobs), 1)

    def test_multiple_media_wait_for_selection_and_low_confidence_requires_explicit_confirmation(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('one', confidence=50), media('two', confidence=50)]))
        self.assertEqual(items[0]['state'], 'waiting_selection')
        self.assertEqual(items[0]['candidate_count'], 2)
        self.assertFalse(self.controller.select_candidate(items[0]['id'], 'one'))
        self.assertFalse(self.queue.jobs)
        self.assertTrue(self.controller.select_candidate(items[0]['id'], 'one', confirmed=True))
        self.assertEqual(len(self.queue.jobs), 1)
        self.assertIn('/one.mp4?', self.queue.jobs[0].request.url)

    def test_confirmed_true_cannot_force_isolated_segment_into_queue(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('segment', media_type='Segment', classification='Media Segment')]))
        self.assertEqual(items[0]['state'], 'failed')
        self.assertFalse(self.controller.select_candidate(items[0]['id'], 'segment', confirmed=True))
        self.assertFalse(self.queue.jobs)

    def test_single_item_discovery_pause_and_resume_does_not_cancel_other_items(self):
        _, items = self.create()
        self.controller.item_action('pause', items[0]['id'])
        self.assertEqual(items[0]['state'], 'paused')
        self.assertEqual(items[0]['pause_scope'], 'item')
        self.wait_for(lambda: len(self.driver.started) == 2)
        self.driver.finish(error='second failed')
        self.controller.item_action('resume', items[0]['id'])
        self.wait_for(lambda: len(self.driver.started) == 3)
        self.assertEqual(self.driver.started[-1][0], items[0]['id'])
        self.assertEqual(items[1]['state'], 'failed')

    def test_batch_pause_resume_preserves_separately_paused_item(self):
        batch_id, items = self.create(('first', 'second', 'third'))
        self.controller.item_action('pause', items[1]['id'])
        self.controller.batch_action('pause', batch_id)
        self.assertTrue(self.controller.batches[batch_id]['paused'])
        self.assertTrue(all(item['state'] == 'paused' for item in items))
        self.assertEqual(items[1]['pause_scope'], 'item')
        self.controller.batch_action('resume', batch_id)
        self.assertEqual(items[1]['state'], 'paused')
        self.assertEqual(items[1]['pause_scope'], 'item')
        self.assertEqual(items[0]['state'], 'analyzing')
        self.assertEqual(items[2]['state'], 'waiting_discovery')

    def test_download_pause_resume_keeps_workspace_and_real_queue_job(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        job = self.queue.jobs[0]
        workspace, output = job.request.temporary_directory, job.output_path
        self.controller.item_action('pause', items[0]['id'])
        self.assertTrue(job.worker.stop_requested)
        job.worker.finish(success=False, cancelled=True)
        self.pump()
        self.assertEqual(job.state, 'paused')
        self.controller.item_action('resume', items[0]['id'])
        self.assertEqual(job.state, 'running')
        self.assertEqual((job.request.temporary_directory, job.output_path), (workspace, output))
        self.assertEqual(len(self.driver.started), 1)

    def test_retry_linked_failed_job_does_not_allocate_new_job_or_workspace(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        job = self.queue.jobs[0]
        key, workspace = queue_key(job), job.request.temporary_directory
        self.finish_job(job, success=False, error='network failed', error_code='NETWORK_ERROR')
        self.assertEqual(items[0]['state'], 'failed')
        self.controller.item_action('retry', items[0]['id'])
        self.assertEqual(len(self.queue.jobs), 1)
        self.assertIs(self.queue.jobs[0], job)
        self.assertEqual((queue_key(job), job.request.temporary_directory), (key, workspace))
        self.assertEqual(len(self.driver.started), 1)
        self.assertEqual(job.state, 'running')

    def test_expired_context_reanalysis_updates_same_job_and_progress_workspace(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        job = self.queue.jobs[0]
        workspace = job.request.temporary_directory
        self.finish_job(job, success=False, error='地址过期', error_code='POSSIBLE_URL_EXPIRED')
        self.assertEqual(items[0]['state'], 'waiting_context')
        self.controller.item_action('retry', items[0]['id'])
        self.assertEqual(len(self.driver.started), 2)
        candidate = media('first', request_context=RequestContext('https://cdn.example.test/first.mp4?token=fresh-token'))
        self.driver.finish(group_media([candidate]))
        self.assertEqual(len(self.queue.jobs), 1)
        self.assertEqual(job.request.temporary_directory, workspace)
        self.assertIn('fresh-token', job.request.url)
        self.assertEqual(job.state, 'running')

    def test_restart_pauses_unfinished_and_never_resubmits_completed_item(self):
        batch_id, items = self.create()
        self.driver.finish(group_media([media('first')]))
        self.finish_job(self.queue.jobs[0])
        self.assertEqual(items[0]['state'], 'completed')
        self.close_controller(self.controller, self.queue)
        controller, queue, driver, _ = self.make_controller(restore=True)
        restored = list(controller.items.values())
        self.assertEqual([item['state'] for item in restored], ['completed', 'paused'])
        self.assertTrue(controller.batches[batch_id]['paused'])
        self.assertEqual(driver.started, [])
        controller.batch_action('resume', batch_id)
        self.wait_for(lambda: len(driver.started) == 1)
        self.assertEqual(driver.started[0][0], items[1]['id'])
        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(queue.jobs[0].state, 'completed')

    def test_numeric_queue_id_reuse_cannot_attach_unrelated_job_to_completed_item(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        self.finish_job(self.queue.jobs[0])
        old_key, old_id = items[0]['queue_key'], items[0]['job_id']
        self.close_controller(self.controller, self.queue)
        controller, queue, driver, _ = self.make_controller()
        unrelated = queue.add(self.template({}))
        self.assertEqual(unrelated.id, old_id)
        self.assertNotEqual(queue_key(unrelated), old_key)
        queue.start()
        self.pump()
        self.assertEqual(list(controller.items.values())[0]['state'], 'completed')
        self.assertEqual(driver.started, [])

    def test_shutdown_waits_for_worker_stop_and_background_storage_flush(self):
        _, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        job = self.queue.jobs[0]
        job.worker.progress.emit({'percent': 31, 'downloaded_bytes': 8192})
        self.controller.shutdown()
        self.assertFalse(self.controller.is_closed)
        self.assertTrue(job.worker.stop_requested)
        job.worker.finish(success=False, cancelled=True)
        self.wait_for(lambda: self.controller.is_closed)
        store = BatchStore(self.path)
        try:
            restored = store.load()['items'][0]
        finally:
            store.close()
        self.assertEqual(restored['state'], 'paused')
        self.assertEqual(restored['progress']['downloaded_bytes'], 8192)
        self.assertEqual(restored['queue_key'], queue_key(job))
        self.assertEqual(restored['url'], items[0]['url'])

    def test_handoff_crash_gap_recovers_by_encrypted_batch_item_identity(self):
        batch_id, items = self.create(('first',))
        self.driver.finish(group_media([media('first')]))
        job = self.queue.jobs[0]
        workspace = job.request.temporary_directory
        item_id = items[0]['id']
        self.close_controller(self.controller, self.queue)
        store = BatchStore(self.path)
        try:
            item = store.load()['items'][0]
            # 模拟队列任务已原子提交，但随后的批次关联快照尚未落盘即中断。
            item.update(queue_key='', job_id=0, state='analyzing')
            store.save_item(item)
        finally:
            store.close()
        controller, queue, driver, _ = self.make_controller(restore=True)
        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(controller.items[item_id]['queue_key'], queue_key(queue.jobs[0]))
        controller.batch_action('resume', batch_id)
        self.assertEqual(driver.started, [])
        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(queue.jobs[0].request.temporary_directory, workspace)
        self.assertEqual(queue.jobs[0].state, 'running')

    def test_cancel_entire_batch_preserves_completed_and_records_unstarted_cancellations(self):
        events = []
        self.controller.history_event.connect(events.append)
        batch_id, items = self.create(('first', 'second', 'third'))
        self.driver.finish(group_media([media('first')]))
        self.finish_job(self.queue.jobs[0])
        self.assertEqual(items[0]['state'], 'completed')
        self.controller.batch_action('cancel', batch_id)
        self.assertEqual([item['state'] for item in items], ['completed', 'cancelled', 'cancelled'])
        self.assertEqual(len(self.queue.jobs), 1)
        self.assertEqual(self.queue.jobs[0].state, 'completed')
        self.assertTrue(self.controller.batches[batch_id]['paused'])
        self.assertEqual([event['task_state'] for event in events], ['cancelled', 'cancelled'])
        self.assertEqual(len(self.driver.started), 2)
        self.assertFalse(self.driver.busy)

    def test_failed_initial_commit_never_navigates_or_queues_downloads(self):
        with patch.object(BatchStore, 'create_batch', side_effect=BatchStoreError('测试磁盘写入失败')):
            self.controller.create(['https://pages.example.test/first'], {})
            self.wait_for(lambda: bool(self.controller.storage_error))
        self.assertEqual(self.driver.started, [])
        self.assertEqual(self.queue.jobs, [])
        self.assertEqual(next(iter(self.controller.items.values()))['state'], 'failed')

    def test_retry_failed_batch_leaves_waiting_selection_unchanged(self):
        batch_id, items = self.create(('first', 'second', 'third'))
        self.driver.finish(error='first failed')
        self.wait_for(lambda: len(self.driver.started) == 2)
        self.driver.finish(group_media([media('second', confidence=50)]))
        self.wait_for(lambda: len(self.driver.started) == 3)
        self.driver.finish(error='third failed')
        self.assertEqual([item['state'] for item in items], ['failed', 'waiting_selection', 'failed'])
        self.controller.batch_action('retry_failed', batch_id)
        self.assertEqual(items[0]['state'], 'analyzing')
        self.assertEqual(items[1]['state'], 'waiting_selection')
        self.assertEqual(items[2]['state'], 'waiting_discovery')
        self.assertEqual(len(self.driver.started), 4)
        self.assertEqual(len(self.controller.candidate_groups(items[1]['id'])), 1)


    def test_track_preference_missing_language_waits_without_stopping_next_page(self):
        from core.media_tracks import ytdlp_catalog
        batch_id = self.controller.create(['https://pages.example.test/first', 'https://pages.example.test/second'],
            {'track_preferences': {'audio_language': 'ja'}})
        self.wait_for(lambda: len(self.driver.started) == 1)
        candidate = media('first', metadata={'track_catalog': ytdlp_catalog({'formats': [
            {'format_id': '140', 'vcodec': 'none', 'acodec': 'aac', 'language': 'en'}]})})
        self.driver.finish(group_media([candidate]))
        self.wait_for(lambda: len(self.driver.started) == 2)
        item = next(item for item in self.controller.items.values() if item['ordinal'] == 1)
        self.assertEqual(item['state'], 'waiting_selection')
        self.assertIn('音轨', item['error'])
        self.assertEqual(self.queue.jobs, [])

    def test_missing_subtitle_is_not_silently_replaced(self):
        from core.media_tracks import ytdlp_catalog
        self.controller.create(['https://pages.example.test/first'], {'track_preferences': {
            'subtitle_mode': 'sidecar', 'subtitle_languages': ['ja']}})
        self.wait_for(lambda: bool(self.driver.started))
        candidate = media('first', metadata={'track_catalog': ytdlp_catalog({
            'subtitles': {'en': [{'url': 'https://cdn.example.test/en.vtt', 'ext': 'vtt'}]}})})
        self.driver.finish(group_media([candidate]))
        item = next(iter(self.controller.items.values()))
        self.assertEqual(item['state'], 'waiting_selection')
        self.assertIn('未替换', item['error'])
        self.assertEqual(self.queue.jobs, [])

    def test_per_task_audio_selection_is_passed_without_cross_task_ids(self):
        from core.media_tracks import default_selection, ytdlp_catalog
        self.create(('first', 'second'))
        for name, language, format_id in (('first', 'en', '140'), ('second', 'ja', '141')):
            values = ytdlp_catalog({'formats': [{'format_id': format_id, 'vcodec': 'none', 'acodec': 'aac', 'language': language}]})
            candidate = media(name, confidence=50, metadata={'track_catalog': values})
            self.driver.finish(group_media([candidate]))
            item = next(item for item in self.controller.items.values() if item['url'].endswith(name))
            self.assertTrue(self.controller.select_candidate(item['id'], candidate.id, confirmed=True,
                track_selection=default_selection(values, {'audio_language': language})))
            job = self.queue.jobs[-1]
            self.assertEqual(job.request.format_selector, 'bestvideo+' + format_id)
            self.assertEqual(job.request.media_metadata['selected_audio']['language'], language)
            if name == 'first':
                self.wait_for(lambda: len(self.driver.started) == 2)
        self.assertNotEqual(self.queue.jobs[0].request.media_metadata['track_selection'],
                            self.queue.jobs[1].request.media_metadata['track_selection'])


if __name__ == '__main__':
    unittest.main()
