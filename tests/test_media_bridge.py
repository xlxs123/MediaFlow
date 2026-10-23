"""真实 loopback HTTP：Manifest 语义与按资源决定的请求头，不运行外部工具。"""
from pathlib import Path
import re
import tempfile
import unittest

from core.http_client import ContextHttpClient
from core.media_bridge import MediaBridge
from core.media_guard import check_media
from core.request_context import RequestContext
from tests.hls_fixture import HlsFixture, DISGUISED_HEADERS, FIXTURE_BROWSER_METADATA


class SemanticBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name) / 'disguised'
        folder.mkdir()
        (folder / 'source.m3u8').write_text(
            '#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",\nIV=0x00000000000000000000000000000000\n'
            '#EXTINF:2,\nabc000.png\n#EXT-X-ENDLIST\n', encoding='utf-8')
        self.key = b'0123456789abcdef'
        self.segment = b'opaque-encrypted-segment' * 20
        (folder / 'enc.key').write_bytes(self.key)
        (folder / 'abc000.png').write_bytes(self.segment)
        self.fixture = HlsFixture(self.temp.name).start()
        self.bridge = None

    def tearDown(self):
        if self.bridge:
            self.bridge.close()
        self.fixture.close()
        self.temp.cleanup()

    def prepare(self, compatible=False):
        route = '/disguised-compat/index' if compatible else '/disguised/index'
        root = RequestContext(self.fixture.base + route, headers=DISGUISED_HEADERS,
                              inherit_browser_metadata=compatible)
        inspection = check_media(root)
        self.assertIs(inspection.root.info.drm, False)
        self.bridge = MediaBridge(inspection).start()
        with ContextHttpClient().open(RequestContext(self.bridge.url)) as response:
            data = response.read().decode()
        key = re.search(r'URI="([^"]+)"', data).group(1)
        segment = next(line for line in data.splitlines() if line and not line.startswith('#'))
        return key, segment

    def test_png_semantics_override_response_mime_and_range_is_resource_specific(self):
        key, segment = self.prepare()
        with ContextHttpClient().open(RequestContext(key, headers={'Range': 'bytes=0-3'})) as response:
            self.assertEqual(response.read(), self.key)
        with ContextHttpClient().open(RequestContext(segment, headers={'Range': 'bytes=8-23'})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.segment[8:24])
            self.assertNotEqual(response.content_type, 'image/png')
        hits = self.fixture.requests
        manifest = next(r for r in hits if r['path'].endswith('/index'))
        key_hit = next(r for r in hits if r['path'].endswith('enc.key'))
        segment_hit = next(r for r in hits if r['path'].endswith('abc000.png'))
        self.assertEqual(manifest['range'], 'bytes=0-')
        self.assertIsNone(key_hit['range'])
        self.assertEqual(segment_hit['range'], 'bytes=8-23')
        self.assertFalse(key_hit['browser_metadata_present'])
        self.assertFalse(segment_hit['browser_metadata_present'])
        self.assertTrue(all(r['headers_match'] for r in hits))

    def test_browser_metadata_compatibility_is_opt_in_for_key_and_segment(self):
        key, segment = self.prepare(compatible=True)
        for url in (key, segment):
            with ContextHttpClient().open(RequestContext(url)) as response:
                self.assertEqual(response.status, 200)
        hits = self.fixture.requests
        self.assertTrue(all(r['browser_metadata_match'] for r in hits))
        self.assertTrue(all(r['range'] is None for r in hits if not r['path'].endswith('/index')))

    def test_curl_retains_raw_range_and_browser_metadata_in_both_shells(self):
        from core.curl_parser import parse_curl
        for mode in ('cmd', 'bash'):
            parsed = parse_curl(self.fixture.curl(mode, '/disguised/index', DISGUISED_HEADERS))
            self.assertEqual(parsed.headers['range'], 'bytes=0-')
            for name, value in FIXTURE_BROWSER_METADATA.items():
                self.assertEqual(parsed.headers[name], value)

    def test_byte_range_tag_is_preserved_for_downloader_and_does_not_apply_to_key(self):
        source = Path(self.temp.name) / 'disguised' / 'source.m3u8'
        source.write_text(source.read_text(encoding='utf-8').replace('#EXTINF:2,', '#EXTINF:2,\n#EXT-X-BYTERANGE:16@8'), encoding='utf-8')
        key, segment = self.prepare()
        with ContextHttpClient().open(RequestContext(self.bridge.url)) as response:
            self.assertIn(b'#EXT-X-BYTERANGE:16@8', response.read())
        with ContextHttpClient().open(RequestContext(key)) as response:
            self.assertEqual(response.read(), self.key)
        with ContextHttpClient().open(RequestContext(segment, headers={'Range': 'bytes=8-23'})) as response:
            self.assertEqual(response.read(), self.segment[8:24])
        self.assertIsNone(next(r for r in self.fixture.requests if r['path'].endswith('enc.key'))['range'])


if __name__ == '__main__':
    unittest.main()
