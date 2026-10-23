"""Native HWND, H264/AAC, MSE and retained profile acceptance using local media."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent, QUrl

from core.browser_runtime import browser_engine_info
from core.site_resolvers import ResolverRegistry
from tests.discovery_fixture import DiscoveryFixture
from ui.discovery_dialog import DiscoveryDialog


def run_webview2_smoke(app, args):
    directory = Path(args[args.index('--webview2-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixtures
    fixtures.ROOT = APP_DIR
    fixture = DiscoveryFixture(directory / 'media')
    app.setQuitOnLastWindowClosed(False)
    dialog = profile = None
    checks, details, errors = [], {}, []

    def pump(predicate, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('WebView2 原生验收等待超时：' + (dialog.status_label.text() if dialog else '关闭阶段'))

    def js(script, world=0):
        values = []
        dialog.page.runJavaScript(script, world, values.append)
        pump(lambda: bool(values))
        return values[-1]

    def close_dialog():
        nonlocal dialog
        complete = []
        current = dialog
        current.shutdown_complete.connect(lambda: complete.append(True))
        current.reject()
        pump(lambda: bool(complete))
        current.deleteLater()
        dialog = None
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    try:
        assert app.platformName() == 'windows', 'Native HWND acceptance requires Windows Qt platform'
        fixture.generate()
        # A real fragmented MP4 from the same generated H264/AAC clip for MSE.
        subprocess.run([str(APP_DIR / 'tools' / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(fixture.directory / 'direct.mp4'), '-c', 'copy', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
            str(fixture.directory / 'mse-fragment.mp4')], check=True, timeout=25,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        fixture.start()
        dialog = DiscoveryDialog(page_url=fixture.page('direct'), resolver_registry=ResolverRegistry([]), browser_backend='webview2')
        dialog.showMinimized()
        dialog.observer.error.connect(errors.append)
        pump(lambda: dialog.page.is_ready, 35)
        assert dialog.browser_backend == 'webview2'
        details['engine'] = browser_engine_info(dialog.profile)
        # Verify that native Edge child windows really belong beneath the Qt view.
        children = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        @enum_proc
        def child(hwnd, _):
            children.append(int(hwnd))
            return True
        user32 = ctypes.windll.user32
        user32.EnumChildWindows.argtypes = [ctypes.c_void_p, enum_proc, ctypes.c_void_p]
        user32.EnumChildWindows(int(dialog.view.winId()), child, None)
        assert children, 'WebView2 未嵌入 Qt 子窗口'
        details['native_child_count'] = len(children)
        checks.append('WebView2 原生 HWND 嵌入 Qt 子窗口，未启动独立浏览器窗口')
        assert js('JSON.stringify({modern: typeof Promise.allSettled==="function",mse:!!window.MediaSource,fetch:!!window.fetch})')
        assert js('globalThis.__isolated_test="isolated";true', 1) is True
        assert js('typeof globalThis.__isolated_test') == 'undefined'
        assert js('globalThis.__isolated_test', 1) == 'isolated'
        checks.append('JS 回调及稳定隔离脚本世界可用，不污染网页全局变量')

        dialog.deep_find()
        pump(lambda: dialog._page_loaded and bool(dialog._candidates))
        js("(()=>{const v=document.querySelector('video');v.src='/media/direct.mp4';v.muted=true;v.load();v.play().catch(()=>{});return true;})()")
        pump(lambda: (js("(()=>{const v=document.querySelector('video');return v.readyState>=2&&v.currentTime>.2&&!v.error;})()") is True))
        probes = []
        assert dialog.observer.probe_capabilities(probes.append)
        pump(lambda: bool(probes))
        report = probes[-1]
        details['capabilities'] = report
        assert report['can_play_type']['h264_aac_mp4']
        assert report['mse']['available']
        checks.append('实际 H264/AAC MP4 解码播放，readyState 与播放时间推进通过')
        assert any(c.media_type == 'Direct' for c in dialog._candidates)
        counts = dialog.observer.network_diagnostics()['counts']
        assert counts.get('Network.responseReceived:Media', 0) > 0
        assert counts.get('Network.responseReceived:Fetch', 0) > 0
        checks.append('原生 CDP 捕获媒体和 Fetch，继续进入原有 MediaCandidate 评分流程')

        fragment = (fixture.directory / 'mse-fragment.mp4').read_bytes()
        import base64
        # Appended real container data; no player API or capability spoofing.
        encoded = json.dumps(base64.b64encode(fragment).decode('ascii'))
        avcc = fragment.index(b'avcC')
        codec = 'avc1.' + fragment[avcc + 5:avcc + 8].hex().upper()
        details['mse_codec'] = codec
        mse_script = '''(()=>{const video=document.querySelector('video');video.pause();
          const ms=new MediaSource();video.src=URL.createObjectURL(ms);video.muted=true;
          window.__mseState='pending';ms.addEventListener('sourceopen',()=>{try{
            const sb=ms.addSourceBuffer('video/mp4; codecs="CODEC, mp4a.40.2"');
            sb.addEventListener('error',()=>{window.__mseState='append_error';});
            sb.addEventListener('updateend',()=>{ms.endOfStream();video.play().catch(()=>{});window.__mseState='appended';},{once:true});
            const binary=atob(DATA);sb.appendBuffer(Uint8Array.from(binary,c=>c.charCodeAt(0)));
          }catch(_){window.__mseState='error';}},{once:true});return true;})()'''.replace('DATA', encoded).replace('CODEC', codec)
        assert js(mse_script) is True
        pump(lambda: js("window.__mseState==='appended'&&document.querySelector('video').currentTime>.2&&!document.querySelector('video').error") is True)
        checks.append('MSE SourceBuffer 实际追加并播放 H264/AAC fragmented MP4')

        js("document.cookie='retained_fixture=session-only; Path=/';localStorage.setItem('retained-fixture','present');true")
        profile = dialog.retain_session_profile()
        folder = profile.host.user_data_folder
        assert profile.isOffTheRecord()
        close_dialog()
        assert profile.host.process.state().name == 'Running'
        dialog = DiscoveryDialog(page_url=fixture.page('empty'), resolver_registry=ResolverRegistry([]), session_profile=profile)
        dialog.showMinimized()
        dialog.deep_find()
        pump(lambda: dialog._page_loaded)
        assert js("document.cookie.includes('retained_fixture=session-only')&&localStorage.getItem('retained-fixture')==='present'") is True
        cookies = []
        profile.cookieStore().cookies(fixture.page('empty'), cookies.append)
        pump(lambda: bool(cookies))
        assert any(item['name'] == 'retained_fixture' for item in cookies[-1])
        checks.append('关闭并重开页面后 retained InPrivate Profile 的 Cookie/LocalStorage 正确保留')
        dialog.page_url.setText(fixture.page('png_segments'))
        previous = dialog.page.page_id
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and any(c.media_type == 'HLS' and c.classification == 'HLS_AES128'
            and c.drm is False and c.context_complete for c in dialog._candidates))
        assert dialog.page.page_id != previous
        checks.append('重复查找创建新控制器；AES-128 与 PNG 扩展名分片保持原 HLS 语义')
        details['network'] = dialog.observer.network_diagnostics()
        close_dialog()
        closed = []
        profile.destroyed.connect(lambda: closed.append(True))
        profile.deleteLater()
        pump(lambda: bool(closed))
        profile = None
        checks.append('会话释放正常关闭控制器和宿主；临时目录只清理本会话')
        details['temporary_folder_removed'] = not folder.exists()
        assert not errors, errors
        result = {'passed': True, 'checks': checks, 'details': details, 'errors': errors,
                  'packaged': bool(getattr(sys, 'frozen', False)), 'system_cookies_used': False}
    except Exception as error:
        if dialog is not None and dialog.page.is_ready:
            details['mse_failure_state'] = js("JSON.stringify({state:window.__mseState,ready:document.querySelector('video')?.readyState,error:document.querySelector('video')?.error?.code,time:document.querySelector('video')?.currentTime})")
        result = {'passed': False, 'checks': checks, 'details': details, 'errors': errors,
                  'error': str(error), 'traceback': traceback.format_exc(),
                  'native_error': dialog.profile.host.last_error if dialog else ''}
    finally:
        if dialog:
            close_dialog()
        if profile:
            profile.deleteLater()
            app.processEvents()
        fixture.stop()
    (directory / 'webview2-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'error': result.get('error')}, ensure_ascii=True), flush=True)
    return 0 if result['passed'] else 1
