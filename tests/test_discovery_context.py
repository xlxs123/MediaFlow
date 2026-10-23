"""候选输入与已有下载管线的连接、隐私与取消回归。"""
from dataclasses import replace
import json
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.discovery_models import MediaCandidate, NetworkObservation
from core.discovery_worker import DiscoveryWorker, context_fingerprint
from core.downloader import DownloadRequest
from core.request_context import RequestContext
from core.errors import MediaCheckError


class DiscoveryContextTests(unittest.TestCase):
    def context(self):
        return RequestContext('https://media.test/final?sign=a%2Fb+%2B&token=secret', headers={
            'referer': 'https://page.test/watch?secure=private', 'user-agent': 'Test Browser',
            'cookie': 'session=private-cookie', 'authorization': 'Bearer private-auth',
            'range': 'bytes=0-'}, original_url='https://media.test/original?secure=private',
            inherit_range=True, redirect_chain=({'from': 'https://media.test/original?secure=private',
            'to': 'https://media.test/final?sign=a%2Fb+%2B&token=secret', 'status': 302},))

    def test_download_snapshot_preserves_actual_context(self):
        context = self.context()
        request = DownloadRequest('yt-dlp.exe', context.url, 'output', headers=context.headers,
                                  inherit_range=True, context_snapshot=context)
        self.assertEqual(request.to_context(), context)
        self.assertEqual(request.to_context().derive('abc000.png').headers, context.headers)

    def test_editing_headers_retains_redirect_metadata(self):
        context = self.context()
        request = DownloadRequest('yt-dlp.exe', context.url, 'output', headers={'user-agent': 'New'},
                                  context_snapshot=context)
        actual = request.to_context()
        self.assertEqual(actual.user_agent, 'New')
        self.assertEqual(actual.original_url, context.original_url)
        self.assertEqual(actual.redirect_chain, context.redirect_chain)

    def test_different_input_never_reuses_stale_snapshot(self):
        request = DownloadRequest('yt-dlp.exe', 'https://other.test/media', 'output', context_snapshot=self.context())
        context = request.to_context()
        self.assertEqual(context.original_url, request.url)
        self.assertEqual(context.redirect_chain, ())
        self.assertNotIn('cookie', context.headers)

    def test_redirect_chain_copied_and_child_chain_independent(self):
        chain = [{'status': 302, 'from': 'https://a.test/start', 'to': 'https://b.test/end'}]
        context = RequestContext('https://b.test/end', redirect_chain=chain)
        chain[0]['to'] = 'https://evil.test'
        self.assertEqual(context.redirect_chain[0]['to'], 'https://b.test/end')
        redirected = context.redirected('https://c.test/end?token=keep', 307)
        self.assertEqual(len(redirected.redirect_chain), 2)
        self.assertEqual(redirected.derive('part').redirect_chain, ())

    def test_candidate_diagnostics_never_expose_secrets(self):
        context = self.context()
        candidate = MediaCandidate('1', context, media_type='HLS', confirmed=True,
                                   redirects=list(context.redirect_chain))
        serialized = json.dumps(candidate.safe_diagnostic())
        for secret in ('private-cookie', 'private-auth', 'a%2Fb', 'private', 'token=secret'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(candidate.url, context.url)
        self.assertEqual(context.headers['cookie'], 'session=private-cookie')

    def test_protected_and_incomplete_candidate_not_downloadable(self):
        candidate = MediaCandidate('1', self.context(), media_type='HLS', confirmed=True, drm=False)
        self.assertTrue(candidate.downloadable)
        self.assertFalse(replace(candidate, drm=True).downloadable)
        self.assertFalse(replace(candidate, context_complete=False).downloadable)
        self.assertFalse(replace(candidate, confirmed=False).downloadable)

    def test_fingerprint_includes_credentials_body_without_echo(self):
        context = self.context()
        for other in (replace(context, headers={**context.headers, 'cookie': 'new'}),
                      replace(context, body=b'new'), replace(context, method='POST')):
            self.assertNotEqual(context_fingerprint(context), context_fingerprint(other))
        self.assertEqual(len(context_fingerprint(context)), 32)

    def test_probe_403_not_drm_and_error_redacted(self):
        error = MediaCheckError('拒绝 private-auth https://media.test?token=secret', 'HTTP_403',
                                {'http_status': 403, 'drm': None})
        with patch('core.discovery_worker.check_media', side_effect=error):
            diagnostic, root = DiscoveryWorker._probe(self.context(), None)
        self.assertIsNone(root)
        self.assertEqual(diagnostic['error_code'], 'HTTP_403')
        self.assertIsNone(diagnostic['drm'])
        self.assertNotIn('private-auth', str(diagnostic))
        self.assertNotIn('token=secret', str(diagnostic))


class DiscoveryWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.worker = DiscoveryWorker(probe=False)
        self.outputs = []
        self.worker.candidates_changed.connect(lambda *args: self.outputs.append(args))
        self.worker.start()

    def tearDown(self):
        self.worker.cancel()
        deadline = time.monotonic() + 3
        while self.worker.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.worker.isRunning())
        self.worker.deleteLater()
        self.app.processEvents()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def observation(self, url):
        return NetworkObservation('1', RequestContext(url, headers={'user-agent': 'Browser'}), 200,
            'application/vnd.apple.mpegurl', b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.png\n#EXT-X-ENDLIST\n',
            body_complete=True)

    def test_events_parsed_in_background_gui_ticks_and_cancel(self):
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        self.worker.reset()
        self.worker.submit('network', self.observation('https://media.test/main'))
        self.wait_for(lambda: bool(self.outputs and self.outputs[-1][0]))
        self.assertTrue(ticks)
        self.assertEqual(self.outputs[-1][0][0].media_type, 'HLS')
        timer.stop()

    def test_reset_drops_previous_candidates_and_generation(self):
        self.worker.reset()
        self.worker.submit('network', self.observation('https://media.test/old'))
        self.wait_for(lambda: bool(self.outputs and self.outputs[-1][0]))
        old_epoch = self.worker.generation
        self.worker.reset()
        self.worker.submit('network', self.observation('https://media.test/new'))
        self.wait_for(lambda: bool(self.outputs and self.outputs[-1][2] > old_epoch and self.outputs[-1][0]))
        self.assertEqual([c.url for c in self.outputs[-1][0]], ['https://media.test/new'])

    def test_memory_queue_bounded_and_stop_refuses_new_work(self):
        other = DiscoveryWorker(probe=False)
        observation = self.observation('https://media.test/main')
        for _ in range(500):
            other.submit('network', observation)
        self.assertLessEqual(other._queue.qsize(), 256)
        other.cancel()
        before = other._queue.qsize()
        other.submit('network', observation)
        self.assertEqual(before, other._queue.qsize())


if __name__ == '__main__':
    unittest.main()
