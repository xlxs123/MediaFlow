"""Real Ed25519 public roots, explicit approval and persisted-settings boundaries."""
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.update_channel import (key_fingerprint, load_channel, merge_keys,
                                 read_channel, validate_channel)
from core.update_manager import UpdateManager, UpdateError
from core.update_manifest import UpdateValidationError
from scripts.configure_update_channel import configure
from utils.config import ConfigStore


def public_key():
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {'algorithm': 'ed25519', 'public_key': base64.b64encode(public).decode('ascii')}


def pending():
    return {'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
            'source_url': '', 'trusted_keys': {}, 'key_state': 'pending'}


class UpdateChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-channel-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = public_key()
        self.channel = dict(pending(), source_url='https://updates.example.invalid/release.json',
                            trusted_keys={'test-only': self.key}, key_state='initialized')
        self.public_file = self.root / 'public.json'
        self.public_file.write_text(json.dumps({'trusted_keys': {'test-only': self.key}}), 'utf-8')
        self.output = self.root / 'channel.json'

    def manager(self):
        release = self.root / 'release'
        release.mkdir(exist_ok=True)
        return UpdateManager(self.root / 'updates', release,
            {'app_version': '2.0.1', 'build_id': 'channel-test'}, self.root / 'data')

    def test_pending_channel_does_not_claim_latest_or_public_success(self):
        self.assertEqual(validate_channel(pending()), pending())
        with patch('core.update_manager.load_channel', return_value=pending()):
            self.assertEqual(self.manager().check_updates()['status'], 'not_configured')

    def test_invalid_pending_channel_cannot_sneak_in_a_key_or_source(self):
        for field, value in [('source_url', self.channel['source_url']), ('trusted_keys', {'x': self.key})]:
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_channel(dict(pending(), **{field: value}))

    def test_initialized_requires_both_source_and_ed25519_key(self):
        for change in ({'source_url': ''}, {'trusted_keys': {}}, {'source_url': 'http://localhost/release.json'}):
            with self.subTest(change=change), self.assertRaises(UpdateValidationError):
                validate_channel(dict(self.channel, **change))

    def test_private_or_unknown_fields_rejected(self):
        for field in ('private_key', 'Cookie', 'command'):
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_channel(dict(self.channel, **{field: 'must-not-be-imported'}))

    def test_duplicate_json_rejected(self):
        self.output.write_text('{"schema_version":1,"schema_version":1}', 'utf-8')
        with self.assertRaises(UpdateValidationError):
            read_channel(self.output)

    def test_fingerprint_is_sha256_of_raw_public_bytes(self):
        expected = hashlib.sha256(base64.b64decode(self.key['public_key'])).hexdigest()
        self.assertEqual(key_fingerprint(self.key), expected)

    def test_configure_requires_independently_confirmed_fingerprint(self):
        with self.assertRaisesRegex(UpdateValidationError, '指纹'):
            configure(self.channel['source_url'], self.public_file, 'test-only', '0'*64, self.output)
        self.assertFalse(self.output.exists())

    def test_configure_then_reload_binds_exact_public_root(self):
        result = configure(self.channel['source_url'], self.public_file, 'test-only',
                           key_fingerprint(self.key), self.output)
        self.assertEqual(read_channel(self.output), self.channel)
        self.assertTrue(result['requires_rebuild'])
        self.assertFalse(result['online_verified'])
        self.assertNotIn(b'PRIVATE KEY', self.output.read_bytes())

    def test_unapproved_second_root_is_not_silently_imported(self):
        self.public_file.write_text(json.dumps({'trusted_keys': {'test-only': self.key, 'other': public_key()}}), 'utf-8')
        with self.assertRaisesRegex(UpdateValidationError, '一个'):
            configure(self.channel['source_url'], self.public_file, 'test-only',
                      key_fingerprint(self.key), self.output)
        self.assertFalse(self.output.exists())

    def test_rotation_retains_previously_approved_roots_only_when_requested(self):
        self.output.write_text(json.dumps(self.channel), 'utf-8')
        new_key = public_key()
        self.public_file.write_text(json.dumps({'trusted_keys': {'next': new_key}}), 'utf-8')
        configure(self.channel['source_url'], self.public_file, 'next',
                  key_fingerprint(new_key), self.output, retain_current_keys=True)
        self.assertEqual(set(read_channel(self.output)['trusted_keys']), {'test-only', 'next'})

    def test_rotation_cannot_reassign_existing_key_id(self):
        self.output.write_text(json.dumps(self.channel), 'utf-8')
        replacement = public_key()
        self.public_file.write_text(json.dumps({'trusted_keys': {'test-only': replacement}}), 'utf-8')
        before = self.output.read_bytes()
        with self.assertRaisesRegex(UpdateValidationError, '新的 key_id'):
            configure(self.channel['source_url'], self.public_file, 'test-only',
                      key_fingerprint(replacement), self.output, retain_current_keys=True)
        self.assertEqual(self.output.read_bytes(), before)

    def test_saved_settings_cannot_replace_fixed_public_root(self):
        manager = self.manager()
        with patch('core.update_manager.load_channel', return_value=self.channel):
            manager.save_settings({'trusted_keys': {}})
            before = (manager.state_dir / 'settings.json').read_bytes()
            with self.assertRaisesRegex(UpdateError, '冲突'):
                manager.save_settings({'trusted_keys': {'test-only': public_key()}})
            self.assertEqual((manager.state_dir / 'settings.json').read_bytes(), before)
            self.assertEqual(manager.load_settings()['trusted_keys'], self.channel['trusted_keys'])

    def test_reassigning_key_id_without_retention_is_also_rejected(self):
        self.output.write_text(json.dumps(self.channel), 'utf-8')
        replacement = public_key()
        self.public_file.write_text(json.dumps({'trusted_keys': {'test-only': replacement}}), 'utf-8')
        with self.assertRaisesRegex(UpdateValidationError, '新的 key_id'):
            configure(self.channel['source_url'], self.public_file, 'test-only',
                      key_fingerprint(replacement), self.output)

    def test_combined_root_limit_checked_before_persisting(self):
        saved = {'user-' + str(index): self.key for index in range(32)}
        with self.assertRaisesRegex(UpdateValidationError, '32'):
            merge_keys(self.channel, saved)

    def test_saved_tampered_key_stops_update_without_modifying_file(self):
        manager = self.manager()
        manager.state_dir.mkdir()
        file = manager.state_dir / 'settings.json'
        file.write_text(json.dumps({'trusted_keys': {'test-only': public_key()}}), 'utf-8')
        before = file.read_bytes()
        with patch('core.update_manager.load_channel', return_value=self.channel), \
                self.assertRaisesRegex(UpdateValidationError, '冲突'):
            manager.load_settings()
        self.assertEqual(file.read_bytes(), before)

    def test_bundled_defaults_do_not_require_writing_settings(self):
        manager = self.manager()
        with patch('core.update_manager.load_channel', return_value=self.channel):
            loaded = manager.load_settings()
        self.assertEqual(loaded['source_url'], self.channel['source_url'])
        self.assertEqual(loaded['channel_state'], 'initialized')
        self.assertFalse(manager.state_dir.exists())

    def test_explicit_source_disable_persists_without_removing_root(self):
        manager = self.manager()
        with patch('core.update_manager.load_channel', return_value=self.channel):
            manager.save_settings({'source_url': '', 'trusted_keys': {}})
            loaded = manager.load_settings()
        self.assertEqual(loaded['source_url'], '')
        self.assertEqual(loaded['trusted_keys'], self.channel['trusted_keys'])

    def test_unrelated_user_approved_roots_preserved(self):
        other = public_key()
        self.assertEqual(merge_keys(self.channel, {'user-choice': other}),
                         {'test-only': self.key, 'user-choice': other})

    def test_frozen_load_uses_embedded_resource(self):
        (self.root / 'assets').mkdir()
        (self.root / 'assets/update-channel.json').write_text(json.dumps(self.channel), 'utf-8')
        with patch.object(sys, '_MEIPASS', str(self.root), create=True), \
                patch('os.getcwd', return_value='Z:/unrelated/startup/directory'):
            self.assertEqual(load_channel(), self.channel)

    def test_default_user_data_is_isolated_and_old_source_discoverable(self):
        from core.data_migration import known_sources
        with patch.dict(os.environ, {'APPDATA': str(self.root)}):
            from utils.branding import APP_VERSION
            self.assertEqual(ConfigStore().path.parent, self.root / 'MediaFlow' / APP_VERSION)
            self.assertIn(self.root / 'MediaFlow/2.0.0', known_sources())
            self.assertIn(self.root / 'MediaFlow/2.0.1', known_sources())

    def test_temporary_name_collision_does_not_delete_other_file(self):
        collision = self.output.with_name('.channel.json.' + 'a'*32 + '.tmp')
        collision.write_bytes(b'must preserve')
        with patch('scripts.configure_update_channel.uuid.uuid4', return_value=type('Id', (), {'hex': 'a'*32})()):
            with self.assertRaises(FileExistsError):
                configure(self.channel['source_url'], self.public_file, 'test-only', key_fingerprint(self.key), self.output)
        self.assertEqual(collision.read_bytes(), b'must preserve')


if __name__ == '__main__':
    unittest.main()
