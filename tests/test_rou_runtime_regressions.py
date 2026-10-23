"""实际响应复用、已验证角色与过期任务恢复的边界回归。"""
import base64
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from core.download_manager import DownloadManager
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest
from core.media_groups import group_media
from tests import test_browser_observer as observer_fixture
from tests.test_download_queue import WorkerFactory
from tests.test_media_groups import candidate


class ImageResponseReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    setUp = observer_fixture.ObserverTests.setUp
    tearDown = observer_fixture.ObserverTests.tearDown
    event = observer_fixture.ObserverTests.event
    answer = observer_fixture.ObserverTests.answer

    def image(self):
        url = 'https://cdn.example/opaque/index.png?auth=actual'
        req = observer_fixture.request(url=url)
        req['type'] = 'Image'
        self.event('Network.requestWillBeSent', req)
        resp = observer_fixture.response(url=url, mime='image/png', has_extra=False)
        resp['type'] = 'Image'
        self.event('Network.responseReceived', resp)
        self.event('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 20})
        self.observer._enabled = True
        return url

    def test_finished_browser_image_reads_actual_response_without_replacement_fetch(self):
        url = self.image()
        values = []
        self.assertTrue(self.observer.fetch_url(url, lambda *args: values.append(args)))
        commands = self.observer._socket.messages
        self.assertFalse(any(c['method'] == 'Runtime.evaluate' for c in commands))
        body = next(c for c in commands if c['method'] == 'Network.getResponseBody')
        self.answer(body, {'body': base64.b64encode(b'actual PNG response').decode(), 'base64Encoded': True})
        self.assertEqual(self.observations[-1].body, b'actual PNG response')
        self.assertEqual(values[0][0]['source'], 'observed_response')
        self.assertTrue(values[0][1])

    def test_completed_image_response_is_reused_without_second_cdp_or_fetch(self):
        url = self.image()
        self.observer.fetch_url(url)
        command = self.observer._socket.messages[-1]
        self.answer(command, {'body': 'observed body', 'base64Encoded': False})
        before = len(self.observer._socket.messages)
        values = []
        self.assertTrue(self.observer.fetch_url(url, lambda *args: values.append(args)))
        self.assertEqual(len(self.observer._socket.messages), before)
        self.assertEqual(values[0][0]['source'], 'observed_response')

    def test_pending_image_waits_for_actual_loading_finished_without_cors_fetch(self):
        url = self.image()
        record = self.observer._store.get('', 'r1')
        record.hops[-1].finished = False
        values = []
        self.assertTrue(self.observer.fetch_url(url, lambda *args: values.append(args)))
        self.assertFalse(self.observer._socket.messages)
        self.event('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 20})
        command = self.observer._socket.messages[-1]
        self.assertEqual(command['method'], 'Network.getResponseBody')
        self.answer(command, {'body': 'actual image bytes', 'base64Encoded': False})
        self.assertEqual(values[0][0]['source'], 'observed_response')
        self.assertFalse(self.observer._pending_image_reads)

    def test_pending_image_timeout_completes_callback_and_reset_discards_old_target(self):
        url = self.image()
        self.observer._store.get('', 'r1').hops[-1].finished = False
        values = []
        self.observer.fetch_url(url, lambda *args: values.append(args))
        self.observer._pending_image_reads[('', 'r1')]['deadline'] = 0
        self.observer._expire_commands()
        self.assertEqual(values[0][0]['reason'], 'body_timeout')
        self.observer.fetch_url(url)
        self.observer.reset()
        self.assertFalse(self.observer._pending_image_reads)

    def test_api_same_origin_credentials_allow_redirect_to_cookie_free_cdn(self):
        self.observer._enabled = True
        self.observer.fetch_url('https://page.example/api/hls/id', headers={
            'cookie': 'actual-api-session', 'referer': 'https://page.example/v/id'})
        command = self.observer._socket.messages[-1]
        self.assertIn('credentials:"same-origin"', command['params']['expression'])

    def test_actual_cross_origin_cookie_context_can_opt_in_credentials(self):
        self.observer._enabled = True
        self.observer.fetch_url('https://cdn.example/index', headers={
            'cookie': 'actual-cdn-session', 'referer': 'https://page.example/v/id'})
        command = self.observer._socket.messages[-1]
        self.assertIn('credentials:"include"', command['params']['expression'])


class VerifiedMediaRoleTests(unittest.TestCase):
    def test_explicit_main_api_role_beats_sample_ad_words_in_resource_path(self):
        for name in ('sample-a', 'preroll', 'movie-trailer'):
            media = candidate(name, metadata={'resolver_verified': True, 'resolver_route': 'api_redirect'})
            group = group_media([media])[0]
            self.assertTrue(group.selected)
            self.assertFalse(group.is_preview)
            self.assertFalse(group.is_ad)

    def test_explicit_preview_or_ad_evidence_still_excludes_default_selection(self):
        for flag in ('preview', 'is_ad'):
            media = candidate('main', metadata={'resolver_verified': True, 'resolver_route': 'api_redirect', flag: True})
            self.assertFalse(group_media([media])[0].selected)


class ResolverRefreshLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = DownloadQueue(worker_factory=WorkerFactory())
        self.manager = DownloadManager(self.queue)
        self.events = []
        self.manager.refresh_required.connect(self.events.append)
        self.request = DownloadRequest('yt-dlp.exe', 'https://cdn.example/index.png?auth=issued-1', self.temp.name,
                                       media_metadata={'resolver_id': 'resolver', 'resolver_verified': True,
                                                       'page_url': 'https://page.example/v/id'})

    def tearDown(self):
        self.queue.stop_all()
        self.manager.deleteLater()
        self.queue.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def test_single_video_without_batch_emits_refresh_once_for_same_issued_url(self):
        job = self.queue.add(self.request)
        job.state = 'waiting_context'
        for _ in range(4):
            self.queue.changed.emit(job.id)
        self.assertEqual(self.events, [job.id])
        self.assertFalse(job.batch_id)

    def test_reissued_urls_can_refresh_again_with_three_attempt_job_limit(self):
        job = self.queue.add(self.request)
        job.state = 'waiting_context'
        for number in range(5):
            job.request = replace(job.request, url=f'https://cdn.example/index.png?auth=issued-{number}')
            self.queue.changed.emit(job.id)
        self.assertEqual(self.events, [job.id] * 3)

    def test_generic_or_active_task_never_starts_resolver_refresh(self):
        job = self.queue.add(replace(self.request, media_metadata={}))
        job.state = 'waiting_context'
        self.queue.changed.emit(job.id)
        job.request = self.request
        job.worker = object()
        self.queue.changed.emit(job.id)
        job.worker = None
        self.assertEqual(self.events, [])
