"""Real native RSA signatures use an ephemeral, non-exported private key."""
import base64
from copy import deepcopy
import ctypes
import hashlib
import json
import os
import struct
import unittest

from core.update_manifest import (
    MAX_FILES, MAX_PACKAGE_BYTES, MAX_UNPACKED_BYTES, REQUIRED_FILES,
    UpdateValidationError, canonical_bytes, parse_version, safe_release_path,
    validate_manifest, validate_trusted_keys,
)


def manifest(version='1.9.0'):
    return {
        'schema_version': 1, 'product': 'MediaFlow', 'version': version,
        'build_id': '20261021T120000Z-local-test', 'platform': 'windows',
        'architecture': 'x86_64', 'release_notes': '本地验证\n更新与版本管理',
        'release_page': '', 'minimum_app_version': '1.8.0', 'data_migration': 'separate',
        'files': [{'path': path, 'size': 12, 'sha256': hashlib.sha256(path.encode()).hexdigest()}
                  for path in REQUIRED_FILES],
    }


class NativeSigningKey:
    """The OS owns the private key for this object's lifetime; only N/E is exported."""
    def __init__(self):
        self.native = ctypes.WinDLL('bcrypt.dll', winmode=0x00000800)
        handle, number = ctypes.c_void_p, ctypes.c_uint32
        declarations = {
            'BCryptOpenAlgorithmProvider': [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, number],
            'BCryptGenerateKeyPair': [handle, ctypes.POINTER(handle), number, number],
            'BCryptFinalizeKeyPair': [handle, number],
            'BCryptExportKey': [handle, handle, ctypes.c_wchar_p, ctypes.c_void_p, number,
                                ctypes.POINTER(number), number],
            'BCryptSignHash': [handle, ctypes.c_void_p, ctypes.c_void_p, number,
                               ctypes.c_void_p, number, ctypes.POINTER(number), number],
            'BCryptDestroyKey': [handle], 'BCryptCloseAlgorithmProvider': [handle, number],
        }
        for name, arguments in declarations.items():
            function = getattr(self.native, name)
            function.argtypes, function.restype = arguments, ctypes.c_int32
        self.algorithm, self.key = handle(), handle()
        try:
            self.check(self.native.BCryptOpenAlgorithmProvider(ctypes.byref(self.algorithm), 'RSA', None, 0))
            self.check(self.native.BCryptGenerateKeyPair(self.algorithm, ctypes.byref(self.key), 2048, 0))
            self.check(self.native.BCryptFinalizeKeyPair(self.key, 0))
            size = number()
            self.check(self.native.BCryptExportKey(self.key, None, 'RSAPUBLICBLOB', None, 0,
                                                  ctypes.byref(size), 0))
            output = ctypes.create_string_buffer(size.value)
            self.check(self.native.BCryptExportKey(self.key, None, 'RSAPUBLICBLOB', output,
                                                  size.value, ctypes.byref(size), 0))
            blob = output.raw[:size.value]
            magic, bits, exponent_size, modulus_size, prime1, prime2 = struct.unpack('<6I', blob[:24])
            if magic != 0x31415352 or bits != 2048 or prime1 or prime2:
                raise AssertionError('Native export was not a 2048-bit public key')
            self.public = {
                'algorithm': 'rsa-pkcs1-sha256',
                'exponent': base64.b64encode(blob[24:24 + exponent_size]).decode('ascii'),
                'modulus': base64.b64encode(blob[24 + exponent_size:24 + exponent_size + modulus_size]).decode('ascii'),
            }
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(status):
        if status:
            raise AssertionError(f'Native test RSA operation failed: {status & 0xffffffff:08x}')

    def sign(self, value, key_id='unit-ephemeral', *, hash_algorithm='SHA256'):
        class Padding(ctypes.Structure):
            _fields_ = [('algorithm', ctypes.c_wchar_p)]

        padding = Padding(hash_algorithm)
        digest = hashlib.new(hash_algorithm.lower(), canonical_bytes(value)).digest()
        digest_buffer = ctypes.create_string_buffer(digest)
        size = ctypes.c_uint32()
        self.check(self.native.BCryptSignHash(self.key, ctypes.byref(padding), digest_buffer,
                                              len(digest), None, 0, ctypes.byref(size), 2))
        output = ctypes.create_string_buffer(size.value)
        self.check(self.native.BCryptSignHash(self.key, ctypes.byref(padding), digest_buffer,
                                              len(digest), output, size.value, ctypes.byref(size), 2))
        value['signature'] = {'algorithm': 'rsa-pkcs1-sha256', 'key_id': key_id,
                              'value': base64.b64encode(output.raw[:size.value]).decode('ascii')}
        return value

    def close(self):
        if self.key.value:
            self.native.BCryptDestroyKey(self.key)
            self.key.value = None
        if self.algorithm.value:
            self.native.BCryptCloseAlgorithmProvider(self.algorithm, 0)
            self.algorithm.value = None


class UpdateManifestTests(unittest.TestCase):
    def test_version_order_is_numeric_and_handles_prerelease_and_local(self):
        self.assertGreater(parse_version('1.10.0'), parse_version('1.9.0'))
        ordered = ['1.9.0a1', '1.9.0b2', '1.9.0rc3', '1.9.0', '1.9.0+test.2', '1.9.0+test.10']
        self.assertEqual(sorted(map(parse_version, reversed(ordered))), list(map(parse_version, ordered)))

    def test_ambiguous_version_syntax_is_rejected(self):
        for version in ('1.9', 'v1.9.0', '1.09.0', ' 1.9.0', '1.9.0 ', '1.9.0.1', '1.9.0rc',
                        '1.9.0.dev1', '1.9.0.post1', '1!1.9.0', True, 19, '', '../1.9.0'):
            with self.subTest(version=version), self.assertRaises(UpdateValidationError):
                parse_version(version)

    def test_local_hashes_are_never_publisher_authentication(self):
        source = manifest()
        result = validate_manifest(source)
        self.assertFalse(result['authenticated'])
        self.assertIn('未经过发布者身份认证', result['authentication'])
        self.assertEqual(result['manifest_sha256'], hashlib.sha256(canonical_bytes(source)).hexdigest())
        result['manifest']['files'][0]['size'] = 500
        self.assertEqual(source['files'][0]['size'], 12)

    def test_unsigned_online_manifest_is_rejected(self):
        with self.assertRaisesRegex(UpdateValidationError, '缺少可信发布者签名'):
            validate_manifest(manifest(), require_signature=True)

    def test_canonical_encoding_covers_unicode_and_excludes_only_signature(self):
        value = {'z': '中文', 'a': {'v': 1}, 'signature': {'value': 'ignored'}}
        expected = '{"a":{"v":1},"z":"中文"}'.encode('utf-8')
        self.assertEqual(canonical_bytes(value), expected)
        self.assertEqual(canonical_bytes(dict(reversed(list(value.items())))), expected)
        with self.assertRaises(UpdateValidationError):
            canonical_bytes({'value': float('nan')})
        with self.assertRaises(UpdateValidationError):
            canonical_bytes({'value': '\ud800'})

    def test_required_fields_are_strict_and_cannot_add_commands_or_targets(self):
        for missing in manifest():
            value = manifest()
            del value[missing]
            with self.subTest(missing=missing), self.assertRaises(UpdateValidationError):
                validate_manifest(value)
        for field in ('command', 'arguments', 'target_path', 'post_install', 'trusted_keys'):
            value = manifest()
            value[field] = 'anything'
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_manifest(value)

    def test_product_platform_schema_and_build_identity_are_checked(self):
        cases = [('product', 'AnotherApp'), ('platform', 'linux'), ('architecture', 'arm64'),
                 ('schema_version', 2), ('schema_version', True), ('build_id', '../escape'),
                 ('build_id', ''), ('build_id', 'x' * 97), ('build_id', '构建'),
                 ('data_migration', 'automatic')]
        for field, content in cases:
            value = manifest()
            value[field] = content
            with self.subTest(field=field, content=content), self.assertRaises(UpdateValidationError):
                validate_manifest(value)

    def test_release_notes_are_bounded_plain_text_with_normal_newlines(self):
        for content in ('x' * 16001, 'hidden\x00text', '\ud800', 'reversed\u202etext', {}):
            value = manifest()
            value['release_notes'] = content
            with self.subTest(content=repr(content)[:30]), self.assertRaises(UpdateValidationError):
                validate_manifest(value)
        value = manifest()
        value['release_notes'] = '文本\r\n\t下一行'
        self.assertFalse(validate_manifest(value)['authenticated'])

    def test_minimum_version_is_optional_but_must_be_consistent(self):
        for minimum in ('', None, '1.8.0'):
            value = manifest()
            value['minimum_app_version'] = minimum
            validate_manifest(value)
        value['minimum_app_version'] = '1.10.0'
        with self.assertRaisesRegex(UpdateValidationError, '最低兼容版本'):
            validate_manifest(value)

    def test_urls_require_https_without_credentials_or_fragments(self):
        for address in ('http://example.invalid/a', 'file:///x', 'https://user:pass@example.invalid',
                        'https://example.invalid#fragment', 'https://example.invalid:bad',
                        'https://example.invalid:0', 'https://example.invalid/\nfoo',
                        'https://example.invalid\\foo', 'https:///missing'):
            value = manifest()
            value['release_page'] = address
            with self.subTest(address=address), self.assertRaises(UpdateValidationError):
                validate_manifest(value)
        value['release_page'] = 'https://example.invalid/releases/1.9.0'
        validate_manifest(value)

    def test_package_metadata_requires_allowed_format_bounded_size_and_sha256(self):
        value = manifest()
        value['package'] = {'format': 'zip', 'url': 'https://example.invalid/release.zip',
                            'size': 123, 'sha256': 'a' * 64}
        validate_manifest(value)
        for field, content in [('format', 'exe'), ('url', 'http://example.invalid'),
                               ('size', 0), ('size', True), ('size', MAX_PACKAGE_BYTES + 1),
                               ('sha256', 'wrong')]:
            changed = deepcopy(value)
            changed['package'][field] = content
            with self.subTest(field=field, content=content), self.assertRaises(UpdateValidationError):
                validate_manifest(changed)

    def test_windows_path_traversal_aliases_devices_and_unicode_controls_are_rejected(self):
        for path in ('../x', '/x', 'C:/x', '//server/share/x', 'a\\x', 'a//x', 'a/./x',
                     'a/../x', 'x/', 'x.', 'x ', '.hidden/x', 'a/file:stream', 'CON.txt',
                     'a/COM1.md', 'a/LPT².md', 'NUL', 'a/b?c', 'a/b\x00c', 'a/b\u202ec',
                     'a/' + 'b' * 240, '\ud800'):
            with self.subTest(path=repr(path)), self.assertRaises(UpdateValidationError):
                safe_release_path(path)
        self.assertEqual(safe_release_path('licenses/中文说明.txt'), 'licenses/中文说明.txt')

    def test_user_data_and_unexpected_executables_are_excluded(self):
        paths = ('config.json', 'tasks.json', 'tasks.json.bak', 'BrowserProfile/Preferences',
                 'history.sqlite3', '.stream-downloads/work', 'secret.json', 'private-key.txt',
                 'trusted-keys.json', 'tools/evil.exe', 'evil.ps1', 'nested/MediaFlow.exe',
                 'MediaFlow-Other.cmd', 'release.json', 'tools/webview2/plugin.dll')
        for path in paths:
            value = manifest()
            value['files'].append({'path': path, 'size': 1, 'sha256': 'a' * 64})
            with self.subTest(path=path), self.assertRaises(UpdateValidationError):
                validate_manifest(value)

    def test_existing_release_documentation_and_fixed_tools_are_allowed(self):
        value = manifest()
        for path in ('README.md', 'migration.png', 'unit-results.json', 'FILES.sha256',
                     'MediaFlow-Qt.cmd', 'tools/FFmpeg-LICENSE', 'tools/yt-dlp-LICENSE.txt',
                     'tools/webview2/MediaFlow.WebView2Host.exe.config',
                     'licenses/pyside6-6.11.2.dist-info/LicenseRef-Qt-Commercial.txt',
                     'licenses/QtWebEngine/qtwebengine-thirdparty.html'):
            value['files'].append({'path': path, 'size': 12, 'sha256': 'b' * 64})
        validate_manifest(value)

    def test_each_required_file_must_exist_and_be_nonempty(self):
        for path in REQUIRED_FILES:
            value = manifest()
            value['files'] = [item for item in value['files'] if item['path'] != path]
            with self.subTest(path=path), self.assertRaisesRegex(UpdateValidationError, '必需'):
                validate_manifest(value)
        value = manifest()
        value['files'][0]['size'] = 0
        with self.assertRaisesRegex(UpdateValidationError, '必需'):
            validate_manifest(value)

    def test_duplicate_case_unicode_and_directory_aliases_are_rejected(self):
        pairs = [('README.md', 'README.md'), ('README.md', 'readme.md'),
                 ('é.txt', 'e\u0301.txt'), ('licenses/Dir/A.txt', 'licenses/dir/B.txt'),
                 ('licenses/é/A.txt', 'licenses/e\u0301/B.txt'),
                 ('licenses/parent', 'licenses/parent/child.txt')]
        for pair in pairs:
            value = manifest()
            value['files'].extend({'path': path, 'size': 1, 'sha256': 'a' * 64} for path in pair)
            with self.subTest(pair=pair), self.assertRaisesRegex(UpdateValidationError, '冲突|重复'):
                validate_manifest(value)

    def test_sizes_hashes_and_file_counts_have_strict_types_and_limits(self):
        for field, content in [('size', True), ('size', -1), ('size', 1.0),
                               ('size', MAX_UNPACKED_BYTES + 1), ('sha256', 'a' * 63),
                               ('sha256', 'g' * 64), ('sha256', 7)]:
            value = manifest()
            value['files'][0][field] = content
            with self.subTest(field=field, content=content), self.assertRaises(UpdateValidationError):
                validate_manifest(value)
        value = manifest()
        value['files'][0]['size'] = MAX_UNPACKED_BYTES
        with self.assertRaises(UpdateValidationError):
            validate_manifest(value)
        value['files'] = [value['files'][0]] * (MAX_FILES + 1)
        with self.assertRaises(UpdateValidationError):
            validate_manifest(value)

    def test_unknown_signature_key_is_rejected_even_for_local_import(self):
        value = manifest()
        value['signature'] = {'algorithm': 'rsa-pkcs1-sha256', 'key_id': 'unknown',
                              'value': base64.b64encode(b'0' * 256).decode('ascii')}
        with self.assertRaisesRegex(UpdateValidationError, '不可信'):
            validate_manifest(value)


@unittest.skipUnless(os.name == 'nt', 'Windows CNG native signatures')
class NativePublisherSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signer = NativeSigningKey()
        cls.keys = {'unit-ephemeral': cls.signer.public}

    @classmethod
    def tearDownClass(cls):
        cls.signer.close()

    def test_real_ephemeral_signature_authenticates_canonical_unicode_manifest(self):
        value = self.signer.sign(manifest())
        result = validate_manifest(value, self.keys, require_signature=True)
        self.assertTrue(result['authenticated'])
        reordered = dict(reversed(list(value.items())))
        self.assertTrue(validate_manifest(reordered, self.keys)['authenticated'])
        roundtrip = json.loads(json.dumps(value, ensure_ascii=True, indent=4))
        self.assertTrue(validate_manifest(roundtrip, self.keys)['authenticated'])

    def test_signed_file_hash_version_and_notes_tampering_fail(self):
        signed = self.signer.sign(manifest())
        for field in ('version', 'release_notes', 'files', 'data_migration'):
            value = deepcopy(signed)
            if field == 'files':
                value['files'][0]['sha256'] = 'f' * 64
            else:
                value[field] = {'version': '1.10.0', 'release_notes': 'changed',
                                'data_migration': 'none'}[field]
            with self.subTest(field=field), self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
                validate_manifest(value, self.keys)

    def test_signature_corruption_truncation_and_noncanonical_base64_fail(self):
        signed = self.signer.sign(manifest())
        raw = base64.b64decode(signed['signature']['value'])
        for text in (base64.b64encode(bytes([raw[0] ^ 1]) + raw[1:]).decode(),
                     base64.b64encode(raw[:-1]).decode(), signed['signature']['value'] + '\n',
                     'not!base64'):
            value = deepcopy(signed)
            value['signature']['value'] = text
            with self.subTest(length=len(text)), self.assertRaises(UpdateValidationError):
                validate_manifest(value, self.keys)

    def test_different_valid_key_with_same_id_cannot_authenticate(self):
        other = NativeSigningKey()
        try:
            with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
                validate_manifest(self.signer.sign(manifest()), {'unit-ephemeral': other.public})
        finally:
            other.close()

    def test_hash_algorithm_substitution_fails(self):
        value = self.signer.sign(manifest(), hash_algorithm='SHA512')
        with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
            validate_manifest(value, self.keys)

    def test_signature_is_bound_to_complete_online_package_metadata(self):
        value = manifest()
        value['package'] = {'format': 'zip', 'url': 'https://example.invalid/release.zip',
                            'size': 123, 'sha256': 'a' * 64}
        self.signer.sign(value)
        self.assertTrue(validate_manifest(value, self.keys, True)['authenticated'])
        value['package']['sha256'] = 'b' * 64
        with self.assertRaisesRegex(UpdateValidationError, '签名验证失败'):
            validate_manifest(value, self.keys, True)

    def test_trusted_key_configuration_rejects_private_fields_and_weak_or_invalid_keys(self):
        valid = validate_trusted_keys(self.keys)
        self.assertEqual(valid, self.keys)
        for field, content in [('private_exponent', 'must-not-be-accepted'),
                               ('algorithm', 'none'), ('exponent', 'AQ=='),
                               ('exponent', 'Ag=='), ('modulus', base64.b64encode(b'\xff' * 128).decode()),
                               ('modulus', 'bad!'), ('exponent', base64.b64encode(b'\x01' * 9).decode())]:
            key = dict(self.signer.public)
            key[field] = content
            with self.subTest(field=field), self.assertRaises(UpdateValidationError):
                validate_trusted_keys({'unit-ephemeral': key})
        with self.assertRaises(UpdateValidationError):
            validate_trusted_keys({'../outside': self.signer.public})
        with self.assertRaises(UpdateValidationError):
            validate_trusted_keys({str(index): self.signer.public for index in range(33)})


if __name__ == '__main__':
    unittest.main()
