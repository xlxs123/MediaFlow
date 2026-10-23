"""Real ZIP bytes, isolated files and native signatures; no website downloads."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from core.update_manager import UpdateManager, UpdateError, UpdateCancelled, _atomic_json, _json_bytes
from core.update_manifest import REQUIRED_FILES, canonical_bytes, UpdateValidationError
from tests.test_update_manifest import NativeSigningKey


def package(path, *, version='1.9.0', build='test-build', mutations=None, extra=None, key=None):
    files = {name:b'MZ\0test-tool-' + name.encode() for name in REQUIRED_FILES}
    files['build-info.json'] = json.dumps({'app_version':version,'build_id':build,'packaged':True}).encode()
    files['README.md'] = '本地测试发布包'.encode()
    manifest = {'schema_version':1,'product':'MediaFlow','version':version,'build_id':build,
        'platform':'windows','architecture':'x86_64','release_notes':'安全更新测试','release_page':'',
        'minimum_app_version':'1.8.0','data_migration':'separate',
        'files':[{'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()]}
    if mutations:
        mutations(manifest, files)
    if key:
        key.sign(manifest)
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('release.json',json.dumps(manifest,ensure_ascii=False))
        for name,data in files.items():
            archive.writestr(name,data)
        for name,data in (extra or []):
            archive.writestr(name,data)
    return manifest


def hashes(directory):
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


class UpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='MediaFlow-update-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.old=self.root/'old-release'; self.old.mkdir()
        (self.old/'MediaFlow.exe').write_bytes(b'MZ-old-untouched')
        self.data=self.root/'old-data'; self.data.mkdir()
        (self.data/'config.json').write_text('{"output_directory":"unchanged"}')
        (self.data/'tasks.json').write_bytes(b'real-old-data-must-not-be-read-or-overwritten')
        self.manager=UpdateManager(self.root/'updates',self.old,
            {'app_version':'1.8.0','build_id':'old'},self.data,health_data_root=self.root/'new-data')
        self.zip=self.root/'release.zip'
        package(self.zip)

    def prepare(self):
        return self.manager.prepare_local(self.zip)

    def deploy(self, preview=None, **kwargs):
        return self.manager.deploy((preview or self.prepare())['id'],confirmed=True,allow_unsigned=True,**kwargs)

    def test_no_source_is_not_latest(self):
        value=self.manager.check_updates()
        self.assertEqual(value['status'],'not_configured')
        self.assertEqual(value['last_checked'],'')
        self.assertFalse(self.manager.load_settings()['startup_check'])

    def test_cache_name_collision_preserves_existing_file(self):
        cache=self.manager.state_dir/'downloads'/('c'*32+'.zip')
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'pre-existing file must be preserved')
        with patch('core.update_manager.uuid.uuid4',return_value=type('Identifier',(),{'hex':'c'*32})()):
            with self.assertRaises(UpdateError):
                self.prepare()
        self.assertEqual(cache.read_bytes(),b'pre-existing file must be preserved')

    def test_default_ed25519_settings_do_not_change_legacy_rsa_state(self):
        legacy=self.root/'local/MediaFlow/Updates/settings.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'legacy RSA settings remain byte-identical')
        with patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'local')}):
            manager=UpdateManager(release_root=self.old,current_identity={'app_version':'2.0.0','build_id':'new'},
                                  current_data_dir=self.data)
            self.assertEqual(manager.state_dir,self.root/'local/MediaFlow/Updates-v2')
            manager.save_settings({'source_url':''})
            self.assertEqual(legacy.read_bytes(),b'legacy RSA settings remain byte-identical')

    def test_normal_2x_rejects_unsigned_local_and_cannot_bypass_with_confirmation(self):
        legacy=self.prepare()
        self.manager.current_identity['app_version']='2.0.0'
        with self.assertRaisesRegex(UpdateError,'缺少可信发布者签名'):
            self.manager.prepare_local(self.zip)
        with self.assertRaisesRegex(UpdateValidationError,'缺少可信发布者签名'):
            self.manager.result(legacy['id'])
        with self.assertRaisesRegex(UpdateError,'缺少可信发布者签名'):
            self.manager.deploy(legacy['id'],confirmed=True,allow_unsigned=True)

    def test_local_deploy_preserves_old_application_and_data(self):
        before=hashes(self.old),hashes(self.data)
        preview=self.prepare()
        self.assertFalse(preview['authenticated'])
        self.assertFalse(Path(preview['target_path']).exists())
        with self.assertRaisesRegex(UpdateError,'确认'):
            self.manager.deploy(preview['id'])
        with self.assertRaisesRegex(UpdateError,'身份认证'):
            self.manager.deploy(preview['id'],confirmed=True)
        result=self.deploy(preview)
        self.assertEqual(result['state'],'deployed')
        self.assertEqual(before,(hashes(self.old),hashes(self.data)))
        self.assertTrue((Path(result['target_path'])/'tools/ffmpeg.exe').exists())
        self.assertEqual(self.manager.deploy(preview['id'],True,True)['state'],'deployed')

    def test_deployed_application_can_manage_versions(self):
        deployed=self.deploy()
        other=UpdateManager(self.root/'updates',deployed['target_path'],
                           {'app_version':'1.9.0','build_id':'test-build'},self.root/'new-data/1.9.0')
        self.assertEqual(other.list_versions()[0]['id'],deployed['id'])
        with self.assertRaises(UpdateError):
            UpdateManager(self.old/'updates',self.old,current_data_dir=self.data)

    def test_restart_and_repeated_operation_does_not_copy_again(self):
        deployed=self.deploy()
        before=hashes(Path(deployed['target_path']))
        again=UpdateManager(self.root/'updates',self.old,{'app_version':'1.8.0','build_id':'old'},self.data)
        self.assertEqual(len(again.list_versions()),1)
        self.assertEqual(again.deploy(deployed['id'],True,True)['state'],'deployed')
        self.assertEqual(before,hashes(Path(deployed['target_path'])))

    def test_independent_import_same_target_cannot_overwrite(self):
        first=self.deploy()
        second=self.prepare()
        with self.assertRaisesRegex(UpdateError,'已经存在'):
            self.deploy(second)
        self.assertTrue(Path(first['target_path']).exists())

    def test_corrupt_zip_and_missing_required_tool(self):
        self.zip.write_bytes(b'not-a-zip')
        with self.assertRaises(UpdateError): self.prepare()
        package(self.zip,mutations=lambda manifest,files:files.pop('tools/ffmpeg.exe'))
        with self.assertRaisesRegex(UpdateError,'缺失|不一致'): self.prepare()

    def test_hash_mismatch_and_changed_cache_rejected(self):
        package(self.zip,mutations=lambda manifest,files:files.__setitem__('README.md',b'changed'))
        with self.assertRaisesRegex(UpdateError,'不一致|不匹配'): self.prepare()
        package(self.zip); preview=self.prepare()
        cache=self.manager.state_dir/'downloads'/(preview['id']+'.zip')
        content=bytearray(cache.read_bytes());content[-1]^=1;cache.write_bytes(content)
        with self.assertRaisesRegex(UpdateError,'发生变化'): self.deploy(preview)
        self.assertFalse(Path(preview['target_path']).exists())

    def test_bad_zip_paths_duplicate_and_symlink(self):
        bad_names=['../outside.txt','/outside.txt','C:/outside.txt','tools/../outside.txt',
                   'tools\\outside.txt','tools/ffmpeg.exe','TOOLS/FFMPEG.EXE','CON.txt','bad\x00.txt']
        for name in bad_names:
            with self.subTest(name=name):
                package(self.zip,extra=[(name,b'bad')])
                with self.assertRaises(UpdateError): self.prepare()
        link=zipfile.ZipInfo('README-link.txt');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        package(self.zip,extra=[(link,b'../target')])
        with self.assertRaisesRegex(UpdateError,'链接'): self.prepare()
        self.assertFalse((self.root/'outside.txt').exists())

    def test_user_data_inside_package_is_rejected(self):
        for name in ['config.json','tasks.json','history.sqlite3','BrowserProfile/Cookies','tools/private-key.pem']:
            with self.subTest(name=name):
                def change(manifest,files):
                    data=b'forbidden';files[name]=data
                    manifest['files'].append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                package(self.zip,mutations=change)
                with self.assertRaises(UpdateError): self.prepare()

    def test_product_platform_schema_and_json_duplicates(self):
        for field,value in [('product','Other'),('platform','linux'),('architecture','arm64'),('schema_version',2)]:
            with self.subTest(field=field):
                package(self.zip,mutations=lambda manifest,files:manifest.__setitem__(field,value))
                with self.assertRaises(UpdateError): self.prepare()
        with self.assertRaises(UpdateError): _json_bytes(b'{"version":"1.9.0","version":"1.10.0"}')

    def test_downgrade_requires_separate_confirmation(self):
        self.manager.current_identity['app_version']='1.10.0'
        preview=self.prepare();self.assertTrue(preview['is_downgrade'])
        with self.assertRaisesRegex(UpdateError,'降级'): self.deploy(preview)
        self.assertEqual(self.deploy(preview,allow_downgrade=True)['state'],'deployed')

    def test_disk_full_prevents_deployment(self):
        preview=self.prepare()
        with patch('core.update_manager.shutil.disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaisesRegex(UpdateError,'空间不足'): self.deploy(preview)
        self.assertFalse(Path(preview['target_path']).exists())

    def test_cancel_extract_retains_old_data_and_can_retry(self):
        preview=self.prepare();event=threading.Event();before=hashes(self.data)
        def progress(value):
            if value['stage']=='extract':event.set()
        with self.assertRaises(UpdateCancelled): self.deploy(preview,cancel=event,progress=progress)
        result=self.manager.result(preview['id'])
        self.assertEqual(result['state'],'cancelled')
        self.assertFalse(Path(preview['target_path']).exists())
        self.assertEqual(hashes(self.data),before)
        self.assertFalse(Path(result['staging_path']).exists())
        self.assertEqual(self.deploy(preview)['state'],'deployed')

    def test_interruption_before_and_after_atomic_commit(self):
        preview=self.prepare()
        def interrupted(source,target): raise KeyboardInterrupt('test process interruption')
        with patch('core.update_manager.os.rename',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.deploy(preview)
        recovered=self.manager.recover_interrupted()
        self.assertEqual(recovered[0]['state'],'interrupted')
        self.deploy(preview)
        record=self.manager.result(preview['id']);record['state']='deploying';self.manager._store(record)
        self.assertEqual(self.manager.recover_interrupted()[0]['state'],'deployed')

    def test_target_appearing_during_extract_is_not_overwritten(self):
        preview=self.prepare();target=Path(preview['target_path'])
        def progress(value):
            if value['stage']=='extract' and not target.exists():
                target.mkdir(parents=True);(target/'keep.txt').write_text('user-data')
        with self.assertRaisesRegex(UpdateError,'目标目录'): self.deploy(preview,progress=progress)
        self.assertEqual((target/'keep.txt').read_text(),'user-data')

    def test_record_write_failure_after_commit_is_recoverable(self):
        preview=self.prepare();original=self.manager._store
        failed=[False]
        def store(record):
            if record['state']=='deployed' and not failed[0]:
                failed[0]=True
                raise OSError('simulated transient record write failure')
            original(record)
        with patch.object(self.manager,'_store',side_effect=store):
            with self.assertRaises(UpdateError): self.deploy(preview)
        self.assertTrue(Path(preview['target_path']).is_dir())
        self.assertEqual(self.manager.recover_interrupted()[0]['state'],'deployed')
        self.assertEqual(self.manager.deploy(preview['id'],True,True)['state'],'deployed')

    def test_byte_progress_covers_exactly_the_processed_members(self):
        events=[]
        preview=self.manager.prepare_local(self.zip,progress=events.append)
        self.manager.deploy(preview['id'],True,True,progress=events.append)
        for stage in ('verify','extract'):
            values=[value for value in events if value['stage']==stage and value['total']]
            self.assertTrue(values)
            self.assertTrue(all(value['completed']<=value['total'] for value in values))
            self.assertEqual(values[-1]['completed'],values[-1]['total'])

    def test_changed_deployed_file_blocks_launch(self):
        result=self.deploy()
        with self.assertRaisesRegex(UpdateError,'健康检查'): self.manager.launch_new(result['id'],True)
        with self.assertRaisesRegex(UpdateError,'确认'): self.manager.check_health(result['id'])
        (Path(result['target_path'])/'README.md').write_text('tampered')
        with self.assertRaisesRegex(UpdateError,'大小|摘要'): self.manager.check_health(result['id'],True)

    def test_launch_failure_keeps_old_launch_available(self):
        result=self.deploy();before=hashes(self.data)
        health=self.manager.check_health(result['id'],True)
        self.assertFalse(health['passed'])
        self.assertEqual(self.manager.result(result['id'])['state'],'health_failed')
        with patch('core.update_manager.subprocess.Popen') as popen:
            popen.return_value.pid=123
            old=self.manager.launch_previous(result['id'],True)
            self.assertEqual(popen.call_args.args[0],[str(self.old/'MediaFlow.exe')])
            self.assertTrue(old['started'])
        self.assertEqual(hashes(self.data),before)

    def test_independent_instance_environment_does_not_modify_parent(self):
        result=self.deploy()
        self.manager.launch_env={'APPDATA':str(self.data),'PYINSTALLER_RESET_ENVIRONMENT':'0'}
        with patch('core.update_manager.subprocess.Popen') as popen:
            popen.return_value.pid=123
            self.manager.launch_previous(result['id'],True)
            self.assertEqual(popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'],'1')
            self.assertEqual(popen.call_args.kwargs['env']['APPDATA'],str(self.data))
        self.assertEqual(self.manager.launch_env['PYINSTALLER_RESET_ENVIRONMENT'],'0')

    def test_signed_local_and_online_release_comparison(self):
        key=NativeSigningKey();self.addCleanup(key.close)
        self.manager.save_settings({'trusted_keys':{'unit-ephemeral':key.public},'source_url':'https://updates.invalid/manifest.json'})
        for version,status in [('1.8.0','up_to_date'),('1.9.0','update_available'),('1.10.0','update_available')]:
            manifest=package(self.zip,version=version,build='old' if version=='1.8.0' else 'new')
            manifest['package']={'format':'zip','url':'https://updates.invalid/package.zip','size':self.zip.stat().st_size,
                                 'sha256':hashlib.sha256(self.zip.read_bytes()).hexdigest()}
            key.sign(manifest)
            with patch.object(self.manager,'_fetch',return_value=json.dumps(manifest).encode()):
                self.assertEqual(self.manager.check_updates()['status'],status)
        package(self.zip,key=key)
        preview=self.prepare();self.assertTrue(preview['authenticated'])
        self.assertEqual(self.manager.deploy(preview['id'],confirmed=True)['state'],'deployed')

    def test_online_timeout_invalid_json_and_unsigned_rejected(self):
        self.manager.save_settings({'source_url':'https://updates.invalid/manifest.json'})
        with patch('core.update_manager.fetch_https',side_effect=__import__('core.update_transport', fromlist=['TransportError']).TransportError('更新连接超时')):
            with self.assertRaisesRegex(UpdateError,'超时'): self.manager.check_updates()
        self.assertTrue(self.manager.load_settings()['last_checked'])
        with patch.object(self.manager,'_fetch',return_value=b'not-json'):
            with self.assertRaisesRegex(UpdateError,'JSON'): self.manager.check_updates()
        manifest=package(self.zip)
        with patch.object(self.manager,'_fetch',return_value=json.dumps(manifest).encode()):
            with self.assertRaisesRegex(UpdateError,'签名'): self.manager.check_updates()
        with self.assertRaisesRegex(UpdateError,'HTTPS'): self.manager.save_settings({'source_url':'http://example.invalid'})

    def test_signed_download_verifies_real_zip_and_deploys(self):
        key=NativeSigningKey();self.addCleanup(key.close)
        self.manager.save_settings({'trusted_keys':{'unit-ephemeral':key.public}})
        outer=package(self.zip)
        payload=self.zip.read_bytes()
        outer['package']={'format':'zip','url':'https://updates.invalid/release.zip',
                          'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        key.sign(outer)
        def fetch(url,**kwargs):
            kwargs['destination'].write(payload)
            return len(payload),hashlib.sha256(payload).hexdigest()
        with patch.object(self.manager,'_fetch',side_effect=fetch):
            preview=self.manager.download_update(outer)
        self.assertTrue(preview['authenticated'])
        self.assertEqual(self.manager.deploy(preview['id'],confirmed=True)['state'],'deployed')

    def test_signed_download_rejects_outer_hash_or_different_inner_release(self):
        key=NativeSigningKey();self.addCleanup(key.close)
        self.manager.save_settings({'trusted_keys':{'unit-ephemeral':key.public}})
        outer=package(self.zip)
        payload=self.zip.read_bytes();digest=hashlib.sha256(payload).hexdigest()
        outer['package']={'format':'zip','url':'https://updates.invalid/release.zip',
                          'size':len(payload),'sha256':'0'*64}
        key.sign(outer)
        def fetch(url,**kwargs):
            kwargs['destination'].write(payload)
            return len(payload),digest
        with patch.object(self.manager,'_fetch',side_effect=fetch):
            with self.assertRaisesRegex(UpdateError,'SHA-256'): self.manager.download_update(outer)
        outer['package']['sha256']=digest
        outer['release_notes']='different from the real inner manifest'
        key.sign(outer)
        with patch.object(self.manager,'_fetch',side_effect=fetch):
            with self.assertRaisesRegex(UpdateError,'内.*清单'): self.manager.download_update(outer)

    def test_unknown_signature_and_tamper_fail_closed(self):
        key=NativeSigningKey();self.addCleanup(key.close)
        package(self.zip,key=key)
        with self.assertRaisesRegex(UpdateError,'不可信'): self.prepare()
        self.manager.save_settings({'trusted_keys':{'unit-ephemeral':key.public}})
        preview=self.prepare()
        self.manager.save_settings({'trusted_keys':{}})
        with self.assertRaisesRegex(UpdateError,'不可信'): self.deploy(preview)

    def test_identifier_and_release_structure_cannot_redirect_writes(self):
        with self.assertRaises(UpdateError): self.manager.result('../config')
        preview=self.prepare()
        path=self.manager._record_path(preview['id'])
        record=json.loads(path.read_text('utf-8'));record['target_path']=str(self.old)
        _atomic_json(path,record)
        result=self.deploy(preview)
        self.assertNotEqual(Path(result['target_path']),self.old)
        self.assertEqual((self.old/'MediaFlow.exe').read_bytes(),b'MZ-old-untouched')


if __name__=='__main__': unittest.main()
