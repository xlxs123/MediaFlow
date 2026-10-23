"""Read-only A/B browser diagnostics, using MediaFlow's unchanged discovery UI.

Run with the project's Python; no download, login, profile import or repackaging.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PAGE_DESCRIPTION_SCRIPT = r'''(()=>{
    let playinfo_json=null;
    try {if(window.__playinfo__ && typeof window.__playinfo__==='object'){
        const s=JSON.stringify(window.__playinfo__);if(s.length<=2097152)playinfo_json=s;}}
    catch(e){}
    const text=document.body?.innerText||'';
    return JSON.stringify({page_url:location.href,document_state:document.readyState,playinfo_json,
        inline_playinfo_marker:[...document.scripts].some(s=>/\b__playinfo__\b/.test(s.textContent)),
        verification:/安全验证|验证身份|验证码|访问异常|网络环境存在风险/.test(text),
        unsupported_html5:/不支持.{0,12}HTML5|HTML5.{0,12}不支持/i.test(text),
        videos:[...document.querySelectorAll('video')].slice(0,12).map(v=>({
            ready_state:v.readyState,error_code:v.error?.code||0,paused:v.paused,ended:v.ended,
            current_time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null}))});})()'''


def pump(app, seconds, stop=None):
    from PySide6.QtCore import QCoreApplication, QEvent
    end = time.monotonic() + seconds
    while time.monotonic() < end and not (stop and stop()):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(.01)


def run_case(app, label, url, expected, directory, seconds):
    from core.browser_runtime import browser_engine_info
    from core.media_chain_diagnostics import MediaChainTrace, safe_url
    from ui.main_window import MainWindow
    from utils.config import ConfigStore

    directory.mkdir(parents=True, exist_ok=True)
    trace = MediaChainTrace(label, url, expected)
    window = MainWindow(ConfigStore(directory / 'session' / 'test-config.json'))
    window.showMinimized()
    dialog = window.open_discovery(page_url=url, activate=False)
    if dialog.browser_backend != 'webview2':
        raise RuntimeError('诊断要求使用当前 WebView2 后端，不能以 Qt 回退结果替代。')
    dialog.listen_seconds = seconds + 20
    dialog.showMinimized()
    loaded, dom_states, fetch_states, page_states = [], [], [], []
    cutoff = {'reason': '', 'next_url': ''}
    target_path = urlsplit(url).path.rstrip('/')
    def observe(value):
        if not cutoff['reason']:
            trace.record_observation(value)
    def candidates(value, *rest):
        if not cutoff['reason']:
            trace.record_candidates(value, *rest)
    dialog.view.loadFinished.connect(loaded.append)
    dialog.observer.observation.connect(observe)
    dialog.worker.candidates_changed.connect(candidates)
    dialog.observer.dom.connect(lambda value: dom_states.append({
        'videos': len(value.get('videos', [])), 'player_type': value.get('player_type', '')}))
    dialog.observer.error.connect(lambda value: trace.errors.append(str(value)[:200]))
    dialog.deep_find()
    # deep_find creates a fresh native Page; subscribe before it navigates.
    page = dialog.page
    diagnostic_reads = set()
    command_id = [1000000]
    def on_cdp(message):
        if cutoff['reason']:
            return
        trace.record_cdp(message)
        if message.get('method') != 'Network.loadingFinished' or len(diagnostic_reads) >= 32:
            return
        params, session = message.get('params', {}), str(message.get('sessionId', ''))
        request_id = str(params.get('requestId', ''))
        record = trace._active.get((session, request_id))
        if (not record or not record['focus_matches'] or record['resource_type'] not in ('Fetch', 'XHR')
                or (session, request_id) in diagnostic_reads):
            return
        sizes = (record['content_length'], record['encoded_transfer_bytes'])
        if any(size is not None and size > 2 * 1024 * 1024 for size in sizes):
            record['diagnostic_body_status'] = 'BODY_TOO_LARGE'
            return
        diagnostic_reads.add((session, request_id))
        command_id[0] += 1
        command = {'id': command_id[0], 'method': 'Network.getResponseBody', 'params': {'requestId': request_id}}
        if session:
            command['sessionId'] = session
        # Reads an already-loaded API response. It never calls fetch_url or
        # set_body/worker.submit, so discovery receives its original data only.
        page.send_cdp(command, lambda result, ok, session=session, request_id=request_id:
            trace.diagnostic_body(session, request_id, result.get('result', {}) if isinstance(result, dict) else {}, ok))
    page.cdpMessage.connect(on_cdp)
    def url_changed(value):
        path = urlsplit(value.toString()).path.rstrip('/')
        if path.startswith('/video/') and path != target_path:
            cutoff.update(reason='TARGET_PAGE_CHANGED', next_url=safe_url(value.toString()))
    page.urlChanged.connect(url_changed)
    def page_description(value):
        if cutoff['reason'] or not isinstance(value, str):
            return
        try:
            payload = json.loads(value)
        except (ValueError, TypeError):
            trace.errors.append('PAGE_DIAGNOSTIC_READ_FAILED')
            return
        if not isinstance(payload, dict) or urlsplit(payload.get('page_url', '')).path.rstrip('/') != target_path:
            return
        trace.record_inline(payload)
        payload.pop('playinfo_json', None)
        payload['page_url'] = safe_url(payload.get('page_url'))
        page_states.append(payload)
        for video in payload.get('videos', []):
            duration, current = video.get('duration'), video.get('current_time')
            if (type(duration) in (int, float) and type(current) in (int, float)
                    and duration > 5 and current > 0 and (video.get('ended') or duration - current <= 3)):
                cutoff['reason'] = 'TARGET_NEAR_END'
    played = False
    started = time.monotonic()
    next_checkpoint = 5
    try:
        while time.monotonic() - started < seconds and not cutoff['reason']:
            pump(app, .1)
            elapsed = time.monotonic() - started
            if dialog._document_ready and elapsed > 8 and not played:
                page.runJavaScript('''(()=>{const v=[...document.querySelectorAll('video')].find(v=>{
                    const r=v.getBoundingClientRect();return r.width>0&&r.height>0;});
                    if(v){v.muted=true;v.play().catch(()=>{});}return !!v;})()''')
                played = True
            if elapsed >= next_checkpoint:
                page.runJavaScript(PAGE_DESCRIPTION_SCRIPT, page_description)
                fetch_states.append({'second': next_checkpoint, 'queued': len(dialog._fetch_queue),
                    'tried': len(dialog._fetch_tried), 'inflight': safe_url(dialog._fetch_inflight.url) if dialog._fetch_inflight else ''})
                next_checkpoint += 5
                trace.write(directory)
        if not cutoff['reason']:
            before = len(page_states)
            page.runJavaScript(PAGE_DESCRIPTION_SCRIPT, page_description)
            pump(app, 2, lambda: len(page_states) > before)
            trace.record_candidates(dialog._candidates)
        page_state = page_states[-1] if page_states else {}
        assert not window.queue.jobs, '诊断不得创建下载任务。'
        snapshot = trace.write(directory)
        snapshot['environment'] = {'engine': browser_engine_info(dialog.profile),
            'qt_platform': app.platformName(), 'source_run': True, 'listen_seconds': seconds,
            'observed_seconds': round(time.monotonic() - started, 2),
            'isolated_profile': True, 'system_cookies_imported': False,
            'resolver_registry_changed': False, 'download_queue_jobs': len(window.queue.jobs)}
        snapshot['page'] = {'load_finished': loaded, 'final_url': safe_url(page.url().toString()),
                            'state': page_state, 'state_checkpoints': page_states, 'sampling_cutoff': cutoff,
                            'maximum_video_elements': max((s['videos'] for s in dom_states), default=0)}
        snapshot['observer'] = dialog.observer.network_diagnostics()
        snapshot['discovery_statistics'] = dialog.discovery_counts()
        snapshot['fetch_queue_checkpoints'] = fetch_states
        (directory / 'trace.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'case': label, 'counts': snapshot['counts'], 'observation': snapshot['current_observation']}, ensure_ascii=True), flush=True)
        return snapshot
    finally:
        closed = []
        dialog.shutdown_complete.connect(lambda: closed.append(True))
        dialog.reject()
        pump(app, 15, lambda: bool(closed))
        window.close()
        pump(app, 15, lambda: window._webview2_profile is None or window._webview2_profile.host._finished_once)


def create_report(cases, directory, unchanged):
    lines = ['# B站媒体发现链路诊断', '',
        '仅增加独立诊断订阅和运行脚本。现有发现、Resolver、候选资格、评分和下载代码未修改。', '',
        'A/B 的成功、失败标签来自用户历史确认；下表为本轮相同环境、独立空白 Profile 的实际观测。未登录、未导入系统 Cookie，未下载第三方视频。', '',
        '| 样本 | 网络请求 | Fetch/XHR | 媒体请求 | 候选 | Segment 候选 | 可下载资格 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for case in cases:
        c = case['counts']
        lines.append(f"| {case['case']} | {c['requests']} | {c['fetch_xhr']} | {c['media']} | {c['candidates']} | {c['segments']} | {c['downloadable']} |")
    for case in cases:
        lines.extend(['', '## ' + case['case'], '', '[' + case['page_url'] + '](' + case['page_url'] + ')', '',
            '- 用户历史标签：' + case['historical_user_label'], '- 本轮观察：' + case['current_observation'],
            '- 真实协议清单：' + ', '.join(k for k in ('HLS', 'DASH_XML') if case['description_presence'][k]) if any(
                case['description_presence'][k] for k in ('HLS', 'DASH_XML')) else '- 本轮未观察到真实 MPD XML / M3U8 正文。'])
        environment, page = case.get('environment', {}), case.get('page', {})
        lines.append(f"- 实际采集 {environment.get('observed_seconds', '未知')} 秒；采样停止原因：{page.get('sampling_cutoff', {}).get('reason') or '达到设定窗口'}。")
        timeline = case['candidate_timeline']
        peak = max((s['downloadable'] for s in timeline), default=0)
        lines.append(f"- 过程峰值可下载资格：{peak}；最终：{case['counts']['downloadable']}。只表示候选资格，未实际下载。")
        focus = [r for r in case['requests'] if r['focus_matches']]
        lines.extend(['', '重点请求：', '', '| ID | 类型/Method | URL | HTTP | Content-Type | 正文/传输字节 | 原观察器正文 |',
                      '|---|---|---|---:|---|---|---|'])
        for r in focus:
            body_size = r.get('observed_body_bytes', r.get('diagnostic_body_bytes', r['decoded_data_bytes']))
            size = f"{body_size if body_size is not None else '未知'} / {r['encoded_transfer_bytes'] if r['encoded_transfer_bytes'] is not None else '未知'}"
            lines.append(f"| {r['id']} | {r['resource_type']}/{r['method']} | {r['url']} | {r['status']} | {r['content_type']} | {size} | {r['body_seen_by_discovery']} |")
        if not focus:
            lines.append('| 无 | | 本轮未观察到匹配请求 | | | | |')
        json_desc = [d for d in case['descriptions'] if d['kind'].startswith('JSON_')]
        inline = [d for d in case['inline_descriptions'] if d['summary']['kind'].startswith('JSON_')]
        if json_desc:
            lines.extend(['', '已观察到 JSON 媒体描述；不能把“没有 MPD/M3U8”说成“没有任何媒体描述”。'])
            for desc in json_desc:
                refs = desc['references']
                associated = {cid for ref in refs for cid in ref['candidate_ids']}
                lines.append(f"- {desc['request']}：{desc['kind']}；来源 {desc['source']}；原观察器读到正文 {desc['body_seen_by_discovery']}；引用URL {len(refs)}；关联候选 {len(associated)}。")
        if inline:
            lines.extend(['', '页面内嵌 `window.__playinfo__` 媒体描述存在。它由本次只读 JS 诊断发现，未注入原媒体发现流程。'])
        if not json_desc and not inline and not any(case['description_presence'].values()):
            lines.extend(['', '本轮缺少可确认的媒体描述正文；仅凭请求名称不能判断响应内部有 DASH 信息。'])
        lines.extend(['', '链路（箭头表示已观察到的阶段；详细 URL/requestId 关联见 trace.json）：', '', '```text',
                      '页面加载 → Fetch/XHR / 页面内嵌描述',
                      ('    → JSON DASH/durl 描述' if json_desc or inline else '    → 媒体描述未确认'),
                      ('    → 实际 MPD/M3U8' if case['description_presence']['HLS'] or case['description_presence']['DASH_XML'] else '    → 未观察到独立 MPD/M3U8'),
                      f"    → 媒体响应 → 候选 {case['counts']['candidates']}（Segment {case['counts']['segments']}，通过下载资格 {case['counts']['downloadable']}）", '```', '',
                      '候选阻断原因：'])
        for c in case['candidates']:
            lines.append(f"- {c['protocol']}，可信度 {c['confidence']}：" + ('；'.join(c['unavailable_reasons']) or '通过现有下载资格检查'))
        if not case['candidates']:
            lines.append('- 没有候选。')
    lines.extend(['', '## 现有代码链路与判断边界', '',
        '- Fetch/XHR JSON 可进入原观察器。UI 的 `config_media_urls` 会扫描其中的 URL，再通过正常浏览器 Fetch 验证。',
        '- 该扫描没有把 JSON 中 DASH 的音视频 Representation、SegmentBase、Initialization、indexRange 组织为完整 DASH 候选；实际 MPD XML 才进入现有结构化 DASH 解析。',
        '- `.m4s` 的孤立响应会成为 Segment；这项阻断来自不完整结构，不是低可信度。',
        '- 诊断专用 `Network.getResponseBody` 只读取已经加载的响应，不发起额外媒体 Fetch、不注入候选。其正文与原观察器正文来源分别记录。',
        '- 未观察到 MPD/M3U8 只针对本轮采集窗口；不能据此断定站点永久不提供这些格式。',
        '- 如果 A 本轮没有可下载候选，历史成功路径尚未复现，报告不能伪造“成功链路”。',
        '', f'保护检查：既有 {len(unchanged)} 个源码/脚本文件 SHA256 全部保持一致。',
        '', '原始诊断文件：每个样本的 `fetch-xhr.jsonl`、`media.jsonl`、`focus.jsonl`、`trace.json`。URL 查询值已隐藏；不保存原始响应正文、Cookie 或 Authorization。',
        '响应大小分别记录 Content-Length、CDP dataLength、encodedDataLength 和实际读取正文大小，缺失值为 null，不能混为文件完整大小。'])
    (directory / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--success-url', required=True)
    parser.add_argument('--failure-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=60, choices=range(30, 121), metavar='30..120')
    args = parser.parse_args()
    from core.request_context import validate_url
    for url in (args.success_url, args.failure_url):
        validate_url(url)
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((ROOT / '.codex' / 'bilibili-chain-baseline.json').read_text(encoding='utf-8'))
    os.environ.setdefault('QT_QPA_PLATFORM', 'windows')
    from core.browser_runtime import prepare_browser_runtime
    prepare_browser_runtime()
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
    from ui.app_setup import configure_app
    app = QApplication([sys.argv[0]])
    configure_app(app)
    app.setQuitOnLastWindowClosed(False)
    cases = [run_case(app, 'A', args.success_url, '用户确认历史可下载', directory / 'A', args.seconds),
             run_case(app, 'B', args.failure_url, '用户确认只能发现 Segment', directory / 'B', args.seconds)]
    changed = [p for p, expected in baseline.items() if not (ROOT / p).is_file() or
               hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != expected]
    boundary = {'existing_files_checked': len(baseline), 'changed_existing_files': changed, 'passed': not changed}
    (directory / 'source-boundary.json').write_text(json.dumps(boundary, indent=2), encoding='utf-8')
    create_report(cases, directory, baseline)
    if changed:
        raise RuntimeError('既有源码发生改动，诊断边界检查未通过。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
