"""同一入口验证源码和最终 EXE 的真实网页、候选确认及原队列下载。

本入口只访问明确列出的公开测试页面。每次运行创建独立的未登录
BrowserProfile；不导入系统 Cookie，不保存请求正文、凭据或媒体签名 URL。
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
import time
import traceback
import types
from urllib.parse import urlsplit, urlunsplit
import uuid


PAGES = {
    'B': 'https://www.bilibili.com/video/BV1fKeg6mEhi',
    'A': 'https://www.bilibili.com/video/BV1CjaQ6NE42',
    'douyin': 'https://www.douyin.com/jingxuan?modal_id=7695103317542967537',
    'rou': 'https://rou.video/v/cm5nlwtpm0000jpseusy9ni66',
}
_MP4_FORMATS = frozenset(('mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'))
_MANIFEST_MIMES = frozenset(('application/dash+xml', 'application/vnd.apple.mpegurl',
                           'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'))
_PAGE_STATE = r'''(()=>JSON.stringify({document_state:document.readyState,
    inline_playinfo_marker:typeof window.__playinfo__==='object'&&!!window.__playinfo__,
    mse:typeof MediaSource!=='undefined',
    players:[...document.querySelectorAll('video')].slice(0,8).map(v=>({
      ready_state:v.readyState,error_code:v.error?.code||0,paused:v.paused,
      current_time:Number.isFinite(v.currentTime)?v.currentTime:0,
      duration:Number.isFinite(v.duration)?v.duration:null,
      width:v.videoWidth,height:v.videoHeight}))}))()'''
_PLAY_VISIBLE = r'''(()=>{const videos=[...document.querySelectorAll('video')].map(v=>{
    const r=v.getBoundingClientRect();let shown=true;
    for(let e=v,depth=0;e&&depth<16;e=e.parentElement,depth++){
        const s=getComputedStyle(e);if(s.display==='none'||s.visibility==='hidden'||
        s.visibility==='collapse'||Number(s.opacity||1)<=0)shown=false;}
    const area=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*
               Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0));
    return {v,area,shown};}).filter(x=>x.shown&&x.area>0&&x.v.readyState>=1)
    .sort((a,b)=>b.area-a.area);
    const v=videos[0]?.v;if(!v)return null;v.muted=true;
    const r=v.getBoundingClientRect();return {paused:v.paused,progress:v.currentTime>1,
    x:(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2,
    y:(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2};})()'''


def _positive_seconds(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('时间必须是正整数秒。') from None
    if not 1 <= number <= 3600:
        raise argparse.ArgumentTypeError('时间范围为 1–3600 秒。')
    return number


def _activate_player(observer, value):
    """普通播放器点击走浏览器原生 Input，触发网页自己的播放状态。"""
    if not isinstance(value, dict):
        return False
    if value.get('paused') is True:
        coordinates = [value.get('x'), value.get('y')]
        if all(type(number) in (int, float) and 0 <= number <= 16384 for number in coordinates):
            params = dict(x=coordinates[0], y=coordinates[1], button='left', clickCount=1)
            observer._send('Input.dispatchMouseEvent', {**params, 'type': 'mousePressed', 'buttons': 1})
            observer._send('Input.dispatchMouseEvent', {**params, 'type': 'mouseReleased', 'buttons': 0})
    return value.get('progress') is True


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-release-check', type=Path, required=True)
    parser.add_argument('--case', choices=tuple(PAGES) + ('all',), default='all')
    parser.add_argument('--capture-seconds', type=_positive_seconds, default=40)
    parser.add_argument('--download-timeout', type=_positive_seconds, default=480)
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--deep-find', action='store_true')
    parser.add_argument('--visible-browser', action='store_true')
    parser.add_argument('--native-smoke', action='store_true')
    parser.add_argument('--rou-url', default='')
    parser.add_argument('--no-auto-play', action='store_true')
    values = list(argv)
    if values and not values[0].startswith('--'):
        values = values[1:]
    args = parser.parse_args(values)
    if args.rou_url:
        parts = urlsplit(args.rou_url)
        if (args.case != 'rou' or parts.scheme != 'https' or parts.hostname != 'rou.video'
                or parts.username or parts.password or parts.query or parts.fragment
                or not re.fullmatch(r'/v/[A-Za-z0-9]+', parts.path)):
            parser.error('--rou-url 仅接受明确授权的 https://rou.video/v/{视频ID} 页面。')
    return args


def _public_url(value):
    """日志中的 URL 仅保留无凭据、无 query 的公开路径。"""
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in ('http', 'https'):
            return '[非 HTTP 地址]'
        return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
    except ValueError:
        return '[无效地址]'


def _safe_text(value, redactor=None):
    from utils.security import Redactor
    text = (redactor or Redactor()).redact(str(value))
    return re.sub(r'https?://[^\s<>"\']+', lambda match: _public_url(match.group()), text)[:4000]


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _pump(app, seconds, predicate=None):
    from PySide6.QtCore import QCoreApplication, QEvent
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if predicate is not None and predicate():
            return True
        time.sleep(.01)
    return bool(predicate and predicate())


class CaptureEvidence:
    """只读观测计数；不向发现服务注入测试数据或主动获取响应。"""

    def __init__(self):
        self.counts = Counter()
        self.requests = OrderedDict()
        self.playurl_bodies = set()
        self.observed_bodies = set()
        self._playurl_json_seen = set()
        self.timeline = []
        self._previous = None
        self.started = time.monotonic()

    def cdp(self, message):
        if not isinstance(message, dict):
            return
        method, params = message.get('method'), message.get('params', {})
        if not isinstance(params, dict):
            return
        identity = (str(message.get('sessionId', '')), str(params.get('requestId', '')))
        if not identity[1]:
            return
        if method == 'Network.requestWillBeSent':
            request = params.get('request', {})
            if not isinstance(request, dict):
                return
            self.counts['page_requests'] += 1
            path = urlsplit(str(request.get('url', ''))).path.lower()
            playurl = 'playurl' in path
            self.requests[identity] = {'playurl': playurl, 'type': str(params.get('type', '')),
                                       'media': False}
            self.counts['playurl_requests'] += int(playurl)
            self.requests.move_to_end(identity)
            while len(self.requests) > 8192:
                self.requests.popitem(last=False)
        elif method == 'Network.responseReceived':
            response = params.get('response', {})
            if not isinstance(response, dict):
                return
            headers = response.get('headers', {})
            headers = {str(k).lower(): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
            mime = str(headers.get('content-type') or response.get('mimeType', '')).partition(';')[0].lower()
            record = self.requests.get(identity, {})
            if record.get('playurl'):
                self.counts['playurl_responses'] += 1
                self.counts['playurl_http_success'] += int(200 <= response.get('status', 0) < 300)
            media = (params.get('type') == 'Media' or mime.startswith(('video/', 'audio/'))
                     or mime in _MANIFEST_MIMES)
            if media and not record.get('media'):
                self.counts['media_responses'] += 1
                record['media'] = True
            if response.get('status') == 206:
                self.counts['range_206_responses'] += 1

    def body_result(self, identity, result, ok):
        """记录原监听器 getResponseBody 的原始完成状态，不重复发出命令。"""
        self.counts['cdp_body_completed'] += 1
        success = bool(ok and isinstance(result, dict) and isinstance(result.get('body'), str))
        self.counts['cdp_body_success' if success else 'cdp_body_failed'] += 1
        if not success:
            return
        if self.requests.get(identity, {}).get('playurl'):
            self.playurl_bodies.add(identity)
        # 不在诊断侧重新解析正文；只有生产观察结果会进入解析指标。

    def observation(self, value):
        identity = str(value.request_id)
        if value.body is not None:
            self.observed_bodies.add(identity)
            if ('playurl' in urlsplit(value.context.url).path.lower()
                    and identity not in self._playurl_json_seen):
                self._playurl_json_seen.add(identity)
                self.counts['playurl_body_seen'] += int(value.body_complete)
                try:
                    json.loads(value.body)
                except (ValueError, UnicodeError, RecursionError):
                    self.counts['playurl_json_failed'] += 1
                else:
                    self.counts['playurl_json_success'] += 1

    def candidates(self, values, *_):
        snapshot = {'candidates': len(values),
                    'downloadable': sum(c.downloadable for c in values),
                    'dash_pairs': sum(bool(c.video_track and c.audio_track) for c in values),
                    'segments': sum(c.media_type == 'Segment' for c in values)}
        if snapshot != self._previous:
            self.timeline.append({'second': round(time.monotonic() - self.started, 3), **snapshot})
            self.timeline = self.timeline[-256:]
            self._previous = snapshot

    def summary(self):
        keys = ('page_requests', 'playurl_requests', 'playurl_responses', 'playurl_http_success',
                'media_responses', 'range_206_responses', 'cdp_body_requested', 'cdp_body_completed',
                'cdp_body_success', 'cdp_body_failed', 'cdp_body_not_sent', 'playurl_body_seen',
                'playurl_json_success', 'playurl_json_failed')
        return {**{key: self.counts[key] for key in keys},
                'playurl_cdp_bodies': len(self.playurl_bodies),
                'responses_with_body_seen': len(self.observed_bodies)}


def _instrument_body_reads(observer, evidence):
    """保留原传输、callback 和返回值；仅在验收实例上增加计数。"""
    original = observer._send

    def send(method, params=None, session='', callback=None):
        if method != 'Network.getResponseBody':
            return original(method, params, session, callback)
        identity = (str(session), str((params or {}).get('requestId', '')))
        evidence.counts['cdp_body_requested'] += 1

        def completed(result, ok):
            evidence.body_result(identity, result, ok)
            if callback:
                callback(result, ok)

        sent = original(method, params, session, completed)
        if not sent:
            evidence.counts['cdp_body_not_sent'] += 1
        return sent

    observer._send = send
    return original


def _candidate_summary(candidate):
    return {'id': candidate.id, 'protocol': candidate.media_type, 'source': candidate.source_type,
            'downloadable': candidate.downloadable, 'confidence': candidate.confidence,
            'quality': candidate.qualityScore, 'resolution': candidate.resolution_label,
            'video_track': bool(candidate.video_track), 'audio_track': bool(candidate.audio_track),
            'manifest_origin': candidate.manifest_origin, 'has_manifest_text': bool(candidate.manifest_text),
            'validated': candidate.validated, 'drm': candidate.drm,
            'unavailable_reasons': [_safe_text(reason) for reason in candidate.unavailable_reasons]}


def _eligible_candidates(label, candidates):
    eligible = [c for c in candidates if c.downloadable and not c.is_ad]
    if label in ('A', 'B'):
        eligible = [c for c in eligible if c.media_type == 'DASH' and c.video_track and c.audio_track]
    elif label == 'rou':
        # 验收固定目标页的解析结果，不能用网页中更高分的广告替代正片。
        video_id = PAGES['rou'].rsplit('/', 1)[-1]
        eligible = [c for c in eligible if c.source_type == 'RouVideoResolver' and c.validated
                    and c.media_type == 'HLS' and c.manifest_origin == 'embedded:png/roUd'
                    and c.manifest_text.lstrip().startswith('#EXTM3U')
                    and c.metadata.get('video_id') == video_id]
    return eligible


def _qobject_valid(value):
    from shiboken6 import isValid
    return value is not None and isValid(value)


def _capture_dialog_issue(dialog):
    # 先检查 C++ 对象有效性；销毁后不得再访问任何 QObject 子对象。
    if not _qobject_valid(dialog):
        return 'dialog_destroyed'
    if getattr(dialog, '_closing', False):
        return 'dialog_closing'
    return ''


def _discovery_snapshot(dialog, evidence):
    candidates = list(dialog._candidates)
    evidence.candidates(candidates)
    statistics = getattr(dialog, '_generic_diagnostic', {}).get('statistics', {})
    return {'network': deepcopy(dialog.observer.network_diagnostics()),
            'discovery': deepcopy(dialog.discovery_counts()), 'capture': evidence.summary(),
            'dash': {'descriptions': statistics.get('dash_descriptions', 0),
                     'inline_descriptions': statistics.get('dash_inline_descriptions', 0),
                     'video_tracks': statistics.get('dash_video_tracks', 0),
                     'audio_tracks': statistics.get('dash_audio_tracks', 0),
                     'paired_candidates': sum(bool(c.video_track and c.audio_track) for c in candidates),
                     'downloadable_pairs': sum(bool(c.video_track and c.audio_track and c.downloadable) for c in candidates)},
            'candidate_states': [_candidate_summary(c) for c in candidates],
            'candidate_timeline': deepcopy(evidence.timeline)}


def _capture_checkpoint(dialog, evidence, result, latest):
    """关闭/销毁时保留上一份有效快照，而非读取被清空的候选或已销毁 timer。"""
    issue = _capture_dialog_issue(dialog)
    if issue:
        result.update(deepcopy(latest))
        result['capture'] = evidence.summary()
        result['failure_code'] = 'DISCOVERY_CLOSED_DURING_CAPTURE'
        result['unexpected_capture_close'] = {
            'reason': issue, 'second': round(time.monotonic() - evidence.started, 3),
            'last_valid_snapshot_second': latest.get('last_valid_snapshot_second')}
        raise RuntimeError('DISCOVERY_CLOSED_DURING_CAPTURE：媒体发现窗口在捕获期间关闭；已保留最近有效计数。')
    latest.clear()
    latest.update(_discovery_snapshot(dialog, evidence))
    latest['last_valid_snapshot_second'] = round(time.monotonic() - evidence.started, 3)


def _safe_frames(frames):
    # 只写位置，不写源码行、局部变量或异常中的网络请求内容。
    return [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name} for frame in frames]


def _select_candidate(dialog, candidate):
    """经过真实质量下拉框/候选列表的选择路径，不直接设置 _selected_id。"""
    if candidate.confidence < 40:
        dialog.include_low.setChecked(True)
    owner = next((group for group in dialog._groups
                  if any(choice.id == candidate.id for choice in dialog._choices_for_group(group))), None)
    if owner is not None:
        dialog._choose_quality(owner.id, candidate.id)
    dialog._select_card(candidate.id)
    selected = dialog.selected_candidate()
    return bool(selected and selected.id == candidate.id and dialog.confirm_button.isEnabled())


def _confirm_selected(app, dialog):
    """点击生产“确定”按钮；需要时对其真实确认提示点击 Yes。"""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    confirmation = {'manual_confirmation_requested': False, 'manual_confirmation_answered': False}
    timer = QTimer()
    timer.setInterval(50)

    def answer_confirmation():
        for widget in app.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.windowTitle() == '确认当前正片':
                confirmation['manual_confirmation_requested'] = True
                button = widget.button(QMessageBox.StandardButton.Yes)
                if button is not None:
                    confirmation['manual_confirmation_answered'] = True
                    button.click()

    timer.timeout.connect(answer_confirmation)
    timer.start()
    try:
        dialog.confirm_button.click()
    finally:
        timer.stop()
    return confirmation


def _valid_av_outputs(summaries):
    return bool(summaries) and all(
        isinstance(value, dict) and set(str(value.get('format_name', '')).split(',')) & _MP4_FORMATS
        and value.get('video_streams') and value.get('audio_streams') for value in summaries)


def _code_fingerprint(code):
    """忽略源码/PYZ文件路径和调试行号，保留真正执行代码与常量。"""
    def constant(value):
        if isinstance(value, types.CodeType):
            return ['code', value.co_code.hex(), [constant(item) for item in value.co_consts],
                    list(value.co_names), list(value.co_varnames), list(value.co_freevars),
                    list(value.co_cellvars), value.co_argcount, value.co_posonlyargcount,
                    value.co_kwonlyargcount, value.co_flags, value.co_exceptiontable.hex()]
        if isinstance(value, (tuple, frozenset)):
            items = [constant(item) for item in value]
            if isinstance(value, frozenset):
                items.sort(key=lambda item: json.dumps(item, ensure_ascii=True))
            return [type(value).__name__, items]
        return [type(value).__name__, value.hex() if isinstance(value, bytes) else repr(value)]
    return hashlib.sha256(json.dumps(constant(code), ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _module_fingerprint(name):
    try:
        module = importlib.import_module(name)
        loader = module.__loader__
        code = loader.get_code(name) if loader and hasattr(loader, 'get_code') else None
        return {'loaded': True, 'code_sha256': _code_fingerprint(code) if code else '',
                'loader': type(loader).__name__}
    except Exception as error:
        return {'loaded': False, 'error_type': type(error).__name__}


def runtime_identity():
    from utils import branding
    identity = {'packaged': bool(getattr(sys, 'frozen', False)), 'executable': str(Path(sys.executable).resolve()),
            'application_version': getattr(branding, 'APP_VERSION', ''),
            'build_id': getattr(branding, 'BUILD_ID', getattr(branding, 'APP_BUILD_ID', '')),
            'dash_modules': {name: _module_fingerprint(name) for name in
                             ('core.dash_json', 'core.dash_models', 'core.media_discovery')}}
    try:
        from utils.build_info import runtime_identity as build_identity
    except ImportError:
        pass
    else:
        identity['build'] = build_identity()
    return identity


def _run_case(app, label, directory, args):
    from core.browser_runtime import browser_engine_info
    from core.output_validator import validate_output
    from PySide6.QtWidgets import QDialog
    from ui.main_window import MainWindow
    from utils.config import ConfigStore
    from utils.security import Redactor

    directory.mkdir(parents=True, exist_ok=True)
    store = ConfigStore(directory / ('session-' + uuid.uuid4().hex[:12]) / 'config.json')
    window = MainWindow(store)
    visible_browser = args.visible_browser
    window.show() if visible_browser else window.showMinimized()
    dialog = None
    job = None
    evidence = CaptureEvidence()
    latest = {}
    capture_active = False
    loaded, errors, page_states = [], [], []
    # 固定公开测试页仅含公开视频 ID；网络中产生的地址仍一律移除 query。
    result = {'case': label, 'page_url': PAGES[label], 'real_public_website': True,
              'simulated_data_used': False, 'isolated_profile': True,
              'system_cookies_imported': False,
              'initial_login_state': 'fresh_anonymous_profile',
              'discovery_action': 'deep_find' if args.deep_find else 'find',
              'discover_only': args.discover_only, 'capture_seconds': args.capture_seconds,
              'visible_browser': visible_browser,
              'download_attempted': False, 'success': False}

    def phase(name, **values):
        result['phase'] = name
        result.update(values)
        _write_json(directory / 'result.json', result)
        event = {'case': label, 'phase': name, 'second': round(time.monotonic() - evidence.started, 3), **values}
        with (directory / 'progress.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
        print(json.dumps({'case': label, 'phase': name, 'success': result['success'],
                          'capture': evidence.summary()}, ensure_ascii=True), flush=True)

    def shutdown_received():
        result.setdefault('discovery_shutdown_events', []).append({
            'second': round(time.monotonic() - evidence.started, 3),
            'phase': result.get('phase', ''), 'during_capture': capture_active,
            'unexpected_close_detected': bool(result.get('unexpected_capture_close')),
            'python_stack': _safe_frames(traceback.extract_stack(limit=12)[:-1])})
        _write_json(directory / 'result.json', result)

    try:
        phase('tools_starting')
        ready = _pump(app, 45, lambda: window.detector is None and window.yt_info.found and window.ff_info.found)
        result['tools_ready'] = ready
        if not ready:
            raise RuntimeError('实际下载所需 yt-dlp / FFmpeg 未就绪。')
        window.output_directory.setText(str(directory / 'downloads'))
        window.filename.setText('MediaFlow-' + label)
        dialog = window.open_discovery(page_url=PAGES[label], activate=False)
        if dialog is None or dialog.browser_backend != 'webview2':
            raise RuntimeError('真实发布验收必须使用 WebView2。')
        dialog.show() if visible_browser else dialog.showMinimized()
        if visible_browser:
            dialog.browser_controls.set_large_mode(True)
        dialog.listen_seconds = args.capture_seconds + 10
        result['browser_profile'] = str(window.browser_profile_directory)
        result['configured_body_limit_mib'] = window.settings.get('browser_body_limit_mib')
        _instrument_body_reads(dialog.observer, evidence)
        dialog.observer.observation.connect(evidence.observation)
        dialog.worker.candidates_changed.connect(evidence.candidates)
        dialog.view.loadFinished.connect(loaded.append)
        dialog.observer.error.connect(lambda value: errors.append(_safe_text(value)))
        dialog.shutdown_complete.connect(shutdown_received)
        result['load_finished'], result['observer_errors'] = loaded, errors
        capture_active = True
        (dialog.deep_find if args.deep_find else dialog.find)()
        dialog.page.cdpMessage.connect(evidence.cdp)
        phase('capturing')
        started, played, checkpoint = time.monotonic(), False, 0
        play_results, last_play_attempt = [], -10
        while time.monotonic() - started < args.capture_seconds:
            _capture_checkpoint(dialog, evidence, result, latest)
            _pump(app, .15)
            _capture_checkpoint(dialog, evidence, result, latest)
            elapsed = time.monotonic() - started
            played = any(value is True for value in play_results)
            if (not args.no_auto_play and dialog._document_ready and elapsed > 8 and not played
                    and elapsed - last_play_attempt >= 2):
                last_play_attempt = elapsed
                dialog.page.runJavaScript(_PLAY_VISIBLE,
                    lambda value: play_results.append(_activate_player(dialog.observer, value)))
            if elapsed > checkpoint + 15:
                checkpoint = elapsed
                phase('capturing', current_candidates=len(dialog._candidates),
                      current_downloadable=sum(c.downloadable for c in dialog._candidates))
        _capture_checkpoint(dialog, evidence, result, latest)
        if dialog.page.is_ready:
            dialog.page.runJavaScript(_PAGE_STATE, page_states.append)
            _pump(app, 3, lambda: bool(page_states))
            _capture_checkpoint(dialog, evidence, result, latest)
            dialog.page.runJavaScript("document.querySelectorAll('video').forEach(v=>v.pause())")
        _capture_checkpoint(dialog, evidence, result, latest)
        dialog._listen_ended()
        _pump(app, 2)
        _capture_checkpoint(dialog, evidence, result, latest)
        candidates = list(dialog._candidates)
        eligible = _eligible_candidates(label, candidates)
        if page_states:
            try:
                result['page'] = json.loads(page_states[-1])
            except (ValueError, TypeError):
                result['page'] = {'state_unavailable': True}
        result.update(deepcopy(latest))
        result.update(engine=browser_engine_info(dialog.profile), load_finished=loaded,
                      document_ready=dialog._document_ready, observer_errors=errors[:30])
        result['browser_page_url'] = _public_url(dialog.page.url().toString())
        result['resolver_codes'] = [str(item.get('error_code') or item.get('code') or '')
                                    for item in dialog._resolver_diagnostics if isinstance(item, dict)][:30]
        result['playback_started'] = played
        if visible_browser:
            dialog.browser_controls.set_large_mode(False)
        phase('discovered', eligible_candidates=len(eligible))
        if not eligible:
            raise RuntimeError('未生成完整可下载媒体候选；未绕过 Segment 完整性检查。')
        selected = dialog.selected_candidate()
        if selected is None or selected.id not in {candidate.id for candidate in eligible}:
            avc = [c for c in eligible if str(c.videoCodec or '').lower().startswith(('avc', 'h264'))]
            selected = max(avc or eligible, key=lambda c: (c.confidence, c.qualityScore, c.height or c.resolution[1]))
        result['selected'] = _candidate_summary(selected)
        result['confirm_button_enabled'] = _select_candidate(dialog, selected)
        if not result['confirm_button_enabled']:
            raise RuntimeError('完整候选未能通过实际候选列表选择并启用“确定”。')
        imported = []
        dialog.media_selected.connect(lambda value: imported.append(value.id))
        capture_active = False
        result.update(_confirm_selected(app, dialog))
        result['dialog_accepted'] = dialog.result() == QDialog.DialogCode.Accepted
        result['imported_through_selection_signal'] = imported == [selected.id]
        _pump(app, 20, lambda: window._discovery_dialog is None)
        if not result['dialog_accepted'] or not result['imported_through_selection_signal']:
            raise RuntimeError('点击“确定”未通过生产 media_selected 信号导入候选。')
        request = window._collect_request()
        result['queue_input'] = {'protocol': request.download_protocol,
                                 'has_manifest_text': bool(request.manifest_text),
                                 'manifest_origin': request.manifest_origin}
        if label in ('A', 'B') and (request.download_protocol != 'DASH' or not request.manifest_text):
            raise RuntimeError('DASH 候选进入原队列前丢失协议或清单正文。')
        phase('selection_confirmed')
        if args.discover_only:
            result['success'] = True
            phase('discovery_only_complete')
        else:
            job = window.add_current_to_queue()
            if job is None:
                raise RuntimeError('已确认候选未进入原下载队列。')
            result['download_attempted'] = True
            window.start_queue()
            phase('downloading')
            finished = _pump(app, args.download_timeout, lambda: job.result is not None and not window.queue.active_count)
            if not finished or job.result is None:
                raise RuntimeError('真实网站下载超过验收时间。')
            redactor = Redactor(job.request.headers, [job.request.url, job.request.manifest_url,
                                                      job.request.transport_url])
            (directory / 'download.log').write_text('\n'.join(_safe_text(line, redactor) for line in job.logs), encoding='utf-8')
            output = job.result
            result['download'] = {key: deepcopy(output.get(key)) for key in
                                  ('success', 'files', 'error_code', 'output_validation')}
            result['download_state'] = job.state
            result['ffmpeg_merge_reported'] = any('Merging formats into' in line or 'merging' in line.lower()
                                                  for line in job.logs)
            if not output.get('success'):
                raise RuntimeError('原下载管线失败：' + str(output.get('error_code') or 'DOWNLOAD_FAILED'))
            probes = [validate_output(path, ffmpeg_path=window.ff_info.path, expected_extension='mp4')
                      for path in output.get('files', [])]
            result['ffprobe'] = probes
            result['mp4_has_video_and_audio'] = _valid_av_outputs(probes)
            if not result['mp4_has_video_and_audio']:
                raise RuntimeError('最终输出未通过 MP4 容器、有效视频轨和音频轨的实际 ffprobe 校验。')
            if label == 'douyin':
                players = result.get('page', {}).get('players', [])
                duration = players[0].get('duration') if players else None
                if not duration or abs(probes[0]['duration'] - duration) >= 2:
                    raise RuntimeError('输出时长与目标主播放器不符；不能将编码探测或预览片段作为正片通过。')
                result['target_duration_verified'] = True
            _pump(app, 30, lambda: not window.thumbnails.busy)
            result['thumbnail'] = deepcopy(window.thumbnails.results.get(job.id, {}))
            result['success'] = True
            phase('download_verified')
    except Exception as error:
        result['failure'] = _safe_text(error)
        result['failure_type'] = type(error).__name__
        result['failure_traceback'] = _safe_frames(traceback.extract_tb(error.__traceback__)[-16:])
        phase('failed')
    finally:
        capture_active = False
        if _qobject_valid(window.queue):
            window.queue.stop_all()
            _pump(app, 30, lambda: not window.queue.active_count)
        if _qobject_valid(dialog) and not dialog._closing:
            closed = []
            dialog.shutdown_complete.connect(lambda: closed.append(True))
            dialog.reject()
            _pump(app, 20, lambda: bool(closed))
        if _qobject_valid(window):
            window.close()
        result['browser_shutdown_complete'] = _pump(app, 20, lambda: window._webview2_profile is None
                                                   or window._webview2_profile.host._finished_once)
        _write_json(directory / 'result.json', result)
    return result


def run_live_release_check(app, argv):
    """main.py 和 PyInstaller EXE 共用的真实验收入口，返回进程退出码。"""
    args = parse_arguments(argv)
    if args.rou_url:
        PAGES['rou'] = args.rou_url
    directory = args.live_release_check.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app.setQuitOnLastWindowClosed(False)
    identity = runtime_identity()
    _write_json(directory / 'runtime.json', identity)
    names = tuple(PAGES) if args.case == 'all' else (args.case,)
    reports = []
    for name in names:
        reports.append(_run_case(app, name, directory / name, args))
        _write_json(directory / 'results.json', {'runtime': identity, 'cases': reports,
                    'success': all(report['success'] for report in reports),
                    'acceptance_mode': 'discovery_only' if args.discover_only else 'real_download'})
    return 0 if all(report['success'] for report in reports) else 1
