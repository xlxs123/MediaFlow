"""Guarded orchestration tests. Fake GitHub/commands are never public acceptance."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts import release_pipeline as p


COMMIT='a'*40


class FakeRunner:
    def __init__(self,root,directory):
        self.root,self.directory,self.calls=Path(root),Path(directory),[]
        self.dirty=False

    def call(self,args,stage,**kwargs):
        self.calls.append((list(map(str,args)),stage,kwargs.get('logs',True)))
        if 'status' in args:return 0,b'?? synthetic-uncommitted.py' if self.dirty else b'',b''
        if 'remote' in args:return 0,b'https://github.com/xlxs123/MediaFlow.git',b''
        if 'rev-parse' in args:
            return (0,COMMIT.encode(),b'') if 'HEAD' in args else (1,b'',b'')
        return 0,b'{}',b''


class FakeGitHub:
    published=True
    calls=[]
    def __init__(self,*args):self.calls=[]
    def api(self,path,**kwargs):
        self.calls.append(path)
        if path=='user':return {'login':'xlxs123'}
        if '/git/ref/heads/' in path:return {'object':{'type':'commit','sha':COMMIT}}
        if '/git/ref/tags/' in path or path.endswith('/releases/latest'):return None
        raise AssertionError('Unexpected synthetic GitHub read: '+path)
    def repo(self,name):
        return {'private':False,'owner':{'login':'xlxs123'},'permissions':{'admin':True},'default_branch':'main'}
    def release(self,repo,version):
        return {'draft':False,'html_url':'https://github.com/'+repo+'/releases/tag/v'+version} if self.published else None
    def ci(self,*args,**kwargs):return {'passed':True,'scope':'synthetic unit fixture only'}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='MediaFlow-release-pipeline-unit-')
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve();self.root=self.base/'project';self.root.mkdir()
        (self.root/'assets').mkdir()
        (self.root/'assets/update-channel.json').write_bytes((p.ROOT/'assets/update-channel.json').read_bytes())
        (self.root/'RELEASE_NOTES.md').write_text('Synthetic local notes',encoding='utf-8')
        self.old=self.base/'old';self.old.mkdir()
        self.stamp={'app_version':'2.0.1','build_id':'unit-old','source_sha256':'b'*64,'packaged':True}
        (self.old/'build-info.json').write_text(json.dumps(self.stamp),encoding='utf-8')
        (self.old/'MediaFlow.exe').write_bytes(b'MZ-unit-only-never-executed')
        self.vault=self.base/'external-vault';self.vault.mkdir()

    def args(self,mode='dry-run'):
        return p.parser().parse_args(['run','--version',p.APP_VERSION,'--mode',mode,
            '--gh','gh-unit','--pwsh','pwsh-unit','--vault-directory',str(self.vault),
            '--old-release-dir',str(self.old),'--ci-wait-seconds','1'])

    def pipeline(self,mode='dry-run'):
        return p.Pipeline(self.args(mode),root=self.root,runner_factory=FakeRunner,github_factory=FakeGitHub)

    def test_credential_urls_invalid_versions_and_output_escape_fail_safely(self):
        for remote in ('https://unit-credential@github.com/xlxs123/MediaFlow.git',
                       'https://lookalike.unit-fixture.org/xlxs123/MediaFlow.git',
                       'https://github.com/xlxs123/MediaFlow.git?unit=1'):
            with self.subTest(remote=remote),self.assertRaises(p.PipelineError):p.repository(remote)
        self.assertEqual(p.repository('git@github.com:xlxs123/MediaFlow.git'),'xlxs123/MediaFlow')
        args=self.args();args.run_directory=str(self.base/'outside')
        with self.assertRaises(p.PipelineError):p.Pipeline(args,root=self.root)
        self.assertFalse((self.base/'outside').exists())
        args=self.args();args.version='../escape'
        with self.assertRaises(RuntimeError):p.Pipeline(args,root=self.root)

    def test_dirty_tree_stops_before_any_authentication_or_signing(self):
        pipeline=self.pipeline();pipeline.runner.dirty=True
        with patch.object(pipeline,'github_factory',side_effect=AssertionError('No auth before clean tree')):
            with self.assertRaisesRegex(p.PipelineError,'不干净'):pipeline.run()
        self.assertTrue(all('publisher_windows_vault.ps1' not in a for args,_,_ in pipeline.runner.calls for a in args))
        self.assertFalse((pipeline.directory/'.pipeline.lock').exists())

    def test_same_published_version_publish_fails_before_key_or_asset_writes(self):
        pipeline=self.pipeline('publish')
        with self.assertRaisesRegex(p.PipelineError,'已经正式发布'):pipeline.run()
        self.assertFalse(pipeline.report['published_by_this_run'])
        self.assertFalse(pipeline.report['remote_writes'])
        self.assertFalse(pipeline.report['production_chain_verified'])
        self.assertEqual(len(pipeline.runner.calls),3)

    def test_dryrun_has_no_publisher_tag_or_upload_commands(self):
        pipeline=self.pipeline()
        with patch.object(pipeline,'prepare'),patch.object(pipeline,'public_acceptance'):
            result=pipeline.run()
        self.assertTrue(result['passed'])
        for flag in ('published','published_by_this_run','remote_writes','production_chain_verified','public_online_verified'):
            self.assertFalse(result[flag])
        self.assertFalse(result['write_plan']['executed'])
        self.assertTrue(any(stage=='formal-vault-check' for _,stage,_ in pipeline.runner.calls))
        self.assertFalse(any('push' in args or 'upload' in args or 'edit' in args for args,_,_ in pipeline.runner.calls))
        self.assertTrue(all(not logs for _,stage,logs in pipeline.runner.calls if stage=='formal-vault-check'))

    def test_wrong_source_version_does_not_auto_bump_or_sign(self):
        args=self.args();args.version='99.0.0'
        pipeline=p.Pipeline(args,root=self.root,runner_factory=FakeRunner,github_factory=FakeGitHub)
        with self.assertRaisesRegex(p.PipelineError,'APP_VERSION'):pipeline.run()
        self.assertEqual(pipeline.runner.calls,[])

    def test_unexpected_exception_is_redacted_and_original_files_remain(self):
        pipeline=self.pipeline()
        with patch.object(pipeline,'prepare',side_effect=ValueError('unit-private-marker-never-log')):
            with self.assertRaises(p.PipelineError):pipeline.run()
        raw=(pipeline.directory/'report.json').read_text('utf-8')
        self.assertNotIn('unit-private-marker-never-log',raw)
        self.assertEqual((self.old/'MediaFlow.exe').read_bytes(),b'MZ-unit-only-never-executed')
        self.assertFalse(pipeline.report['published'])

    def test_resume_requires_original_signed_manifest_and_preserves_report(self):
        directory=self.root/'.release-runs/resume-unit';directory.mkdir(parents=True)
        signed=directory/'signed';signed.mkdir();(signed/'release.json').write_bytes(b'changed')
        data={'kind':'MediaFlow.release-pipeline','mode':'dry-run','version':p.APP_VERSION,
            'signed_manifest_sha256':'0'*64,'commit':COMMIT,'signed_directory':str(signed),
            'release_directory':str(self.old),'events':[],'published':False}
        (directory/'report.json').write_text(json.dumps(data),'utf-8')
        args=self.args();args.resume=True;args.run_directory=str(directory)
        pipeline=p.Pipeline(args,root=self.root,runner_factory=FakeRunner,github_factory=FakeGitHub)
        with patch.object(pipeline,'publish',side_effect=AssertionError('No upload')):
            with self.assertRaisesRegex(p.PipelineError,'发生变化'):pipeline.run()
        self.assertEqual((signed/'release.json').read_bytes(),b'changed')

    def test_existing_run_lock_does_not_get_removed_by_a_second_process(self):
        pipeline=self.pipeline()
        lock=pipeline.directory/'.pipeline.lock';lock.write_bytes(b'other-unit-owner')
        with self.assertRaisesRegex(p.PipelineError,'中断锁'):pipeline.run()
        self.assertEqual(lock.read_bytes(),b'other-unit-owner')

    def test_normal_mode_delegates_draft_verification_and_never_clobbers(self):
        pipeline=self.pipeline('publish')
        pipeline.source_repo='xlxs123/MediaFlow';pipeline.release_repo='xlxs123/MediaFlow-Releases'
        pipeline.report.update(commit=COMMIT,source_tag={'exists':False,'remote_exists':False})
        pipeline.signed=self.base/'signed';pipeline.notes=self.root/'RELEASE_NOTES.md'
        pipeline.github=FakeGitHub();pipeline.github.published=False;pipeline.github.env=p.safe_environment()
        calls=[]
        def command(program,*args,stage,**kwargs):
            calls.append((stage,args,kwargs))
            if stage=='github-draft-upload-verify-publish':
                report=Path(args[args.index('--report')+1])
                report.write_text(json.dumps({'passed':True,'draft_verified':True,
                    'published_assets_verified':True,'published':True,
                    'release_url':'https://github.com/xlxs123/MediaFlow-Releases/releases/tag/v2.1.0'}),'utf-8')
        pipeline.command=command
        pipeline.publish()
        self.assertEqual([stage for stage,_,_ in calls],['source-tag-create','source-tag-push','github-draft-upload-verify-publish'])
        self.assertTrue(pipeline.report['published_by_this_run'])
        self.assertFalse(pipeline.report['production_chain_verified'])
        self.assertFalse(calls[-1][2]['logs'])
        self.assertNotIn('--clobber',calls[-1][1])

    def test_public_client_must_match_this_staged_zip_in_production(self):
        pipeline=self.pipeline('publish')
        pipeline.release_repo='xlxs123/MediaFlow-Releases';pipeline.github=FakeGitHub()
        pipeline.github.published=True;pipeline.release=self.old;pipeline.old=self.old
        data=(self.old/'MediaFlow.exe').read_bytes()
        pipeline.manifest={'files':[{'path':'MediaFlow.exe','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}],
            'package':{'sha256':'a'*64}}
        def command(*args,**kwargs):
            folder=pipeline.attempt/'public-acceptance';folder.mkdir()
            (folder/'result.json').write_text(json.dumps({'passed':True,'public_online_verified':True,
                'public_manifest':{'package':{'sha256':'b'*64}}}),'utf-8')
        pipeline.command=command
        with patch.object(p,'_match_release_inventory',return_value={}):
            with self.assertRaisesRegex(p.PipelineError,'本次正式签名资产不同'):pipeline.public_acceptance()
        self.assertFalse(pipeline.report['production_chain_verified'])

    def test_parent_environment_drops_signing_and_github_secrets(self):
        env=p.safe_environment({'GH_TOKEN':'unit-token','GITHUB_TOKEN':'unit-token',
            'MEDIAFLOW_PROTECTED_PUBLISHER_PASSWORD':'unit-secret',
            'MEDIAFLOW_OTHER_PRIVATE_KEY':'unit-secret','APPDATA':'synthetic-private-data'})
        self.assertNotIn('GH_TOKEN',env);self.assertNotIn('GITHUB_TOKEN',env)
        self.assertFalse(any('PASSWORD' in name or 'PRIVATE_KEY' in name for name in env))
        self.assertEqual(env['APPDATA'],'synthetic-private-data')
        runner=p.Runner(self.root,self.base)
        with patch.dict(os.environ,{'GH_TOKEN':'unit-token'},clear=False):
            code,out,_=runner.call([sys.executable,'-B','-c','import os; print(os.getenv("GH_TOKEN","clean"))'],
                'environment-unit',timeout=15,logs=False)
        self.assertEqual(code,0);self.assertEqual(out.strip(),b'clean')
        self.assertFalse(list(self.base.glob('*.log')))

    def test_owned_process_timeout_is_bounded(self):
        runner=p.Runner(self.root,self.base);started=time.monotonic()
        with self.assertRaisesRegex(p.PipelineError,'超时'):
            runner.call([sys.executable,'-B','-c','import time; time.sleep(30)'],'timeout-unit',timeout=.2,logs=False)
        self.assertLess(time.monotonic()-started,10)


class GitHubGateTests(unittest.TestCase):
    def client(self,mutate=None):
        client=object.__new__(p.GitHub)
        run={'id':17,'head_sha':COMMIT,'head_repository':{'full_name':'owner/repo'},
            'event':'push','path':'.github/workflows/ci.yml','status':'completed','conclusion':'success',
            'html_url':'https://github.com/owner/repo/actions/runs/17'}
        artifact={'id':21,'name':'MediaFlow-2.1.0-windows-x64-ci-17','expired':False,
            'digest':'sha256:'+'a'*64,'size_in_bytes':100}
        jobs={'jobs':[{'steps':[{'name':name,'conclusion':'success'} for name in (
            'Update, signature, deployment and rollback regression','Build an independent Windows folder',
            'Start the packaged EXE and check complete release structure','Bind passed CI checks to the complete build',
            'Retain passed build and public summaries')]}]}
        if mutate:mutate(run,artifact,jobs)
        def api(path,**kwargs):
            if path.endswith('/jobs?per_page=100'):return jobs
            if path.endswith('/artifacts'):return {'artifacts':[artifact]}
            return run
        client.api=api
        return client

    def test_ci_must_match_commit_trusted_workflow_steps_and_artifact_digest(self):
        good=self.client().ci('owner/repo',COMMIT,'2.1.0',run_id='17',wait_seconds=1)
        self.assertEqual(good['artifact']['id'],21)
        changes=[lambda r,a,j:r.update(head_sha='b'*40),lambda r,a,j:r.update(event='pull_request'),
            lambda r,a,j:r.update(path='.github/workflows/untrusted.yml'),
            lambda r,a,j:r.update(conclusion='failure'),lambda r,a,j:a.update(expired=True),
            lambda r,a,j:a.update(digest=''),lambda r,a,j:j['jobs'][0]['steps'].pop()]
        for mutate in changes:
            with self.subTest(mutate=mutate),self.assertRaises(p.PipelineError):
                self.client(mutate).ci('owner/repo',COMMIT,'2.1.0',run_id='17',wait_seconds=1)

    def test_authentication_fallback_never_logs_or_returns_credentials(self):
        class Credentials:
            def __init__(self):self.calls=[]
            def call(self,args,stage,**kwargs):
                self.calls.append((stage,kwargs))
                return (1,b'',b'') if stage=='github-auth' else (0,b'username=unit\npassword=unit-secret-not-production\n',b'')
        runner=Credentials()
        client=p.GitHub(runner,'gh-unit','git-unit')
        self.assertEqual(client.auth_method,'git-credential-manager')
        self.assertTrue(all(kwargs['logs'] is False for _,kwargs in runner.calls))
        self.assertNotIn('unit-secret-not-production',repr([(s,k.get('input_bytes')) for s,k in runner.calls]))
        self.assertIn('GH_TOKEN',client.env)


class CIArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='MediaFlow-ci-archive-unit-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.archive=self.root/'artifact.zip'

    def write(self,entries):
        with zipfile.ZipFile(self.archive,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,value in entries:archive.writestr(name,value)
        data=self.archive.read_bytes()
        return {'size_in_bytes':len(data),'digest':'sha256:'+hashlib.sha256(data).hexdigest()}

    def invoke(self,artifact):
        return p.extract_ci_artifact(self.archive,self.root/'new',artifact,'unit/repo',COMMIT,'17','2.1.0','a'*64)

    def test_hash_mismatch_prevents_any_extraction(self):
        artifact=self.write([('dist-mediaflow-ci/README.md',b'unit')]);artifact['digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(p.PipelineError,'SHA-256'):self.invoke(artifact)
        self.assertFalse((self.root/'new').exists())

    def test_traversal_links_duplicates_and_extra_paths_are_rejected(self):
        link=zipfile.ZipInfo('dist-mediaflow-ci/link');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        cases=[[('../outside',b'unit')],[(link,b'../outside')],[('other-data.json',b'unit')],
            [('dist-mediaflow-ci/A.txt',b'a'),('dist-mediaflow-ci/a.txt',b'b')]]
        for entries in cases:
            with self.subTest(entries=entries):
                artifact=self.write(entries)
                with self.assertRaises(RuntimeError):self.invoke(artifact)
                self.assertFalse((self.root/'outside').exists());self.assertFalse((self.root/'new').exists())

    def test_local_archive_binding_alone_never_claims_ci_provenance(self):
        artifact=self.write([('dist-mediaflow-ci/README.md',b'unit'),
            ('verification/ci/build-attestation.json',b'unit-only-mocked-receipt')])
        with patch('scripts.ci_build_receipt.verify_receipt',return_value={'passed':True,'ci_provenance_verified':False}) as verify:
            value=self.invoke(artifact)
        self.assertFalse(value['ci_provenance_verified'])
        self.assertEqual(verify.call_args.kwargs['expected_commit'],COMMIT)
        self.assertEqual((self.root/'new/dist-mediaflow-ci/README.md').read_bytes(),b'unit')


if __name__=='__main__':unittest.main()
