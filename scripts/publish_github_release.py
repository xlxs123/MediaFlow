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
import uuid
from urllib.parse import unquote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
if __package__ in ('',None): sys.path.insert(0,str(ROOT))
from core.update_channel import load_channel
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


def publish(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',args.repository):
        raise PublicationError('Repository must be an explicit owner/name.')
    gh=str(Path(args.gh).absolute()) if args.gh else shutil.which('gh')
    if not gh or not Path(gh).is_file():raise PublicationError('A normal installed GitHub CLI is required by this publisher tool.')
    manifest,tag,assets,records=prepare_assets(args.artifacts,args.repository,args.asset)
    report_path=Path(args.report).absolute()
    if not report_path.parent.is_dir():raise PublicationError('Report parent must exist outside publication assets.')
    if report_path.exists():raise PublicationError('Use a new report path; previous evidence is preserved.')
    report={'passed':False,'repository':args.repository,'tag':tag,'assets':records,
        'public_online_verified':False,'draft_verified':False,'published':False,
        'publication_requested':False,'published_assets_verified':False,'stage':'repository_check'}
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    def run(*command,json_result=False,timeout=120):
        p=subprocess.run([gh,*map(str,command)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
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
        if not current:
            report['stage']='create_draft';save()
            run('release','create',tag,'--repo',args.repository,'--target',repo['default_branch'],
                '--title','MediaFlow '+manifest['version'],'--notes-file',notes,'--draft')
            current=release()
            if not current or not current['draft']:raise PublicationError('Draft creation could not be confirmed.')
        report.update(release_url=current['html_url'],release_id=current['id'])
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
            run('release','edit',tag,'--repo',args.repository,'--draft=false','--latest')
            report['stage']='public_release_inventory';save()
            current=release()
            expected_url='https://github.com/'+args.repository+'/releases/tag/'+tag
            if (not current or current.get('id')!=report['release_id'] or current.get('tag_name')!=tag
                    or current.get('draft') is not False or current.get('prerelease') is not False
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
        report.update(passed=True,stage='published_pending_anonymous_client_acceptance' if args.publish else 'verified_draft')
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
    args=p.parse_args()
    try:
        result=publish(args)
        print(json.dumps({k:result[k] for k in ('passed','repository','tag','release_url','draft_verified','published','public_online_verified')}))
        return 0
    except Exception as e:
        print(json.dumps({'passed':False,'error_type':type(e).__name__,'message':str(e) if isinstance(e,PublicationError) else 'Publication failed; inspect the private stage report.'}))
        return 1


if __name__=='__main__':raise SystemExit(main())
