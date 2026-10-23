"""Resolver、浏览器操作和链接刷新续传的实际 Chromium / yt-dlp 验收。"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from unittest.mock import patch
from core.downloader import DownloadRequest
from core.request_context import RequestContext
from core.site_resolvers import ResolverRegistry
from core.url_policy import UrlResolutionPolicy
from tests.resolver_fixture import ResolverFixture, LocalRouResolver
from ui.discovery_dialog import DiscoveryDialog
from ui.main_window import MainWindow
from utils.config import ConfigStore


def run_resolver_smoke(app, args):
    directory = Path(args[args.index('--resolver-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as hls
    import tests.integration_hls as integration
    hls.ROOT = integration.ROOT = APP_DIR
    fixture = ResolverFixture(directory / 'media')
    window = dialog = None
    checks, metrics, outputs = [], {}, []
    native = app.platformName() == 'windows'
    ticks = [0]
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start()
    app.setQuitOnLastWindowClosed(False)
    def pump(predicate=lambda: False, timeout=12, required=True):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        if required:
            raise AssertionError('Resolver / 浏览器 / 下载验收等待超时')
    def js(expression):
        values = []
        dialog.page.runJavaScript('JSON.stringify(' + expression + ')', lambda value: values.append(value))
        pump(lambda: bool(values), 4)
        return json.loads(values[0])
    def close_dialog():
        nonlocal dialog
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None, 15)
            dialog = None
    def find(path, predicate=None):
        nonlocal dialog
        close_dialog()
        dialog = DiscoveryDialog(window, fixture.url(path), resolver_registry=ResolverRegistry([LocalRouResolver()]))
        window._discovery_dialog = dialog
        dialog.media_selected.connect(window.import_discovered_media)
        dialog.shutdown_complete.connect(lambda current=dialog: window._discovery_closed(current))
        dialog.showMinimized() if native else dialog.show()
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and (predicate is None or predicate(dialog)), 20)
        return dialog.selected_candidate()
    def wait_job(job):
        pump(lambda: job.state in ('completed', 'failed', 'cancelled', 'waiting_context') and job.worker is None, 60)
    def save_check(name):
        checks.append(name)
    try:
        fixture.generate()
        fixture.start()
        window = MainWindow(ConfigStore(directory / 'test-config.json'))
        window.showMinimized() if native else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 30)
        candidate = find('/v/plain', lambda d: any(c.metadata.get('resolver_id') for c in d._candidates))
        assert candidate.media_type == 'HLS' and candidate.drm is False
        assert candidate.request_context.user_agent and not candidate.request_context.cookies
        assert not candidate.request_context.headers.get('authorization')
        assert candidate.metadata.get('resolver_route') == 'legacy'
        assert any(item['path'] == '/api/hls/plain' for item in fixture.requests)
        save_check('当前API失败后legacy NEXT ev与roUd明文进入统一候选')
        before = sum(item['path'] == '/v/plain' for item in fixture.requests)
        page = dialog.page
        dialog.find()
        pump(lambda: bool(dialog._candidates), 10)
        assert dialog.page is page
        assert sum(item['path'] == '/v/plain' for item in fixture.requests) == before
        save_check('快速查找不Reload且使用当前DOM与已有Network历史')
        ready = []
        dialog.observer.ready.connect(lambda: ready.append(len(fixture.requests)))
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and bool(dialog._candidates), 15)
        assert dialog.page is not page and ready
        assert fixture.requests[ready[-1]]['path'] == '/v/plain'
        save_check('深度查找先监听Ready再加载并隔离旧Page')
        controls = dialog.browser_controls
        for zoom in (50, 60, 67, 75, 80, 90, 100, 110, 125, 150):
            controls.set_zoom(zoom)
            assert abs(dialog.view.zoomFactor() - zoom/100) < .001
        controls.reset_zoom()
        controls.set_large_mode(True)
        assert dialog.result_panel.isHidden()
        controls.escape_shortcut.activated.emit()
        assert not dialog.result_panel.isHidden()
        assert dialog.windowFlags() & dialog.windowFlags().WindowMaximizeButtonHint
        save_check('浮窗最大化缩放档位Ctrl快捷键与大网页Esc恢复')
        candidate = find('/v/deflate', lambda d: any(c.metadata.get('resolver_id') and c.classification == 'HLS_AES128' for c in d._candidates))
        captured = deepcopy(candidate)
        assert candidate.drm is False and candidate.playlist_type == 'VOD'
        assert all(item['role'] == 'Media Segment' for item in candidate.metadata['manifest']['resources'])
        assert candidate.metadata['manifest']['keys'][0]['uri'] == 'enc.key'
        assert 'token=old' in candidate.request_context.derive('enc.key').url
        assert candidate.metadata.get('resolver_route') == 'legacy'
        save_check('legacy roUd deflate与AES128 PNG分片Key语义及限定Query继承')
        dialog.confirm_button.click()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        output_dir = directory / 'downloads' / str(time.time_ns())
        window.output_directory.setText(str(output_dir))
        window.filename.setText('Resolver AES PNG')
        job = window.add_current_to_queue()
        assert job.request.media_metadata['video_id'] == 'deflate'
        assert job.request.to_context().playlist_decoder == 'rou'
        window.queue.start()
        wait_job(job)
        assert job.result['success'], job.result
        output = Path(job.result['files'][0])
        integration.probe(output)
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        outputs.append(str(output))
        save_check('Resolver确认后经原队列yt-dlpFFmpeg真实AES解密画面哈希一致')
        requests = [item for item in fixture.requests if item['path'].startswith('/cdn/aes/')]
        assert requests and all(item['token'] == 'old' for item in requests)
        assert all(item['headers'].get('referer') and item['headers'].get('user-agent') for item in requests)
        assert all(not item['headers'].get('cookie') and not item['headers'].get('authorization') for item in requests)
        save_check('三类资源实际RefererUA与签名保存且无凭据伪造')
        candidate = find('/v/api', lambda d: any(c.metadata.get('resolver_id') for c in d._candidates))
        assert any(item['path'] == '/api/hls/api' for item in fixture.requests)
        assert candidate.metadata.get('resolver_route') == 'legacy'
        save_check('当前API无重定向后显式API JSON入口legacy兜底')
        candidate = find('/v/variants', lambda d: any(c.metadata.get('resolver_id') for c in d._candidates))
        pump(timeout=.4, required=False)
        variant_paths = [item['path'] for item in fixture.requests if item['path'].startswith('/cdn/variants/')]
        assert candidate.metadata.get('resolver_route') == 'legacy'
        assert candidate.url == fixture.entry('variants', basename='index.jpg')
        assert any(item['path'] == '/api/hls/variants' for item in fixture.requests)
        assert variant_paths and set(variant_paths) == {'/cdn/variants/index.jpg'}
        save_check('API明确返回index.jpg且不请求相邻猜测地址')
        find('/v/normal', lambda d: any(item.get('error_code') == 'ROU_CHUNK_NOT_FOUND'
                                      for item in d._resolver_diagnostics))
        normal_paths = [item['path'] for item in fixture.requests if item['path'].startswith('/cdn/normal/')]
        assert normal_paths and set(normal_paths) == {'/cdn/normal/index.png'}
        assert not any(candidate.downloadable for candidate in dialog._candidates)
        save_check('普通PNG缺少roUd拒绝且不猜邻近清单')
        find('/v/fallback', lambda d: any(c.media_type == 'Direct' and c.context_complete and c.drm is False for c in d._candidates))
        generic = next(c for c in dialog._candidates if c.media_type == 'Direct'
                       and c.context_complete and c.drm is False)
        assert not generic.metadata.get('resolver_id')
        if generic.confidence < 70:
            assert dialog.selected_candidate() is None
        save_check('专用Resolver失败回到Generic Direct识别，低中分不假定自动选择')
        find('/tools/config', lambda d: any(c.media_type == 'HLS' and c.context_complete for c in d._candidates))
        configured = next(c for c in dialog._candidates if c.media_type == 'HLS' and c.context_complete)
        assert not configured.metadata.get('resolver_id')
        if configured.confidence < 70:
            assert dialog.selected_candidate() is None
        save_check('有界Fetch JSON配置实际响应验证与真实性门槛分开')
        find('/tools/close')
        controls = dialog.browser_controls
        original = js('document.getElementById("cover").getAttribute("style")')
        controls.scan_overlay()
        pump(lambda: controls.undo_button.isEnabled())
        assert js('window.closeClicked||0') == 0
        assert not controls.hide_overlay_button.isEnabled()
        assert js('document.getElementById("close").style.outline')
        controls.undo_page_changes()
        pump(lambda: not controls.undo_button.isEnabled())
        assert js('document.getElementById("cover").getAttribute("style")') == original
        save_check('遮挡关闭按钮只高亮滚动不自动点击')
        find('/tools/hide')
        controls = dialog.browser_controls
        original = js('[document.getElementById("cover").getAttribute("style"),document.body.getAttribute("style"),document.documentElement.getAttribute("style")]')
        controls.scan_overlay()
        pump(lambda: controls.hide_overlay_button.isEnabled())
        assert js('getComputedStyle(document.getElementById("cover")).display') != 'none'
        controls.hide_overlay_button.click()
        pump(lambda: js('getComputedStyle(document.getElementById("cover")).display') == 'none')
        assert js('getComputedStyle(document.getElementById("main")).display') != 'none'
        controls.undo_page_changes()
        pump(lambda: not controls.undo_button.isEnabled())
        restored = js('[document.getElementById("cover").getAttribute("style"),document.body.getAttribute("style"),document.documentElement.getAttribute("style")]')
        assert restored == original, (original, restored)
        save_check('用户手动隐藏普通遮挡且style滚动状态精确撤销')
        for case in ('login', 'player', 'frame'):
            find('/tools/' + case)
            controls = dialog.browser_controls
            controls.scan_overlay()
            pump(timeout=.4, required=False)
            assert not controls.hide_overlay_button.isEnabled(), case
            assert js('getComputedStyle(document.getElementById("main")).display') != 'none'
        save_check('播放器祖先Canvas登录权限层与跨源iframe保护')
        find('/tools/fit')
        controls = dialog.browser_controls
        controls.fit_window()
        pump(lambda: not controls._fit_running, 8)
        assert dialog.view.zoomFactor() <= 1
        find('/tools/too-wide')
        controls = dialog.browser_controls
        controls.fit_window()
        pump(lambda: not controls._fit_running, 8)
        assert dialog.view.zoomFactor() == .5 and '仍' in dialog.status_label.text()
        save_check('适应窗口逐档缩放且仍超出窗口时给出真实提示')
        find('/v/plain')
        dialog.browser_controls.back_button.click()
        # Initial dialog has one URL; navigate a second URL before verifying Back/Forward.
        dialog.page_url.setText(fixture.url('/v/api'))
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and dialog.page.url().path() == '/v/api')
        assert dialog.browser_controls.back_button.isEnabled()
        dialog.browser_controls.back_button.click()
        pump(lambda: dialog._page_loaded and dialog.page.url().path() == '/v/plain')
        assert dialog.browser_controls.forward_button.isEnabled()
        dialog.browser_controls.forward_button.click()
        pump(lambda: dialog._page_loaded and dialog.page.url().path() == '/v/api')
        save_check('浏览器前进后退刷新在新Page监听下继续工作')
        close_dialog()
        fixture.expire = True
        source = fixture.entry('clear', token='old', basename='index.m3u8')
        context = RequestContext(source, headers={'Referer': fixture.url('/v/expiry'), 'User-Agent': captured.request_context.user_agent},
                                 url_resolution_policy=UrlResolutionPolicy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING)
        request = DownloadRequest(window.yt_info.path, source, str(output_dir), filename='refresh-resume',
                                  headers=context.headers, ffmpeg_path=window.ff_info.path, context_snapshot=context,
                                  concurrent_fragments=1, segment_max_attempts=3)
        job = window.queue.add(request)
        window.queue.start()
        wait_job(job)
        assert job.state == 'waiting_context' and job.result['error_code'] == 'POSSIBLE_URL_EXPIRED', job.result
        assert job.diagnostic['drm'] is False and job.diagnostic['attempt'] == 3
        assert job.diagnostic['segment_index'] == 3
        partial = list(output_dir.glob('refresh-resume.*'))
        assert any('.part' in item.name or '.ytdl' in item.name for item in partial)
        baseline = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in partial}
        before_segment_count = sum('/part' in item['path'] for item in fixture.requests)
        window.queue.retry_with_context(job.id, replace(context, url=source + '&video=other', final_url=source + '&video=other', original_url=source + '&video=other'))
        wait_job(job)
        assert job.result['error_code'] == 'RESUME_MISMATCH'
        assert sum('/part' in item['path'] for item in fixture.requests) == before_segment_count
        assert baseline == {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in partial}
        save_check('不同视频身份更新在下载前拒绝且部分文件不变')
        new_source = fixture.entry('clear', token='new', basename='index.m3u8')
        fresh = replace(context, url=new_source, final_url=new_source, original_url=new_source)
        window.queue.retry_with_context(job.id, fresh)
        wait_job(job)
        assert job.result['success'], job.result
        resumed_requests = [item for item in fixture.requests if item['token'] == 'new' and '/part' in item['path']]
        assert resumed_requests and all(not item['path'].endswith(('part000.ts', 'part001.ts')) for item in resumed_requests)
        output = Path(job.result['files'][0])
        integration.probe(output)
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        outputs.append(str(output))
        metrics['resume'] = {'preserved_fragments': 2, 'fresh_segment_requests': len(resumed_requests),
                             'redownloaded_finished_fragments': 0, 'old_token_failure_attempts': 3}
        save_check('更新合法签名后同任务真实续传不重下已完成分片且画面一致')
        # Exercise the actual "refresh media" queue button, Resolver, candidate
        # confirmation and same-job resume together; only registry mapping is test-only.
        fixture.token = 'old'
        window._refresh_job_id = None
        candidate = find('/v/expiry', lambda d: any(c.metadata.get('resolver_id') for c in d._candidates))
        dialog.confirm_button.click()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        window.filename.setText('resolver-button-resume')
        window.concurrent_fragments.setValue(1)
        refreshed_job = window.add_current_to_queue()
        window.queue.start()
        wait_job(refreshed_job)
        assert refreshed_job.result['error_code'] == 'POSSIBLE_URL_EXPIRED', refreshed_job.result
        fixture.token = 'new'
        window.select_job(refreshed_job.id)
        window.update_queue_controls()
        assert window.refresh_media_button.isEnabled()
        def mapped_dialog(parent=None, page_url='', listen_seconds=60, resolver_registry=None, session_profile=None):
            return DiscoveryDialog(parent, page_url, listen_seconds=listen_seconds,
                                   resolver_registry=resolver_registry or ResolverRegistry([LocalRouResolver()]),
                                   session_profile=session_profile)
        with patch('ui.discovery_dialog.DiscoveryDialog', side_effect=mapped_dialog):
            window.refresh_media_button.click()
        dialog = window._discovery_dialog
        assert dialog is not None
        pump(lambda: dialog._page_loaded and any(c.metadata.get('video_id') == 'expiry' and 'token=new' in c.url for c in dialog._candidates), 20)
        count_before_refresh = len(fixture.requests)
        dialog.confirm_button.click()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        wait_job(refreshed_job)
        assert refreshed_job.result['success'], refreshed_job.result
        refreshed_segments = [item for item in fixture.requests[count_before_refresh:] if '/part' in item['path']]
        assert refreshed_segments and all(not item['path'].endswith(('part000.ts', 'part001.ts')) for item in refreshed_segments)
        output = Path(refreshed_job.result['files'][0])
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        outputs.append(str(output))
        save_check('队列刷新媒体按钮实际重跑Resolver并确认后同任务断点续传')
        fixture.expire = False
        fixture.fail_retry = True
        source = fixture.entry('retry', token='new', basename='index.m3u8')
        retry_context = replace(context, url=source, final_url=source, original_url=source)
        request = replace(request, url=source, filename='retry-limit', context_snapshot=retry_context, resume_identity=None)
        job = window.queue.add(request)
        wait_job(job)
        assert job.state == 'failed' and fixture.retry_count == 3, (job.result, fixture.retry_count)
        metrics['retry'] = {'actual_server_attempts': fixture.retry_count, 'configured_attempts': 3}
        save_check('HTTP500分片总尝试3次不叠加内部HTTP重试')
        fixture.retry_count = 0
        fixture.retry_pattern = [500, 403, 500]
        mixed = window.queue.add(replace(request, filename='mixed-retry-limit', resume_identity=None))
        wait_job(mixed)
        assert mixed.state == 'failed' and fixture.retry_count == 3, (mixed.result, fixture.retry_count)
        save_check('混合500与403不突破每分片实际3次请求上限')
        fixture.retry_count = 0
        fixture.retry_pattern = [500, 500]
        recovered = window.queue.add(replace(request, filename='retry-recovered', resume_identity=None))
        wait_job(recovered)
        assert recovered.result['success'] and fixture.retry_count == 3, (recovered.result, fixture.retry_count)
        output = Path(recovered.result['files'][0])
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        outputs.append(str(output))
        save_check('HTTP500暂时失败后第3次恢复并完成真实下载')
        assert ticks[0] > 100
        save_check('Resolver解析浏览器辅助及下载期间GUI保持响应且取消清理')
        report = {'passed': True, 'checks': checks, 'metrics': metrics, 'outputs': outputs,
                  'packaged': bool(getattr(sys, 'frozen', False)), 'qt_platform': app.platformName(),
                  'source_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else '',
                  'gui_ticks': ticks[0], 'third_party_site_tested': False,
                  'fixture': '自生成媒体 / localhost / 测试专用Resolver映射'}
    except Exception as error:
        report = {'passed': False, 'checks': checks, 'metrics': metrics, 'error': str(error), 'traceback': traceback.format_exc()}
        if dialog:
            report['status'] = dialog.status_label.text()
            report['candidates'] = [candidate.safe_diagnostic() for candidate in dialog._candidates]
    finally:
        timer.stop()
        if dialog:
            close_dialog()
        if window:
            window.queue.stop_all()
            pump(lambda: window.queue.active_count == 0, 20)
            window.close()
        fixture.close()
    (directory / 'resolver-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1
