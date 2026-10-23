"""Ed25519 tests use fresh in-memory test keys, never a shipped trust root."""
import base64
from copy import deepcopy
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.update_manifest import (UpdateValidationError, canonical_bytes,
                                  validate_manifest, validate_trusted_keys)
from tests.test_update_manifest import manifest


class Ed25519ManifestTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.keys = {'test-only': {'algorithm': 'ed25519', 'public_key':
            base64.b64encode(self.private.public_key().public_bytes_raw()).decode('ascii')}}

    def signed(self):
        value = manifest('2.0.0')
        value.update(schema_version=2, published_at='2026-10-22T10:00:00Z',
                     signing={'algorithm': 'ed25519', 'key_id': 'test-only'})
        return self.sign(value)

    def sign(self, value):
        value['signature'] = {'algorithm': 'ed25519', 'key_id': 'test-only', 'value':
            base64.b64encode(self.private.sign(canonical_bytes(value))).decode('ascii')}
        return value

    def test_valid_signature_includes_unicode_and_signing_identity(self):
        value = self.signed()
        result = validate_manifest(value, self.keys, require_signature=True)
        self.assertTrue(result['authenticated'])
        payload = canonical_bytes(value)
        self.assertIn('本地验证'.encode('utf-8'), payload)
        self.assertIn(b'"signing":', payload)
        self.assertNotIn(b'"signature":', payload)
        self.assertFalse(payload.startswith(b'\xef\xbb\xbf'))
        self.assertFalse(payload.endswith(b'\n'))

    def test_every_release_and_package_field_is_bound(self):
        value = self.signed()
        value['package'] = {'format': 'zip', 'url': 'https://localhost/release.zip',
                            'size': 100, 'sha256': 'a' * 64}
        self.sign(value)
        edits = [('version', '2.0.1'), ('build_id', 'different-build'),
                 ('release_notes', 'changed'), ('release_page', 'https://localhost/changed'),
                 ('minimum_app_version', '1.9.0'), ('data_migration', 'none'),
                 ('published_at', '2026-10-23T10:00:00Z')]
        for field, replacement in edits:
            altered = deepcopy(value)
            altered[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
                validate_manifest(altered, self.keys, require_signature=True)
        for field, replacement in [('url', 'https://localhost/other.zip'), ('size', 101), ('sha256', 'b'*64)]:
            altered = deepcopy(value)
            altered['package'][field] = replacement
            with self.subTest(field='package.'+field), self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
                validate_manifest(altered, self.keys, require_signature=True)
        altered = deepcopy(value)
        altered['files'][0]['sha256'] = 'f' * 64
        with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
            validate_manifest(altered, self.keys)

    def test_missing_signature_online_rejected(self):
        value = self.signed()
        del value['signature']
        with self.assertRaisesRegex(UpdateValidationError, '缺少可信发布者签名'):
            validate_manifest(value, self.keys, require_signature=True)
        self.assertFalse(validate_manifest(value, self.keys)['authenticated'])

    def test_wrong_public_key_and_untrusted_id_rejected(self):
        value = self.signed()
        wrong = deepcopy(self.keys)
        wrong['test-only']['public_key'] = base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode('ascii')
        with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
            validate_manifest(value, wrong)
        with self.assertRaisesRegex(UpdateValidationError, '不可信'):
            validate_manifest(value, {})

    def test_key_id_cannot_be_switched_even_if_both_alias_same_public_key(self):
        value = self.signed()
        keys = dict(self.keys, alias=self.keys['test-only'])
        value['signature']['key_id'] = 'alias'
        with self.assertRaisesRegex(UpdateValidationError, '密钥身份不一致'):
            validate_manifest(value, keys)
        value['signing']['key_id'] = 'alias'
        with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
            validate_manifest(value, keys)

    def test_malformed_base64_and_wrong_length_rejected(self):
        for signature in ('!', '', 'a' * 9000, base64.b64encode(b'short').decode('ascii')):
            value = self.signed()
            value['signature']['value'] = signature
            with self.subTest(signature_size=len(signature)), self.assertRaises(UpdateValidationError):
                validate_manifest(value, self.keys)

    def test_key_config_never_accepts_private_fields_or_wrong_length(self):
        for key in ({'algorithm':'ed25519','public_key':base64.b64encode(b'short').decode()},
                    dict(self.keys['test-only'],private_key='not-accepted'),
                    {'algorithm':'ed25519','public_key':'!'}, {'algorithm':'unknown','public_key':'a'}):
            with self.subTest(fields=list(key)), self.assertRaises(UpdateValidationError):
                validate_trusted_keys({'key':key})

    def test_algorithm_confusion_and_unsupported_schema_rejected(self):
        value = self.signed()
        for field, replacement in [('schema_version', 3), ('product','AnotherProduct'),
                                   ('platform','linux'), ('architecture','arm64'), ('version','2.invalid')]:
            altered = deepcopy(value)
            altered[field] = replacement
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_manifest(altered, self.keys)
        value['signature']['algorithm'] = 'rsa-pkcs1-sha256'
        with self.assertRaises(UpdateValidationError):
            validate_manifest(value, self.keys)

    def test_publish_date_must_be_real_utc_second_precision(self):
        for date in ('2026-02-30T10:00:00Z','2026-10-22','2026-10-22T10:00:00+08:00',False):
            value = self.signed()
            value['published_at'] = date
            self.sign(value)
            with self.subTest(date=date), self.assertRaises(UpdateValidationError):
                validate_manifest(value, self.keys)

    def test_signed_manifest_cannot_provide_trust_or_commands(self):
        for field in ('trusted_keys','command','destination'):
            value = self.signed()
            value[field] = self.keys if field == 'trusted_keys' else 'ignored'
            self.sign(value)
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_manifest(value, self.keys)


if __name__ == '__main__':
    unittest.main()
