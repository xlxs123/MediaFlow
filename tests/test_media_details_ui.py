"""媒体详情的实际 Qt 展示与发现入口回归，不启动浏览器或发出外部请求。"""
from copy import deepcopy
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from core.discovery_models import MediaCandidate, NetworkObservation
from core.media_discovery import MediaDiscoveryService
from core.request_context import RequestContext
from tests.test_rou_current_ui import DialogHarness, verified
from ui.discovery_dialog import candidate_details
from ui.media_details_panel import MediaDetailsPanel, bitrate_text, size_text


class MediaDetailsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = MediaDetailsPanel()

    def tearDown(self):
        self.panel.deleteLater()
        self.app.processEvents()

    def test_unknown_is_not_zero_or_invented(self):
        candidate = MediaCandidate('unknown', RequestContext('https://media.example/no-extension'),
                                   media_type='Direct')
        self.panel.show_candidate(candidate)
        for key in ('duration', 'resolution', 'fps', 'videoCodec', 'audioCodec', 'size', 'container'):
            self.assertEqual(self.panel.values[key].text(), '未知', key)
        self.assertEqual(self.panel.values['quality'].text(), '待分析')

    def test_details_display_units_estimate_and_evidence(self):
        candidate = MediaCandidate('full', RequestContext('https://media.example/video'),
            media_type='HLS', source_type='Network', duration=19.5,
            width=1920, height=1080, fps=29.97, videoCodec='H.264', audioCodec='AAC',
            videoBitrate=4_000_000, audioBitrate=128_000, estimatedSize=10_485_760,
            container='MPEG-TS', confidenceScore=98,
            metadata={'title': '示例正片', 'size_is_estimate': True,
                      'detail_sources': {'videoCodec': 'HLS CODECS'}})
        before = deepcopy(candidate)
        self.panel.show_candidate(candidate)
        self.assertEqual(self.panel.values['title'].text(), '示例正片')
        self.assertEqual(self.panel.values['resolution'].text(), '1920 × 1080')
        self.assertEqual(self.panel.values['size'].text(), '约 10.0 MiB')
        self.assertEqual(self.panel.values['duration'].text(), '19秒')
        self.assertEqual(self.panel.values['confidence'].text(), '98/100 · 推荐')
        self.assertIn('HLS CODECS', self.panel.values['videoCodec'].toolTip())
        self.assertEqual(candidate, before)

    def test_direct_http_total_size_reaches_details(self):
        service = MediaDiscoveryService()
        context = RequestContext('https://cdn.example/video', headers={'Referer': 'https://page.example/'})
        service.consume_network(NetworkObservation('capture', context, 206, 'video/mp4',
            content_length=1024, response_headers={'Content-Range': 'bytes 0-1023/10485760'}))
        candidate = service.ranked(include_low=True)[0]
        self.panel.show_candidate(candidate)
        self.assertEqual(candidate.estimatedSize, 10_485_760)
        self.assertEqual(self.panel.values['size'].text(), '10.0 MiB')
        self.assertEqual(candidate.request_context.headers, context.headers)

    def test_resolver_png_transport_is_hls_and_never_image_size(self):
        service = MediaDiscoveryService()
        original = verified()
        payload = deepcopy(original.download_payload())
        service.consume_resolver(original)
        candidate = service.ranked(include_low=True)[0]
        self.panel.show_candidate(candidate)
        self.assertEqual(candidate.media_type, 'HLS')
        self.assertNotIn('PNG', self.panel.values['container'].text().upper())
        self.assertIsNone(candidate.estimatedSize)
        self.assertEqual(candidate.download_payload(), payload)
        self.assertTrue(candidate.manifest_text.startswith('#EXTM3U'))

    def test_selection_updates_panel_without_changing_payload(self):
        dialog = DialogHarness()
        dialog.media_details = self.panel
        candidate = verified()
        payload = deepcopy(candidate.download_payload())
        dialog._update_candidates([candidate], 'BLOB_MSE', dialog.worker.generation)
        self.assertEqual(self.panel.values['type'].text(), 'HLS')
        self.assertEqual(self.panel.values['source'].text(), 'RouVideoResolver')
        self.assertEqual(dialog.selected_candidate().download_payload(), payload)
        dialog._closing = True
        dialog.deleteLater()

    def test_new_detail_text_is_redacted(self):
        context = RequestContext('https://media.example/v?token=SECRET', headers={'Cookie': 'session=PRIVATE'})
        candidate = MediaCandidate('redaction', context, media_type='Direct',
                                   videoCodec='session=PRIVATE', container='token=SECRET')
        self.panel.show_candidate(candidate)
        rendered = candidate_details(candidate) + '\n'.join(
            label.text() + label.toolTip() for label in self.panel.values.values())
        self.assertNotIn('PRIVATE', rendered)
        self.assertNotIn('SECRET', rendered)
        self.assertIn('PRIVATE', str(candidate.request_context.headers))

    def test_invalid_values_and_actual_size_format(self):
        for number in (None, 0, -1, float('nan'), float('inf'), True):
            self.assertEqual(size_text(number), '未知')
            self.assertEqual(bitrate_text(number), '未知')
        self.assertEqual(size_text(1024, False), '1.0 KiB')

    def test_duration_is_readable_and_detail_duration_remains_a_display_fallback(self):
        for duration, expected in ((19.5, '19秒'), (344, '5分44秒'), (3661, '1小时1分1秒')):
            with self.subTest(duration=duration):
                candidate = MediaCandidate('duration', RequestContext('https://media.example/v'), duration=duration)
                before = deepcopy(candidate)
                self.panel.show_candidate(candidate)
                self.assertEqual(self.panel.values['duration'].text(), expected)
                self.assertEqual(candidate, before)
        candidate = MediaCandidate('fallback', RequestContext('https://media.example/v'),
                                   metadata={'detail_duration': 344})
        self.panel.show_candidate(candidate)
        self.assertEqual(self.panel.values['duration'].text(), '5分44秒')
        self.assertIsNone(candidate.duration)

    def test_placeholder_text_is_localized_without_hiding_known_values(self):
        for unknown in (None, 'None', ' null ', 'Unknown'):
            with self.subTest(unknown=unknown):
                candidate = MediaCandidate('placeholders', RequestContext('https://media.example/v'),
                    media_type=unknown, source_type=unknown, videoCodec=unknown,
                    audioCodec=unknown, container=unknown,
                    metadata={'title': unknown, 'quality_recommendation': unknown})
                self.panel.show_candidate(candidate)
                for key in ('title', 'source', 'type', 'container', 'videoCodec', 'audioCodec'):
                    self.assertEqual(self.panel.values[key].text(), '未知', key)
                self.assertEqual(self.panel.values['recommendation'].text(), '待分析')
        candidate = MediaCandidate('known', RequestContext('https://media.example/v'),
            container='MPEG-TS', audioCodec='Unknown',
            metadata={'title': 'None', 'page_title': '当前页面', 'has_audio': True})
        self.panel.show_candidate(candidate)
        self.assertEqual(self.panel.values['title'].text(), '当前页面')
        self.assertEqual(self.panel.values['container'].text(), 'MPEG-TS')
        self.assertEqual(self.panel.values['audioCodec'].text(), '存在音轨，编码未知')

    def test_confidence_status_and_diagnostics_are_read_only_evidence(self):
        for score, status in ((39, '低可信度'), (40, '需确认'), (70, '需确认'), (79, '需确认'), (80, '推荐')):
            with self.subTest(score=score):
                candidate = MediaCandidate('status', RequestContext('https://media.example/v'),
                    confidenceScore=score, context_complete=False, duration=344,
                    metadata={'confidence_evidence': {'current_player': False},
                              'duration_mismatch': True,
                              'confidence_details': [{'points': 20, 'reason': '时长有效：344 秒'}]})
                before = deepcopy(candidate)
                payload = deepcopy(candidate.download_payload())
                self.panel.show_candidate(candidate)
                self.assertEqual(self.panel.values['confidence'].text(), f'{score}/100 · {status}')
                diagnostic = self.panel.values['diagnostics']
                self.assertIn('非当前播放器', diagnostic.text())
                self.assertIn('缺少页面上下文', diagnostic.text())
                self.assertIn('缺少可复现的请求上下文', diagnostic.text())
                self.assertIn('未检测到完整媒体信息：分辨率、编码', diagnostic.text())
                self.assertIn('时长不匹配', diagnostic.text())
                self.assertTrue(diagnostic.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
                self.assertIn('✓ 时长检测通过', self.panel.values['confidence'].toolTip())
                self.assertNotIn('时长有效', self.panel.values['confidence'].toolTip())
                self.assertEqual(candidate, before)
                self.assertEqual(candidate.download_payload(), payload)

    def test_confidence_reason_tooltip_is_redacted(self):
        context = RequestContext('https://media.example/v?token=UNITSECRET',
                                 headers={'Cookie': 'session=UNITPRIVATE'})
        candidate = MediaCandidate('private-reason', context, confidenceReasons=[
            '请求上下文 UNITPRIVATE，签名 UNITSECRET'],
            metadata={'detail_sources': {'diagnostics': 'UNITPRIVATE'}})
        self.panel.show_candidate(candidate)
        rendered = '\n'.join(label.text() + label.toolTip() for label in self.panel.values.values())
        self.assertNotIn('UNITPRIVATE', rendered)
        self.assertNotIn('UNITSECRET', rendered)
        self.assertIn('********', self.panel.values['confidence'].toolTip())

    def test_portrait_quality_label_matches_short_edge_and_details_keep_full_dimensions(self):
        candidate = MediaCandidate('portrait', RequestContext('https://media.example/opaque'),
                                   width=1080, height=1920)
        self.panel.show_candidate(candidate)
        self.assertEqual(candidate.resolution_label, '1080p')
        self.assertEqual(self.panel.values['resolution'].text(), '1080 × 1920')


if __name__ == '__main__':
    unittest.main()
