"""Real, explicitly isolated GitHub publication/fault acceptance (not DryRun).

Uses the existing formal package without signing again. All test releases and
fault evidence are retained. Never deletes a release, asset, tag or user file.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime,timezone
import http.client
import json
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import quote,urlsplit
import uuid

ROOT=Path(__file__).resolve().parents[1]
if __package__ in ('',None):sys.path.insert(0,str(ROOT))
from core.data_migration import frozen_file
from core.update_channel import load_channel
from core.update_github import release_asset
from core.update_manager import UpdateManager
from scripts import publish_github_release as publisher
from scripts.release_pipeline import Runner,save
from scripts.release_test_transaction import cli,github,test_arguments
from scripts.accept_release_update import _environment


def snapshot(release):
    return {**{key:release.get(key) for key in ('id','tag_name','draft','prerelease','published_at','target_commitish','html_url')},
        'assets':[{key:a.get(key) for key in ('id','name','size','digest','browser_download_url','created_at')}
            for a in sorted(release.get('assets',[]),key=lambda a:a['name'])]}


def release(client,repository,tag):
    # The authenticated list includes drafts; by-tag lookup did not in live API.
    matches=[r for r in client.api('repos/'+repository+'/releases?per_page=100') if r.get('tag_name')==tag]
    if len(matches)>1:raise publisher.PublicationError('Ambiguous test release identity; nothing changed.')
    return matches[0] if matches else None


def create_draft(client,repository,tag,notes):
    existing=release(client,repository,tag)
    if existing:
        if existing.get('draft') is True and existing.get('prerelease') is True:return existing
        raise publisher.PublicationError('Existing test release is not a prerelease draft; no replacement allowed.')
    repo=client.repo(repository)
    reference=client.api('repos/'+repository+'/git/ref/heads/'+repo['default_branch'])
    cli(client,'api','--method','POST','repos/'+repository+'/git/refs',
        '-f','ref=refs/tags/'+tag,'-f','sha='+reference['object']['sha'])
    cli(client,'release','create',tag,'--repo',repository,'--verify-tag','--draft','--prerelease',
        '--latest=false','--title','MediaFlow isolated publication TEST','--notes-file',notes)
    for attempt in range(6):
        value=release(client,repository,tag)
        if value:break
        if attempt<5:time.sleep(1)
    if not value or value['draft'] is not True or value['prerelease'] is not True:
        raise publisher.PublicationError('Actual test draft identity could not be confirmed.')
    return value


def interrupted_upload(client,repository,current,package,*,cutoff=2*1024*1024):
    """Send a real incomplete HTTPS upload, then close only this connection."""
    address=current['upload_url'].split('{',1)[0]
    expected='https://uploads.github.com/repos/'+repository+'/releases/'+str(current['id'])+'/assets'
    if address!=expected:raise publisher.PublicationError('Unexpected upload endpoint; no token was sent.')
    token=client.env.get('GH_TOKEN') or client.env.get('GITHUB_TOKEN')
    if not token:
        code,value=cli(client,'auth','token','--hostname','github.com')
        token=value.decode('utf-8').strip();value=b''
    if not token:raise publisher.PublicationError('Existing authenticated upload credential is unavailable.')
    sent=0;connection=None;out={'scope':'real_https_upload_intentionally_interrupted',
        'declared_size':package.stat().st_size,'server_completion_claimed':False}
    try:
        connection=http.client.HTTPSConnection('uploads.github.com',timeout=8,context=ssl.create_default_context())
        endpoint=urlsplit(address).path+'?name='+quote(package.name,safe='')
        connection.putrequest('POST',endpoint)
        connection.putheader('Authorization','Bearer '+token)
        connection.putheader('User-Agent','MediaFlow-Isolated-Publisher-Acceptance')
        connection.putheader('Accept','application/vnd.github+json')
        connection.putheader('Content-Type','application/zip')
        connection.putheader('Content-Length',str(package.stat().st_size));connection.endheaders()
        with frozen_file(package) as source:
            while sent<cutoff:
                block=source.read(min(64*1024,cutoff-sent))
                if not block:break
                connection.send(block);sent+=len(block)
        out['intentional_connection_close']=True
    except (OSError,http.client.HTTPException) as error:
        out['transport_error_type']=type(error).__name__
    finally:
        if connection:connection.close()
        token=None
    out['bytes_sent_before_close']=sent
    if not 0<sent<out['declared_size']:
        raise publisher.PublicationError('A real partial upload was not transmitted; interruption test is not passed.')
    return out


def load_resume(path,repository):
    """Only recover a published transaction whose anonymous read failed."""
    source=publisher.plain(path)
    if source.stat().st_size>2*1024*1024:raise publisher.PublicationError('Resume report exceeds bound.')
    value=json.loads(source.read_text('utf-8'))
    if (value.get('schema_version')!=1 or value.get('scope')!='real_github_isolated_publication_transaction'
            or value.get('repository')!=repository or value.get('passed') is not False
            or value.get('stage') not in ('anonymous-public-download-signature-and-zip-verification',
                'published-repeat-refusal','actual-upload-interruption','actual-remote-hash-conflict',
                'publisher-create-only-checkpoint','formal-signature-tamper-refusal','stable-channel-protection')
            or value.get('faults',{}).get('partial_assets',{}).get('passed') is not True
            or value.get('faults',{}).get('publication_failure',{}).get('passed') is not True
            or not value.get('release_id') or not value.get('test_release_url')):
        raise publisher.PublicationError('Only a retained, actually published transaction at a supported checkpoint can resume.')
    if (value['stage']!='anonymous-public-download-signature-and-zip-verification'
            and value.get('public_verification',{}).get('mirror_public_verified') is not True):
        raise publisher.PublicationError('Later recovery requires an original passed anonymous mirror verification.')
    return value


def run(args):
    directory=Path(args.output).resolve()
    if directory.exists():raise publisher.PublicationError('Use a new acceptance directory; previous evidence is preserved.')
    directory.mkdir(parents=True)
    resume=load_resume(args.resume_from,args.repository) if args.resume_from else None
    if resume and args.tag_prefix and args.tag_prefix!=resume['tag_prefix']:
        raise publisher.PublicationError('Resume tag prefix differs from retained transaction.')
    tag_prefix=resume['tag_prefix'] if resume else (args.tag_prefix or (
        'mediaflow-pipeline-test-'+datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')+'-'+uuid.uuid4().hex[:6]))
    notes=directory/'test-notes.md'
    notes.write_text('MediaFlow isolated publication acceptance only.\n\n'
        'Original formally signed 2.1.0 bytes; NOT a new stable version. '
        'Fault releases may intentionally contain incomplete/corrupt negative-test assets. '
        'All test records are preserved; do not configure these as an updater source.\n',encoding='utf-8')
    state={'schema_version':1,'passed':False,'scope':'real_github_isolated_publication_transaction',
        'production_chain_verified':False,'new_stable_version_published':False,
        'repository':args.repository,'tag_prefix':tag_prefix,'events':[],'faults':{}}
    if resume:
        state.update(resumed_from=str(Path(args.resume_from).resolve()),faults=deepcopy(resume['faults']))
    def stage(name,**details):
        state['stage']=name;state['events'].append({'stage':name,'at':datetime.now(timezone.utc).isoformat(),**details})
        save(directory/'report.json',state);print('阶段：'+name,flush=True)
    def require(condition,message):
        if not condition:raise publisher.PublicationError(message)
    def call_wrapper(tag,name,*,draft=False,create_only=False,artifacts=None,allow_failure=False):
        target=directory/name
        command=[args.pwsh,'-NoLogo','-NoProfile','-NonInteractive','-File',ROOT/'scripts/release_mediaflow.ps1',
            '-Version',args.version,'-TestRelease','-TestRepository',args.repository,'-TestTag',tag,
            '-SignedArtifactsDirectory',artifacts or args.artifacts,'-Gh',args.gh,
            '-ReleaseNotesFile',notes,'-RunDirectory',target]
        if draft:command+=['-TestDraftOnly']
        if create_only:command+=['-TestCreateOnly']
        code,out,_=Runner(ROOT,directory).call(command,'test-powershell-'+name,timeout=2400,
            logs=True,allow_failure=True)
        if code and not allow_failure:raise publisher.PublicationError('Actual TestRelease entry failed; its original reports were retained.')
        return code,json.loads((target/'result.json').read_text('utf-8'))
    stage('formal-content-preflight')
    try:
        seed=test_arguments(args.version,args.repository,tag_prefix+'-resume',args.artifacts,args.gh,notes,
            directory/'unused.json')
        manifest,_,assets,records,formal=publisher.publication_inputs(seed)
        client=github(args.gh,directory)
        identity=client.api('user')['login']
        stable_repo=client.repo(formal)
        require(stable_repo['owner']['login'].casefold()==identity.casefold() and
            stable_repo.get('permissions',{}).get('admin') is True,'Actual owner/admin permission required.')
        stable_before=snapshot(client.api('repos/'+formal+'/releases/latest'))
        if resume:
            prior=json.loads((Path(args.resume_from).resolve().parent/'stable-before.json').read_text('utf-8'))
            require(stable_before==prior,'Stable repository changed since the retained transaction; stopping.')
            require(resume.get('formal_package')==manifest['package'] and
                resume.get('formal_key_id')==manifest['signature']['key_id'],'Resume formal content identity differs.')
        channel_before=deepcopy(load_channel())
        save(directory/'stable-before.json',stable_before)
        repository=client.api('repos/'+args.repository,missing=True)
        if repository is None:
            require(args.create_repository,'Dedicated repository is absent; explicit --create-repository is required.')
            stage('create-isolated-public-repository')
            cli(client,'repo','create',args.repository,'--public','--add-readme',
                '--description','Isolated MediaFlow release pipeline acceptance. NOT the stable update channel.')
            repository=client.repo(args.repository)
        require(repository.get('private') is False and repository['owner']['login'].casefold()==identity.casefold()
            and repository.get('permissions',{}).get('admin') is True,'Dedicated test repository permission check failed.')
        state['authentication']={'login':identity,'method':client.auth_method,'admin':True,'public_repository':True}
        state['formal_package']=manifest['package'];state['formal_key_id']=manifest['signature']['key_id']

        tag=tag_prefix+'-resume'
        if resume:
            stage('resume-published-release-read-only-recheck')
            final=release(client,args.repository,tag)
            prior=json.loads((Path(args.resume_from).resolve().parent/'published-release.json').read_text('utf-8'))
            require(final is not None and final['id']==resume['release_id'] and final['draft'] is False
                and final['prerelease'] is True and snapshot(final)==snapshot(prior),
                'Published remote identity/assets changed; recovery stopped without writes.')
            publisher.verified_asset_urls(final,records,args.repository,tag,published=True)
        else:
            stage('real-partial-state-creation')
            tag=tag_prefix+'-resume'
            current=create_draft(client,args.repository,tag,notes)
            if not current['assets']:
                cli(client,'release','upload',tag,assets[0],'--repo',args.repository,timeout=1800)
            partial=release(client,args.repository,tag)
            require(len(partial['assets'])==1,'Partial-state fixture did not contain exactly one actually uploaded asset.')
            first=partial['assets'][0]
            require(first['size']==records[0]['size'] and first.get('digest')=='sha256:'+records[0]['sha256'],
                'Actual partial upload inventory does not match formal package.')
            save(directory/'partial-before.json',snapshot(partial))
            stage('resume-partial-assets-with-actual-powershell')
            _,draft=call_wrapper(tag,'resume-draft',draft=True)
            complete=release(client,args.repository,tag)
            require(draft['passed'] and draft['draft_verified'] and complete['draft'] is True,
                'Partial asset recovery did not produce a genuinely verified draft.')
            require(next(a for a in complete['assets'] if a['name']==first['name'])['id']==first['id'],
                'Recovery replaced the previously uploaded asset.')
            state['faults']['partial_assets']={'passed':True,'release_id':complete['id'],
                'reused_asset_ids':[first['id']],'new_asset_ids':[a['id'] for a in complete['assets'] if a['id']!=first['id']]}
            save(directory/'complete-draft.json',snapshot(complete))

            stage('real-api-publish-failure')
            before=snapshot(complete)
            code,body=cli(client,'api','--method','PATCH','repos/'+args.repository+'/releases/'+str(complete['id']),
                '-F','draft=false','-F','prerelease=true','-f','make_latest=false','-f','tag_name=..',allow_failure=True)
            try:response=json.loads(body)
            except ValueError:response={}
            after=release(client,args.repository,tag)
            require(code!=0 and str(response.get('status'))=='422' and snapshot(after)==before,
                'Injected invalid publication must actually fail with 422 while preserving the draft.')
            state['faults']['publication_failure']={'passed':True,'actual_http_status':422,
                'injection':'invalid tag_name on actual publication PATCH','draft_unchanged':True}

            stage('actual-test-release-publication')
            _,published=call_wrapper(tag,'publish-recovered-draft')
            final=release(client,args.repository,tag)
            require(published['passed'] and final['draft'] is False and final['prerelease'] is True,
                'Actual prerelease publication was not confirmed.')
            require(final['id']==partial['id'] and next(a for a in final['assets'] if a['name']==first['name'])['id']==first['id'],
                'Recovery must retain the original release and asset identities.')
        state['test_release_url']=final['html_url'];state['release_id']=final['id']
        save(directory/'published-release.json',final)
        if resume and resume.get('public_verification',{}).get('mirror_public_verified') is True:
            stage('revalidate-retained-anonymous-files')
            public_path=Path(resume.get('public_verification_report') or (
                Path(args.resume_from).resolve().parent/'public-verification/result.json'))
            public=json.loads(publisher.plain(public_path).read_text('utf-8'))
            require(public.get('scope')=='real_public_isolated_prerelease_mirror' and
                public.get('passed') is True and public.get('mirrored_release_url')==final['html_url'],
                'Retained public report does not bind this actual mirror.')
            downloads=public_path.parent/'downloads'
            for record in records:
                path=publisher.plain(downloads/record['name'])
                require(path.stat().st_size==record['size'] and publisher.digest(path)==record['sha256'],
                    'Retained actual anonymous download bytes changed; stopping.')
            publisher.prepare_assets(downloads,formal,[])
            state['anonymous_bytes_revalidated_without_redownload']=True
        else:
            stage('anonymous-public-download-signature-and-zip-verification')
            Runner(ROOT,directory).call([sys.executable,'-X','utf8','-B',ROOT/'scripts/release_test_transaction.py','verify-public',
                '--version',args.version,'--repository',args.repository,'--tag',tag,'--artifacts',args.artifacts,
                '--remote',directory/'published-release.json','--output',directory/'public-verification'],
                'anonymous-mirror-download',timeout=2000,env=_environment(directory/'anonymous-process'),logs=True)
            public_path=directory/'public-verification/result.json'
            public=json.loads(public_path.read_text('utf-8'))
        require(public['passed'] and public['mirror_public_verified'],'Anonymous actual mirror verification failed.')
        state['public_verification']=public
        state['public_verification_report']=str(public_path.resolve())
        stage('published-repeat-refusal')
        before=snapshot(final)
        code,repeated=call_wrapper(tag,'published-repeat',allow_failure=True)
        after=release(client,args.repository,tag)
        require(code!=0 and not repeated['passed'] and snapshot(after)==before,
            'Repeated published operation must refuse without changes.')
        state['faults']['published_repeat']={'passed':True,'release_and_asset_ids_unchanged':True,'exit_code':code}

        stage('publisher-create-only-checkpoint')
        creator_tag=tag_prefix+'-creator'
        _,creator=call_wrapper(creator_tag,'publisher-created-draft',create_only=True)
        for attempt in range(6):
            created=release(client,args.repository,creator_tag)
            if created:break
            if attempt<5:time.sleep(1)
        require(creator['passed'] and not creator['draft_verified'] and created is not None
            and created['draft'] is True and created['prerelease'] is True and not created['assets'],
            'Actual publisher draft creation must retain empty, unverified, non-published state.')
        state['publisher_creation']={'passed':True,'release_id':created['id'],'url':created['html_url'],
            'assets_uploaded':0,'published':False,'draft_verified':False}
        creation_report=json.loads((directory/'publisher-created-draft/publication.json').read_text('utf-8'))
        state['publisher_creation']['draft_created_by_this_run']=creation_report['draft_created_by_this_run']

        stage('actual-upload-interruption')
        interrupted_tag=tag_prefix+'-interrupted'
        if resume and resume.get('faults',{}).get('upload_interruption',{}).get('passed') is True:
            prior=resume['faults']['upload_interruption']
            retained=release(client,args.repository,interrupted_tag)
            require(retained is not None and retained['draft'] is True and retained['prerelease'] is True
                and retained['html_url']==prior['retained_release_url']
                and snapshot(retained)['assets']==prior['remote_assets_after_resume'],
                'Previously verified interruption result changed; refusing to replay writes.')
            stage('retained-upload-interruption-state-revalidated')
        else:
            interrupted=create_draft(client,args.repository,interrupted_tag,notes)
            interruption=interrupted_upload(client,args.repository,interrupted,assets[0])
            time.sleep(2)
            incomplete=release(client,args.repository,interrupted_tag)
            save(directory/'interrupted-remote-state.json',snapshot(incomplete))
            code,resumed=call_wrapper(interrupted_tag,'interrupted-resume',draft=True,allow_failure=True)
            retained=release(client,args.repository,interrupted_tag)
            if incomplete['assets']:
                require(code!=0 and not resumed['passed'] and snapshot(retained)==snapshot(incomplete),
                    'An incomplete conflicting asset must be retained and refused, never overwritten.')
                outcome='safe_refusal_incomplete_remote_asset_retained'
            else:
                require(code==0 and resumed['passed'] and resumed['draft_verified'] and retained['draft'] is True,
                    'An interrupted upload with no committed asset must safely recover to a verified draft.')
                outcome='retried_absent_upload_and_verified_draft'
            state['faults']['upload_interruption']={'passed':True,**interruption,'outcome':outcome,
                'retained_release_url':retained['html_url'],'remote_assets_before_resume':snapshot(incomplete)['assets'],
                'remote_assets_after_resume':snapshot(retained)['assets']}

        stage('actual-remote-hash-conflict')
        bad_tag=tag_prefix+'-hash-conflict'
        bad=create_draft(client,args.repository,bad_tag,notes)
        bad_folder=directory/'intentional-corrupt-asset';bad_folder.mkdir()
        bad_file=bad_folder/assets[0].name
        bad_file.write_bytes(b'INTENTIONAL NEGATIVE TEST: not a valid MediaFlow package; never install.\n')
        if not bad['assets']:
            cli(client,'release','upload',bad_tag,bad_file,'--repo',args.repository)
        else:
            require(len(bad['assets'])==1 and bad['assets'][0]['name']==bad_file.name
                and bad['assets'][0]['size']==bad_file.stat().st_size
                and bad['assets'][0].get('digest')=='sha256:'+publisher.digest(bad_file),
                'Existing negative-test asset changed; no overwrite allowed.')
        bad_before=release(client,args.repository,bad_tag)
        code,bad_result=call_wrapper(bad_tag,'remote-hash-refusal',allow_failure=True)
        bad_after=release(client,args.repository,bad_tag)
        require(code!=0 and not bad_result['passed'] and snapshot(bad_after)==snapshot(bad_before),
            'Real remote conflicting bytes must be rejected without deletion or replacement.')
        bad_report=json.loads((directory/'remote-hash-refusal/publication.json').read_text('utf-8'))
        require('Existing asset differs' in bad_report.get('error',''),'Conflict failure was not the expected digest gate.')
        state['faults']['remote_hash']={'passed':True,'retained_release_url':bad_after['html_url'],
            'unchanged_conflicting_assets':snapshot(bad_after)['assets']}

        stage('formal-signature-tamper-refusal')
        tampered=directory/'invalid-signature-input';tampered.mkdir()
        changed=deepcopy(manifest);changed['release_notes']+=' INTENTIONAL INVALID SIGNATURE TEST'
        save(tampered/'release.json',changed)
        signature_tag=tag_prefix+'-signature-rejected'
        require(release(client,args.repository,signature_tag) is None,'Unique signature test tag unexpectedly exists.')
        code,refused=call_wrapper(signature_tag,'signature-refusal',artifacts=tampered,allow_failure=True)
        require(code!=0 and refused.get('stage')=='formal-content-and-isolation-check'
            and release(client,args.repository,signature_tag) is None,'Signature rejection must occur before GitHub writes.')
        state['faults']['signature']={'passed':True,'signed_field_changed':'release_notes',
            'formal_public_key_unchanged':True,'remote_release_created':False,'failure_report':refused}

        stage('stable-channel-protection')
        stable_after=snapshot(client.api('repos/'+formal+'/releases/latest'))
        save(directory/'stable-after.json',stable_after)
        require(stable_after==stable_before and stable_after['tag_name']=='v'+args.version
            and load_channel()==channel_before,'Stable latest release, assets or client trust/channel changed.')
        identity={'app_version':manifest['version'],'build_id':manifest['build_id']}
        check=UpdateManager(state_dir=directory/'stable-client-check',release_root=directory/'stable-installation-context',
            current_identity=identity,current_data_dir=directory/'never-opened-stable-user-data').check_updates()
        require(check['authenticated'] is True and check['status']=='up_to_date'
            and check['manifest']['build_id']==manifest['build_id'],'Actual HTTPS stable-client check must still find the original stable build.')
        state['stable_protection']={'passed':True,'latest_release_id':stable_after['id'],
            'latest_tag':stable_after['tag_name'],'assets_unchanged':len(stable_after['assets']),
            'client_status':check['status'],'source_url':channel_before['source_url'],
            'formal_keys_unchanged':True}
        require(client.api('repos/'+args.repository+'/releases/latest',missing=True) is None,
            'The isolated repository must not expose a latest stable release.')
        state['stable_protection']['test_repository_has_no_latest_stable_release']=True
        state['passed']=True;stage('real-test-transaction-complete')
        return state
    except Exception as error:
        state.update(error_type=type(error).__name__,error=str(error)
            if isinstance(error,publisher.PublicationError) else 'Real test stopped; original stage reports and all remote records retained.')
        raise
    finally:save(directory/'report.json',state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',required=True);parser.add_argument('--repository',required=True)
    parser.add_argument('--artifacts',required=True);parser.add_argument('--gh',required=True)
    parser.add_argument('--pwsh',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--create-repository',action='store_true')
    parser.add_argument('--tag-prefix',default='',help='Explicitly reuse a retained test draft with a new local report directory')
    parser.add_argument('--resume-from',default='',help='Revalidate a retained published transaction before continuing supported failed acceptance stages')
    args=parser.parse_args()
    try:
        result=run(args)
        print(json.dumps({key:result[key] for key in ('passed','test_release_url','production_chain_verified')}))
        return 0
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__,
            'message':str(error) if isinstance(error,publisher.PublicationError) else 'Actual transaction acceptance stopped; inspect retained reports.'}))
        return 1


if __name__=='__main__':raise SystemExit(main())
