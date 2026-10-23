"""Local HTTPS integration: real TLS, Ed25519, ZIP bytes and safe deployment."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from core.update_manager import UpdateError, UpdateCancelled, UpdateManager
from core.update_manifest import canonical_bytes
from tests.test_publish_release import PublisherFixture
from tests.update_https_fixture import HTTPSFixture


class OnlineUpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-online-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = HTTPSFixture()
        self.addCleanup(self.server.close)
        self.fixture = PublisherFixture(self.root)
        result = self.fixture.publish(package_url=self.server.url+'/release.zip')
        self.document = result['manifest']
        self.zip = Path(result['package_path']).read_bytes()
        self.server.routes['/release.zip'] = self.zip
        self.set_document(self.document)
        self.data = self.root/'old-data'
        self.data.mkdir()
        (self.data/'unchanged.bin').write_bytes(b'user media and opaque recovery data')
        self.manager = UpdateManager(self.root/'Updates', self.fixture.release,
            dict(self.fixture.stamp,app_version='1.9.0'), self.data,
            ssl_context=self.server.context,health_data_root=self.root/'new-data',
            network_timeouts={'connect':.5,'check':6,'download':6})
        self.manager.save_settings({'source_url':self.server.url+'/release.json','trusted_keys':self.fixture.keys})

    def set_document(self, value, sign=False):
        value = deepcopy(value)
        if sign:
            value['signature']['value'] = base64.b64encode(self.fixture.private.sign(canonical_bytes(value))).decode('ascii')
        self.server.routes['/release.json'] = json.dumps(value,ensure_ascii=False).encode('utf-8')
        return value

    def test_signed_https_download_and_independent_deployment(self):
        original = (self.data/'unchanged.bin').read_bytes()
        values=[]
        result = self.manager.check_updates(progress=values.append)
        self.assertEqual(result['status'],'update_available')
        self.assertTrue(result['authenticated'])
        self.assertIn('signature_verified',[v['stage'] for v in values])
        preview = self.manager.download_update(result['manifest'],progress=values.append)
        self.assertTrue(preview['authenticated'])
        self.assertFalse(Path(preview['target_path']).exists())
        deployed = self.manager.deploy(preview['id'],confirmed=True)
        self.assertEqual(deployed['state'],'deployed')
        self.assertFalse(self.manager._launched)
        self.assertEqual((self.data/'unchanged.bin').read_bytes(),original)
        self.assertEqual(self.manager.deploy(preview['id'],confirmed=True)['id'],preview['id'])
        self.assertTrue(self.fixture.release.is_dir())

    def test_missing_tampered_or_untrusted_signature_never_creates_install(self):
        cases=[]
        missing=deepcopy(self.document);missing.pop('signature');cases.append(missing)
        tampered=deepcopy(self.document);tampered['release_notes']='modified';cases.append(tampered)
        untrusted=deepcopy(self.document);untrusted['signature']['key_id']='unknown';cases.append(untrusted)
        for value in cases:
            self.set_document(value)
            with self.subTest(signature=value.get('signature',{}).get('key_id')), self.assertRaises(UpdateError):
                self.manager.check_updates()
            self.assertFalse(self.manager.list_versions())
        self.assertFalse((self.manager.state_dir/'versions').exists())

    def test_bad_hash_and_corrupt_zip_discard_only_owned_download(self):
        preserve = self.manager.state_dir/'downloads/user-file.txt'
        preserve.parent.mkdir()
        preserve.write_bytes(b'keep non-operation file')
        mismatch = deepcopy(self.document)
        mismatch['package']['sha256']='0'*64
        mismatch=self.set_document(mismatch,sign=True)
        with self.assertRaisesRegex(UpdateError,'SHA-256'):
            self.manager.download_update(mismatch)
        self.assertFalse(list(preserve.parent.glob('*.zip')))
        corrupt=b'not a zip'
        value=deepcopy(self.document)
        value['package'].update(size=len(corrupt),sha256=hashlib.sha256(corrupt).hexdigest())
        value=self.set_document(value,sign=True)
        self.server.routes['/release.zip']=corrupt
        with self.assertRaises(UpdateError):
            self.manager.download_update(value)
        self.assertFalse(list(preserve.parent.glob('*.zip')))
        self.assertEqual(preserve.read_bytes(),b'keep non-operation file')

    def test_cancel_after_transfer_and_read_failure_leave_no_partial_package(self):
        event=threading.Event()
        def progress(value):
            if value['stage']=='download' and value['completed']>0:
                event.set()
        with self.assertRaises(UpdateCancelled):
            self.manager.download_update(self.document,cancel=event,progress=progress)
        self.assertFalse(list((self.manager.state_dir/'downloads').glob('*.zip')))
        self.assertFalse(self.manager.list_versions())

    def test_cross_origin_redirect_not_followed(self):
        self.server.routes['/release.zip']=(302,b'',{'Location':'https://localhost:1/not-trusted.zip'})
        with self.assertRaisesRegex(UpdateError,'来源'):
            self.manager.download_update(self.document)
        self.assertFalse(list((self.manager.state_dir/'downloads').glob('*.zip')))

    def test_total_timeout_is_explicit_failure_and_saved_status(self):
        self.server.routes['/release.json']=lambda h:self.server.stopped.wait(3)
        self.manager.network_timeouts.update(connect=3,check=.35)
        with self.assertRaisesRegex(UpdateError,'总请求时限'):
            self.manager.check_updates()
        settings=self.manager.load_settings()
        self.assertTrue(settings['last_checked'])
        self.assertIn('总请求时限',settings['last_status'])

    def test_equal_and_older_versions_use_semantic_comparison(self):
        self.manager.current_identity.update(app_version=self.document['version'],build_id=self.document['build_id'])
        self.assertEqual(self.manager.check_updates()['status'],'up_to_date')
        self.manager.current_identity['app_version']='2.10.0'
        self.assertEqual(self.manager.check_updates()['status'],'older_available')


if __name__ == '__main__':
    unittest.main()
