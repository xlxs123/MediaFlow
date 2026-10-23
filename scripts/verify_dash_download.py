"""原生 WebView2 → 真实 DASH JSON → 原队列下载 → ffprobe 双轨验收。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.diagnose_media_chain import PAGE_DESCRIPTION_SCRIPT, pump

PAGES = {
    'A': 'https://www.bilibili.com/video/BV1CjaQ6NE42/?spm_id_from=333.1387.favlist.content.click',
    'B': 'https://www.bilibili.com/video/BV1fKeg6mEhi/?spm_id_from=333.1007.top_right_bar_window_history.content.click',
}


def run_case(app, label, url, directory, capture_seconds, download_timeout):
    from core.browser_runtime import browser_engine_info
    from core.media_chain_diagnostics import MediaChainTrace
    from ui.main_window import MainWindow
    from utils.config import ConfigStore
    directory.mkdir(parents=True, exist_ok=True)
    window = MainWindow(ConfigStore(directory / 'session' / 'config.json'))
    window.showMinimized()
    dialog = None
    trace = MediaChainTrace(label, url, '修复后的实际下载验收')
    result = {'case': label, 'page_url': url.split('?', 1)[0], 'success': False}
    try:
        pump(app, 40, lambda: window.detector is None and window.yt_info.found and window.ff_info.found)
        if not (window.yt_info.found and window.ff_info.found):
            raise RuntimeError('缺少实际下载所需的 yt-dlp / FFmpeg。')
        window.output_directory.setText(str(directory / 'downloads'))
        window.filename.setText('DASH-' + label)
        dialog = window.open_discovery(page_url=url, activate=False)
        dialog.showMinimized()
        if dialog.browser_backend != 'webview2':
            raise RuntimeError('实际验收必须使用 WebView2。')
        dialog.observer.observation.connect(trace.record_observation)
        dialog.worker.candidates_changed.connect(trace.record_candidates)
        dialog.deep_find()
        dialog.page.cdpMessage.connect(trace.record_cdp)
        started, played = time.monotonic(), False
        while time.monotonic() - started < capture_seconds:
            pump(app, .15)
            if dialog._document_ready and time.monotonic() - started > 8 and not played:
                dialog.page.runJavaScript('''(()=>{const v=[...document.querySelectorAll('video')].find(v=>{
                    const r=v.getBoundingClientRect();return r.width>0&&r.height>0;});
                    if(v){v.muted=true;v.play().catch(()=>{});}return !!v;})()''')
                played = True
        page_states = []
        dialog.page.runJavaScript(PAGE_DESCRIPTION_SCRIPT, page_states.append)
        pump(app, 3, lambda: bool(page_states))
        dialog.page.runJavaScript("document.querySelectorAll('video').forEach(v=>v.pause())")
        dialog._listen_ended()
        pump(app, 2)
        candidates = deepcopy(dialog._candidates)
        trace.record_candidates(candidates)
        snapshot = trace.write(directory / 'trace')
        eligible = [c for c in candidates if c.video_track and c.audio_track and c.downloadable]
        result.update(engine=browser_engine_info(dialog.profile), capture_seconds=capture_seconds,
            discovery=dialog.discovery_counts(), dash=getattr(dialog, '_generic_diagnostic', {}).get('statistics', {}),
            candidate_states=[{'id': c.id, 'type': c.media_type, 'downloadable': c.downloadable,
                'confidence': c.confidence, 'resolution': c.resolution_label,
                'has_video_track': bool(c.video_track), 'has_audio_track': bool(c.audio_track),
                'unavailable_reasons': c.unavailable_reasons} for c in candidates],
            isolated_profile=True, system_cookies_imported=False,
            candidate_transitions=snapshot['candidate_transitions'])
        if page_states:
            page = json.loads(page_states[-1])
            page.pop('playinfo_json', None)
            page.pop('page_url', None)
            result['page'] = page
        if not eligible:
            raise RuntimeError('未生成带视频和音频的可下载 DASH 候选。')
        avc = [c for c in eligible if c.video_track.codecs.lower().startswith(('avc', 'h264'))]
        selected = max(avc or eligible, key=lambda c: (c.video_track.height, c.video_track.bandwidth))
        result['selected'] = {'id': selected.id, 'resolution': selected.resolution_label,
            'video_id': selected.video_track.id, 'audio_id': selected.audio_track.id,
            'video_codec': selected.videoCodec, 'audio_codec': selected.audioCodec,
            'manifest_origin': selected.manifest_origin, 'protocol': selected.media_type}
        print(json.dumps({'case': label, 'phase': 'discovered', 'dash': result['dash'],
                          'eligible_dash': len(eligible), 'selected': result['selected']}, ensure_ascii=True), flush=True)
        window.import_discovered_media(selected)
        request = window._collect_request()
        assert request.download_protocol == 'DASH' and request.manifest_text.startswith('<MPD')
        assert request.manifest_origin in ('captured:dash-json', 'embedded:html-dash-json')
        job = window.add_current_to_queue()
        if job is None:
            raise RuntimeError('配对候选未成功进入原下载队列。')
        window.start_queue()
        pump(app, download_timeout, lambda: job.result is not None and not window.queue.active_count)
        if job.result is None:
            raise RuntimeError('实际下载超过验收时间。')
        result['download'] = deepcopy(job.result)
        result['download_state'] = job.state
        (directory / 'download.log').write_text('\n'.join(job.logs), encoding='utf-8')
        if not job.result.get('success'):
            raise RuntimeError('原下载管线返回失败：' + str(job.result.get('error_code') or job.result.get('error', '')))
        validations = job.result.get('output_validation', [])
        if not validations or not all(v.get('video_streams') and v.get('audio_streams') for v in validations):
            raise RuntimeError('最终输出未同时包含真实视频轨和音频轨。')
        pump(app, 30, lambda: not window.thumbnails.busy)
        result['thumbnail'] = deepcopy(window.thumbnails.results.get(job.id, {}))
        result['success'] = True
    except Exception as error:
        # 各原下载路径已经脱敏；这里不序列化候选或清单原文。
        from utils.security import Redactor
        result['failure'] = Redactor().redact(str(error))[:2000]
    finally:
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'case': label, 'phase': 'finished', 'success': result['success'],
                          'failure': result.get('failure', ''), 'files': result.get('download', {}).get('files', [])}, ensure_ascii=True), flush=True)
        window.queue.stop_all()
        pump(app, 30, lambda: not window.queue.active_count)
        if dialog is not None and not dialog._closing:
            closed = []
            dialog.shutdown_complete.connect(lambda: closed.append(True))
            dialog.reject()
            pump(app, 15, lambda: bool(closed))
        window.close()
        pump(app, 15, lambda: window._webview2_profile is None or window._webview2_profile.host._finished_once)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', choices=('A', 'B', 'both'), default='both')
    parser.add_argument('--capture-seconds', type=int, default=22)
    parser.add_argument('--download-timeout', type=int, default=240)
    args = parser.parse_args()
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
    directory = args.output.resolve()
    cases = [run_case(app, name, PAGES[name], directory / name, args.capture_seconds, args.download_timeout)
             for name in (('A', 'B') if args.case == 'both' else (args.case,))]
    (directory / 'results.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(case['success'] for case in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
