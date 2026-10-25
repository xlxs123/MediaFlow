"""Scanner boundary unit tests; all downloads are mocked, no tool is executed.

Synthetic archives are constructed from real ZIP/TAR bytes. File writes are
limited to this test's owned temporary directory, never source/user data paths.
These fixtures are not evidence of an actual public scanner download.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request
import warnings
import zipfile

from scripts import security_tools as tools


BINARY = b'synthetic-unit-scanner-never-executed'
ZIP_URL = 'https://github.com/gitleaks/gitleaks/releases/download/unit-fixture/scanner.zip'
TAR_URL = 'https://github.com/gitleaks/gitleaks/releases/download/unit-fixture/scanner.tar.gz'


def zip_bytes(entries):
    data = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(data, 'w') as archive:
            for name, value in entries:
                archive.writestr(name, value)
    return data.getvalue()


def tar_bytes(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for info, value in entries:
            if value is not None:
                info.size = len(value)
            archive.addfile(info, io.BytesIO(value) if value is not None else None)
    return data.getvalue()


def record_for(data, *, binary=BINARY, tar=False):
    return {'archive_url': TAR_URL if tar else ZIP_URL,
            'archive_size': len(data), 'archive_sha256': hashlib.sha256(data).hexdigest(),
            'executable_name': 'gitleaks' if tar else 'gitleaks.exe',
            'executable_sha256': hashlib.sha256(binary).hexdigest()}


class FakePublicResponse(io.BytesIO):
    status = 200

    def __init__(self, data, url=ZIP_URL):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class SecurityToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-scanner-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / 'new-scanner-directory'

    def manifest(self, record):
        return {'binary_tools': {'gitleaks': {'version': 'unit-fixture-not-a-release',
                                             'platforms': {'windows-x64': record}}}}

    def install(self, data, record, *, response_url=ZIP_URL, destination=None):
        opener = Mock()
        opener.open.return_value = FakePublicResponse(data, response_url)
        with patch.object(tools, 'platform_key', return_value='windows-x64'), \
             patch.object(tools.urllib.request, 'build_opener', return_value=opener):
            target = tools.install('gitleaks', destination or self.destination,
                                   manifest=self.manifest(record))
        return target, opener

    def test_zip_reads_only_exact_pinned_member_without_releasing_other_paths(self):
        data = zip_bytes([('gitleaks.exe', BINARY), ('../../escape.txt', b'unit-escape'),
                          ('nested/unwanted.exe', b'unit-other')])
        with patch.object(zipfile.ZipFile, 'extractall', side_effect=AssertionError('must not extract')), \
             patch.object(zipfile.ZipFile, 'extract', side_effect=AssertionError('must not extract')):
            target, opener = self.install(data, record_for(data))
        self.assertEqual(target.read_bytes(), BINARY)
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {'gitleaks.exe', 'verified-tool.json'})
        self.assertFalse((self.root / 'escape.txt').exists())
        self.assertFalse((self.destination / 'nested').exists())
        receipt = json.loads((self.destination / 'verified-tool.json').read_text('utf-8'))
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['executable_sha256'], hashlib.sha256(BINARY).hexdigest())
        opener.open.assert_called_once()

    def test_duplicate_zip_target_member_is_rejected(self):
        data = zip_bytes([('gitleaks.exe', BINARY), ('gitleaks.exe', b'unit-conflicting-copy')])
        with self.assertRaisesRegex(tools.ToolVerificationError, 'duplicated'):
            tools.extract_binary(data, record_for(data))

    def test_zip_symlink_target_is_rejected_even_when_content_hash_matches(self):
        info = zipfile.ZipInfo('gitleaks.exe')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = zip_bytes([(info, BINARY)])
        with self.assertRaisesRegex(tools.ToolVerificationError, 'ordinary'):
            tools.extract_binary(data, record_for(data))

    def test_tar_symbolic_and_hardlink_targets_are_both_rejected(self):
        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(link_type=link_type):
                info = tarfile.TarInfo('gitleaks')
                info.type = link_type
                info.linkname = '../outside-unit-fixture'
                data = tar_bytes([(info, None)])
                with self.assertRaisesRegex(tools.ToolVerificationError, 'ordinary'):
                    tools.extract_binary(data, record_for(data, tar=True))

    def test_verified_archive_still_requires_actual_binary_hash_to_match(self):
        data = zip_bytes([('gitleaks.exe', BINARY)])
        record = record_for(data)
        record['executable_sha256'] = '0' * 64
        with self.assertRaisesRegex(tools.ToolVerificationError, 'executable SHA-256'):
            self.install(data, record)
        self.assertFalse((self.destination / 'gitleaks.exe').exists())
        self.assertFalse((self.destination / 'verified-tool.json').exists())

    def test_archive_size_or_hash_mismatch_blocks_binary_processing(self):
        data = zip_bytes([('gitleaks.exe', BINARY)])
        for field, incorrect in (('archive_size', len(data) + 1), ('archive_sha256', '0' * 64)):
            with self.subTest(field=field):
                record = record_for(data)
                record[field] = incorrect
                destination = self.root / field
                with patch.object(tools, 'extract_binary') as extract:
                    with self.assertRaises(tools.ToolVerificationError):
                        self.install(data, record, destination=destination)
                extract.assert_not_called()
                self.assertFalse((destination / 'gitleaks.exe').exists())

    def test_non_https_foreign_host_and_credential_source_urls_fail_before_network(self):
        invalid_urls = ['http://github.com/unit.zip', 'https://foreign.invalid/unit.zip',
            'https://github.com.foreign.invalid/unit.zip', 'https://user:unit-only@github.com/unit.zip',
            'https://github.com:444/unit.zip', ZIP_URL + '?redirect=unit', ZIP_URL + '#unit']
        data = zip_bytes([('gitleaks.exe', BINARY)])
        for address in invalid_urls:
            with self.subTest(address=address):
                record = record_for(data)
                record['archive_url'] = address
                with patch.object(tools, 'platform_key', return_value='windows-x64'), \
                     patch.object(tools.urllib.request, 'build_opener') as opener:
                    with self.assertRaises(tools.ToolVerificationError):
                        tools.install('gitleaks', self.destination, manifest=self.manifest(record))
                opener.assert_not_called()
                self.assertFalse(self.destination.exists())

    def test_redirect_and_final_response_cannot_escape_approved_https_hosts(self):
        request = urllib.request.Request(ZIP_URL)
        handler = tools.Redirects()
        for address in ('http://github.com/unit.zip', 'https://foreign.invalid/unit.zip',
                        'https://user:unit-only@github.com/unit.zip'):
            with self.subTest(address=address):
                with self.assertRaises(tools.ToolVerificationError):
                    handler.redirect_request(request, None, 302, 'unit', {}, address)
        data = zip_bytes([('gitleaks.exe', BINARY)])
        with self.assertRaisesRegex(tools.ToolVerificationError, 'unexpected response'):
            self.install(data, record_for(data), response_url='https://foreign.invalid/unit.zip')
        self.assertFalse((self.destination / 'gitleaks.exe').exists())

    def test_existing_destination_is_preserved_without_network_or_overwrite(self):
        self.destination.mkdir()
        sentinel = self.destination / 'gitleaks.exe'
        sentinel.write_bytes(b'previous-verified-unit-file')
        data = zip_bytes([('gitleaks.exe', BINARY)])
        with patch.object(tools, 'platform_key', return_value='windows-x64'), \
             patch.object(tools.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(tools.ToolVerificationError, 'existing files are preserved'):
                tools.install('gitleaks', self.destination, manifest=self.manifest(record_for(data)))
        opener.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'previous-verified-unit-file')
        self.assertEqual(list(self.destination.iterdir()), [sentinel])

    def test_unsupported_architecture_or_missing_platform_pin_is_rejected(self):
        with patch.object(tools.platform, 'machine', return_value='arm64'):
            with self.assertRaises(tools.ToolVerificationError):
                tools.platform_key()
        data = zip_bytes([('gitleaks.exe', BINARY)])
        manifest = self.manifest(record_for(data))
        manifest['binary_tools']['gitleaks']['platforms'] = {}
        with patch.object(tools, 'platform_key', return_value='windows-x64'), \
             patch.object(tools.urllib.request, 'build_opener') as opener:
            # The CLI also safely catches missing-map KeyError. Keep this test
            # compatible if the helper improves that diagnostic to its own error.
            with self.assertRaises((tools.ToolVerificationError, KeyError)):
                tools.install('gitleaks', self.destination, manifest=manifest)
        opener.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_reviewed_manifest_has_full_action_and_scanner_digests(self):
        manifest = json.loads((tools.ROOT / 'security/toolchain.json').read_text('utf-8'))
        digest = re.compile(r'[0-9a-f]{64}\Z')
        commit = re.compile(r'[0-9a-f]{40}\Z')
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(set(manifest['binary_tools']), {'gitleaks', 'actionlint'})
        for name, tool in manifest['binary_tools'].items():
            self.assertEqual(set(tool['platforms']), {'windows-x64', 'linux-x64'})
            for key, record in tool['platforms'].items():
                with self.subTest(tool=name, platform=key):
                    self.assertRegex(record['archive_sha256'], digest)
                    self.assertRegex(record['executable_sha256'], digest)
                    self.assertTrue(tools.approved_url(record['archive_url']))
                    self.assertTrue(tools.approved_url(record['checksum_url']))
                    self.assertGreater(record['archive_size'], 0)
                    self.assertLess(record['archive_size'], 32 * 1024 * 1024)
                    self.assertEqual(record['executable_name'], name + ('.exe' if key == 'windows-x64' else ''))
        for action in manifest['actions'].values():
            self.assertRegex(action['commit'], commit)
            self.assertTrue(action['entrypoints'])
            for entrypoint in action['entrypoints']:
                self.assertRegex(entrypoint['sha256'], digest)

    def test_tar_reads_only_exact_regular_target_without_extracting_other_members(self):
        unwanted = tarfile.TarInfo('../../outside-unit-fixture')
        link = tarfile.TarInfo('unwanted-link')
        link.type, link.linkname = tarfile.SYMTYPE, '../outside-unit-fixture'
        data = tar_bytes([(tarfile.TarInfo('gitleaks'), BINARY),
                          (unwanted, b'unit-other'), (link, None)])
        with patch.object(tarfile.TarFile, 'extractall', side_effect=AssertionError('must not extract')), \
             patch.object(tarfile.TarFile, 'extract', side_effect=AssertionError('must not extract')):
            self.assertEqual(tools.extract_binary(data, record_for(data, tar=True)), BINARY)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
