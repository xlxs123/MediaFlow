import json
import unittest
from core.config_media import config_media_urls


class ConfigMediaTests(unittest.TestCase):
    def scan(self, value):
        return config_media_urls(json.dumps(value).encode(), 'https://page.test/api/config', 'application/json')

    def test_nested_urls_are_only_plans_with_signatures_intact(self):
        self.assertEqual(self.scan({'data': {'source': [{'url': 'https://cdn.test/no-extension?secure=a%2Fb'},
                                                   {'hls': '../playlist?token=x'}]}}),
                         ['https://cdn.test/no-extension?secure=a%2Fb', 'https://page.test/playlist?token=x'])

    def test_non_url_and_license_thumbnail_not_probed(self):
        self.assertEqual(self.scan({'url': 'javascript:alert(1)', 'hls': 'blob:https://page.test/a',
                                    'source': ['https://cdn.test/license', 'https://cdn.test/thumbs']}), [])

    def test_size_depth_and_url_limits(self):
        self.assertEqual(config_media_urls(b'x' * (2 * 1024 * 1024 + 1), 'https://page.test'), [])
        nested = {'url': 'https://cdn.test/index'}
        for _ in range(14):
            nested = {'child': nested}
        self.assertEqual(self.scan(nested), [])
        self.assertEqual(len(self.scan({'sources': [f'https://cdn.test/{i}' for i in range(100)]})), 16)

    def test_manifest_text_not_configuration(self):
        self.assertEqual(config_media_urls(b'#EXTM3U\n#EXTINF:1,\nhttps://cdn.test/segment', 'https://page.test', 'text/plain'), [])

    def test_dash_container_does_not_turn_codec_mime_and_range_into_urls(self):
        self.assertEqual(self.scan({'data': {'dash': {'video': [{'baseUrl': 'https://cdn.test/video',
            'codecs': 'avc1.640028', 'mimeType': 'video/mp4',
            'SegmentBase': {'Initialization': '0-999', 'indexRange': '1000-1200'}}]}}}),
            ['https://cdn.test/video'])

    def test_configured_large_json_media_description(self):
        data = json.dumps({'padding': 'x' * (3 * 1024 * 1024),
                           'source': {'url': 'https://cdn.test/media'}}).encode()
        self.assertEqual(config_media_urls(data, 'https://page.test', 'application/json'),
                         ['https://cdn.test/media'])
        self.assertEqual(config_media_urls(data, 'https://page.test', 'application/json',
                                           max_bytes=2 * 1024 * 1024), [])
