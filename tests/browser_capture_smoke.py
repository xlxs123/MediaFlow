"""Native first-worker request, extensionless MIME and slow-load DOM regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent

from core.site_resolvers import ResolverRegistry
from tests.discovery_fixture import DiscoveryFixture
from ui.main_window import MainWindow
from utils.config import ConfigStore


class CaptureFixture(DiscoveryFixture):
    def html(self, case):
        if case != 'browser-regression':
            return super().html(case)
        return b'''<!doctype html><meta charset="utf-8"><title>Browser capture regression</title>
<video controls muted autoplay style="width:640px;height:360px" src="/media/opaque-video"></video>
<img src="/assets/slow.png">
<script>
fetch('/media/opaque-fetch').then(r=>r.arrayBuffer());
const x=new XMLHttpRequest();x.open('GET','/media/opaque-xhr');x.send();
fetch('/streams/clear/index').then(r=>r.text());
fetch('/streams/dash/index').then(r=>r.text());
const worker=new Worker(URL.createObjectURL(new Blob([
  "fetch('"+location.origin+"/media/opaque-worker').then(r=>r.arrayBuffer()).then(()=>postMessage('done'));"
],{type:'application/javascript'})));
worker.onmessage=()=>{window.fixtureWorkerComplete=true;};
</script>'''


def run_browser_capture_smoke(app, args):
    directory = Path(args[args.index('--browser-capture-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixtures
    fixtures.ROOT = APP_DIR
    fixture = CaptureFixture(directory / 'media')
    app.setQuitOnLastWindowClosed(False)
    window = dialog = None
    checks, reports = [], {}

    def pump(predicate, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('浏览器捕获验收等待超时：' + (dialog.status_label.text() if dialog else '关闭阶段'))

    def js(script):
        values = []
        dialog.page.runJavaScript(script, values.append)
        pump(lambda: bool(values))
        return values[-1]

    try:
        fixture.generate()
        fixture.start()
        window = MainWindow(ConfigStore(directory / 'test-config.json'))
        window.showMinimized()
        pump(lambda: window.detector is None)
        dialog = window.open_discovery(page_url=fixture.page('browser-regression'), activate=False)
        dialog.worker.resolver_registry = ResolverRegistry([])
        dialog.showMinimized()
        dialog.deep_find()
        pump(lambda: fixture.slow_requested.is_set() and dialog._document_ready)
        assert not dialog._page_loaded
        pump(lambda: bool(dialog._last_dom) and bool(dialog._candidates), timeout=4)
        assert not dialog._page_loaded, '慢图片加载完成前应已收到 DOM 与候选'
        assert dialog._last_dom['page_url'] == fixture.page('browser-regression')
        checks.append('DOMContentLoaded 即分析当前文档，不等待慢图片或所有子资源完成')
        fixture.slow_release.set()
        pump(lambda: dialog._page_loaded and js('window.fixtureWorkerComplete===true') is True)
        pump(lambda: all(any(c.request_context.url.endswith('/media/opaque-' + kind) and c.media_type == 'Direct'
            and c.confirmed for c in dialog._candidates) for kind in ('video', 'fetch', 'xhr', 'worker')))
        checks.append('无后缀 MP4 在 Media / Fetch / XHR / Worker 四条原生 CDP 路径生成候选')
        worker = next(obs for obs in dialog._network_history.values() if obs.context.url.endswith('/media/opaque-worker'))
        assert ':' in worker.request_id and worker.content_type == 'video/mp4'
        pump(lambda: any(obs.context.url.endswith('/media/opaque-worker') and obs.context_complete
                         for obs in dialog._network_history.values()), timeout=5)
        worker = next(obs for obs in dialog._network_history.values() if obs.context.url.endswith('/media/opaque-worker'))
        assert worker.context_complete
        checks.append('Worker 第一条 Fetch 被捕获且具有完整实际请求头，未虚构播放器关联')
        pump(lambda: {'HLS', 'DASH'} <= {c.media_type for c in dialog._candidates})
        assert all(type(c.confidenceScore) in (int, float) and type(c.qualityScore) in (int, float) for c in dialog._candidates)
        checks.append('无后缀 HLS / DASH 经既有解析器生成 MediaCandidate 并继续原评分')
        reports['extensionless'] = {'network': dialog.observer.network_diagnostics(), 'candidates': [
            {'protocol': c.media_type, 'confidence': c.confidenceScore, 'quality': c.qualityScore,
             'confirmed': c.confirmed, 'context_complete': c.context_complete} for c in dialog._candidates]}
        counts = reports['extensionless']['network']['counts']
        assert counts.get('media_requests', 0) >= 6
        assert counts.get('Network.responseReceived:Fetch', 0) >= 4
        assert counts.get('Network.responseReceived:XHR', 0) >= 1
        assert counts.get('Network.responseReceived:Media', 0) >= 1
        checks.append('总请求、按来源响应及媒体相关请求计数可诊断，重复事件不重复计媒体请求')
        dialog.page_url.setText(fixture.page('png_segments'))
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and any(c.media_type == 'HLS' and c.classification == 'HLS_AES128'
            and c.drm is False and c.context_complete for c in dialog._candidates))
        reports['hls_aes_png'] = dialog.observer.network_diagnostics()
        checks.append('复用持久 Profile 重新查找，AES-128 / PNG 分片仍进入原 HLS 流程')
        result = {'passed': True, 'checks': checks, 'reports': reports,
                  'packaged': bool(getattr(sys, 'frozen', False)), 'site_resolvers_used': False}
    except Exception as error:
        result = {'passed': False, 'checks': checks, 'reports': reports, 'error': str(error),
                  'traceback': traceback.format_exc(), 'network': dialog.observer.network_diagnostics() if dialog else {},
                  'record_states': [{'session': bool(record.session), 'hops': len(record.hops), 'extras': len(record.extras),
                    'response_extras': len(record.response_extras), 'states': [dict(resource_type=hop.resource_type,
                      expected_extra=hop.expected_extra, response_seen=hop.response_seen, from_cache=hop.from_cache,
                      header_names=sorted(hop.initial_headers), wire=hop.wire is not None) for hop in record.hops]}
                    for record in dialog.observer._store.records.values()] if dialog else []}
    finally:
        fixture.slow_release.set()
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None)
        if window:
            window.close()
            if window._webview2_profile:
                pump(lambda: window._webview2_profile.host._finished_once and not window.isVisible())
        fixture.stop()
    (directory / 'browser-capture-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'error': result.get('error')}, ensure_ascii=True), flush=True)
    return 0 if result['passed'] else 1
