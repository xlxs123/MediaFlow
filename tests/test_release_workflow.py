"""Executable release gates and Windows ACL tests; never production credentials."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.update_channel import key_fingerprint
from scripts.publish_release import PublishError
from utils.branding import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('mediaflow_release_gates', ROOT / 'release-workflows/release_gates.py')
gates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gates)


def powershell():
    options = [shutil.which('pwsh'), Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe',
               Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'PowerShell/7/pwsh.exe']
    return next((str(path) for path in options if path and Path(path).is_file()), None)


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-release-gates-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / 'project'
        (self.project / 'assets').mkdir(parents=True)
        self.key = Ed25519PrivateKey.generate()
        self.key_id = 'unit-test-key-never-production'
        self.public = {'algorithm': 'ed25519', 'public_key': base64.b64encode(self.key.public_key().public_bytes_raw()).decode('ascii')}
        self.keys = self.directory / 'public.json'
        self.keys.write_text(json.dumps({'trusted_keys': {self.key_id: self.public}}), encoding='utf-8')
        self.channel = self.project / 'assets/update-channel.json'
        self.value = {'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
            'source_url': 'https://publisher.unit-fixture.org/release.json',
            'trusted_keys': {self.key_id: self.public}, 'key_state': 'initialized'}
        self.channel.write_text(json.dumps(self.value), encoding='utf-8')
        self.private = self.directory / 'encrypted-unit-key.pem'
        self.password = secrets.token_urlsafe(32)
        self.private.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(self.password.encode())))
        self.environment = 'MEDIAFLOW_WORKFLOW_UNIT_TEST_PASSWORD'
        self.environment_patch = patch.dict(os.environ, {self.environment: self.password})
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.arguments = dict(expected_version=APP_VERSION, channel_file=self.channel,
            trusted_keys_file=self.keys, key_id=self.key_id,
            package_url='https://publisher.unit-fixture.org/MediaFlow-unit-fixture.zip',
            release_dir=self.directory / 'new-release', output_dir=self.directory / 'signed-output',
            verification_dir=self.directory / 'verification', private_key_file=self.private,
            password_env=self.environment, project_root=self.project)

    def test_valid_preflight_matches_real_encrypted_key_and_creates_nothing(self):
        value = gates.preflight(**self.arguments)
        self.assertTrue(value['passed'])
        self.assertEqual(value['key_fingerprint'], key_fingerprint(self.public))
        self.assertFalse(value['uploaded'])
        self.assertEqual(value['public_verification'], 'pending')
        for field in ('release_dir', 'output_dir', 'verification_dir'):
            self.assertFalse(self.arguments[field].exists())

    def test_pending_configuration_stops_before_private_key_or_outputs(self):
        self.channel.write_text(json.dumps(dict(self.value, key_state='pending', source_url='', trusted_keys={})), encoding='utf-8')
        with patch.object(gates, '_private_key', side_effect=AssertionError('must not read secret')), \
                self.assertRaisesRegex(gates.ReleaseGateError, '待初始化'):
            gates.preflight(**self.arguments)
        self.assertFalse(self.arguments['output_dir'].exists())

    def test_different_client_channel_rejected_before_signing(self):
        other = self.directory / 'different-channel.json'
        other.write_text(json.dumps(dict(self.value, source_url='https://other.unit-fixture.org/release.json')), encoding='utf-8')
        with self.assertRaisesRegex(gates.ReleaseGateError, '即将嵌入'):
            gates.preflight(**dict(self.arguments, channel_file=other))

    def test_wrong_publisher_key_cannot_publish_to_pinned_client(self):
        self.keys.write_text(json.dumps({'trusted_keys': {self.key_id: dict(self.public,
            public_key=base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode())}}), encoding='utf-8')
        with patch.object(gates, '_private_key', side_effect=AssertionError('must not decrypt')), \
                self.assertRaisesRegex(gates.ReleaseGateError, '内置公钥不同'):
            gates.preflight(**self.arguments)

    def test_same_public_key_with_other_encrypted_private_key_is_rejected(self):
        other = Ed25519PrivateKey.generate()
        self.private.write_bytes(other.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(self.password.encode())))
        with self.assertRaisesRegex(gates.ReleaseGateError, '私钥.*不匹配'):
            gates.preflight(**self.arguments)

    def test_existing_build_requires_reports_and_new_output_paths(self):
        self.arguments['release_dir'].mkdir()
        with self.assertRaisesRegex(gates.ReleaseGateError, '已经存在'):
            gates.preflight(**self.arguments)
        with self.assertRaisesRegex(gates.ReleaseGateError, '至少一份'):
            gates.preflight(**dict(self.arguments, existing=True))
        self.arguments['output_dir'].mkdir()
        with self.assertRaisesRegex(gates.ReleaseGateError, '尚不存在'):
            gates.preflight(**self.arguments)

    def test_paths_cannot_overlap_and_test_urls_cannot_be_production(self):
        with self.assertRaisesRegex(gates.ReleaseGateError, '互相独立'):
            gates.preflight(**dict(self.arguments, output_dir=self.arguments['release_dir'] / 'output'))
        for value in ('http://public.unit-fixture.org/release.zip', 'https://localhost/release.zip',
                      'https://127.0.0.1/release.zip', 'https://10.0.0.1/release.zip',
                      'https://example.com/release.zip', 'https://sub.example.net/release.zip',
                      'https://publisher.invalid/release.zip'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                gates.public_url(value)

    def test_wrong_version_and_prerelease_stable_are_rejected(self):
        for value in ('2.0.0', '2.0.1rc1', '2.0.1+test-only'):
            with self.subTest(value=value), self.assertRaises(gates.ReleaseGateError):
                gates.preflight(**dict(self.arguments, expected_version=value))

    def test_actual_exe_identity_must_have_exact_channel_and_build(self):
        release = self.arguments['release_dir']
        release.mkdir()
        executable = release / 'MediaFlow.exe'
        executable.write_bytes(b'UNIT TEST FIXTURE; NOT A REAL ACCEPTANCE EXE')
        stamp = {'packaged': True, 'app_version': APP_VERSION, 'build_id': 'workflow-unit-only', 'source_sha256': 'a' * 64}
        (release / 'build-info.json').write_text(json.dumps(stamp), encoding='utf-8')
        identity = dict(stamp, executable=str(executable), update_channel={
            'key_state': 'initialized', 'source_sha256': hashlib.sha256(self.value['source_url'].encode()).hexdigest(),
            'key_fingerprints': {self.key_id: key_fingerprint(self.public)}})
        report = self.directory / 'identity.json'
        report.write_text(json.dumps(identity), encoding='utf-8')
        arguments = dict(identity_file=report, release_dir=release,
            channel_file=self.channel, expected_version=APP_VERSION)
        self.assertEqual(gates.check_runtime(**arguments)['exe_sha256'], hashlib.sha256(executable.read_bytes()).hexdigest())
        identity['update_channel']['key_fingerprints'][self.key_id] = 'f' * 64
        report.write_text(json.dumps(identity), encoding='utf-8')
        with self.assertRaisesRegex(gates.ReleaseGateError, '实际启动'):
            gates.check_runtime(**arguments)

    @unittest.skipUnless(os.name == 'nt', 'PowerShell/Windows ACL contract')
    def test_powershell_pipeline_pending_gate_executes_without_creating_outputs(self):
        shell = powershell()
        self.assertIsNotNone(shell, 'Windows release tests require PowerShell 7')
        pending = self.directory / 'pending.json'
        pending.write_text(json.dumps(dict(self.value, key_state='pending', source_url='', trusted_keys={})), encoding='utf-8')
        command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
            str(ROOT / 'scripts/release_production.ps1'), '-ExpectedVersion', APP_VERSION,
            '-ChannelConfiguration', str(pending), '-TrustedKeysFile', str(self.keys), '-KeyId', self.key_id,
            '-PackageUrl', self.arguments['package_url'], '-OutputDirectory', str(self.arguments['output_dir']),
            '-VerificationDirectory', str(self.arguments['verification_dir']),
            '-PasswordEnvironment', self.environment, '-PrivateKeyFile', str(self.private), '-Python', sys.executable]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn('待初始化', text)
        self.assertNotIn(self.password, text)
        self.assertFalse(self.arguments['output_dir'].exists())
        self.assertFalse(self.arguments['verification_dir'].exists())

    @unittest.skipUnless(os.name == 'nt', 'PowerShell/Windows ACL contract')
    def test_explicit_initializer_uses_restricted_acl_and_encrypted_test_key(self):
        shell = powershell()
        self.assertIsNotNone(shell)
        vault = self.directory / 'external-unit-vault'
        command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
            str(ROOT / 'scripts/initialize_release_key.ps1'), '-ExternalDirectory', str(vault),
            '-KeyId', self.key_id, '-PasswordEnvironment', self.environment, '-Python', sys.executable]
        before = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(before.returncode, 0)
        self.assertFalse(vault.exists(), 'No key generation without explicit confirmation')
        result = subprocess.run(command + ['-ConfirmInitialize'], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn(self.password, output)
        private = vault / 'keys/private-key.encrypted.pem'
        self.assertTrue(private.read_bytes().startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'))
        key = serialization.load_pem_private_key(private.read_bytes(), self.password.encode())
        public = json.loads((vault / 'keys/trusted-public-keys.json').read_text('utf-8'))['trusted_keys'][self.key_id]
        self.assertEqual(public['public_key'], base64.b64encode(key.public_key().public_bytes_raw()).decode())
        # The wrapper inspected every actual child ACL before reporting success.
        self.assertIn('"acl_restricted": true', output)
        self.assertIn('"production_trust_configured": false', output)
        before = private.read_bytes()
        again = subprocess.run(command + ['-ConfirmInitialize'], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(private.read_bytes(), before, 'Initialization never replaces an existing key')


if __name__ == '__main__':
    unittest.main()
