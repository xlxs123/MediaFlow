"""Rou 解码清单传递、协议分流及最终输出拒绝的回归。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import Mock, patch
import zlib

from PySide6.QtWidgets import QApplication
from core.discovery_models import MediaCandidate
from core.download_manager import DownloadManager
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest, DownloadWorker, FILE_MARKER
from core.errors import MediaCheckError, ProtectedMediaError
from core.http_client import ContextHttpClient
from core.manifests import parse_manifest
from core.media_bridge import MediaBridge
from core.media_discovery import MediaDiscoveryService
from core.media_groups import group_media
from core.media_guard import MediaInspection, MediaSnapshot, check_media
from core.request_context import RequestContext
from core.site_resolvers import RouVideoResolver
from core.yt_dlp_manager import ToolInfo
from tests.test_rou_current_protocol import observation, context, INDEX
from tests.test_rou_playlist_decoder import png


PLAYLIST = ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n'
    '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",\nIV=0x00000000000000000000000000000000\n'
    '#EXTINF:2.000000,\nabc000.png\n#EXTINF:2.000000,\nabc001.png\n#EXT-X-ENDLIST\n')


def resolved_candidate():
    result = RouVideoResolver(legacy_fallback=False).resolve(
        context(observation(png(zlib.compress(PLAYLIST.encode()), 1))))
    assert result.success, result.diagnostics
    return result.candidates[0]


class RouPipelineTests(unittest.TestCase):
    def inspect(self, candidate=None):
        candidate = candidate or resolved_candidate()
        payload = candidate.download_payload()
        payload.pop('transport_url')
        payload['expected_media_type'] = payload.pop('download_protocol')
        return check_media(candidate.request_context, **payload)

    def test_1_resolver_retains_decoded_hls_and_transport_separately(self):
        candidate = resolved_candidate()
        self.assertEqual(candidate.media_type, 'HLS')
        self.assertEqual(candidate.transport_url, INDEX)
        self.assertEqual(candidate.manifest_url, INDEX)
        self.assertEqual(candidate.manifest_text, PLAYLIST)
        self.assertEqual(candidate.manifest_origin, 'embedded:png/roUd')
        self.assertTrue(candidate.validated)
        self.assertEqual(candidate.classification, 'HLS_AES128')
        self.assertIs(candidate.drm, False)

    def test_discovery_copy_keeps_the_decoded_manifest(self):
        candidate = resolved_candidate()
        service = MediaDiscoveryService()
        service.consume_resolver(candidate)
        copied = next(iter(service.candidates.values()))
        self.assertEqual(copied.manifest_text, PLAYLIST)
        self.assertEqual(copied.download_payload(), candidate.download_payload())
        self.assertTrue(copied.validated)

    def test_2_png_url_with_manifest_text_uses_hls_without_transport_get(self):
        with patch('core.media_guard.ContextHttpClient.open', side_effect=AssertionError('不要请求 index.png')):
            inspection = self.inspect()
        self.assertEqual(inspection.root.info.media_type, 'HLS')
        self.assertEqual(inspection.root.data, PLAYLIST.encode())
        self.assertTrue(inspection.root.use_cached_manifest)
        self.assertEqual(inspection.diagnostic()['manifest_origin'], 'embedded:png/roUd')

    def test_3_png_uris_are_hls_media_segments(self):
        info = self.inspect().root.info
        segments = [r for r in info.resources if r.role == 'Media Segment']
        self.assertEqual([r.uri for r in segments], ['abc000.png', 'abc001.png'])
        self.assertEqual(info.keys[0].uri, 'enc.key')
        self.assertEqual(info.encryption, 'AES-128')
        self.assertIs(info.drm, False)

    def test_relative_uris_use_final_manifest_location(self):
        candidate = resolved_candidate()
        candidate.manifest_url = 'https://final.example/new/folder/index.png?auth=keep'
        info = self.inspect(candidate).root.info
        self.assertEqual(info.keys[0].resolved_uri, 'https://final.example/new/folder/enc.key')
        self.assertEqual(info.resources[0].resolved_uri, 'https://final.example/new/folder/abc000.png')
        self.assertEqual(candidate.transport_url, INDEX)

    def test_missing_origin_and_explicit_range_survive_key_and_segment(self):
        candidate = resolved_candidate()
        candidate.request_context = replace(candidate.request_context,
            headers={'Referer': 'https://rou.video/watch', 'User-Agent': 'Fixture Browser', 'Range': 'bytes=0-'},
            inherit_range=True)
        root = self.inspect(candidate).root
        for uri in ('enc.key', 'abc000.png'):
            child = root.context.derive(uri)
            self.assertEqual(child.headers['range'], 'bytes=0-')
            self.assertEqual(child.referer, 'https://rou.video/watch')
            self.assertEqual(child.user_agent, 'Fixture Browser')
            self.assertNotIn('origin', child.headers)

    def test_5_every_manifest_get_uses_cached_decoded_text(self):
        inspection = self.inspect()
        bridge = MediaBridge(inspection).start()
        try:
            self.assertTrue(bridge.url.endswith('.m3u8'))
            with patch('core.media_bridge.read_media', side_effect=AssertionError('不要重新 GET index.png')) as read:
                client = ContextHttpClient()
                for method in ('HEAD', 'GET', 'GET', 'HEAD', 'GET'):
                    with client.open(RequestContext(bridge.url, method=method)) as response:
                        self.assertEqual(response.status, 200)
                        data = response.read()
                        if method == 'GET':
                            self.assertTrue(data.startswith(b'#EXTM3U'))
                            self.assertFalse(data.startswith(b'\x89PNG'))
                read.assert_not_called()
            self.assertEqual({r.kind for r in bridge.resources.values()}, {'manifest', 'segment', 'key'})
        finally:
            bridge.close()

    def test_declared_hls_cannot_fall_back_to_direct(self):
        candidate = resolved_candidate()
        direct_info = parse_manifest(b'PNG bytes', INDEX, 'video/mp4')
        direct = MediaSnapshot(candidate.request_context, b'PNG bytes', 'video/mp4', direct_info)
        with patch('core.media_guard.read_media', return_value=direct), self.assertRaises(MediaCheckError) as caught:
            check_media(candidate.request_context, expected_media_type='HLS')
        self.assertEqual(caught.exception.code, 'MEDIA_PROTOCOL_MISMATCH')

    def test_dispatch_bridge_rejects_mismatched_inspection(self):
        candidate = resolved_candidate()
        info = parse_manifest(b'PNG bytes', INDEX, 'video/mp4')
        snapshot = MediaSnapshot(candidate.request_context, b'PNG bytes', 'video/mp4', info)
        with self.assertRaises(MediaCheckError) as caught:
            MediaBridge(MediaInspection(snapshot, expected_media_type='HLS')).start()
        self.assertEqual(caught.exception.code, 'MEDIA_PROTOCOL_MISMATCH')

    def test_embedded_true_drm_is_still_blocked_before_network(self):
        for keyformat in ('com.apple.streamingkeydelivery', 'com.widevine', 'com.microsoft.playready'):
            text = PLAYLIST.replace('METHOD=AES-128', f'METHOD=SAMPLE-AES,KEYFORMAT="{keyformat}"')
            with self.subTest(keyformat=keyformat), patch('core.media_guard.ContextHttpClient.open') as request:
                with self.assertRaises(ProtectedMediaError):
                    check_media(RequestContext(INDEX), manifest_text=text, expected_media_type='HLS')
                request.assert_not_called()

    def test_manifest_text_is_not_in_repr_or_diagnostic(self):
        candidate = resolved_candidate()
        self.assertNotIn(PLAYLIST, repr(candidate))
        self.assertNotIn(PLAYLIST, json.dumps(candidate.safe_diagnostic()))
        self.assertNotIn('manifest_text', candidate.safe_diagnostic())
        request = DownloadRequest('yt-dlp.exe', INDEX, '.', **candidate.download_payload())
        self.assertNotIn(PLAYLIST, repr(request))

    def test_batch_request_retains_hls_payload(self):
        candidate = resolved_candidate()
        template = DownloadRequest('yt-dlp.exe', 'https://unused.example', '.', ffmpeg_path='ffmpeg.exe')
        request = DownloadManager._request(candidate, group_media([candidate])[0], template)
        self.assertEqual(request.download_protocol, 'HLS')
        self.assertEqual(request.manifest_text, PLAYLIST)
        self.assertEqual(request.manifest_origin, 'embedded:png/roUd')
        self.assertEqual(request.manifest_url, INDEX)

    def test_empty_hls_payload_fetches_and_parses_normal_manifest(self):
        candidate = resolved_candidate()
        snapshot = self.inspect(candidate).root
        with patch('core.media_guard.read_media', return_value=snapshot) as read:
            inspection = check_media(candidate.request_context, expected_media_type='HLS')
        self.assertEqual(inspection.root.info.media_type, 'HLS')
        read.assert_called_once()

    def test_master_embedded_text_fetches_children_only(self):
        candidate = resolved_candidate()
        child_snapshot = self.inspect(candidate).root
        candidate.manifest_text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nchild.m3u8\n'
        with patch('core.media_guard.read_media', return_value=child_snapshot) as read:
            self.inspect(candidate)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[0].url, 'https://cdn.example/randomDir/child.m3u8')

    def test_4_worker_exit_zero_with_png_output_is_failed(self):
        from tests.test_output_validator import png_image
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'image.mp4'
            output.write_bytes(png_image(320, 192))
            candidate = resolved_candidate()
            request = DownloadRequest('yt-dlp.exe', candidate.url, folder, filename='image',
                                      **candidate.download_payload())
            process = Mock()
            process.stdout = io.StringIO(FILE_MARKER + json.dumps(str(output)) + '\n')
            process.wait.return_value = process.poll.return_value = 0
            bridge = Mock()
            bridge.url, bridge.base = 'http://127.0.0.1:12345/token/index.png.m3u8', 'http://127.0.0.1:12345/token/'
            bridge.last_error = None
            bridge.resource_domains = {'cdn.example'}
            bridge.start.return_value = bridge
            worker = DownloadWorker(request)
            result = []
            worker.completed.connect(result.append)
            real_popen = subprocess.Popen
            def spawn(args, **kwargs):
                return process if args[0] == 'yt-dlp.exe' else real_popen(args, **kwargs)
            with patch('core.downloader.MediaBridge', return_value=bridge), \
                 patch('core.downloader.resolve_required_tools'), \
                 patch('core.downloader.subprocess.Popen', side_effect=spawn), \
                 patch('core.downloader.ProcessTree'):
                # 输出校验 subprocess 不属于 yt-dlp mock，使用真实 ffprobe。
                worker.run()
            self.assertFalse(result[0]['success'], result)
            self.assertEqual(result[0]['error_code'], 'OUTPUT_NOT_VIDEO', result)
            self.assertFalse(result[0]['files'])
            self.assertTrue(output.is_file())


class RouPipelineGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.main_window import MainWindow
        from utils.config import ConfigStore
        self.folder = tempfile.TemporaryDirectory()
        with patch('ui.main_window.detect_yt_dlp', return_value=ToolInfo(error='unit test')), \
             patch('ui.main_window.detect_ffmpeg', return_value=ToolInfo(error='unit test')):
            self.window = MainWindow(ConfigStore(Path(self.folder.name) / 'config.json'))
            end = time.monotonic() + 8
            while self.window.detector and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.01)
        self.window.yt_info = ToolInfo('yt-dlp.exe', 'test')
        self.window.ff_info = ToolInfo('ffmpeg.exe', 'test')
        self.window.output_directory.setText(self.folder.name)

    def tearDown(self):
        self.window.queue.stop_all()
        self.window.close()
        from PySide6.QtCore import QCoreApplication, QEvent
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
                self.window.library_service.is_closed and self.window.batch_controller.is_closed):
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.002)
        self.assertTrue(self.window.library_service.is_closed)
        self.assertTrue(self.window.batch_controller.is_closed)
        self.folder.cleanup()

    def test_single_import_collect_and_queue_keep_decoded_manifest(self):
        candidate = resolved_candidate()
        self.window.import_discovered_media(candidate)
        request = self.window._collect_request()
        self.assertEqual(request.manifest_text, PLAYLIST)
        self.assertEqual(request.download_protocol, 'HLS')
        job = self.window.add_current_to_queue()
        self.assertEqual(job.request.manifest_text, PLAYLIST)
        self.assertEqual(job.request.context_snapshot.playlist_decoder, 'rou')

    def test_url_edit_clears_stale_decoded_manifest(self):
        self.window.import_discovered_media(resolved_candidate())
        self.window.url.setText('https://other.example/video.mp4')
        request = self.window._collect_request()
        self.assertEqual(request.manifest_text, '')
        self.assertEqual(request.download_protocol, '')

    def test_resolver_refresh_replaces_decoded_manifest(self):
        candidate = resolved_candidate()
        self.window.import_discovered_media(candidate)
        job = self.window.add_current_to_queue()
        job.state = 'failed'
        refreshed = deepcopy(candidate)
        refreshed.request_context = replace(candidate.request_context, url=INDEX.replace('A%2FB%2BC', 'new'),
                                              final_url=INDEX.replace('A%2FB%2BC', 'new'))
        refreshed.transport_url = refreshed.manifest_url = refreshed.url
        refreshed.manifest_text = PLAYLIST.replace('abc000.png', 'fresh000.png')
        self.window.queue.update_context(job.id, refreshed.request_context, refreshed.metadata, refreshed.download_payload())
        self.assertEqual(job.request.manifest_text, refreshed.manifest_text)
        self.assertEqual(job.request.manifest_url, refreshed.url)
        self.assertEqual(job.request.download_protocol, 'HLS')

    def test_raw_new_context_cannot_keep_old_decoded_playlist(self):
        self.window.import_discovered_media(resolved_candidate())
        job = self.window.add_current_to_queue()
        job.state = 'failed'
        refreshed = replace(job.request.context_snapshot, url='https://new.example/index.png',
                            final_url='https://new.example/index.png')
        self.window.queue.update_context(job.id, refreshed, job.request.media_metadata)
        self.assertEqual(job.request.manifest_text, '')
        self.assertEqual(job.request.download_protocol, 'HLS')


if __name__ == '__main__':
    unittest.main()
