"""Real signed byte fixtures; these unit reports are not release acceptance."""
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.update_manager import UpdateManager
from core.update_manifest import REQUIRED_FILES, canonical_bytes, validate_manifest, UpdateValidationError
from scripts.package_update import create_package, PackageError
from scripts.publish_release import (PublishError, publish_release, create_acceptance_receipt,
                                     initialize_key_directory, ROOT)


class PublisherFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.release = self.root/'release'
        for name in REQUIRED_FILES:
            path = self.release/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'UNIT TEST BYTES: '+name.encode())
        self.stamp = {'app_version':'2.0.0','build_id':'test-only-build',
                      'source_sha256':'a'*64,'packaged':True}
        (self.release/'build-info.json').write_text(json.dumps(self.stamp),encoding='utf-8')
        self.password = b'unit-only-encrypted-test-passphrase'
        self.private = Ed25519PrivateKey.generate()
        self.private_file = self.root/'external-test-key.pem'
        self.private_file.write_bytes(self.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.BestAvailableEncryption(self.password)))
        self.keys = {'test-only': {'algorithm':'ed25519','public_key':base64.b64encode(
            self.private.public_key().public_bytes_raw()).decode('ascii')}}
        self.keys_file = self.root/'independent-public-keys.json'
        self.keys_file.write_text(json.dumps(self.keys),encoding='utf-8')
        self.report = self.root/'unit-only-report.json'
        identity = dict(self.stamp,executable=str(self.release/'MediaFlow.exe'))
        self.report.write_text(json.dumps({'passed':True,'runtime_identity':identity}),encoding='utf-8')
        self.receipt = self.root/'unit-only-receipt.json'
        create_acceptance_receipt(self.release,[self.report],self.receipt)

    def publish(self, **kwargs):
        options = dict(package_url='https://localhost/test-only.zip',published_at='2026-10-22T10:00:00Z',
            key_id='test-only',trusted_keys_file=self.keys_file,acceptance_receipt=self.receipt,
            private_key_file=self.private_file,password=self.password,
            release_notes='仅用于单元测试，不是正式发布凭据')
        options.update(kwargs)
        return publish_release(self.release,self.root/'output',**options)


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-publisher-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = PublisherFixture(self.root)

    def test_signed_zip_outer_manifest_and_detached_signature_match(self):
        before = {str(p.relative_to(self.fixture.release)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.fixture.release.rglob('*') if p.is_file()}
        result = self.fixture.publish()
        outer = json.loads(Path(result['manifest_path']).read_text('utf-8'))
        self.assertTrue(validate_manifest(outer,self.fixture.keys,require_signature=True)['authenticated'])
        self.assertEqual(Path(result['manifest_path']).read_bytes(),
                         json.dumps(outer,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
        self.assertEqual(hashlib.sha256(Path(result['package_path']).read_bytes()).hexdigest(),outer['package']['sha256'])
        self.assertEqual(Path(result['package_path']).stat().st_size,outer['package']['size'])
        detached = json.loads(Path(result['signature_path']).read_text('utf-8'))
        self.assertEqual(detached['signature'],outer['signature'])
        self.assertEqual(detached['manifest_sha256'],hashlib.sha256(canonical_bytes(outer)).hexdigest())
        with zipfile.ZipFile(result['package_path']) as archive:
            inner = json.loads(archive.read('release.json'))
            self.assertTrue(validate_manifest(inner,self.fixture.keys,True)['authenticated'])
            self.assertEqual({k:v for k,v in outer.items() if k not in ('signature','package')},
                             {k:v for k,v in inner.items() if k!='signature'})
            self.assertFalse(any('key' in name.lower() or name.endswith('.pem') for name in archive.namelist()))
        self.assertEqual(before,{str(p.relative_to(self.fixture.release)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in self.fixture.release.rglob('*') if p.is_file()})
        self.assertFalse(result['uploaded'])
        manager = UpdateManager(self.root/'updates', self.fixture.release, self.fixture.stamp,
                                self.root/'user-data', health_data_root=self.root/'new-data')
        manager.save_settings({'trusted_keys':self.fixture.keys})
        preview = manager.prepare_local(result['package_path'])
        self.assertTrue(preview['authenticated'])
        self.assertEqual(manager.deploy(preview['id'],confirmed=True)['state'],'deployed')

    def test_report_must_pass_and_bind_actual_executable(self):
        value = json.loads(self.fixture.report.read_text('utf-8'))
        for key,replacement in [('passed',False),('runtime_identity',dict(value['runtime_identity'],build_id='other'))]:
            modified = deepcopy(value)
            modified[key] = replacement
            self.fixture.report.write_text(json.dumps(modified),'utf-8')
            with self.subTest(key=key), self.assertRaises(PublishError):
                create_acceptance_receipt(self.fixture.release,[self.fixture.report],self.root/'receipt2.json')
        self.assertFalse((self.root/'receipt2.json').exists())

    def test_report_change_after_receipt_is_rejected(self):
        self.fixture.report.write_text(self.fixture.report.read_text('utf-8')+' ',encoding='utf-8')
        with self.assertRaisesRegex(PublishError,'发生变化'):
            self.fixture.publish()
        self.assertFalse((self.root/'output').exists())

    def test_changed_executable_rejected(self):
        (self.fixture.release/'MediaFlow.exe').write_bytes(b'different')
        with self.assertRaises(PublishError):
            self.fixture.publish()

    def test_wrong_key_or_untrusted_id_rejected(self):
        for key_id in ('missing','test-only'):
            if key_id == 'test-only':
                keys = deepcopy(self.fixture.keys)
                keys[key_id]['public_key'] = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
                self.fixture.keys_file.write_text(json.dumps(keys),'utf-8')
            with self.subTest(key_id=key_id), self.assertRaises(PublishError):
                self.fixture.publish(key_id=key_id)
        self.assertFalse((self.root/'output').exists())

    def test_private_material_inside_project_or_release_never_opened(self):
        for path in (ROOT/'never-create-this-key.pem',self.fixture.release/'private-key.pem'):
            with self.subTest(path=str(path)),self.assertRaisesRegex(PublishError,'私钥必须'):
                self.fixture.publish(private_key_file=path)
            self.assertFalse(path.exists())

    def test_unencrypted_key_wrong_password_and_missing_environment_rejected(self):
        with self.assertRaisesRegex(PublishError,'解密'):
            self.fixture.publish(password=b'wrong password')
        self.fixture.private_file.write_bytes(self.fixture.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        with self.assertRaisesRegex(PublishError,'加密 PKCS8'):
            self.fixture.publish()
        with self.assertRaises(PublishError):
            self.fixture.publish(private_key_file=None,private_key_env='MEDIAFLOW_TEST_NONEXISTENT_PRIVATE_ENV')

    def test_existing_output_not_overwritten_and_illegal_url_not_published(self):
        with self.assertRaises(UpdateValidationError):
            self.fixture.publish(package_url='http://localhost/not-https.zip')
        self.assertFalse((self.root/'output').exists())
        (self.root/'output').mkdir()
        marker = self.root/'output/keep.txt'
        marker.write_bytes(b'keep')
        with self.assertRaises(PublishError):
            self.fixture.publish()
        self.assertEqual(marker.read_bytes(),b'keep')

    def test_explicit_initialization_encrypted_external_and_never_replaced(self):
        result = initialize_key_directory(self.root/'explicit-test-keys',key_id='new-test-key',password=self.fixture.password)
        private_path = Path(result['private_key_file'])
        self.assertTrue(private_path.read_bytes().startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'))
        key = serialization.load_pem_private_key(private_path.read_bytes(),self.fixture.password)
        public = json.loads(Path(result['trusted_keys_file']).read_text('utf-8'))['trusted_keys']['new-test-key']
        self.assertEqual(public['public_key'],base64.b64encode(key.public_key().public_bytes_raw()).decode())
        before = private_path.read_bytes()
        with self.assertRaises(PublishError):
            initialize_key_directory(self.root/'explicit-test-keys',key_id='new-test-key',password=self.fixture.password)
        self.assertEqual(private_path.read_bytes(),before)

    def test_signing_hook_cannot_change_signed_inventory(self):
        def malicious(value):
            value['version'] = '2.0.1'
            return value
        with self.assertRaisesRegex(PackageError,'签名钩子'):
            create_package(self.fixture.release,self.root/'bad.zip',sign_manifest=malicious)
        self.assertFalse((self.root/'bad.zip').exists())

    def test_original_crypto_license_names_remain_in_package(self):
        for name in ('LICENSE.APACHE','LICENSE.BSD'):
            path=self.fixture.release/'licenses/cryptography'/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'original license bytes')
        result=self.fixture.publish()
        with zipfile.ZipFile(result['package_path']) as archive:
            self.assertEqual(archive.read('licenses/cryptography/LICENSE.APACHE'),b'original license bytes')


if __name__ == '__main__':
    unittest.main()
