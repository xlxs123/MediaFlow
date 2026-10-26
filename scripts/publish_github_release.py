"""Controlled publisher: verified assets -> draft -> re-download -> publish.

Uses normal gh authentication. Never reads a private signing key, deletes an
asset, overwrites a release, or treats draft access as anonymous public access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import unquote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
if __package__ in ('',None): sys.path.insert(0,str(ROOT))
from core.update_channel import load_channel
from core.update_github import release_asset
from core.update_manifest import validate_manifest, canonical_bytes, parse_version
from core.data_migration import frozen_file
from scripts.publish_release import _read_document, _save_new


class PublicationError(RuntimeError):
    pass


def digest(path):
    h=hashlib.sha256()
    with frozen_file(path) as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def plain(path):
    path=Path(path).absolute()
    if not path.is_file() or any(p.is_symlink() or p.is_junction() for p in (path,*path.parents)):
        raise PublicationError('Publication input must be an existing ordinary file without reparse ancestors.')
    if re.search(r'(?i)(?:private.?key|password|cookie|credentials|\.pem$|\.dpapi$|\.pfx$)',path.name):
        raise PublicationError('Private publisher or user data cannot be a release asset.')
    return path


def prepare_assets(directory,repository,extras):
    directory=Path(directory).absolute()
    raw=plain(directory/'release.json').read_bytes()
    if len(raw)>2*1024**2:raise PublicationError('Manifest exceeds bound.')
    manifest,_,_=_read_document(directory/'release.json')
    channel=load_channel()
    if channel['key_state']!='initialized':raise PublicationError('Formal channel is not initialized.')
    verified=validate_manifest(manifest,channel['trusted_keys'],require_signature=True)
    if not verified['authenticated']:raise PublicationError('Manifest authentication failed.')
    version=parse_version(manifest['version'])
    if version.is_prerelease or version.local:raise PublicationError('Stable publication requires a stable release version.')
    url=urlsplit(manifest['package']['url'])
    tag='v'+manifest['version']
    prefix='/'+repository+'/releases/download/'+tag+'/'
    if (url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment
            or not url.path.startswith(prefix) or '/' in url.path[len(prefix):]):
        raise PublicationError('Signed package URL does not match the approved repository/version asset.')
    package=plain(directory/url.path[len(prefix):])
    if package.stat().st_size!=manifest['package']['size'] or digest(package)!=manifest['package']['sha256']:
        raise PublicationError('Final signed package size/SHA-256 mismatch.')
    detached=plain(directory/'signature.json').read_bytes()
    value,_,_=_read_document(directory/'signature.json')
    if value.get('signature')!=manifest['signature'] or value.get('manifest_sha256')!=hashlib.sha256(canonical_bytes(manifest)).hexdigest():
        raise PublicationError('Detached signature does not match the signed manifest.')
    for name,data in (('manifest.json',raw),('manifest.sig',detached)):
        path=directory/name
        if path.exists():
            if plain(path).read_bytes()!=data:raise PublicationError('Alias already exists with different bytes; preserving it.')
        else:
            _save_new(path,data)
    assets=[package,directory/'manifest.json',directory/'manifest.sig',directory/'release.json',directory/'signature.json']
    assets += [plain(path) for path in extras]
    names=[p.name.casefold() for p in assets]
    if len(set(names))!=len(names):raise PublicationError('Asset names collide; no upload was attempted.')
    records=[{'name':p.name,'size':p.stat().st_size,'sha256':digest(p),'path':str(p)} for p in assets]
    if any(not 0<r['size']<2*1024**3 for r in records):raise PublicationError('GitHub asset size is empty or exceeds the 2 GiB bound.')
    return manifest,tag,assets,records


def verified_asset_urls(current,records,repository,tag,*,published=False):
    entries=current.get('assets')
    if not isinstance(entries,list):raise PublicationError('Release asset inventory is missing or invalid.')
    remote={}
    names=set()
    for entry in entries:
        name=entry.get('name') if isinstance(entry,dict) else None
        if not isinstance(name,str) or not name or name.casefold() in names:
            raise PublicationError('Release asset inventory contains invalid or duplicate names.')
        names.add(name.casefold());remote[name]=entry
    urls={}
    for item in records:
        entry=remote.get(item['name'],{})
        if entry.get('size')!=item['size'] or entry.get('digest')!='sha256:'+item['sha256']:
            raise PublicationError('Release asset size/server SHA-256 mismatch; existing assets were preserved.')
        address=entry.get('browser_download_url')
        if not isinstance(address,str):raise PublicationError('Release asset download URL is missing.')
        if published:
            value=urlsplit(address)
            prefix='/'+repository+'/releases/download/'+tag+'/'
            if (value.scheme!='https' or value.netloc!='github.com' or value.query or value.fragment
                    or not value.path.startswith(prefix)
                    or unquote(value.path[len(prefix):],errors='strict')!=item['name']):
                raise PublicationError('Published asset URL does not match the approved repository/version asset.')
        urls[item['name']]=address
    return urls


def publication_inputs(args):
    """Keep formal content validation separate from an explicit test mirror."""
    testing=getattr(args,'test_release',False)
    test_tag=getattr(args,'test_tag','')
    create_only=getattr(args,'test_create_only',False)
    if create_only and (not testing or args.publish):
        raise PublicationError('Create-only checkpoint is limited to explicit non-publishing TestRelease mode.')
    if test_tag and not testing:
        raise PublicationError('A test tag is only allowed in explicit TestRelease mode.')
    if not testing:
        return (*prepare_assets(args.artifacts,args.repository,args.asset),None)
    source_url=load_channel().get('source_url','')
    channel=release_asset(source_url)
    if not channel:
        raise PublicationError('TestRelease requires an initialized, real GitHub stable channel.')
    formal='/'.join(urlsplit(source_url).path.split('/')[1:3])
    allowed=channel[0]+'/MediaFlow-Release-Acceptance'
    if args.repository.casefold()!=allowed.casefold() or args.repository.casefold()==formal.casefold():
        raise PublicationError('TestRelease may only target the separate owner/MediaFlow-Release-Acceptance repository.')
    if not re.fullmatch(r'mediaflow-pipeline-test-[a-z0-9][a-z0-9.-]{0,90}',test_tag):
        raise PublicationError('TestRelease requires an explicit, bounded mediaflow-pipeline-test-* tag.')
    manifest,_,assets,records=prepare_assets(args.artifacts,formal,args.asset)
    expected=getattr(args,'expected_version','')
    if expected and manifest['version']!=expected:
        raise PublicationError('Requested test version differs from the formally signed content.')
    return manifest,test_tag,assets,records,formal


def publish(args,*,environment=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',args.repository):
        raise PublicationError('Repository must be an explicit owner/name.')
    gh=str(Path(args.gh).absolute()) if args.gh else shutil.which('gh')
    if not gh or not Path(gh).is_file():raise PublicationError('A normal installed GitHub CLI is required by this publisher tool.')
    manifest,tag,assets,records,formal_repository=publication_inputs(args)
    testing=getattr(args,'test_release',False)
    report_path=Path(args.report).absolute()
    if not report_path.parent.is_dir():raise PublicationError('Report parent must exist outside publication assets.')
    if report_path.exists():raise PublicationError('Use a new report path; previous evidence is preserved.')
    report={'passed':False,'repository':args.repository,'tag':tag,'assets':records,
        'public_online_verified':False,'draft_verified':False,'published':False,
        'publication_requested':False,'published_assets_verified':False,'stage':'repository_check',
        'test_release':testing,'formal_content_repository':formal_repository,
        'draft_created_by_this_run':False,
        'mirror_public_verified':False,'production_chain_verified':False}
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    def run(*command,json_result=False,timeout=120):
        p=subprocess.run([gh,*map(str,command)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=environment)
        if p.returncode:raise PublicationError('GitHub CLI operation failed; credentials and raw diagnostics were not logged: '+command[0])
        return json.loads(p.stdout) if json_result else p.stdout.strip()
    def release():
        all_releases=run('api','repos/'+args.repository+'/releases?per_page=100',json_result=True)
        matches=[r for r in all_releases if r['tag_name']==tag]
        if len(matches)>1:raise PublicationError('Multiple matching releases require manual investigation; none changed.')
        return matches[0] if matches else None
    save()
    try:
        login=run('api','user','--jq','.login')
        repo=run('api','repos/'+args.repository,json_result=True)
        if (repo['owner']['login'].casefold()!=login.casefold() or repo.get('private') is not False
                or repo.get('permissions',{}).get('admin') is not True):
            raise PublicationError('Only an actually owned public repository with admin permission is allowed.')
        notes=plain(args.notes)
        current=release()
        if current and not current['draft']:
            raise PublicationError('This version is already published. Existing release/assets were preserved.')
        if not testing:
            from scripts.release_security import official_publisher_key, verify_package, ReleaseSecurityError
            if official_publisher_key(load_channel()['trusted_keys'], manifest['signature']['key_id']):
                try:
                    report['stage']='formal_authenticode_package_verification';save()
                    report['authenticode']=verify_package(assets[0],manifest,report_path.parent,
                        thumbprint=getattr(args,'certificate_thumbprint',''),
                        store=getattr(args,'certificate_store','CurrentUser'),
                        timestamp_url=getattr(args,'timestamp_url',''),
                        sign_tool=getattr(args,'sign_tool_path',''),pwsh=getattr(args,'pwsh','') or None)
                except ReleaseSecurityError as error:
                    raise PublicationError(str(error)) from None
        if not current:
            report['stage']='create_draft';save()
            flags=['--prerelease','--latest=false'] if testing else []
            run('release','create',tag,'--repo',args.repository,'--target',repo['default_branch'],
                '--title',('MediaFlow transaction TEST ' if testing else 'MediaFlow ')+manifest['version'],
                '--notes-file',notes,'--draft',*flags)
            for attempt in range(6):
                current=release()
                if current:break
                if attempt<5:time.sleep(1)
            if not current or not current['draft']:raise PublicationError('Draft creation could not be confirmed.')
            report['draft_created_by_this_run']=True
        if current.get('prerelease') is not testing:
            raise PublicationError('Draft prerelease state differs from the explicit publication mode; preserving it.')
        report.update(release_url=current['html_url'],release_id=current['id'])
        if getattr(args,'test_create_only',False):
            if current['assets']:
                raise PublicationError('Create-only checkpoint requires an empty test draft; existing assets preserved.')
            report.update(passed=True,stage='isolated_test_draft_created_no_upload')
            return report
        report['stage']='upload';save()
        existing={r['name']:r for r in current['assets']}
        for path,record in zip(assets,records):
            known=existing.get(path.name)
            if known:
                if known.get('size')!=record['size'] or known.get('digest')!='sha256:'+record['sha256']:
                    raise PublicationError('Existing asset differs or lacks a verified server digest; no clobber/delete performed.')
            else:
                with frozen_file(path):
                    if path.stat().st_size!=record['size'] or digest(path)!=record['sha256']:
                        raise PublicationError('Local asset changed before upload; publication stopped.')
                    run('release','upload',tag,path,'--repo',args.repository,timeout=1800)
            report['uploaded_assets']=report.get('uploaded_assets',0)+1;save()
        report['stage']='remote_asset_inventory';save()
        current=release()
        if (not current or current.get('id')!=report['release_id'] or current.get('tag_name')!=tag
                or current.get('draft') is not True):
            raise PublicationError('Expected draft identity/state could not be confirmed; publication stopped.')
        urls=verified_asset_urls(current,records,args.repository,tag)
        for item in records:
            item['url']=urls[item['name']]
        # A draft is authenticated-only on GitHub. Never pretend this is the
        # anonymous public verification performed by the actual client later.
        report['stage']='authenticated_draft_download';save()
        download=report_path.parent/('draft-verification-'+uuid.uuid4().hex)
        download.mkdir()
        run('release','download',tag,'--repo',args.repository,'--pattern',assets[0].name,
            '--pattern','manifest.json','--pattern','manifest.sig','--dir',download,timeout=1800)
        expected={r['name']:r for r in records}
        for name in (assets[0].name,'manifest.json','manifest.sig'):
            path=plain(download/name)
            if path.stat().st_size!=expected[name]['size'] or digest(path)!=expected[name]['sha256']:
                raise PublicationError('Re-downloaded draft bytes differ; draft retained.')
        remote_manifest,_,_=_read_document(download/'manifest.json')
        validate_manifest(remote_manifest,load_channel()['trusted_keys'],require_signature=True)
        if (canonical_bytes(remote_manifest)!=canonical_bytes(manifest)
                or digest(download/assets[0].name)!=remote_manifest['package']['sha256']
                or (download/assets[0].name).stat().st_size!=remote_manifest['package']['size']):
            raise PublicationError('Draft package does not match the actually verified signed manifest.')
        report['draft_verified']=True;save()
        if args.publish:
            report.update(stage='publish',publication_requested=True);save()
            flags=['--prerelease','--latest=false'] if testing else ['--latest']
            run('release','edit',tag,'--repo',args.repository,'--draft=false',*flags)
            report['stage']='public_release_inventory';save()
            current=release()
            expected_url='https://github.com/'+args.repository+'/releases/tag/'+tag
            if (not current or current.get('id')!=report['release_id'] or current.get('tag_name')!=tag
                    or current.get('draft') is not False or current.get('prerelease') is not testing
                    or not current.get('published_at') or current.get('html_url')!=expected_url):
                raise PublicationError('Publication state not confirmed; no public acceptance claimed.')
            report.update(published=True,release_url=current['html_url'])
            # GitHub replaces draft untagged-* URLs at publication. Discard the
            # old addresses even if the final inventory check subsequently fails.
            for item in records:item.pop('url',None)
            save()
            urls=verified_asset_urls(current,records,args.repository,tag,published=True)
            for item in records:item['url']=urls[item['name']]
            report['published_assets_verified']=True
        report.update(passed=True,stage=('test_release_published_pending_anonymous_verification' if testing
            else 'published_pending_anonymous_client_acceptance') if args.publish else 'verified_draft')
        return report
    except Exception as e:
        report.update(error_type=type(e).__name__,error=str(e) if isinstance(e,PublicationError) else 'Bounded publication operation failed; prior files and draft retained.')
        raise
    finally:save()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gh');p.add_argument('--repository',required=True)
    p.add_argument('--artifacts',required=True);p.add_argument('--notes',required=True)
    p.add_argument('--asset',action='append',default=[]);p.add_argument('--report',required=True)
    p.add_argument('--publish',action='store_true',help='Explicitly publish only after authenticated draft re-download verification')
    p.add_argument('--test-release',action='store_true',help='Only publish an isolated prerelease mirror of existing formal content')
    p.add_argument('--test-tag',default='');p.add_argument('--expected-version',default='')
    p.add_argument('--test-create-only',action='store_true',help='Test-only empty draft checkpoint; never uploads or publishes')
    p.add_argument('--certificate-thumbprint',default='')
    p.add_argument('--certificate-store',choices=('CurrentUser','LocalMachine'),default='CurrentUser')
    p.add_argument('--timestamp-url',default='');p.add_argument('--sign-tool-path',default='');p.add_argument('--pwsh',default='')
    args=p.parse_args()
    try:
        result=publish(args)
        print(json.dumps({k:result[k] for k in ('passed','repository','tag','release_url','draft_verified','published','public_online_verified')}))
        return 0
    except Exception as e:
        print(json.dumps({'passed':False,'error_type':type(e).__name__,'message':str(e) if isinstance(e,PublicationError) else 'Publication failed; inspect the private stage report.'}))
        return 1


if __name__=='__main__':raise SystemExit(main())
