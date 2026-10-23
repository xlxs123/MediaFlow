"""候选的结构资格、真实上下文、分片关联和手动下载入口回归。"""
from copy import deepcopy
from unittest.mock import patch
import unittest
import threading
import time

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from core.discovery_models import MediaCandidate, NetworkObservation
from core.download_manager import DownloadManager
from core.discovery_worker import DiscoveryWorker
from core.media_discovery import MediaDiscoveryService
from core.media_groups import available_candidate, group_media
from core.media_quality import recommend_candidate
from core.request_context import RequestContext
from tests.test_media_discovery import AES_HLS, DASH, MP4, box
from tests.test_mediaflow_ui import media
from tests.test_rou_current_ui import DialogHarness, verified


class CandidateStateTests(unittest.TestCase):
    def candidate(self, **fields):
        values = dict(media_type='Direct', confirmed=True, context_complete=True,
                      drm=False, confidenceScore=0)
        values.update(fields)
        return MediaCandidate('candidate', RequestContext('https://cdn.example/video'), **values)

    def test_zero_confidence_is_manual_available_but_not_recommended(self):
        candidate = self.candidate()
        self.assertEqual((candidate.discovery_state, candidate.validation_state, candidate.download_state),
                         ('DISCOVERED', 'NEEDS_CONFIRMATION', 'AVAILABLE'))
        self.assertTrue(available_candidate(candidate))
        self.assertIsNone(recommend_candidate([candidate]))
        self.assertFalse(group_media([candidate])[0].selected)

    def test_availability_updates_after_late_context_and_probe_without_stale_fields(self):
        candidate = self.candidate(context_complete=False, confirmed=False)
        self.assertFalse(candidate.downloadable)
        candidate.context_complete = candidate.confirmed = True
        self.assertEqual(candidate.unavailable_reasons, [])
        self.assertEqual(candidate.download_state, 'AVAILABLE')
        candidate.http_status = 403
        self.assertIn('HTTP 403', ' '.join(candidate.unavailable_reasons))
        self.assertIs(candidate.drm, False)

    def test_every_real_rejection_has_reason_and_never_uses_low_score(self):
        for fields, reason in (
            ({'drm': True}, 'DRM'), ({'context_complete': False}, '请求上下文'),
            ({'confirmed': False}, '媒体类型验证'), ({'http_status': 401}, '登录'),
            ({'media_type': 'Unsupported'}, '无法处理'),
            ({'media_type': 'HLS', 'classification': 'HLS_SAMPLE_AES'}, '无法处理'),
            ({'metadata': {'probe_error': '检测失败'}}, '检测失败'),
            ({'metadata': {'resource_role': 'Media Segment'}}, '只有媒体分片'),
            ({'content_type': 'image/png'}, '不是完整媒体'),
            ({'is_ad': True}, '广告'),
            ({'metadata': {'required_headers': ['Referer']}}, '必要请求 Header')):
            with self.subTest(fields=fields):
                candidate = self.candidate(confidenceScore=100, **fields)
                self.assertFalse(available_candidate(candidate))
                self.assertIn(reason, ' '.join(candidate.unavailable_reasons))
                self.assertEqual(candidate.validation_state, 'UNAVAILABLE')

    def test_no_origin_or_cookie_does_not_invent_missing_header(self):
        candidate = self.candidate()
        candidate.request_context = RequestContext(candidate.url, headers={'Referer': 'https://page.example/',
                                                                         'User-Agent': 'Fixture Browser'})
        self.assertTrue(candidate.downloadable)
        self.assertEqual(candidate.unavailable_reasons, [])

    def test_low_confidence_manager_request_keeps_all_context_and_decoded_playlist(self):
        candidate = verified()
        candidate.confidenceScore = 0
        group = group_media([candidate])[0]
        from core.downloader import DownloadRequest
        # 构造已存在请求接口所需的本地配置，调用队列使用的实际转换入口。
        template = DownloadRequest(url=candidate.url, output_directory='D:/candidate-fixture',
                                   yt_dlp_path='tools/yt-dlp.exe', ffmpeg_path='tools/ffmpeg.exe')
        request = DownloadManager._request(candidate, group, template)
        self.assertTrue(available_candidate(candidate))
        self.assertEqual(request.context_snapshot, candidate.request_context)
        self.assertEqual(request.headers, candidate.request_context.headers)
        self.assertEqual(request.manifest_text, candidate.manifest_text)
        self.assertEqual(request.download_protocol, 'HLS')


class CandidateProtocolFlowTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaDiscoveryService()

    def network(self, identifier, url, mime, body, **fields):
        self.service.consume_network(NetworkObservation(identifier, RequestContext(url,
            headers={'Referer': 'https://page.example/', 'User-Agent': 'Fixture Browser',
                     'Cookie': 'session=PRIVATE', 'Range': 'bytes=0-'}), 200, mime, body,
            response_headers={'Content-Type': mime, 'X-Trace': 'fixture'}, body_complete=True, **fields))

    def test_single_m4s_and_ts_are_selectable_diagnostics_never_final_direct_targets(self):
        for suffix, mime, body in (('m4s', 'video/mp4', box(b'moof', b'fragment')),
                                   ('ts', 'video/mp2t', b'\x47' * 600)):
            with self.subTest(suffix=suffix):
                self.service.clear()
                self.network('part', 'https://cdn.example/part0.' + suffix, mime, body)
                candidate = self.service.ranked(include_low=True)[0]
                self.assertEqual(candidate.media_type, 'Segment')
                self.assertFalse(candidate.downloadable)
                self.assertIn('只有媒体分片', ' '.join(candidate.unavailable_reasons))

    def test_late_dash_manifest_associates_old_fragments_and_keeps_complete_mpd(self):
        self.network('part', 'https://cdn.example/chunk_low_00001.m4s', 'video/mp4', box(b'moof'))
        self.network('mpd', 'https://cdn.example/manifest', 'application/dash+xml', DASH)
        values = self.service.ranked(include_low=True)
        self.assertEqual([c.media_type for c in values], ['DASH'])
        self.assertTrue(values[0].downloadable)
        stats = self.service.discovery_diagnostic()['statistics']
        self.assertEqual(stats['filtered_reasons']['DASH Media Segment'], 1)

    def test_complete_mp4_response_overrides_weak_ts_m4s_extension(self):
        for suffix in ('ts', 'm4s'):
            self.service.clear()
            self.network('full', 'https://cdn.example/full.' + suffix, 'video/mp4', MP4)
            candidate = self.service.ranked(include_low=True)[0]
            self.assertEqual(candidate.media_type, 'Direct')
            self.assertTrue(candidate.downloadable)

    def test_new_valid_browser_response_recovers_stale_probe_failure(self):
        url = 'https://cdn.example/index'
        self.network('initial', url, 'application/vnd.apple.mpegurl', AES_HLS)
        candidate = self.service.ranked(include_low=True)[0]
        self.service.apply_probe(candidate.id, {'http_status': 403, 'error_code': 'HTTP_403', 'probe_error': '旧请求失败'})
        self.assertFalse(self.service.get(candidate.id).downloadable)
        self.network('retry', url, 'application/vnd.apple.mpegurl', AES_HLS)
        recovered = self.service.get(candidate.id)
        self.assertTrue(recovered.downloadable)
        self.assertNotIn('probe_error', recovered.metadata)
        self.assertEqual(recovered.metadata['last_request_id'], 'retry')

    def test_late_clear_response_never_clears_explicit_drm_protection(self):
        url = 'https://cdn.example/index'
        self.network('initial', url, 'application/vnd.apple.mpegurl', AES_HLS)
        candidate = self.service.ranked(include_low=True)[0]
        self.service.apply_probe(candidate.id, {'drm': True, 'classification': 'DRM_WIDEVINE',
            'error_code': 'PROTECTED_MEDIA', 'probe_error': '明确保护证据'})
        self.network('retry', url, 'application/vnd.apple.mpegurl', AES_HLS)
        current = self.service.get(candidate.id)
        self.assertIs(current.drm, True)
        self.assertFalse(current.downloadable)

    def test_hls_png_segments_associate_in_either_order_and_keep_context(self):
        for segment_first in (True, False):
            self.service.clear()
            if segment_first:
                self.network('part', 'https://cdn.example/abc000.png', 'video/mp2t', b'\x47' * 600)
            self.network('manifest', 'https://cdn.example/index', 'application/octet-stream', AES_HLS)
            self.network('part', 'https://cdn.example/abc000.png', 'image/png', b'encrypted bytes')
            values = self.service.ranked(include_low=True)
            self.assertEqual(len(values), 1)
            candidate = values[0]
            self.assertEqual((candidate.media_type, candidate.encryption, candidate.drm), ('HLS', 'AES-128', False))
            self.assertTrue(candidate.downloadable)
            self.assertEqual(candidate.metadata['segment_requests'], 1)
            self.assertEqual(candidate.metadata['response_headers']['X-Trace'], 'fixture')
            self.assertEqual(candidate.request_context.headers['range'], 'bytes=0-')
            self.assertEqual(self.service.discovery_diagnostic()['statistics']['filtered'], 1)


class CandidateSelectionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DialogHarness()
        self.dialog.discovery_stats_label = QLabel()
        self.imported = []
        self.dialog.media_selected.connect(self.imported.append)

    def tearDown(self):
        self.dialog._closing = True
        self.dialog.deleteLater()
        self.app.processEvents()

    def show(self, candidate):
        self.dialog._update_candidates([candidate], '', self.dialog.worker.generation)
        self.dialog.include_low.setChecked(True)
        self.dialog._select_card(candidate.id)

    def test_score_zero_manual_confirm_import_preserves_context_response_and_score(self):
        candidate = media('low')
        candidate.confidenceScore = 0
        candidate.metadata['response_headers'] = {'Content-Type': 'video/mp4'}
        before = deepcopy((candidate.request_context, candidate.confidenceReasons, candidate.metadata))
        self.show(candidate)
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog.confirm()
        imported = self.imported[0]
        self.assertEqual(imported.confidenceScore, 0)
        self.assertEqual((imported.request_context, imported.confidenceReasons), before[:2])
        self.assertEqual(imported.metadata['response_headers'], before[2]['response_headers'])

    def test_unavailable_is_selectable_for_details_and_exact_reason_is_visible(self):
        candidate = media('failed', http_status=403)
        self.show(candidate)
        self.assertEqual(self.dialog.selected_candidate().id, candidate.id)
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        self.assertIn('HTTP 403', self.dialog.details.toPlainText())
        self.dialog.confirm()
        self.assertIn('HTTP 403', self.dialog.status_label.text())
        self.assertFalse(self.imported)

    def test_counts_separate_found_inspectable_downloadable_and_filtered(self):
        candidate = media('low')
        candidate.confidenceScore = 0
        self.show(candidate)
        self.dialog._generic_details({'statistics': {'page_requests': 328, 'media_requests': 15,
            'filtered': 11, 'filtered_reasons': {'Media Segment': 11}}}, self.dialog.worker.generation)
        values = self.dialog.discovery_counts()
        self.assertEqual((values['candidates'], values['selectable'], values['downloadable']), (1, 1, 1))
        self.assertEqual((values['page_requests'], values['media_requests'], values['filtered']), (328, 15, 11))
        self.assertIn('低可信度：1', self.dialog.discovery_stats_label.text())
        self.assertIn('Media Segment：11', self.dialog.discovery_stats_label.toolTip())


class CandidateAsyncValidationTests(unittest.TestCase):
    def test_late_old_probe_cannot_override_new_browser_request_with_same_headers(self):
        app = QApplication.instance() or QApplication([])
        worker = DiscoveryWorker()
        started, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls, observed = [], []
        worker.candidates_changed.connect(lambda values, *_: observed.append(values))

        def pump(predicate, timeout=5):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                app.processEvents()
                if predicate():
                    return
                time.sleep(.01)
            self.fail('异步候选验证等待超时')

        def probe(context, cancel_event):
            calls.append(context)
            if len(calls) == 1:
                started.set()
                release.wait(5)
                returned.set()
                return {'http_status': 403, 'error_code': 'HTTP_403', 'probe_error': '过期的旧探测'}, None
            return {'http_status': 200, 'media_type': 'HLS', 'drm': False,
                    'classification': 'HLS_AES128', 'encryption': 'AES-128'}, None

        context = RequestContext('https://cdn.example/index', headers={'User-Agent': 'Fixture'})
        observation = lambda identifier: NetworkObservation(identifier, context, 200,
            'application/vnd.apple.mpegurl', AES_HLS, body_complete=True)
        try:
            with patch.object(worker, '_probe', side_effect=probe):
                worker.start()
                worker.submit('network', observation('first'))
                pump(started.is_set)
                worker.submit('network', observation('retry'))
                pump(lambda: len(calls) >= 2 and observed and
                     observed[-1][0].metadata.get('last_request_id') == 'retry')
                release.set()
                pump(returned.is_set)
                deadline = time.monotonic() + .3
                while time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.01)
                self.assertTrue(observed[-1][0].downloadable)
                self.assertNotIn('probe_error', observed[-1][0].metadata)
                self.assertEqual(calls[0], calls[1])
        finally:
            release.set()
            worker.cancel()
            self.assertTrue(worker.wait(5000))
            worker.deleteLater()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
