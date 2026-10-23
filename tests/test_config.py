import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.config import ConfigStore, DEFAULTS


class BrowserCaptureConfigTests(unittest.TestCase):
    def test_old_settings_keep_preferences_and_gain_default_capture_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps({'output_directory': 'D:\\videos', 'quality_preference': 'balanced'}), encoding='utf-8')
            values = ConfigStore(path).load()
            self.assertEqual(values['browser_body_limit_mib'], 16)
            self.assertEqual(values['output_directory'], 'D:\\videos')
            self.assertEqual(values['quality_preference'], 'balanced')

    def test_configured_capture_limit_is_saved_and_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'settings.json')
            for limit in (1, 16, 48, 128):
                with self.subTest(limit=limit):
                    self.assertTrue(store.save({'browser_body_limit_mib': limit, 'Cookie': 'do-not-save'}))
                    self.assertEqual(store.load()['browser_body_limit_mib'], limit)
                    self.assertNotIn('Cookie', json.loads(store.path.read_text(encoding='utf-8')))

    def test_invalid_limits_use_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'settings.json')
            for value in (0, -1, 129, True, '32', 16.5, None):
                with self.subTest(value=value):
                    self.assertEqual(store._safe({'browser_body_limit_mib': value})['browser_body_limit_mib'],
                                     DEFAULTS['browser_body_limit_mib'])

    def test_confirmed_legacy_migration_keeps_limit_without_mutating_legacy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'APPDATA': directory}):
            legacy = Path(directory) / '流媒体下载器' / 'config.json'
            legacy.parent.mkdir()
            legacy.write_text(json.dumps({'browser_body_limit_mib': 32, 'concurrent_fragments': 8}), encoding='utf-8')
            before = legacy.read_bytes()
            store = ConfigStore()
            self.assertEqual(store.load()['browser_body_limit_mib'], DEFAULTS['browser_body_limit_mib'])
            self.assertFalse(store.path.exists())
            from core.data_migration import MigrationEngine
            engine = MigrationEngine(store.path.parent)
            preview = engine.preview(legacy.parent)
            self.assertEqual(engine.apply(preview, confirmed=True)['status'], 'completed')
            self.assertEqual(store.load()['browser_body_limit_mib'], 32)
            self.assertEqual(store.load()['concurrent_fragments'], 8)
            self.assertEqual(legacy.read_bytes(), before)
            self.assertTrue(store.path.is_file())
