"""Actual localhost TLS bytes are integration evidence, never public acceptance."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from core.update_manager import UpdateManager
from core.update_manifest import canonical_bytes
from scripts.verify_hosted_release import HostedReleaseError, verify_hosted_release, main
from tests.test_publish_release import PublisherFixture
from tests.update_https_fixture import HTTPSFixture


class HostedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-hosted-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = HTTPSFixture()
        self.addCleanup(self.server.close)
        self.fixture = PublisherFixture(self.root)
        result = self.fixture.publish(package_url=self.server.url + '/release.zip?secret=do-not-report')
        self.document = result['manifest']
        self.package = Path(result['package_path']).read_bytes()
        self.server.routes['/release.zip'] = self.package
        self.set_document(self.document)
        self.arguments = dict(release_dir=self.fixture.release, trusted_keys_file=self.fixture.keys_file,
            acceptance_receipt=self.fixture.receipt, local_test=True, ssl_context=self.server.context,
            network_timeouts={'connect':.5, 'check':5, 'download':5})

    def set_document(self, value, sign=False):
        value = deepcopy(value)
        if sign:
            value['signature']['value'] = base64.b64encode(
                self.fixture.private.sign(canonical_bytes(value))).decode('ascii')
        self.server.routes['/release.json'] = json.dumps(value, ensure_ascii=False).encode('utf-8')

    def verify(self, **kwargs):
        arguments = dict(self.arguments, **kwargs)
        return verify_hosted_release(self.server.url + '/release.json?token=do-not-report', **arguments)

    def test_valid_local_https_bound_to_exact_release_no_deploy_or_public_claim(self):
        with patch('core.update_manager.UpdateManager.deploy', side_effect=AssertionError('must not deploy')), \
                patch('core.update_manager.UpdateManager.check_health', side_effect=AssertionError('must not run EXE')):
            result = self.verify()
        self.assertTrue(result['passed'])
        self.assertTrue(result['signature_verified'])
        self.assertTrue(result['package_integrity_verified'])
        self.assertTrue(result['acceptance_receipt_verified'])
        self.assertTrue(result['release_matches_expected'])
        self.assertEqual(result['expected']['files_count'], len(self.document['files']))
        self.assertEqual(len(result['expected']['inventory_sha256']), 64)
        self.assertTrue(result['temporary_files_removed'])
        self.assertFalse(result['public_online_verified'])
        self.assertEqual(result['scope'], 'local_https_integration')
        self.assertFalse(result['deployed'])
        self.assertFalse(result['executable_started'])
        self.assertNotIn('do-not-report', json.dumps(result))
        self.assertEqual(self.server.requests, ['/release.json', '/release.zip'])
        self.assertEqual((self.fixture.release / 'MediaFlow.exe').read_bytes(), b'UNIT TEST BYTES: MediaFlow.exe')

    def test_production_rejects_local_and_private_hosts_before_network(self):
        for host in ('localhost', '127.0.0.1', '10.1.2.3', '192.168.1.1', '[::1]', 'name.local', 'example.org'):
            with self.subTest(host=host), self.assertRaises(HostedReleaseError) as caught:
                verify_hosted_release('https://' + host + '/release.json',
                    **dict(self.arguments, local_test=False, ssl_context=None))
            self.assertFalse(caught.exception.report['passed'])
            self.assertFalse(caught.exception.report['public_online_verified'])
        self.assertFalse(self.server.requests)

    def test_production_rejects_hostname_resolving_to_private_network(self):
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('scripts.verify_hosted_release.socket.getaddrinfo', return_value=resolved), \
                self.assertRaisesRegex(HostedReleaseError, '私有'):
            verify_hosted_release('https://publisher-unit-host.net/release.json',
                **dict(self.arguments, local_test=False, ssl_context=None))
        self.assertFalse(self.server.requests)

    def test_missing_changed_or_unknown_signature_rejected(self):
        missing = deepcopy(self.document); missing.pop('signature')
        changed = deepcopy(self.document); changed['release_notes'] = 'tampered'
        unknown = deepcopy(self.document)
        unknown['signing']['key_id'] = unknown['signature']['key_id'] = 'untrusted'
        for document in (missing, changed, unknown):
            self.set_document(document)
            with self.subTest(document=document.get('signing')), self.assertRaises(HostedReleaseError) as caught:
                self.verify()
            self.assertFalse(caught.exception.report['signature_verified'])
            self.assertTrue(caught.exception.report['temporary_files_removed'])
        self.assertNotIn('/release.zip', self.server.requests)

    def test_tampered_package_fails_real_byte_hash(self):
        self.server.routes['/release.zip'] = self.package[:-1] + bytes([self.package[-1] ^ 1])
        with self.assertRaisesRegex(HostedReleaseError, 'SHA-256') as caught:
            self.verify()
        self.assertTrue(caught.exception.report['signature_verified'])
        self.assertFalse(caught.exception.report['package_integrity_verified'])
        self.assertTrue(caught.exception.report['temporary_files_removed'])

    def test_signed_wrong_version_build_or_exe_rejected_before_package_download(self):
        cases = []
        for key, replacement in (('version', '2.0.1'), ('build_id', 'different-build')):
            value = deepcopy(self.document); value[key] = replacement; cases.append(value)
        value = deepcopy(self.document)
        next(entry for entry in value['files'] if entry['path'] == 'MediaFlow.exe')['sha256'] = 'b' * 64
        cases.append(value)
        for value in cases:
            self.set_document(value, sign=True)
            with self.subTest(value=value.get('version')), self.assertRaisesRegex(HostedReleaseError, '不一致') as caught:
                self.verify()
            self.assertTrue(caught.exception.report['signature_verified'])
            self.assertFalse(caught.exception.report['release_matches_expected'])
        self.assertNotIn('/release.zip', self.server.requests)

    def test_resigned_valid_zip_with_changed_tool_rejected_against_complete_release(self):
        replacement = b'CHANGED TOOL: still signed by the trusted fixture publisher'
        inner = deepcopy(self.document)
        inner.pop('package')
        tool = next(entry for entry in inner['files'] if entry['path'] == 'tools/ffmpeg.exe')
        tool.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
        inner['signature']['value'] = base64.b64encode(self.fixture.private.sign(canonical_bytes(inner))).decode('ascii')
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.package)) as source, zipfile.ZipFile(output, 'w') as destination:
            for info in source.infolist():
                value = (replacement if info.filename == 'tools/ffmpeg.exe' else
                         json.dumps(inner, ensure_ascii=False).encode('utf-8') if info.filename == 'release.json' else
                         source.read(info))
                destination.writestr(info, value)
        package = output.getvalue()
        outer = deepcopy(inner)
        outer['package'] = dict(self.document['package'], size=len(package), sha256=hashlib.sha256(package).hexdigest())
        outer['signature']['value'] = base64.b64encode(self.fixture.private.sign(canonical_bytes(outer))).decode('ascii')
        manager = UpdateManager(self.root/'standalone-inspection', self.fixture.release, self.fixture.stamp,
                                self.root/'never-opened-user-data')
        archive, _, verified = manager._inspect(io.BytesIO(package), {'trusted_keys':self.fixture.keys}, None, None, outer=outer)
        archive.close()
        self.assertTrue(verified['authenticated'])
        self.server.routes['/release.zip'] = package
        self.set_document(outer)
        with self.assertRaisesRegex(HostedReleaseError, '工具或资源') as caught:
            self.verify()
        self.assertTrue(caught.exception.report['signature_verified'])
        self.assertFalse(caught.exception.report['release_matches_expected'])
        self.assertFalse(caught.exception.report['package_integrity_verified'])
        self.assertEqual(self.server.requests, ['/release.json'])
        self.assertEqual((self.fixture.release/'tools/ffmpeg.exe').read_bytes(), b'UNIT TEST BYTES: tools/ffmpeg.exe')

    def test_final_complete_inventory_rejects_resource_changed_during_download(self):
        original = (self.fixture.release/'tools/ffprobe.exe').read_bytes()
        modified = False
        def progress(value):
            nonlocal modified
            if value['stage'] == 'integrity_verified' and not modified:
                modified = True
                (self.fixture.release/'tools/ffprobe.exe').write_bytes(b'X' * len(original))
        with self.assertRaisesRegex(HostedReleaseError, '工具或资源') as caught:
            self.verify(progress=progress)
        self.assertTrue(modified)
        self.assertTrue(caught.exception.report['package_integrity_verified'])
        self.assertFalse(caught.exception.report['release_matches_expected'])
        self.assertTrue(caught.exception.report['temporary_files_removed'])

    def test_oserror_does_not_serialize_credentials_urls_or_paths(self):
        error = OSError(13, 'network failure https://user:private-password@localhost/secret?token=private-query',
                        'C:\\Private\\never-report-key.pem')
        with patch('scripts.verify_hosted_release._file_hash', side_effect=error), \
                self.assertRaises(HostedReleaseError) as caught:
            self.verify()
        value = json.dumps(caught.exception.report)
        for secret in ('private-password', 'private-query', 'never-report-key.pem', 'C:\\\\Private'):
            self.assertNotIn(secret, value)
        self.assertEqual(caught.exception.report['error_type'], 'PermissionError')
        self.assertIn('PermissionError', caught.exception.report['error'])
        arguments = ['--manifest-url', self.server.url+'/release.json', '--release-dir', str(self.fixture.release),
            '--trusted-keys', str(self.fixture.keys_file), '--acceptance-receipt', str(self.fixture.receipt),
            '--report', str(self.root/'never-created.json')]
        with patch('scripts.verify_hosted_release._plain', side_effect=error), patch('builtins.print') as printed:
            self.assertEqual(main(arguments), 1)
        self.assertNotIn('private-password', str(printed.call_args))
        self.assertNotIn('never-report-key.pem', str(printed.call_args))

    def test_acceptance_report_change_refuses_network_and_unsigned_ca_bypass(self):
        self.fixture.report.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(HostedReleaseError, '变化'):
            self.verify()
        self.assertFalse(self.server.requests)
        invalid = ssl._create_unverified_context()
        with self.assertRaisesRegex(HostedReleaseError, '校验证书'):
            self.verify(ssl_context=invalid)
        with self.assertRaisesRegex(HostedReleaseError, '系统 TLS'):
            self.verify(local_test=False)

    def test_cancelled_transfer_cleans_only_own_temporary_files(self):
        event = threading.Event()
        def progress(value):
            if value['stage'] == 'download' and value['completed'] > 0:
                event.set()
        with self.assertRaises(HostedReleaseError) as caught:
            self.verify(cancel=event, progress=progress)
        self.assertTrue(caught.exception.report['cancelled'])
        self.assertTrue(caught.exception.report['temporary_files_removed'])
        self.assertTrue(self.fixture.report.exists())
        self.assertTrue((self.fixture.release / 'MediaFlow.exe').exists())

    def test_timeout_and_cross_origin_redirect_preserve_failure_state(self):
        self.server.routes['/release.json'] = lambda handler:self.server.stopped.wait(2)
        with self.assertRaisesRegex(HostedReleaseError, '总请求时限') as caught:
            self.verify(network_timeouts={'connect':1, 'check':.3, 'download':2})
        self.assertFalse(caught.exception.report['signature_verified'])
        self.assertTrue(caught.exception.report['temporary_files_removed'])
        self.set_document(self.document)
        self.server.routes['/release.zip'] = (302, b'', {'Location':'https://localhost:1/untrusted.zip'})
        with self.assertRaisesRegex(HostedReleaseError, '来源') as caught:
            self.verify()
        self.assertFalse(caught.exception.report['package_integrity_verified'])

    def test_cli_failure_report_is_sanitized_and_never_overwrites(self):
        output = self.root / 'public-check.json'
        arguments = ['--manifest-url', self.server.url+'/release.json?secret=do-not-report',
            '--release-dir', str(self.fixture.release), '--trusted-keys', str(self.fixture.keys_file),
            '--acceptance-receipt', str(self.fixture.receipt), '--report', str(output)]
        with patch('builtins.print'):
            self.assertEqual(main(arguments), 1)
        report = json.loads(output.read_text('utf-8'))
        self.assertFalse(report['passed'])
        self.assertFalse(report['public_online_verified'])
        self.assertNotIn('do-not-report', output.read_text('utf-8'))
        before = output.read_bytes()
        with patch('builtins.print'):
            self.assertEqual(main(arguments), 1)
        self.assertEqual(output.read_bytes(), before)

    def test_rejected_credential_url_does_not_put_credentials_in_report(self):
        with self.assertRaises(HostedReleaseError) as caught:
            verify_hosted_release('https://user:private-password@localhost/release.json?secret=private-query',
                                  **self.arguments)
        value = json.dumps(caught.exception.report)
        self.assertNotIn('private-password', value)
        self.assertNotIn('private-query', value)
        self.assertNotIn('user:', value)


if __name__ == '__main__':
    unittest.main()
