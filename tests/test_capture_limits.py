import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from core.browser_observer import BrowserNetworkObserver, _NetworkStore, capture_body_limit, capture_total_limit
from core.discovery_models import NetworkObservation
from core.discovery_worker import DiscoveryWorker
from core.request_context import RequestContext
from core.yt_dlp_manager import ToolInfo
from tests.test_browser_observer import FakePage, FakeSocket, request, response
from utils.config import ConfigStore

MIB = 1024 * 1024


def document_store(size, max_bytes=16 * MIB, mime='application/json'):
    store = _NetworkStore(max_bytes)
    store.consume('Network.requestWillBeSent', request(url='https://api.example/playurl'))
    store.consume('Network.responseReceived', response(url='https://api.example/playurl', mime=mime,
        headers={'Content-Length': str(size)}))
    store.consume('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': size})
    return store, store.get('', 'r1')


class CaptureLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_defaults_allow_complete_json_above_previous_two_mib_limit(self):
        body = json.dumps({'data': {'dash': {}}, 'padding': 'x' * (3 * MIB)}).encode()
        store, record = document_store(len(body))
        self.assertTrue(store.wants_body(record))
        observed = store.set_body(record, record.hops[-1], {'body': body.decode()})
        self.assertTrue(observed.body_complete)
        self.assertEqual(json.loads(observed.body)['data'], {'dash': {}})

    def test_large_mpd_is_retained_without_truncating_to_two_mib(self):
        body = '<MPD><!--' + 'x' * (3 * MIB) + '--></MPD>'
        store, record = document_store(len(body), mime='application/dash+xml')
        self.assertTrue(store.wants_body(record))
        observed = store.set_body(record, record.hops[-1], {'body': body})
        self.assertEqual(observed.body.decode(), body)

    def test_configured_per_response_limit_is_enforced_before_and_after_body_read(self):
        store, record = document_store(4 * MIB, max_bytes=3 * MIB)
        self.assertFalse(store.wants_body(record))
        self.assertIsNone(store.set_body(record, record.hops[-1], {'body': 'x' * (3 * MIB + 1)}))

    def test_limits_and_aggregate_memory_remain_bounded(self):
        self.assertEqual(capture_body_limit(), 16 * MIB)
        self.assertEqual(capture_body_limit(0), MIB)
        self.assertEqual(capture_body_limit(1000 * MIB), 128 * MIB)
        self.assertEqual(capture_body_limit(True), 16 * MIB)
        self.assertEqual(capture_total_limit(MIB), 64 * MIB)
        self.assertEqual(capture_total_limit(128 * MIB), 512 * MIB)
        store, record = document_store(100)
        store.total_body_read = store.max_total_body_bytes - 2
        self.assertIsNone(store.set_body(record, record.hops[-1], {'body': '123'}))

    def test_cdp_buffers_and_browser_fetch_share_configured_limit(self):
        observer = BrowserNetworkObserver(FakePage(), max_body_bytes=24 * MIB)
        observer._active = observer._enabled = True
        observer._socket = FakeSocket(parent=observer)
        try:
            observer._enable_session('')
            command = next(command for command in observer._socket.messages if command['method'] == 'Network.enable')
            self.assertEqual(command['params']['maxResourceBufferSize'], 24 * MIB)
            self.assertEqual(command['params']['maxTotalBufferSize'], 96 * MIB)
            self.assertTrue(observer.fetch_url('https://api.example/playurl'))
            command = next(command for command in reversed(observer._socket.messages) if command['method'] == 'Runtime.evaluate')
            self.assertIn(f'size > {24 * MIB}', command['params']['expression'])
            self.assertEqual(observer._store.max_body_bytes, 24 * MIB)
            self.assertEqual(observer.network_diagnostics()['max_body_bytes'], 24 * MIB)
        finally:
            observer.stop()
            observer.deleteLater()

    def test_worker_preserves_large_complete_description_under_configured_limit(self):
        body = b' ' * (3 * MIB)
        worker = DiscoveryWorker(probe=False, max_body_bytes=4 * MIB)
        observed = NetworkObservation('json', RequestContext('https://api.example/playurl'),
            200, 'application/json', body, body_complete=True)
        worker.submit('network', observed)
        _, kind, queued, size = worker._queue.get_nowait()
        self.assertEqual(kind, 'network')
        self.assertTrue(queued.body_complete)
        self.assertIs(queued.body, body)
        self.assertEqual(size, 3 * MIB)

    def test_worker_discards_oversized_body_but_keeps_request_observation(self):
        worker = DiscoveryWorker(probe=False, max_body_bytes=MIB)
        observed = NetworkObservation('json', RequestContext('https://api.example/playurl'),
            200, 'application/json', b'x' * (MIB + 1), body_complete=True)
        worker.submit('network', observed)
        _, _, queued, size = worker._queue.get_nowait()
        self.assertIsNone(queued.body)
        self.assertFalse(queued.body_complete)
        self.assertEqual(queued.request_id, 'json')
        self.assertEqual(size, 0)

    def test_worker_queue_stays_inside_aggregate_budget(self):
        worker = DiscoveryWorker(probe=False, max_body_bytes=MIB)
        worker.max_queued_bytes = 2 * MIB
        for index in range(3):
            worker.submit('network', NetworkObservation(str(index), RequestContext('https://api.example/playurl'),
                200, 'application/json', b'x' * (900 * 1024), body_complete=True))
        self.assertEqual(worker._queue.qsize(), 2)
        self.assertLessEqual(worker._queued_bytes, worker.max_queued_bytes)

    def test_complete_large_dash_json_reaches_service_without_plain_api_probe(self):
        payload = {'data': {'dash': {'duration': 20, 'video': [{
            'id': 80, 'baseUrl': 'https://cdn.example/video.m4s', 'bandwidth': 1000000,
            'width': 1920, 'height': 1080, 'codecs': 'avc1.640028', 'mimeType': 'video/mp4',
            'SegmentBase': {'Initialization': '0-1251', 'indexRange': '1252-1631'}}],
            'audio': [{'id': 30280, 'baseUrl': 'https://cdn.example/audio.m4s', 'bandwidth': 128000,
                       'codecs': 'mp4a.40.2', 'mimeType': 'audio/mp4',
                       'SegmentBase': {'Initialization': '0-251', 'indexRange': '252-631'}}]}},
            'padding': 'x' * (3 * MIB)}
        body = json.dumps(payload).encode()
        context = RequestContext('https://api.example/playurl', headers={
            'Referer': 'https://page.example/watch', 'User-Agent': 'Fixture'})
        worker = DiscoveryWorker(probe=True, max_body_bytes=4 * MIB)
        outputs, batches = [], []
        worker.candidates_changed.connect(lambda values, *_: outputs.append(values))
        worker.batch_ready.connect(lambda values, *_: batches.append(values))
        with patch.object(worker, '_probe') as probe:
            try:
                worker.start()
                worker.submit('network', NetworkObservation('complete-dash', context, 200,
                    'application/json', body, resource_type='XHR', body_complete=True))
                worker.freeze()
                deadline = time.monotonic() + 4
                while not batches and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertTrue(batches)
                paired = [candidate for candidate in batches[-1] if getattr(candidate, 'video_track', None)]
                self.assertTrue(paired)
                self.assertTrue(all(candidate.media_type == 'DASH' and candidate.manifest_text for candidate in paired))
                probe.assert_not_called()
            finally:
                worker.cancel()
                deadline = time.monotonic() + 3
                while worker.isRunning() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertFalse(worker.isRunning())
                worker.deleteLater()

    def test_main_window_control_persists_capture_limit(self):
        from ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as directory, \
                patch('ui.main_window.detect_yt_dlp', return_value=ToolInfo(error='unit test')), \
                patch('ui.main_window.detect_ffmpeg', return_value=ToolInfo(error='unit test')):
            store = ConfigStore(Path(directory) / 'config.json')
            window = MainWindow(store)
            try:
                self.assertEqual(window.browser_body_limit.value(), 16)
                window.browser_body_limit.setValue(48)
                self.assertEqual(window.settings['browser_body_limit_mib'], 48)
                self.assertEqual(store.load()['browser_body_limit_mib'], 48)
            finally:
                deadline = time.monotonic() + 5
                self.app.processEvents()
                while window.detector and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                window.close()
                self.app.processEvents()
