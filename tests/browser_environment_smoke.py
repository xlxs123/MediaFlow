"""实际选用的浏览器环境验收，仅使用 localhost 自生成媒体和合成 Cookie。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent

from core.browser_runtime import browser_engine_info
from tests.discovery_fixture import DiscoveryFixture
from ui.main_window import MainWindow
from utils.config import ConfigStore


def run_browser_environment_smoke(app, args):
    directory = Path(args[args.index('--browser-environment-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixtures
    fixtures.ROOT = APP_DIR
    fixture = DiscoveryFixture(directory / 'media')
    app.setQuitOnLastWindowClosed(False)
    window = dialog = panel = None
    checks, reports = [], {}

    def pump(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('等待浏览器环境验收条件超时')

    def js(script):
        values = []
        dialog.page.runJavaScript(script, values.append)
        pump(lambda: bool(values))
        return values[-1]

    def diagnostic():
        panel.refresh_button.click()
        pump(lambda: panel.refresh_button.isEnabled() and panel._report is not None)
        return panel._report

    try:
        fixture.generate()
        fixture.start()
        config = ConfigStore(directory / 'test-config.json')
        assert config.save({}), config.error
        window = MainWindow(config)
        window.showMinimized() if app.platformName() == 'windows' else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 30)
        dialog = window.open_discovery()
        dialog.environment_button.click()
        panel = dialog.browser_environment
        pump(lambda: panel.refresh_button.isEnabled() and panel._report is not None)
        assert panel._report['video_tag'] is True
        assert not panel.busy
        checks.append('首次查找前的隔离世界异步探针可用；打开诊断不读取系统 Cookie')

        dialog.page_url.setText(fixture.page('direct'))
        dialog.find_button.click()
        pump(lambda: dialog._page_loaded and dialog._candidates)
        pump(lambda: js("document.querySelector('video').readyState") >= 2)
        js("const x=new XMLHttpRequest();x.open('GET','/pages/empty');x.send();true")
        pump(lambda: dialog.observer.network_diagnostics()['counts'].get('Network.responseReceived:XHR', 0) > 0)
        report = diagnostic()
        assert report['video_tag'] is True and report['html_video_element'] is True
        assert report['mse']['available'] is True
        assert report['media_capabilities']['available'] is True
        assert report['video_elements']['states'][0]['ready_state'] >= 2
        assert report['video_elements']['states'][0]['error_code'] == 0
        reports['capabilities'] = report
        checks.append('真实 HTML5 video 已载入本地 WebM；MediaSource / mediaCapabilities 存在')
        ua = report['browser']['user_agent']
        assert ua == dialog.profile.httpUserAgent() and 'QtWebEngine' not in ua
        assert report['browser']['platform'] == 'Win32' if sys.platform == 'win32' else bool(report['browser']['platform'])
        with fixture.lock:
            document_requests = [value for value in fixture.requests if value['path'] == '/pages/direct']
        assert document_requests and document_requests[-1]['headers']['user-agent'] == ua
        checks.append('navigator UA、真实 HTTP UA 一致，保留实际 Chromium 版本与 Windows 平台')
        counts = dialog.observer.network_diagnostics()['counts']
        for kind in ('Fetch', 'XHR', 'Media'):
            assert counts.get('Network.requestWillBeSent:' + kind, 0) > 0
            assert counts.get('Network.responseReceived:' + kind, 0) > 0
        reports['network'] = dialog.observer.network_diagnostics()
        assert any(item.media_type == 'Direct' for item in dialog._candidates)
        checks.append('CDP 原生捕获 Fetch / XHR / Media 请求与响应，并生成 MediaCandidate')

        assert js("localStorage.setItem('fixture-keep','local');sessionStorage.setItem('fixture-keep','session');document.cookie='fixture_keep=present; Path=/';true") is True
        report = diagnostic()
        for name in ('cookie', 'local_storage', 'session_storage'):
            assert report[name]['writable'] is True and report[name]['cleanup'] is True
        assert js("localStorage.getItem('fixture-keep')==='local'&&sessionStorage.getItem('fixture-keep')==='session'&&document.cookie.includes('fixture_keep=present')") is True
        checks.append('Cookie / localStorage / sessionStorage 可用；随机诊断项清理且不覆盖页面已有状态')

        cookie_file = directory / 'synthetic-cookies.txt'
        cookie_file.write_text('# Netscape HTTP Cookie File\n'
                               '#HttpOnly_127.0.0.1\tFALSE\t/\tFALSE\t0\tfixture_import\tlocal-test-only\n'
                               'unrelated.example\tFALSE\t/\tFALSE\t0\tother\tnot-imported\n', encoding='utf-8')
        panel._start_import(fixture.page('direct'), file_path=str(cookie_file))
        pump(lambda: not panel.busy and not panel._pending_cookies and panel._accepted_count == 1)
        assert '1/1' in panel.cookie_status.text()
        assert js("!document.cookie.includes('fixture_import=')") is True
        js("fetch('/pages/empty?cookie-check=1').then(r=>r.text());true")
        pump(lambda: any(value['path'] == '/pages/empty' and 'fixture_import=local-test-only' in value['headers'].get('cookie', '')
                         for value in fixture.requests))
        assert 'local-test-only' not in panel.output.toPlainText() + panel.cookie_status.text()
        checks.append('主动导入合成当前域 Cookie：HttpOnly 保持、真实请求携带、其他域不导入、界面不显示值')

        # 实际播放器尝试 H264/AAC；声明不支持时必须观察到错误，不能被 UA 修复成虚假成功。
        js("const v=document.querySelector('video');v.src='/media/direct.mp4';v.load();true")
        if report['can_play_type']['h264_aac_mp4'] == '':
            pump(lambda: js("document.querySelector('video').error?.code||0") in (3, 4))
            diagnostic()
            assert '修改 User-Agent 不会补充' in panel.output.toPlainText()
            checks.append('当前构建 H264/AAC 真正播放失败可诊断；未伪造 canPlayType / MSE 能力')
        else:
            pump(lambda: js("document.querySelector('video').readyState") >= 2)
            checks.append('当前构建 H264/AAC 实际播放通过，与声明核对')
        reports['unsupported_player'] = panel._report
        engine = browser_engine_info(dialog.profile)
        panel.grab().save(str(directory / 'browser-environment.png'))
        dialog.reject()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        checks.append('诊断面板和浏览器关闭后页面、监听与 worker 回收，持久 Profile 保留')
        result = {'passed': True, 'checks': checks, 'reports': reports, 'engine': engine,
                  'qt_platform': app.platformName(), 'packaged': bool(getattr(sys, 'frozen', False)),
                  'system_cookies_used': False, 'online_site_test': False,
                  'exe_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else ''}
    except Exception as error:
        result = {'passed': False, 'checks': checks, 'reports': reports, 'error': str(error),
                  'traceback': traceback.format_exc(), 'qt_platform': app.platformName(),
                  'browser_status': dialog.status_label.text() if dialog else '',
                  'network': dialog.observer.network_diagnostics() if dialog else {}}
    finally:
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None)
        if window:
            window.close()
            if window._webview2_profile:
                pump(lambda: window._webview2_profile.host._finished_once)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        fixture.stop()
    (directory / 'browser-environment-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'error': result.get('error')}, ensure_ascii=True), flush=True)
    return 0 if result['passed'] else 1
