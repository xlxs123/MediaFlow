"""实际内嵌 Chromium / CDP / 浮窗 / 下载管线验收，支持冻结 EXE。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QMessageBox
from ui.main_window import MainWindow
from utils.config import ConfigStore
from utils.security import Redactor
from tests.discovery_fixture import DiscoveryFixture, AUTH_HEADER, COOKIE_VALUE, SIGNATURE_QUERY


def run_discovery_smoke(app, args):
    directory = Path(args[args.index('--discovery-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixture_module
    fixture_module.ROOT = APP_DIR
    import tests.integration_hls as integration
    integration.ROOT = APP_DIR
    checks, samples = [], []
    fixture = DiscoveryFixture(directory / 'media')
    window = dialog = None
    ticks = [0]
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start()
    app.setQuitOnLastWindowClosed(False)
    native = app.platformName() == 'windows'
    captured = {}
    seen_dom = []

    def pump(predicate=lambda: False, timeout=12, required=True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(0.01)
        if required:
            raise AssertionError('等待实际 Chromium / GUI 条件超时')

    def close_dialog():
        nonlocal dialog
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None)
            dialog = None

    def main_input():
        return (window.url.text(), window.collect_headers(), window._request_method,
                window._request_body, window._discovery_context, window.inherit_range.isChecked(),
                window.inherit_browser_metadata.isChecked())

    def find_case(case, predicate, timeout=15):
        nonlocal dialog
        seen_dom.clear()
        before = main_input()
        dialog = window.open_discovery()
        dialog.observer.dom.connect(lambda snapshot: seen_dom.append(snapshot))
        assert dialog.windowTitle() == 'MediaFlow · 媒体发现'
        assert not dialog.isModal() and dialog.windowModality() == Qt.WindowModality.NonModal
        assert not dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert [button.text() for button in (dialog.find_button, dialog.confirm_button, dialog.cancel_button)] == ['查找', '确定', '取消']
        assert dialog.profile.isOffTheRecord() == (dialog.browser_backend != 'webview2')
        dialog.page_url.setText(fixture.page(case))
        dialog.find_button.click()
        try:
            pump(lambda: predicate(dialog), timeout)
        except AssertionError:
            # 仅写脱敏的候选资料用于定位，浏览器 body/原始头不输出。
            safe = [candidate.safe_diagnostic() for candidate in dialog._candidates]
            (directory / 'failed-candidates.json').write_text(json.dumps({'case': case,
                'status': dialog.status_label.text(), 'candidates': safe}, ensure_ascii=False, indent=2), encoding='utf-8')
            raise
        assert main_input() == before, '查找过程修改了主输入'
        for candidate in dialog._candidates:
            assert not candidate.url.startswith('blob:')
        samples.append({'case': case, 'candidate_count': len(dialog._candidates),
                        'visible_count': dialog.table.rowCount(), 'player_type': dialog._player_type,
                        'selected': dialog.selected_candidate().safe_diagnostic() if dialog.selected_candidate() else None})
        return before

    def candidate(kind=None, suffix=None, classification=None):
        return next((item for item in dialog._candidates
                     if (kind is None or item.media_type == kind)
                     and (suffix is None or item.url.split('?', 1)[0].endswith(suffix))
                     and (classification is None or item.classification == classification)), None)

    try:
        fixture.generate()
        fixture.start()
        store = ConfigStore(directory / 'test-config.json')
        window = MainWindow(store)
        window.showMinimized() if native else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 30)
        window.url.setText('https://untouched.test/manual?token=original-private')
        checks.append('自动查找入口与非模态父窗口浮窗无全局置顶')

        before = find_case('direct', lambda d: bool(candidate('Direct', '/direct.mp4')
                          and candidate('Direct', '/direct.mp4').http_status == 206
                          and candidate('Direct', '/direct.mp4').context_complete
                          and candidate('Direct', '/direct.mp4').drm is False))
        direct = candidate('Direct', '/direct.mp4')
        assert direct.request_context.headers['range'] == 'bytes=0-'
        captured['Direct MP4'] = deepcopy(direct)
        checks.append('Direct MP4真实206与完整Range上下文')
        # 实际 WebM 播放与 seek 事件，从页面自己的主播放器读取；没有模拟 DOM 数据。
        seek_done = []
        pump(lambda: any(v.get('duration') and v.get('width') for snapshot in seen_dom
                         for v in snapshot.get('videos', [])), 12)
        dialog.page.runJavaScript("(() => {const v=document.getElementById('main');v.currentTime=4;return v.currentSrc;})()",
                                 lambda value: seek_done.append(value))
        pump(lambda: bool(seek_done), 3)
        pump(lambda: any(event.get('type') in ('seeking', 'seeked') for snapshot in seen_dom
                         for video in snapshot.get('videos', []) for event in video.get('events', [])), 5)
        checks.append('内嵌页面真实播放与拖动后继续监听')
        close_dialog()
        assert main_input() == before
        checks.append('取消停止监听PageProfileWorker且不修改主输入')

        for case, classification, name in [('hls_clear', 'CLEAR_HLS', 'HLS Clear'),
                                            ('hls_aes', 'HLS_AES128', 'HLS AES-128'),
                                            ('png_segments', 'HLS_AES128', 'HLS PNG')]:
            find_case(case, lambda d, value=classification: bool(candidate('HLS', classification=value)
                           and candidate('HLS', classification=value).context_complete
                           and candidate('HLS', classification=value).drm is False
                           and candidate('HLS', classification=value).metadata.get('manifest')))
            item = candidate('HLS', classification=classification)
            assert item.downloadable
            assert item.request_context.headers['referer'].startswith(fixture.page(case))
            assert item.request_context.headers['user-agent']
            assert item.request_context.inherit_range
            if case == 'hls_aes':
                assert item.request_context.headers['authorization'] == AUTH_HEADER
                assert COOKIE_VALUE in item.request_context.cookies
                assert AUTH_HEADER not in dialog.details.toPlainText() and COOKIE_VALUE not in dialog.details.toPlainText()
            if case == 'png_segments':
                assert not any(c.url.split('?', 1)[0].endswith('.png') for c in dialog._candidates)
                resources = item.metadata['manifest'].get('resources', [])
                assert any(r['uri'] == 'abc000.png' and r['role'] == 'Media Segment' for r in resources)
                assert not item.request_context.origin and not item.request_context.cookies
            captured[name] = deepcopy(item)
            checks.append(name + '内容识别及实际Headers / AES非DRM / 分片语义')
            close_dialog()

        find_case('dash', lambda d: bool(candidate('DASH') and candidate('DASH').drm is False
                                        and candidate('DASH').context_complete))
        captured['DASH'] = deepcopy(candidate('DASH'))
        checks.append('DASH实际网络与Manifest检测')
        close_dialog()

        find_case('mse', lambda d: d._player_type == 'BLOB_MSE' and bool(candidate('HLS')))
        assert not any(c.url.startswith('blob:') for c in dialog._candidates)
        checks.append('Blob MSE通过Network关联真实HLS而非下载blob')
        close_dialog()

        find_case('variants', lambda d: len({item.resolution[1] for item in d._candidates if item.media_type == 'HLS'}) >= 3)
        assert {480, 720, 1080} <= {item.resolution[1] for item in dialog._candidates}
        # 等待真实子清单检测齐全；保留的 Master/低分诊断行不能作为完成信号。
        def variants_ready():
            if len(dialog._groups) != 1:
                return False
            group = dialog._groups[0]
            choices = dialog._choices_for_group(group)
            return ({item.resolution[1] for item in choices} == {480, 720, 1080}
                    and all(item.duration and 200 <= item.http_status < 300 for item in choices)
                    and group.selected_candidate is not None and group.selected_candidate.resolution[1] == 1080)
        pump(variants_ready)
        quality_row = next(row for row in range(dialog.table.rowCount())
            if dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == dialog._groups[0].selected_candidate.id
            and dialog.table.cellWidget(row, 2) is not None)
        assert dialog.selected_candidate() is None
        assert not any(group.selected for group in dialog._groups)
        dialog.table.selectRow(quality_row)  # 证据不足70分时由用户选择，不再默选列表首项。
        assert dialog.selected_candidate().resolution[1] == 1080
        assert any(c.manifest_type == 'Master' and c.metadata.get('codecs') for c in dialog._candidates)
        master = next(c for c in dialog._candidates if c.manifest_type == 'Master')
        assert master.id in dialog._candidate_cards, 'Master入口也保留为诊断卡片'
        pump(timeout=0.2, required=False)
        dialog.grab().save(str(directory / 'discovery-variants.png'))
        quality = dialog.table.cellWidget(quality_row, 2)
        quality.setCurrentIndex(next(index for index in range(quality.count()) if quality.itemText(index).startswith('480p')))
        dialog._render_candidates()
        assert dialog.selected_candidate().resolution[1] == 480
        checks.append('手动选择480p后持续更新保持用户选择')
        checks.append('Master多清晰度Codecs帧率与1080p排序，中分候选需手动选择')
        close_dialog()

        find_case('redirect', lambda d: bool(candidate('HLS') and candidate('HLS').redirects))
        redirected = candidate('HLS')
        assert redirected.request_context.original_url == fixture.base + '/redirect/manifest'
        assert redirected.request_context.redirect_chain[0]['status'] == 302
        assert redirected.final_url == fixture.base + '/streams/clear/index?' + SIGNATURE_QUERY
        assert redirected.request_context.derive('part000.ts').url == fixture.base + '/streams/clear/part000.ts'
        captured['redirect'] = deepcopy(redirected)
        checks.append('302原始最终地址及重定向链完整保留')
        close_dialog()

        find_case('signed', lambda d: bool(candidate('HLS', classification='HLS_AES128')
                        and candidate('HLS', classification='HLS_AES128').context_complete))
        signed = candidate('HLS', classification='HLS_AES128')
        assert signed.url == fixture.base + '/streams/png/index?' + SIGNATURE_QUERY
        assert 'fixture%2F%2Bvalue' not in dialog.details.toPlainText()
        assert 'token=one' not in dialog.details.toPlainText()
        checks.append('secure sign token重复query原样保留且UI隐藏')
        close_dialog()

        find_case('vast', lambda d: bool(candidate('Direct', '/main.webm')) and any(c.is_ad for c in d._candidates))
        pump(timeout=1, required=False)
        assert dialog.selected_candidate().url.endswith('/main.webm')
        assert not dialog.selected_candidate().is_ad
        assert all(c.confidence >= 40 for c in dialog._visible_candidates())
        dialog.include_low.setChecked(True)
        assert any(c.is_ad for c in dialog._visible_candidates())
        pump(timeout=0.2, required=False)
        dialog.grab().save(str(directory / 'discovery-ads.png'))
        checks.append('VAST与ad-provider通用降权正片默认胜出；所有候选保留，低分可展开')
        close_dialog()

        for case in ('thumbnail', 'poster', 'empty'):
            find_case(case, lambda d: d.page.url().toString() == fixture.page(case))
            pump(timeout=1.1, required=False)
            assert not dialog._candidates, case
            dialog._listen_ended()
            assert 'PLAYER_NOT_STARTED' in dialog.status_label.text()
            checks.append({'thumbnail': 'thumbnail.vtt不作为媒体候选', 'poster': 'video poster不作为完整媒体',
                           'empty': '无媒体页面给出明确提示'}[case])
            close_dialog()

        find_case('multiple', lambda d: bool(candidate('Direct', '/main.webm'))
                  and len([c for c in d._candidates if c.media_type == 'Direct']) >= 2)
        pump(timeout=1, required=False)
        assert dialog.selected_candidate().url.endswith('/main.webm')
        checks.append('多个video以实际主播放器和播放行为优先')
        close_dialog()

        find_case('drm', lambda d: any(c.drm is True for c in d._candidates))
        protected = next(c for c in dialog._candidates if c.drm is True)
        assert not protected.downloadable
        assert not dialog.confirm_button.isEnabled()
        old_input = main_input()
        dialog.confirm()
        assert main_input() == old_input
        assert not any('/license' in r['path'] for r in fixture.requests)
        checks.append('真实FairPlay证据禁止确定且不请求许可证或密钥')
        # 查找第二轮清空 protected 并重新监听另一页面。
        previous_epoch = dialog.worker.generation
        dialog.page_url.setText(fixture.page('hls_clear'))
        dialog.find_button.click()
        assert not dialog._candidates
        pump(lambda: bool(candidate('HLS', classification='CLEAR_HLS') and candidate('HLS', classification='CLEAR_HLS').context_complete))
        assert dialog.worker.generation > previous_epoch and not any(c.drm is True for c in dialog._candidates)
        checks.append('重复查找清空旧结果及旧epoch不污染新页面')
        close_dialog()

        before = find_case('slow_navigation', lambda d: fixture.slow_requested.is_set()
                           and bool(candidate('HLS', classification='CLEAR_HLS')))
        assert not dialog._page_loaded, '测试旧页面应仍在等待慢资源'
        old_page = dialog.page
        old_target = old_page.devToolsId()
        kept_profile, kept_observer, kept_view = dialog.profile, dialog.observer, dialog.view
        previous_epoch = dialog.worker.generation
        dialog.page_url.setText(fixture.page('empty'))
        dialog.find_button.click()
        assert dialog.page is not old_page and dialog.page.devToolsId() != old_target
        assert (dialog.profile, dialog.observer, dialog.view) == (kept_profile, kept_observer, kept_view)
        assert dialog.worker.generation > previous_epoch
        fixture.slow_release.set()
        pump(lambda: dialog.page.url().toString() == fixture.page('empty') and dialog._page_loaded)
        pump(timeout=1.2, required=False)
        assert not dialog._candidates and main_input() == before
        assert not any('/streams/' in hop.url for record in dialog.observer._store.records.values() for hop in record.hops)
        checks.append('旧页面尚未加载完成连续查找不串DOM iframe或媒体请求')
        close_dialog()

        find_case('iframe', lambda d: bool(candidate('Direct', '/main.webm')
                                            and candidate('Direct', '/main.webm').context_complete), 18)
        assert any('localhost' in c.url for c in dialog._candidates)
        checks.append('跨源iframe实际媒体与独立frame上下文')
        close_dialog()

        find_case('post', lambda d: bool(candidate('Direct', '/post') and candidate('Direct', '/post').context_complete))
        post = candidate('Direct', '/post')
        assert post.request_context.method == 'POST'
        assert post.request_context.body == b'fixture=body%2Bvalue'
        assert post.request_context.headers['authorization'] == AUTH_HEADER
        captured['POST Direct'] = deepcopy(post)
        checks.append('POST实际Method Body Authorization完整采集')
        close_dialog()

        # 从自动查找导入的真实上下文仍进入原有队列 / guard / MediaBridge / yt-dlp。
        window.output_directory.setText(str(directory / 'downloads' / str(time.time_ns())))
        jobs = []
        for name in ('HLS AES-128', 'HLS PNG', 'Direct MP4', 'DASH', 'POST Direct'):
            item = captured[name]
            window.import_discovered_media(item)
            request = window._collect_request()
            assert request.to_context() == item.request_context
            assert window._request_body == item.request_context.body
            window.filename.setText('自动查找 ' + name.replace('/', '-'))
            jobs.append(window.add_current_to_queue())
        assert all(job.state == 'queued' for job in jobs)
        checks.append('确认仅导入候选完整上下文且不自动启动下载')
        window.start_queue()
        pump(lambda: window.queue.active_count == 0 and all(job.worker is None for job in jobs), 100)
        reference = integration.decoded_hash(fixture.directory / 'direct.mp4')
        outputs = []
        for job in jobs:
            assert job.result and job.result['success'], job.result
            file = Path(job.result['files'][0])
            info = integration.probe(file)
            assert integration.decoded_hash(file) == reference
            outputs.append(str(file))
        checks.append('自动查找AES PNG Direct DASH POST均经现有管线真实下载与画面哈希一致')
        # 单独验证“确定”按钮关闭浮窗并送回当前选择。
        find_case('hls_aes', lambda d: bool(candidate('HLS', classification='HLS_AES128')
                       and candidate('HLS', classification='HLS_AES128').context_complete
                       and 40 <= candidate('HLS', classification='HLS_AES128').confidence < 70))
        assert dialog.selected_candidate() is None and not dialog.confirm_button.isEnabled()
        dialog.table.selectRow(0)
        assert dialog.confirm_button.isEnabled()
        selected = deepcopy(dialog.selected_candidate())
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as confirmation:
            dialog.confirm_button.click()
            pump(lambda: window._discovery_dialog is None)
        confirmation.assert_called_once()
        dialog = None
        assert window.url.text() == selected.url and window.collect_headers() == selected.request_context.headers
        assert window._discovery_context.redirect_chain == selected.request_context.redirect_chain
        checks.append('中分候选手动选行并确认后完整导入及释放监听浏览器后台任务')
        assert window.parse_text(f'curl "{selected.url}" -H "User-Agent: Manual Fixture"')
        assert window._discovery_context is None
        assert window._collect_request().to_context().redirect_chain == ()
        assert not window.collect_headers().get('cookie') and not window.collect_headers().get('authorization')
        checks.append('同URL手动cURL替换自动查找上下文且不串用凭据')
        assert ticks[0] > 100
        checks.append('实际Chromium分析下载期间GUI持续响应')
        persisted = store.path.read_text(encoding='utf-8')
        for secret in (AUTH_HEADER, COOKIE_VALUE, SIGNATURE_QUERY, 'body%2Bvalue', 'original-private'):
            assert secret not in persisted and secret not in window.log_view.toPlainText()
        checks.append('配置日志候选详情不持久化请求凭据或完整签名')
        # 保存最终非敏感窗口截图；后台验收不控制用户桌面或剪贴板。
        window.grab().save(str(directory / 'discovery-main.png'))
        report = {'passed': True, 'packaged': bool(getattr(sys, 'frozen', False)),
                  'qt_platform': app.platformName(), 'checks': checks, 'cases': samples,
                  'outputs': outputs, 'gui_ticks': ticks[0], 'minimum_scenarios': 15,
                  'site_domain_rules': 0, 'fixture': 'FFmpeg 自生成视频 / localhost 测试网页',
                  'source_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else ''}
    except Exception as error:
        report = {'passed': False, 'packaged': bool(getattr(sys, 'frozen', False)),
                  'qt_platform': app.platformName(), 'checks': checks, 'cases': samples,
                  'error': Redactor().redact(str(error)), 'traceback': Redactor().redact(traceback.format_exc())}
    finally:
        if dialog:
            close_dialog()
        if window:
            window.queue.stop_all()
            if window.queue.active_count:
                pump(lambda: window.queue.active_count == 0, 20)
            window.close()
            if window._webview2_profile:
                pump(lambda: window._webview2_profile.host._finished_once)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        timer.stop()
        fixture.stop()
    (directory / 'discovery-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('cases',)}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['passed'] else 1
