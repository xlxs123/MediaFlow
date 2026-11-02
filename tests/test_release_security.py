"""Byte/phase boundaries; synthetic PE certificates are never trust evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from scripts import release_security as security


def unsigned_pe(plus=True, *, with_archive=False):
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 148, 240 if plus else 224)
    struct.pack_into('<H', data, 152, 0x20b if plus else 0x10b)
    struct.pack_into('<I', data, 152 + (108 if plus else 92), 16)
    if with_archive:
        from tests.pyinstaller_archive_fixture import archive_data
        return archive_data(prefix=bytes(data))
    return bytes(data)


def synthetic_certificate_append(data):
    result = bytearray(data)
    checksum, directory, _, _ = security._pe_signature_layout(data)
    struct.pack_into('<I', result, checksum, 0x1234)
    struct.pack_into('<II', result, directory, len(data), 16)
    result += struct.pack('<IHH', 16, 0x200, 2) + b'UNITDATA'
    return bytes(result)


class ReleaseSecurityTests(unittest.TestCase):
    def test_production_requires_explicit_thumbprint_and_timestamp_without_credentials(self):
        for thumb in ('', 'A' * 39, 'not-a-certificate', '../identity'):
            with self.subTest(thumb=thumb), self.assertRaises(security.ReleaseSecurityError):
                security.production_options(thumb, timestamp_url='https://timestamp.unit.invalid')
        for url in ('', 'http://timestamp.unit.invalid', 'https://user@timestamp.unit.invalid',
                    'https://timestamp.unit.invalid?q=1', 'https://timestamp.unit.invalid/#x'):
            with self.subTest(url=url), self.assertRaises(security.ReleaseSecurityError):
                security.production_options('a' * 40, timestamp_url=url)
        configured = security.production_options('a' * 40, timestamp_url='https://timestamp.unit.invalid')
        self.assertEqual(configured['thumbprint'], 'A' * 40)

    def test_official_key_identity_uses_application_pin_not_caller_claimed_test_mode(self):
        key = {'algorithm': 'ed25519', 'public_key': 'unit-public-verification-material'}
        with patch('core.update_channel.load_channel', return_value={'trusted_keys': {'official': key}}):
            self.assertTrue(security.official_publisher_key({'renamed': key}, 'renamed'))
            self.assertFalse(security.official_publisher_key({'official': {'public_key': 'different'}}, 'official'))
            self.assertFalse(security.official_publisher_key({}, 'official'))

    def test_only_documented_authenticode_fields_and_certificate_append_are_permitted(self):
        for plus in (True, False):
            with self.subTest(plus=plus):
                before = unsigned_pe(plus)
                after = synthetic_certificate_append(before)
                result = security.signature_only_change(before, after)
                self.assertTrue(result['executable_payload_unchanged'])
                self.assertEqual(result['certificate_bytes'], 16)
                self.assertNotIn('cryptographically_verified', result)

    def test_payload_replacement_invalid_certificate_boundaries_and_resigning_are_rejected(self):
        before = unsigned_pe()
        after = synthetic_certificate_append(before)
        bad_payload = bytearray(after); bad_payload[420] = 1
        bad_revision = bytearray(after); struct.pack_into('<H', bad_revision, 516, 0x100)
        bad_pointer = bytearray(after); struct.pack_into('<I', bad_pointer, 296, 511)
        for altered in (bytes(bad_payload), bytes(bad_revision), bytes(bad_pointer), after + b'overlay'):
            with self.subTest(altered=hashlib.sha256(altered).hexdigest()), self.assertRaises(security.ReleaseSecurityError):
                security.signature_only_change(before, altered)
        with self.assertRaises(security.ReleaseSecurityError):
            security.signature_only_change(after, after)

    def test_missing_configuration_blocks_before_zip_read_or_native_command(self):
        with patch.object(security.zipfile, 'ZipFile', side_effect=AssertionError('ZIP must remain unopened')):
            with self.assertRaisesRegex(security.ReleaseSecurityError, 'Authenticode 未配置'):
                security.verify_package(Path('does-not-exist.zip'), {}, Path('.'))

    def test_real_publisher_key_cannot_load_private_key_before_native_verification(self):
        from scripts import publish_release as publisher
        with tempfile.TemporaryDirectory(prefix='MediaFlow-auth-publisher-unit-') as temporary:
            root = Path(temporary); release = root / 'release'; release.mkdir()
            (release / 'MediaFlow.exe').write_bytes(unsigned_pe())
            stamp = {'app_version': '2.1.0', 'build_id': 'synthetic-only', 'source_sha256': 'a' * 64, 'packaged': True}
            (release / 'build-info.json').write_text(json.dumps(stamp), 'utf-8')
            with patch.object(publisher, '_acceptance', return_value=({}, 'b' * 64)), \
                    patch.object(publisher, '_trusted_keys', return_value={'unit-formal': {'algorithm': 'ed25519'}}), \
                    patch.object(security, 'official_publisher_key', return_value=True), \
                    patch.object(publisher, '_private_key', side_effect=AssertionError('No private-key access')):
                with self.assertRaisesRegex(publisher.PublishError, 'Authenticode 未配置'):
                    publisher.publish_release(release, root / 'publication',
                        package_url='https://assets.unit.invalid/release.zip', published_at='2026-10-25T00:00:00Z',
                        key_id='unit-formal', trusted_keys_file=root / 'unused.json', acceptance_receipt=root / 'unused-receipt.json')
            self.assertFalse((root / 'publication').exists())

    def test_derivation_preserves_original_receipt_and_rejects_non_pe_changes(self):
        with tempfile.TemporaryDirectory(prefix='MediaFlow-auth-derivation-unit-') as temporary:
            root = Path(temporary); source, target = root / 'source', root / 'target'
            records = []
            for name in (*security.FIRST_PARTY, 'LICENSE'):
                before = unsigned_pe(with_archive=name == 'MediaFlow.exe') if name in security.FIRST_PARTY else b'unit license'
                after = synthetic_certificate_append(before) if name in security.FIRST_PARTY else before
                for tree, data in ((source, before), (target, after)):
                    path = tree / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
                records.append(dict(path=name, size=len(before), sha256=hashlib.sha256(before).hexdigest()))
            receipt = root / 'original.json'
            receipt.write_text(json.dumps({'files': records, 'github': {'repository': 'unit/source',
                'commit': 'a' * 40, 'run_id': '1'}, 'source_sha256': 'b' * 64}), 'utf-8')
            original = receipt.read_bytes()
            result = security.signing_derivation(source, target, receipt,
                {'mode': 'Production', 'formal_verified': True}, root / 'derivation.json')
            self.assertEqual(receipt.read_bytes(), original)
            self.assertFalse(result['original_ci_receipt_rewritten'])
            self.assertEqual(len(result['first_party_changes']), 3)
            (target / 'LICENSE').write_bytes(b'changed license')
            with self.assertRaisesRegex(security.ReleaseSecurityError, 'third-party'):
                security.signing_derivation(source, target, receipt,
                    {'mode': 'Production', 'formal_verified': True}, root / 'rejected.json')
            self.assertFalse((root / 'rejected.json').exists())

    def test_derivation_does_not_accept_unsigned_or_test_trust_claims(self):
        with tempfile.TemporaryDirectory(prefix='MediaFlow-auth-no-test-bypass-') as temporary:
            root = Path(temporary); source, target = root / 's', root / 't'; source.mkdir(); target.mkdir()
            receipt = root / 'receipt.json'; receipt.write_text(json.dumps({'files': []}), 'utf-8')
            for report in ({'mode': 'Test', 'formal_verified': False}, {'mode': 'CI', 'formal_verified': True}):
                with self.assertRaises(security.ReleaseSecurityError):
                    security.signing_derivation(source, target, receipt, report, root / 'output.json')


if __name__ == '__main__':
    unittest.main()
