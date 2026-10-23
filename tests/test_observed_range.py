"""第三站精确请求组合：实际 HTTP206，只有 Referer/UA/Range，没有 Origin 或凭据。"""
from dataclasses import replace
from pathlib import Path
import re
import tempfile
import unittest

from core.curl_parser import parse_curl
from core.errors import MediaCheckError
from core.http_client import ContextHttpClient
from core.media_bridge import MediaBridge
from core.media_guard import check_media
from core.request_context import RequestContext
from tests.hls_fixture import HlsFixture, OBSERVED_HEADERS


class ObservedRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name) / 'disguised'
        folder.mkdir()
        (folder / 'source.m3u8').write_text(
            '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",\nIV=0x00000000000000000000000000000000\n'
            '#EXTINF:2.000000,\nabc000.png\n#EXTINF:2.000000,\nabc001.png\n#EXT-X-ENDLIST\n', encoding='utf-8')
        self.key = b'0123456789abcdef'
        self.segment = b'opaque-encrypted-segment' * 20
        (folder / 'enc.key').write_bytes(self.key)
        for name in ('abc000.png', 'abc001.png'):
            (folder / name).write_bytes(self.segment)
        self.fixture = HlsFixture(self.temp.name).start()
        self.bridge = None
        self.errors = []

    def tearDown(self):
        if self.bridge:
            self.bridge.close()
        self.fixture.close()
        self.temp.cleanup()

    def prepare(self, route='/observed/index.m3u8', inherit=True, context=None):
        root = context or RequestContext(self.fixture.base + route, headers=OBSERVED_HEADERS, inherit_range=inherit)
        inspection = check_media(root)
        self.assertEqual(inspection.root.http_status, 206)
        self.assertEqual(inspection.root.info.classification, 'HLS_AES128')
        self.assertIs(inspection.root.info.drm, False)
        self.assertTrue(all(resource.role == 'Media Segment' for resource in inspection.root.info.resources))
        self.bridge = MediaBridge(inspection, on_error=self.errors.append).start()
        with ContextHttpClient().open(RequestContext(self.bridge.url)) as response:
            data = response.read().decode()
        return re.search(r'URI="([^"]+)"', data)[1], [line for line in data.splitlines() if line and not line.startswith('#')]

    def assert_context(self, full_range=True):
        self.assertTrue(self.fixture.requests)
        for request in self.fixture.requests:
            self.assertFalse(request['has_origin'])
            self.assertFalse(request['has_cookie'])
            self.assertFalse(request['has_authorization'])
            self.assertFalse(request['browser_metadata_present'])
            if full_range:
                self.assertTrue(request['observed_headers_match'], request)

    def test_full_open_range_reaches_key_and_both_png_segments(self):
        key, segments = self.prepare()
        for url, expected in [(key, self.key)] + [(url, self.segment) for url in segments]:
            with ContextHttpClient().open(RequestContext(url)) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
                self.assertEqual(response.stream.headers['Content-Range'], f'bytes 0-{len(expected)-1}/{len(expected)}')
                self.assertNotEqual(response.content_type, 'image/png')
        self.assert_context()
        self.assertEqual({Path(hit['path']).name for hit in self.fixture.requests},
                         {'index.m3u8', 'enc.key', 'abc000.png', 'abc001.png'})

    def test_cross_host_segments_and_key_do_not_require_origin_or_credentials(self):
        key, segments = self.prepare('/observed-cross/index.m3u8')
        for url in [key, *segments]:
            with ContextHttpClient().open(RequestContext(url)) as response:
                self.assertEqual(response.status, 206)
                response.read()
        self.assert_context()
        self.assertEqual({hit['host'].split(':')[0] for hit in self.fixture.requests}, {'127.0.0.1', 'localhost'})

    def test_segment_byte_range_overrides_original_but_key_uses_original(self):
        source = Path(self.temp.name) / 'disguised' / 'source.m3u8'
        source.write_text(source.read_text(encoding='utf-8').replace('#EXTINF:2.000000,',
                          '#EXTINF:2.000000,\n#EXT-X-BYTERANGE:16@8'), encoding='utf-8')
        key, segments = self.prepare()
        with ContextHttpClient().open(RequestContext(key, headers={'Range': 'bytes=0-3'})) as response:
            self.assertEqual(response.read(), self.key)
        with ContextHttpClient().open(RequestContext(segments[0], headers={'Range': 'bytes=8-23'})) as response:
            self.assertEqual(response.read(), self.segment[8:24])
            self.assertEqual(response.stream.headers['Content-Range'], f'bytes 8-23/{len(self.segment)}')
        with ContextHttpClient().open(RequestContext(self.bridge.url)) as response:
            self.assertIn(b'#EXT-X-BYTERANGE:16@8', response.read())
        self.assert_context(full_range=False)
        self.assertEqual(next(hit for hit in self.fixture.requests if hit['path'].endswith('enc.key'))['range'], 'bytes=0-')
        self.assertEqual(next(hit for hit in self.fixture.requests if hit['path'].endswith('abc000.png'))['range'], 'bytes=8-23')

    def test_isolation_mode_key_403_is_not_drm(self):
        key, _ = self.prepare(inherit=False)
        with self.assertRaises(MediaCheckError) as caught:
            with ContextHttpClient().open(RequestContext(key)) as response:
                response.read()
        self.assertEqual(caught.exception.code, 'HTTP_403')
        self.assertEqual(self.errors[0].code, 'HTTP_403')
        self.assertIsNot(self.errors[0].diagnostic.get('drm'), True)
        self.assertIsNone(next(hit for hit in self.fixture.requests if hit['path'].endswith('enc.key'))['range'])

    def test_redirect_preserves_range_and_resolves_relative_resources(self):
        key, segments = self.prepare('/observed-redirect')
        for url in [key, *segments]:
            with ContextHttpClient().open(RequestContext(url)) as response:
                self.assertEqual(response.status, 206)
                response.read()
        self.assert_context()
        self.assertTrue(all(hit['host'].startswith('localhost:') for hit in self.fixture.requests[1:]))
        self.assertTrue(any(hit['path'] == '/observed/enc.key' for hit in self.fixture.requests))

    def test_cmd_and_bash_preserve_exact_three_headers_for_http_requests(self):
        for mode in ('cmd', 'bash'):
            with self.subTest(mode=mode):
                parsed = parse_curl(self.fixture.curl(mode, '/observed/index.m3u8', OBSERVED_HEADERS))
                self.assertEqual(parsed.headers, OBSERVED_HEADERS)
                key, segments = self.prepare(context=replace(parsed.to_context(), inherit_range=True))
                for url in [key, *segments]:
                    with ContextHttpClient().open(RequestContext(url)) as response:
                        self.assertEqual(response.status, 206)
                        response.read()
                self.assert_context()
                self.bridge.close()
                self.bridge = None

    def test_master_child_manifest_inherits_range_before_download(self):
        context = RequestContext(self.fixture.base + '/observed/master.m3u8', headers=OBSERVED_HEADERS, inherit_range=True)
        inspection = check_media(context)
        self.assertEqual(inspection.root.info.playlist_type, 'Master')
        self.assertTrue(any(snapshot.info.classification == 'HLS_AES128' for snapshot in inspection.manifests.values()))
        self.assertTrue(all(snapshot.http_status == 206 and snapshot.context.headers['range'] == 'bytes=0-'
                            for snapshot in inspection.manifests.values()))
        self.assert_context()


if __name__ == '__main__':
    unittest.main()
