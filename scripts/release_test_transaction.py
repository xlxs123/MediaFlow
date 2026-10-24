"""Explicit isolated GitHub prereleases of already authenticated formal content.

Never signs, changes product versions, writes the stable repository or alters a
client trust root. Anonymous mirror verification is deliberately a separate
result from production updater/deployment acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
if __package__ in ('',None):sys.path.insert(0,str(ROOT))
from core.update_channel import load_channel
from core.update_github import ReleaseRedirectPolicy
from core.update_manifest import canonical_bytes,validate_manifest
from core.update_transport import _SameOriginRedirect,_origin
from core.update_manager import UpdateManager
from scripts import publish_github_release as publisher
from scripts.release_pipeline import GitHub,Runner,save
from scripts.accept_release_update import _environment


def test_arguments(version,repository,tag,artifacts,gh,notes,report,*,publish=True):
    return argparse.Namespace(repository=repository,gh=str(gh),artifacts=str(artifacts),asset=[],
        notes=str(notes),report=str(report),publish=publish,test_release=True,test_tag=tag,
        expected_version=version)


def github(gh,directory):
    executable=Path(gh).resolve() if gh else Path(shutil.which('gh') or '')
    git=shutil.which('git')
    if not executable.is_file() or not git:
        raise publisher.PublicationError('An existing GitHub CLI and Git are required.')
    return GitHub(Runner(ROOT,directory),str(executable),git)


def cli(client,*arguments,timeout=120,allow_failure=False):
    # Only callers below construct commands; credentials remain in child env.
    code,out,_=client.runner.call([client.gh,*arguments],'test-github-operation',timeout=timeout,
        env=client.env,logs=False,allow_failure=True)
    if code and not allow_failure:
        raise publisher.PublicationError('An isolated GitHub operation failed; raw credential diagnostics were not saved.')
    return code,out


def run_publish(args):
    directory=Path(args.run_directory).resolve() if args.run_directory else (
        ROOT/'.release-runs'/('test-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]))
    if directory.exists():raise publisher.PublicationError('Use a new test report directory; remote drafts resume by the same TestTag.')
    directory.mkdir(parents=True)
    notes=Path(args.notes).resolve() if args.notes else directory/'test-notes.md'
    if not args.notes:
        notes.write_text('MediaFlow release transaction TEST only.\n\n'
            'An unchanged, formally authenticated stable package is mirrored here to test publication. '
            'This prerelease is NOT an update channel and is never marked latest.\n',encoding='utf-8')
    inputs=test_arguments(args.version,args.repository,args.tag,args.artifacts,args.gh,notes,
        directory/'publication.json',publish=not (args.draft_only or args.create_only))
    inputs.test_create_only=args.create_only
    stage='formal-content-and-isolation-check'
    try:
        publisher.publication_inputs(inputs)  # Before accessing GitHub credentials.
        stage='github-authentication'
        client=github(args.gh,directory)
        stage='actual-github-publication'
        result=publisher.publish(inputs,environment=client.env)
        save(directory/'result.json',dict(passed=True,test_release=True,production_chain_verified=False,
            publication_report=str(directory/'publication.json'),**{
                k:result[k] for k in ('repository','tag','release_url','draft_verified','published','published_assets_verified')}))
        return directory/'result.json'
    except Exception as error:
        save(directory/'result.json',dict(passed=False,test_release=True,stage=stage,
            production_chain_verified=False,error_type=type(error).__name__,
            error=str(error) if isinstance(error,(publisher.PublicationError,ValueError,RuntimeError))
                else 'Isolated test publication stopped; existing assets and records retained.'))
        raise


def anonymous_file(url,path,expected,*,timeout=1800):
    """Fixed local mirror URL + system TLS + narrow GitHub CDN redirects."""
    policy=ReleaseRedirectPolicy(url,url)
    if not policy.enabled:raise publisher.PublicationError('Invalid explicitly selected mirror asset URL.')
    opener=urllib.request.build_opener(_SameOriginRedirect(_origin(url),policy),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request=urllib.request.Request(url,headers={'User-Agent':'MediaFlow-Publisher-Acceptance/2.1',
        'Accept-Encoding':'identity'})
    started=time.monotonic();digest=hashlib.sha256();done=0;last=started
    # Parent process also supplies a bounded process-tree deadline.
    with opener.open(request,timeout=8) as response,path.open('xb') as output:
        if response.status!=200 or not policy.accepts(response.geturl()):
            raise publisher.PublicationError('Anonymous mirror did not return a complete approved response.')
        declared=response.headers.get('Content-Length')
        if declared and (not declared.isdecimal() or int(declared)!=expected['size']):
            raise publisher.PublicationError('Anonymous server size differs from verified asset inventory.')
        if response.headers.get('Content-Encoding','identity').lower()!='identity':
            raise publisher.PublicationError('Unexpected encoding from mirror.')
        while block:=response.read1(128*1024):
            done+=len(block)
            if done>expected['size'] or time.monotonic()-started>timeout:
                raise publisher.PublicationError('Anonymous mirror exceeded signed size or total deadline.')
            digest.update(block);output.write(block)
            if time.monotonic()-last>10:
                print(json.dumps({'asset':path.name,'downloaded':done,'expected':expected['size']}),flush=True)
                last=time.monotonic()
        output.flush();os.fsync(output.fileno())
    if done!=expected['size'] or digest.hexdigest()!=expected['sha256']:
        raise publisher.PublicationError('Anonymous mirror bytes differ from authenticated formal inputs.')
    return dict(name=path.name,size=done,sha256=digest.hexdigest(),url=url,anonymous=True)


def verify_public(args):
    directory=Path(args.output).resolve()
    if directory.exists():raise publisher.PublicationError('Public verification directory must be new.')
    directory.mkdir(parents=True)
    environment=_environment(directory)
    # Preserve Windows system/home paths but remove every auth/proxy/CA override.
    os.environ.clear();os.environ.update(environment)
    result={'passed':False,'scope':'real_public_isolated_prerelease_mirror',
        'mirror_public_verified':False,'public_online_verified':False,
        'production_chain_verified':False,'assets':[]}
    try:
        inputs=test_arguments(args.version,args.repository,args.tag,args.artifacts,'','',directory/'unused.json')
        manifest,tag,_,records,formal=publisher.publication_inputs(inputs)
        remote=json.loads(Path(args.remote).read_text('utf-8'))
        if (remote.get('tag_name')!=tag or remote.get('draft') is not False
                or remote.get('prerelease') is not True or not remote.get('published_at')):
            raise publisher.PublicationError('Actual mirror must be a published prerelease.')
        urls=publisher.verified_asset_urls(remote,records,args.repository,tag,published=True)
        result['stage']='anonymous-release-metadata'
        api='https://api.github.com/repos/'+args.repository+'/releases/tags/'+tag
        api_opener=urllib.request.build_opener(_SameOriginRedirect(_origin(api)),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with api_opener.open(urllib.request.Request(api,headers={'User-Agent':'MediaFlow-Publisher-Acceptance/2.1'}),timeout=8) as response:
            raw=response.read(2*1024*1024+1)
            if response.status!=200 or len(raw)>2*1024*1024:
                raise publisher.PublicationError('Anonymous release metadata is unavailable or exceeds the bound.')
            anonymous=json.loads(raw)
        if anonymous.get('id')!=remote['id'] or anonymous.get('draft') is not False or anonymous.get('prerelease') is not True:
            raise publisher.PublicationError('Anonymous release identity differs from actual published prerelease.')
        result['anonymous_release_verified']=True
        downloads=directory/'downloads';downloads.mkdir()
        for record in records:
            result['stage']='anonymous-asset-download';result['current_asset']=record['name']
            result['assets'].append(anonymous_file(urls[record['name']],downloads/record['name'],record))
        result['stage']='formal-signature-verification'
        actual,_,_=publisher._read_document(downloads/'manifest.json')
        verified=validate_manifest(actual,load_channel()['trusted_keys'],require_signature=True)
        detached,_,_=publisher._read_document(downloads/'manifest.sig')
        if (canonical_bytes(actual)!=canonical_bytes(manifest)
                or detached.get('signature')!=actual['signature']
                or detached.get('manifest_sha256')!=hashlib.sha256(canonical_bytes(actual)).hexdigest()
                or (downloads/'manifest.json').read_bytes()!=(downloads/'release.json').read_bytes()
                or (downloads/'manifest.sig').read_bytes()!=(downloads/'signature.json').read_bytes()):
            raise publisher.PublicationError('Downloaded aliases/signature differ from original authenticated content.')
        # Reuse full ZIP member/manifest/file validation, without deployment or migration.
        result['stage']='full-local-zip-verification'
        manager=UpdateManager(state_dir=directory/'local-package-validation',release_root=directory/'installation-context',
            current_identity={'app_version':args.version,'build_id':manifest['build_id']},
            current_data_dir=directory/'never-opened-user-data')
        prepared=manager.prepare_local(downloads/records[0]['name'])
        if prepared.get('authenticated') is not True:
            raise publisher.PublicationError('Downloaded package failed the existing full local import verifier.')
        result.update(passed=True,mirror_public_verified=True,formal_signature_verified=verified['authenticated'],
            formal_content_repository=formal,formal_package_url=manifest['package']['url'],
            mirrored_release_url=remote['html_url'],key_id=actual['signature']['key_id'],
            package_size=prepared['package_size'],package_sha256=prepared['package_sha256'],
            full_zip_structure_verified=True)
        result['stage']='mirror-verification-complete'
    except Exception as error:
        result.update(error_type=type(error).__name__,error=str(error)
            if isinstance(error,publisher.PublicationError) else 'Anonymous mirror verification stopped; no deployment was attempted.')
        if isinstance(getattr(error,'code',None),int):result['http_status']=error.code
        if getattr(error,'headers',None):
            result['safe_retry_headers']={name:error.headers[name] for name in
                ('X-RateLimit-Limit','X-RateLimit-Remaining','X-RateLimit-Reset','Retry-After') if name in error.headers}
        raise
    finally:save(directory/'result.json',result)
    return directory/'result.json'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    for name in ('publish','verify-public'):
        p=sub.add_parser(name)
        p.add_argument('--version',required=True);p.add_argument('--repository',required=True)
        p.add_argument('--tag',required=True);p.add_argument('--artifacts',required=True)
        if name=='publish':
            p.add_argument('--gh',default='');p.add_argument('--notes',default='')
            p.add_argument('--run-directory',default='');p.add_argument('--draft-only',action='store_true')
            p.add_argument('--create-only',action='store_true')
        else:
            p.add_argument('--remote',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        report=run_publish(args) if args.operation=='publish' else verify_public(args)
        print(json.dumps({'passed':True,'test_release':True,'report':str(report),'production_chain_verified':False}))
        return 0
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__,
            'message':str(error) if isinstance(error,publisher.PublicationError) else 'Test operation stopped; inspect its new report.'}))
        return 1


if __name__=='__main__':raise SystemExit(main())
