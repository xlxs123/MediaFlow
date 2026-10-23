import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.config import ConfigStore, DEFAULTS
from utils.branding import APP_VERSION


class BrandMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict('os.environ', {'APPDATA': self.temp.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def write(self, name, values):
        path = self.root / name / 'config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        return path

    def test_legacy_is_discoverable_but_never_silently_imported(self):
        from core.data_migration import MigrationEngine
        old = self.write('流媒体下载器', {**DEFAULTS, 'Cookie': 'must-not-migrate'})
        original = old.read_bytes()
        store = ConfigStore()
        self.assertEqual(store.path, self.root / 'MediaFlow' / APP_VERSION / 'config.json')
        self.assertEqual(store.load(), DEFAULTS)
        self.assertFalse(store.path.exists())
        self.assertIsNone(store.migrated_from)
        self.assertTrue(MigrationEngine(store.path.parent).discover())
        self.assertEqual(old.read_bytes(), original)

    def test_new_config_takes_precedence_without_overwriting_either_file(self):
        old = self.write('流媒体下载器', {'output_directory': 'D:\\old'})
        new = self.write('MediaFlow/' + APP_VERSION, {'output_directory': 'D:\\new'})
        before = old.read_bytes(), new.read_bytes()
        store = ConfigStore()
        self.assertEqual(store.load()['output_directory'], 'D:\\new')
        self.assertIsNone(store.migrated_from)
        self.assertEqual((old.read_bytes(), new.read_bytes()), before)

    def test_multiple_legacy_sources_are_separate_choices(self):
        from core.data_migration import MigrationEngine
        self.write('M3U8 Downloader', {'output_directory': 'D:\\first'})
        self.write('流媒体下载器', {'output_directory': 'D:\\recent'})
        store = ConfigStore()
        self.assertEqual(store.load(), DEFAULTS)
        self.assertEqual(len(MigrationEngine(store.path.parent).discover()), 2)

    def test_no_target_write_when_only_legacy_is_present(self):
        self.write('M3U8 Downloader', {'output_directory': 'D:\\first'})
        store = ConfigStore()
        self.assertEqual(store.load(), DEFAULTS)
        self.assertFalse(store.path.is_file())

    def test_unavailable_target_does_not_read_old_preferences(self):
        old = self.write('流媒体下载器', {'output_directory': 'D:\\saved'})
        (self.root / 'MediaFlow').write_bytes(b'not a directory')
        self.assertEqual(ConfigStore().load(), DEFAULTS)
        self.assertTrue(old.is_file())

    def test_explicit_test_path_never_reads_global_legacy_configuration(self):
        self.write('流媒体下载器', {'output_directory': 'D:\\global'})
        store = ConfigStore(self.root / 'explicit' / 'config.json')
        self.assertEqual(store.load(), DEFAULTS)
        self.assertFalse((self.root / 'MediaFlow').exists())

    def test_corrupt_modern_config_preserved_without_importing_stale_legacy(self):
        self.write('流媒体下载器', {'output_directory': 'D:\\stale'})
        path = self.write('MediaFlow/' + APP_VERSION, {})
        path.write_bytes(b'{broken')
        store = ConfigStore()
        self.assertEqual(store.load(), DEFAULTS)
        self.assertTrue(store.error)
        self.assertEqual(path.read_bytes(), b'{broken')
