import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.config import ConfigStore, DEFAULTS
from utils.security import Redactor, is_sensitive_header, normalize_headers


class SecurityTests(unittest.TestCase):
    def test_sensitive_names(self):
        for name in ('Cookie', 'Authorization', 'Proxy-Authorization', 'X-Api-Key', 'X-Auth-Token', 'X-Session-Secret'):
            self.assertTrue(is_sensitive_header(name))
        self.assertFalse(is_sensitive_header('Sec-CH-UA'))

    def test_redacts_values_even_without_header_label(self):
        redactor = Redactor({'Cookie': 'session=sensitive-cookie; uid=private-user', 'Authorization': 'Bearer private-token'})
        text = redactor.redact('sent sensitive-cookie private-user private-token')
        self.assertNotIn('private', text)
        self.assertNotIn('sensitive-cookie', text)

    def test_url_signatures_redacted(self):
        text = Redactor().redact('GET https://example.com/index.m3u8?sign=private&t=123&x=secret')
        self.assertNotIn('private', text)
        self.assertNotIn('123', text)
        self.assertNotIn('secret', text)

    def test_header_validation(self):
        self.assertEqual(normalize_headers({'Origin': '', 'ACCEPT': '*/*'}), {'accept': '*/*'})
        with self.assertRaises(ValueError):
            normalize_headers({'X-Test': 'ok\r\nCookie: bad'})
        with self.assertRaises(ValueError):
            normalize_headers({'Invalid Header': 'ok'})

    def test_configuration_whitelist(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / 'config.json')
            self.assertTrue(store.save({'Cookie': 'SECRET', 'Authorization': 'SECRET', 'url': 'SECRET',
                                        'headers': {'token': 'SECRET'}, 'output_directory': temp, 'user_agent': 'Test UA'}))
            content = store.path.read_text(encoding='utf-8')
            self.assertNotIn('SECRET', content)
            self.assertEqual(json.loads(content)['output_directory'], temp)

    def test_corrupt_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / 'config.json')
            store.path.write_text('{bad', encoding='utf-8')
            self.assertEqual(store.load()['output_directory'], 'E:\\download')
            self.assertTrue(store.error)

    def test_concurrency_configuration_bounds_and_no_payload_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / 'config.json')
            for value in (0, -1, 17, True, '4', 2.5):
                self.assertTrue(store.save({'concurrent_fragments': value, 'max_concurrent_videos': value,
                                            'queue': [{'url': 'private-url', 'body': 'private-body'}]}))
                self.assertEqual(store.load()['concurrent_fragments'], 4)
                self.assertEqual(store.load()['max_concurrent_videos'], 3)
                self.assertNotIn('private-', store.path.read_text(encoding='utf-8'))
            store.save({'concurrent_fragments': 16, 'max_concurrent_videos': 4})
            self.assertEqual(store.load()['concurrent_fragments'], 16)
            self.assertEqual(store.load()['max_concurrent_videos'], 4)

    def test_new_name_loads_legacy_preferences_without_modifying_legacy_file(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {'APPDATA': temp}):
            legacy = Path(temp) / 'M3U8 Downloader' / 'config.json'
            legacy.parent.mkdir()
            content = '{"output_directory":"E:\\\\saved","Cookie":"private-cookie"}'
            legacy.write_text(content, encoding='utf-8')
            store = ConfigStore()
            settings = store.load()
            self.assertEqual(settings['output_directory'], DEFAULTS['output_directory'])
            from utils.branding import APP_VERSION
            self.assertEqual(store.path.parent.name, APP_VERSION)
            self.assertEqual(store.path.parent.parent.name, 'MediaFlow')
            store.save(settings)
            self.assertEqual(legacy.read_text(encoding='utf-8'), content)
            self.assertNotIn('private-cookie', store.path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
