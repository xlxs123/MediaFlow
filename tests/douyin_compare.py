"""Read the actual app-owned browser session; write allowlisted diagnostic data only."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.parse import parse_qsl, urlencode, urlsplit, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = {'A': '7657489271422549747', 'B': '7526879737634950451'}
DYNAMIC = re.compile(r'(?:sign|token|auth|credential|expires|expire|nonce|timestamp|^ts$|^t$|range|^bytes$|^start$|^end$)', re.I)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:20]


def resource(value):
    p = urlsplit(str(value))
    if p.scheme == 'blob':
        return 'blob:' + digest(value)
    stable = [(k, v) for k, v in parse_qsl(p.query) if not DYNAMIC.search(k)]
    return digest(p.scheme + '://' + p.netloc + p.path + '?' + urlencode(sorted(stable)))


def path_resource(value):
    p = urlsplit(str(value))
    return digest((p.hostname or '') + p.path)


def safe_url(value):
    value = str(value)
    p = urlsplit(value)
    if p.scheme == 'blob':
        return 'blob:<id:' + digest(value) + '>'
    if p.scheme not in ('http', 'https'):
        return p.scheme + ':<redacted>' if p.scheme else ''
    suffix = Path(p.path).suffix
    suffix = suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,7}', suffix) else ''
    return p.scheme + '://' + (p.hostname or '') + '/<path:' + digest(p.path) + '>' + suffix


def page_url(value):
    p = urlsplit(str(value))
    q = [(k, v) for k, v in parse_qsl(p.query) if k in ('modal_id', 'showTab', 'from_tab_name')]
    return p.scheme + '://' + (p.hostname or '') + p.path + ('?' + urlencode(q) if q else '')


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


PAGE_STATE = r'''(()=>{
 const visible=e=>{const r=e.getBoundingClientRect();let s=true;for(let a=e,d=0;a&&d<16;a=a.parentElement,d++){const c=getComputedStyle(a);if(c.display==='none'||c.visibility==='hidden'||Number(c.opacity||1)<=0)s=false;}return s&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;};
 const ids=e=>{const result=new Set();for(let a=e,d=0;a&&d<7;a=a.parentElement,d++){
   for(const k of ['data-aweme-id','data-item-id','data-video-id']){const v=a.getAttribute(k);if(v&&/^\d{15,24}$/.test(v))result.add(v);}
   if(d<4)for(const n of [...a.querySelectorAll('a[href]')].slice(0,20)){for(const m of n.href.matchAll(/(?:\/video\/|modal_id=)(\d{15,24})/g))result.add(m[1]);}}
   return [...result].slice(0,16);};
 const videos=[...document.querySelectorAll('video')].slice(0,32).map(v=>({src:v.src,current_src:v.currentSrc,
  visible:visible(v),paused:v.paused,ended:v.ended,ready_state:v.readyState,error_code:v.error?.code||0,
  duration:Number.isFinite(v.duration)?v.duration:null,current_time:v.currentTime,width:v.videoWidth,height:v.videoHeight,
  rect:{width:v.getBoundingClientRect().width,height:v.getBoundingClientRect().height},nearby_work_ids:ids(v)}));
 const dialogs=[...document.querySelectorAll('[role="dialog"],[class*="login"]')].filter(visible).slice(0,30);
 return {href:location.href,modal_id:new URL(location.href).searchParams.get('modal_id'),document_state:document.readyState,
  diagnostic_paused:window.__mediaflowDiagnosticPaused===true,
  videos,mse:typeof MediaSource!=='undefined',login_prompt:dialogs.some(e=>/扫码登录|手机号登录|验证码登录/.test(e.innerText||''))};})()'''


class Evidence:
    def __init__(self, target):
        self.target = target
        self.start = time.monotonic()
        self.counts = Counter()
        self.statuses = Counter()
        self.requests = {}
        self.targets = {}
        self.dom = []
        self.pages = []
        self.candidate_timeline = []
        self.observations = {}
        self.api_targets = []
        self.api_resources = {}
        self.api_paths = {}
        self.api_tokens = set()
        self.playback_guard = []
        self.last_candidates = []
        self.navigation = []
        self._previous = None

    def cdp(self, message):
        method = message.get('method', '')
        p = message.get('params', {})
        session = str(message.get('sessionId') or message.get('_session') or '')
        self.counts[method] += 1
        key = digest(session + ':' + str(p.get('requestId', '')))
        if method == 'Target.attachedToTarget':
            info = p.get('targetInfo', {})
            self.targets[digest(p.get('sessionId', ''))] = {'type': info.get('type'), 'url': safe_url(info.get('url', ''))}
        elif method == 'Network.requestWillBeSent':
            req = p.get('request', {})
            url = req.get('url', '')
            mime_path = urlsplit(url).path.lower()
            self.counts['request:' + p.get('type', 'Other')] += 1
            self.requests[key] = {'request': key, 'resource': resource(url), 'url': safe_url(url),
                'resource_type': p.get('type'), 'frame': digest(p.get('frameId', '')), 'session': digest(session),
                'initiator_type': p.get('initiator', {}).get('type'), 'method': req.get('method'),
                'range': str(req.get('headers', {}).get('Range', req.get('headers', {}).get('range', '')))[:100],
                'second':round(time.monotonic()-self.start,3),
                'media_related': p.get('type') == 'Media' or mime_path.endswith(('.mp4','.m4s','.m3u8','.mpd','.webm','.m4a'))}
        elif method == 'Network.responseReceived':
            resp = p.get('response', {})
            mime = str(resp.get('mimeType', '')).lower()
            status = int(resp.get('status', 0))
            self.statuses[str(status)] += 1
            self.counts['response:' + p.get('type', 'Other')] += 1
            headers = {k.lower(): str(v) for k, v in resp.get('headers', {}).items()}
            record = self.requests.setdefault(key, {'request': key, 'resource': resource(resp.get('url', '')),
                                                      'url': safe_url(resp.get('url', ''))})
            record.update(status=status, content_type=headers.get('content-type', mime),
                content_length=headers.get('content-length'),content_range=headers.get('content-range'),
                media_related=record.get('media_related', False) or mime.startswith(('video/','audio/'))
                              or mime in ('application/dash+xml','application/vnd.apple.mpegurl'),
                protocol=resp.get('protocol'), from_disk_cache=resp.get('fromDiskCache', False))
        elif method == 'Network.loadingFailed':
            self.requests.setdefault(key, {'request': key}).update(failure=str(p.get('errorText', ''))[:150],
                blocked_reason=p.get('blockedReason'), cancelled=p.get('canceled'))

    def observation(self, obs):
        headers = {k.lower(): v for k, v in obs.response_headers.items()}
        key = obs.request_id
        self.observations[key] = {'request': digest(key), 'resource': resource(obs.context.final_url),
            'path_resource':path_resource(obs.context.final_url),
            'url': safe_url(obs.context.final_url), 'type': obs.resource_type, 'status': obs.status,
            'content_type': obs.content_type, 'content_length': obs.content_length,
            'content_range': headers.get('content-range'), 'body_bytes': len(obs.body or b''),
            'body_complete': obs.body_complete, 'context_complete': obs.context_complete,
            'frame': digest(obs.frame_id), 'has_cookie': bool(obs.context.cookies),
            'has_authorization': bool(obs.context.headers.get('authorization'))}
        if obs.body and str(headers.get('content-range', '')).startswith('bytes 0-'):
            from core.media_details import _mp4
            from core.media_discovery import _complete_moov
            details = _mp4(obs.body, obs.body_complete)
            self.observations[key]['container_evidence'] = {
                'first_box': obs.body[4:8].decode('ascii', 'replace'),
                'complete_moov': _complete_moov(obs.body, obs.body_complete),
                **{k:v for k,v in details.items() if k in ('duration','width','height','videoCodec','audioCodec','has_audio','container')}}
        if obs.body_complete and obs.body and obs.resource_type in ('Fetch', 'XHR', 'Document'):
            try:
                data = json.loads(obs.body)
            except (ValueError, UnicodeError, RecursionError):
                return
            stack, seen = [data], 0
            while stack and seen < 25000:
                item = stack.pop(); seen += 1
                if isinstance(item, dict):
                    work = str(item.get('aweme_id') or item.get('item_id') or '')
                    if work == self.target:
                        video = item.get('video') or {}
                        urls = []
                        pending = [video]
                        while pending and len(urls) < 128:
                            value = pending.pop()
                            if isinstance(value, dict):
                                for name, token in value.items():
                                    if name in ('uri','vid','video_id','file_id') and isinstance(token, str) and len(token) >= 12:
                                        self.api_tokens.add(token)
                                pending.extend(value.values())
                            elif isinstance(value, list): pending.extend(value[:200])
                            elif isinstance(value, str) and value.startswith(('https://','http://')): urls.append(value)
                        for url in urls:
                            self.api_resources[resource(url)] = work
                            self.api_paths[path_resource(url)] = work
                        entry = {'work_id': work, 'duration_ms': video.get('duration'),
                            'width': video.get('width'), 'height': video.get('height'),
                            'resource_ids': sorted({resource(url) for url in urls}),
                            'path_resource_ids':sorted({path_resource(url) for url in urls}),
                            'source': safe_url(obs.context.final_url)}
                        if entry not in self.api_targets: self.api_targets.append(entry)
                    stack.extend(item.values())
                elif isinstance(item, list): stack.extend(item[:500])

    def dom_snapshot(self, value):
        record = {'second': round(time.monotonic()-self.start, 3), 'page_url': page_url(value.get('page_url','')),
            'frame': digest(value.get('frame_id','')), 'videos': []}
        for video in value.get('videos', []):
            item = {k: video.get(k) for k in ('element_id','tag','video_id','duration','current_time','paused',
                'visible','area','is_main','ready_state','width','height','progressing','ended','media_keys','encrypted')}
            item.update(src=safe_url(video.get('src','')),current_src=safe_url(video.get('current_src','')),
                src_resource=resource(video.get('src','')),current_resource=resource(video.get('current_src','')))
            record['videos'].append(item)
        self.dom.append(record)
        self.dom = self.dom[-180:]

    def candidates(self, values, *args):
        current = []
        for c in values:
            evidence = c.metadata.get('confidence_evidence', {})
            current.append({'id': c.id,'resource': resource(c.final_url),
                'path_resource':path_resource(c.final_url),'url': safe_url(c.final_url),
                'protocol': c.media_type,'classification': c.classification,'status': c.http_status,
                'content_type': c.content_type,'duration': c.duration,'width':c.width,'height':c.height,
                'detail_duration':c.metadata.get('detail_duration'),'has_audio':c.metadata.get('has_audio'),
                'intrinsic_mp4':{k:v for k,v in c.metadata.get('intrinsic_mp4',{}).items()
                    if k in ('has_video','duration','width','height','videoCodec','audioCodec',
                             'video_track_count','audio_track_count','unknown_track_count')},
                'api_identity_match':any(token in unquote(c.final_url) for token in self.api_tokens),
                'confidence': c.confidence,'quality': c.qualityScore,'drm': c.drm,'validated':c.validated,
                'downloadable': c.downloadable,'requires_confirmation':c.requires_confirmation,
                'unavailable_reasons': [re.sub(r'https?://[^\s\"<>]+',lambda m:safe_url(m[0]),str(v))[:250] for v in c.unavailable_reasons],
                'associated':evidence.get('associated'),'current_player':evidence.get('current_player'),
                'visible':evidence.get('visible'),'hidden':evidence.get('hidden'),
                'fragment_only':c.metadata.get('fragment_only'),'resource_role':c.metadata.get('resource_role'),
                'partial_resource':c.metadata.get('partial_resource'),'segment_mode':c.metadata.get('segment_mode'),
                'protocol_validated':c.metadata.get('protocol_validated'),
                'direct_player_verified':c.metadata.get('direct_player_verified'),
                'progressive_direct_verified':c.metadata.get('progressive_direct_verified'),
                'association_kind':c.metadata.get('association_kind'),
                'dom_video_id':c.metadata.get('dom_video_id'),
                'confidence_reasons':[re.sub(r'https?://[^\s\"<>]+',lambda m:safe_url(m[0]),str(v))[:300] for v in c.confidenceReasons],
                'video_id':str(c.metadata.get('video_id','')) if str(c.metadata.get('video_id','')) in SAMPLES.values() else '',
                'frame':digest(c.frame_id),'source_types':sorted(c.sources),
                'source_page':page_url(c.metadata.get('source_page') or c.metadata.get('page_url') or '')})
        summary = {'candidates':len(current),'complete_candidates':sum(c['protocol'] in ('Direct','HLS','DASH') for c in current),
            'segments':sum(c['protocol']=='Segment' for c in current),'low_confidence':sum(c['confidence']<40 for c in current),
            'downloadable':sum(c['downloadable'] for c in current)}
        marker = json.dumps(current, sort_keys=True)
        if marker != self._previous:
            self.candidate_timeline.append({'second':round(time.monotonic()-self.start,3),**summary,'values':current})
            self._previous=marker
        self.last_candidates=current

    def page_state(self, value):
        if not isinstance(value, dict): return
        value=deepcopy(value)
        value['href']=page_url(value.get('href',''))
        for video in value.get('videos',[]):
            for key in ('src','current_src'):
                video[key+'_resource']=resource(video.get(key,''))
                video[key]=safe_url(video.get(key,''))
        value['second']=round(time.monotonic()-self.start,3)
        self.pages.append(value)

    def report(self, dialog):
        records = list(self.requests.values())
        media = [r for r in records if r.get('media_related')]
        return {'target_work_id':self.target,'capture_seconds':round(time.monotonic()-self.start,3),
            'counts':dict(self.counts),'http_statuses':dict(self.statuses),'requests':records,
            'media_requests':len(media),'deduplicated_media_resources':len({r.get('resource') for r in media}),
            'targets':self.targets,'observations':list(self.observations.values()),'dom':self.dom,'pages':self.pages,
            'api_target_records':self.api_targets,'candidate_timeline':self.candidate_timeline,
            'api_token_hashes':sorted(digest(token) for token in self.api_tokens),
            'navigation':self.navigation,
            'playback_guard':self.playback_guard,
            'candidates':self.last_candidates,'current_page_url':page_url(dialog.page.url().toString()),
            'ui_address':page_url(dialog.page_url.text()),'discovery_counts':dialog.discovery_counts(),
            'network_diagnostics':dialog.observer.network_diagnostics(),
            'selected_candidate':dialog.selected_candidate().id if dialog.selected_candidate() else None,
            'api_resource_matches':{c['id']:self.api_resources.get(c['resource']) for c in self.last_candidates},
            'api_path_matches':{c['id']:self.api_paths.get(c['path_resource']) for c in self.last_candidates}}


def main(app=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output','--douyin-compare',dest='output',type=Path,required=True)
    parser.add_argument('--sample',choices=('A','B'),default='B')
    parser.add_argument('--seconds',type=int,default=65)
    parser.add_argument('--probe-resources',action='store_true')
    parser.add_argument('--download',action='store_true')
    parser.add_argument('--download-timeout',type=int,default=240)
    parser.add_argument('--native-smoke',action='store_true')
    args=parser.parse_args()
    out=args.output.resolve()
    if out.exists(): raise SystemExit('Use a new output directory')
    out.mkdir(parents=True)
    os.environ.setdefault('QT_QPA_PLATFORM','windows')
    from core.browser_runtime import prepare_browser_runtime
    prepare_browser_runtime()
    from PySide6.QtCore import QCoreApplication,Qt
    if app is None:QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow
    from ui.app_setup import configure_app
    from utils.config import ConfigStore
    from ui.webview2_browser import browser_profile_path
    from utils.build_info import runtime_identity
    from tests.live_release_check import _pump,_activate_player,_PLAY_VISIBLE,_select_candidate,_confirm_selected,_qobject_valid,_valid_av_outputs
    app=app or QApplication(sys.argv);configure_app(app);app.setQuitOnLastWindowClosed(False)
    profile=browser_profile_path()
    require_profile=profile.is_dir()
    save(out/'session.json',{'build':runtime_identity(),'profile_path':str(profile),'profile_exists':require_profile,
        'profile_mode':'existing_user_persistent','credentials_exported':False,'sensitive_urls_saved':False,
        'settings_tasks':'isolated_from_user_settings_and_tasks'})
    if not require_profile: raise SystemExit('Existing app profile is unavailable')
    store=ConfigStore(out/'private-config/config.json')
    settings=ConfigStore().load();settings['output_directory']=str(out/'downloads');store.save(settings)
    window=MainWindow(store);window.browser_profile_directory=profile;window.showMinimized()
    dialog=None
    try:
        if not _pump(app,45,lambda:window.detector is None and window.yt_info.found and window.ff_info.found):
            raise RuntimeError('TOOLS_UNAVAILABLE')
        for label in (args.sample,):
            work=SAMPLES[label];url=f'https://www.douyin.com/user/self?from_tab_name=main&modal_id={work}&showTab=record'
            dialog=window.open_discovery(page_url=url,activate=False)
            dialog.showMinimized();dialog.listen_seconds=args.seconds+20
            evidence=Evidence(work)
            dialog.observer.observation.connect(evidence.observation)
            dialog.observer.dom.connect(evidence.dom_snapshot)
            dialog.worker.candidates_changed.connect(evidence.candidates)
            dialog.find()
            from PySide6.QtWebEngineCore import QWebEngineScript
            guard=QWebEngineScript()
            guard.setName('diagnostic-target-guard')
            guard.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            guard.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            guard.setSourceCode("""(()=>{const target=TARGET;const check=()=>{
                if(new URL(location.href).searchParams.get('modal_id')!==target)return;
                for(const v of document.querySelectorAll('video')){
                    const r=v.getBoundingClientRect();
                    if(r.width>0&&r.height>0&&v.currentTime>=4&&v.duration>0){
                        window.__mediaflowDiagnosticPaused=true;v.pause();}}
                };document.addEventListener('timeupdate',check,true);setInterval(check,150);})()""".replace('TARGET',json.dumps(work)))
            dialog.page.scripts().insert(guard)
            dialog.page.cdpMessage.connect(evidence.cdp)
            def navigation(event, value='', current=dialog, trace=evidence):
                trace.navigation.append({'event':event,'url':page_url(value),
                    'generation':current.worker.generation,'second':round(time.monotonic()-trace.start,3)})
            dialog.page.urlChanged.connect(lambda u: navigation('urlChanged',u.toString()))
            dialog.page.loadStarted.connect(lambda: navigation('loadStarted'))
            dialog.page.loadFinished.connect(lambda ok: navigation('loadFinished',str(ok)))
            began=time.monotonic();last_query=-10;last_play=-10
            guarded=[False]
            def state_received(value,current=dialog,trace=evidence,target=work):
                trace.page_state(value)
                if not isinstance(value,dict) or str(value.get('modal_id'))!=target:return
                if value.get('diagnostic_paused') and not guarded[0]:
                    guarded[0]=True
                    trace.playback_guard.append({'second':round(time.monotonic()-trace.start,3),
                        'reason':'page-local timeupdate guard paused requested work after playback proof'})
                videos=[v for v in value.get('videos',[]) if v.get('visible') and not v.get('paused')
                        and v.get('duration') and v['duration']-v.get('current_time',0)<4]
                if videos and not guarded[0]:
                    guarded[0]=True
                    trace.playback_guard.append({'second':round(time.monotonic()-trace.start,3),
                        'reason':'pause before auto-next; keep requested work active',
                        'duration':videos[0]['duration'],'current_time':videos[0]['current_time']})
                    current.page.runJavaScript("document.querySelectorAll('video').forEach(v=>v.pause())")
            while time.monotonic()-began<args.seconds:
                _pump(app,.1)
                elapsed=time.monotonic()-began
                if elapsed-last_query>=1:
                    last_query=elapsed
                    dialog.page.runJavaScript(PAGE_STATE,state_received)
                    evidence.candidates(dialog._candidates)
                    save(out/(label+'.json'),evidence.report(dialog))
                if elapsed>8 and elapsed-last_play>=10 and dialog._document_ready and not guarded[0]:
                    last_play=elapsed
                    dialog.page.runJavaScript(_PLAY_VISIBLE,lambda v,current=dialog:_activate_player(current.observer,v))
            _pump(app,2)
            if args.probe_resources:
                from core.media_guard import check_media
                def probe(candidate):
                    context=replace(candidate.request_context, headers={k:v for k,v in candidate.request_context.headers.items()
                        if k.lower() not in ('range','if-range')}, inherit_range=False)
                    result={'candidate':candidate.id,'resource':resource(candidate.final_url)}
                    try:
                        inspection=check_media(context)
                        root=inspection.root
                        result.update(status=root.http_status,protocol=root.info.media_type,drm=root.info.drm,
                            headers={k:v for k,v in root.response_headers.items() if k.lower() in
                                ('content-length','content-range','content-type','accept-ranges')},
                            same_url=root.context.final_url==candidate.final_url,prefix_bytes=len(root.data))
                    except Exception as error:
                        result.update(error_type=type(error).__name__,error_code=getattr(error,'code',''))
                    return result
                values=[c for c in dialog._candidates if c.context_complete and c.metadata.get('partial_resource')
                    and not c.metadata.get('segment_mode')][:4]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(probe,c) for c in values]
                    _pump(app,90,lambda:all(f.done() for f in futures))
                    save(out/(label+'-probes.json'),[f.result() for f in futures])
            evidence.candidates(dialog._candidates)
            save(out/(label+'.json'),evidence.report(dialog))
            print(json.dumps({'sample':label,'candidates':len(evidence.last_candidates),
                'downloadable':sum(c['downloadable'] for c in evidence.last_candidates),
                'dom_snapshots':len(evidence.dom),'requests':len(evidence.requests)},ensure_ascii=True),flush=True)
            if args.download:
                from core.output_validator import validate_output
                import subprocess
                result={'success':False,'target_work_id':work,'selection_path':'production UI selection and confirmation',
                        'simulated_media':False,'build':runtime_identity()}
                save(out/(label+'-download.json'),result)
                if not evidence.pages or str(evidence.pages[-1].get('modal_id'))!=work:
                    raise RuntimeError('TARGET_PAGE_CHANGED')
                eligible=[c for c in dialog._candidates if c.downloadable
                    and c.metadata.get('progressive_direct_verified')
                    and c.metadata.get('confidence_evidence',{}).get('current_player')]
                if len(eligible)!=1:raise RuntimeError('CURRENT_PLAYER_AMBIGUOUS')
                selected=eligible[0]
                result.update(selected=next(c for c in evidence.last_candidates if c['id']==selected.id),
                    page_url=page_url(dialog.page.url().toString()),expected_duration=selected.duration)
                if not _select_candidate(dialog,selected):raise RuntimeError('UI_SELECTION_FAILED')
                result.update(_confirm_selected(app,dialog))
                dialog=None
                _pump(app,20,lambda:window._discovery_dialog is None)
                job=window.add_current_to_queue()
                if job is None:raise RuntimeError('QUEUE_IMPORT_FAILED')
                window.start_queue()
                complete=_pump(app,args.download_timeout,lambda:job.result is not None and not window.queue.active_count)
                result.update(download_completed=complete,download_state=job.state)
                output=job.result or {}
                result['download']={k:output.get(k) for k in ('success','files','error_code','output_validation')}
                if not complete or not output.get('success'):
                    save(out/(label+'-download.json'),result)
                    raise RuntimeError('DOWNLOAD_FAILED')
                probes=[validate_output(path,ffmpeg_path=window.ff_info.path,expected_extension='mp4') for path in output.get('files',[])]
                result.update(ffprobe=probes,mp4_has_video_and_audio=_valid_av_outputs(probes),
                    target_duration_verified=bool(probes and all(abs(p['duration']-selected.duration)<0.5 for p in probes)))
                decodes=[]
                for path in output.get('files',[]):
                    decoded=subprocess.run([window.ff_info.path,'-v','error','-i',str(path),'-f','null','-'],
                        capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    decodes.append({'file':str(path),'returncode':decoded.returncode,'error_bytes':len(decoded.stderr),
                        'passed':decoded.returncode==0 and not decoded.stderr.strip()})
                result.update(ffmpeg_full_decode=decodes,success=result['mp4_has_video_and_audio']
                    and result['target_duration_verified'] and bool(decodes) and all(d['passed'] for d in decodes))
                save(out/(label+'-download.json'),result)
                if not result['success']:raise RuntimeError('OUTPUT_VALIDATION_FAILED')
                _pump(app,30,lambda:not window.thumbnails.busy)
                print(json.dumps({'sample':label,'download_verified':result['success'],'duration':probes[0]['duration']}),flush=True)
                continue
            dialog.reject();_pump(app,20,lambda:window._discovery_dialog is None);dialog=None
        return 0
    except Exception as error:
        save(out/'collector-error.json',{'exception_type':type(error).__name__,'code':getattr(error,'code',''),
            'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                      for f in traceback.extract_tb(error.__traceback__)],
            'message':str(error) if str(error) in ('TOOLS_UNAVAILABLE','TARGET_PAGE_CHANGED','CURRENT_PLAYER_AMBIGUOUS',
                'UI_SELECTION_FAILED','QUEUE_IMPORT_FAILED','DOWNLOAD_FAILED','OUTPUT_VALIDATION_FAILED') else '<details omitted to protect session>'})
        return 1
    finally:
        window.queue.stop_all();_pump(app,20,lambda:not window.queue.active_count)
        if _qobject_valid(dialog) and not dialog._closing:
            dialog.reject();_pump(app,20,lambda:window._discovery_dialog is None)
        window.close();_pump(app,20,lambda:window._webview2_profile is None or window._webview2_profile.host._finished_once)


if __name__=='__main__':
    raise SystemExit(main())
