"""Real process restarts, synthetic local login, no personal browser data."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QProcess
from PySide6.QtWidgets import QMessageBox


def _pump(app, predicate, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('持久 Profile 验收等待超时')


def run_browser_profile_stage(app, args):
    from core.site_resolvers import ResolverRegistry
    from ui.main_window import MainWindow
    from utils.config import ConfigStore
    i = args.index('--browser-profile-stage')
    stage, folder, base = args[i + 1:i + 4]
    directory = Path(folder).resolve()
    checks = []
    window = dialog = None
    app.setQuitOnLastWindowClosed(False)

    def open_page(path='/state'):
        nonlocal dialog
        dialog = window.open_discovery(page_url=base + path, activate=False)
        dialog.worker.resolver_registry = ResolverRegistry([])
        dialog.showMinimized()
        dialog.deep_find()
        _pump(app, lambda: dialog._page_loaded)
        assert dialog.browser_backend == 'webview2'
        assert not dialog.profile.isOffTheRecord()
        assert dialog.profile.host.user_data_folder == directory / 'BrowserProfile'

    def js(script):
        result = []
        dialog.page.runJavaScript(script, result.append)
        _pump(app, lambda: bool(result))
        return result[-1]

    def state():
        # Only booleans leave the browser; HttpOnly cookie checked by fixture.
        return json.loads(js("(async()=>JSON.stringify({login:(await (await fetch('/logged-in',{cache:'no-store'})).json()).login,local:localStorage.getItem('fixture-state')==='saved',session:sessionStorage.getItem('fixture-session')==='saved'}))()"))

    def close_page():
        nonlocal dialog
        dialog.reject()
        _pump(app, lambda: window._discovery_dialog is None)
        dialog = None

    try:
        window = MainWindow(ConfigStore(directory / 'test-config.json'))
        window.showMinimized()
        _pump(app, lambda: window.detector is None)
        open_page('/seed' if stage == 'seed' else '/state')
        if stage == 'seed':
            assert js("localStorage.setItem('fixture-state','saved');sessionStorage.setItem('fixture-session','saved');true") is True
            assert state() == {'login': True, 'local': True, 'session': True}
            profile = dialog.profile
            checks.append('合成 HttpOnly 持久 Cookie 与 LocalStorage 在本地 Profile 中建立')
            denied = []
            profile.clear_browsing_data(lambda value, ok: denied.append((value, ok)))
            _pump(app, lambda: bool(denied))
            assert denied[-1][1] is False and profile.host.last_error == 'PROFILE_IN_USE'
            checks.append('原生层拒绝清除仍被页面使用的 Profile')
            with patch('ui.main_window.QMessageBox.information'), patch('ui.main_window.QMessageBox.question') as question:
                window.clear_browser_button.click()
            question.assert_not_called()
            window.settings_toggle.setChecked(True)
            window.grab().save(str(directory / 'browser-profile-settings.png'))
            close_page()
            with patch('ui.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
                window.clear_browser_button.click()
            assert question.call_args.args[-1] == QMessageBox.StandardButton.No
            open_page()
            assert dialog.profile is profile
            assert state() == {'login': True, 'local': True, 'session': False}
            checks.append('关闭/重开查找窗口保持登录；取消清除保留状态；SessionStorage 遵守页面生命周期')
        elif stage == 'read-clear':
            assert state() == {'login': True, 'local': True, 'session': False}
            checks.append('全新 MediaFlow 进程重新读取持久 Cookie 与 LocalStorage，保持合成登录')
            close_page()
            with patch('ui.main_window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                window.clear_browser_button.click()
            _pump(app, lambda: not window._clearing_browser_data)
            assert '已清除' in window.status_label.text()
            assert window.clear_browser_button.isEnabled()
            checks.append('用户确认后原生 AllProfile 异步清除完成，UI 才报告成功')
            open_page()
            assert state() == {'login': False, 'local': False, 'session': False}
            checks.append('清除后重开页面确认 Cookie 与 LocalStorage 均已移除')
        elif stage == 'verify-clear':
            assert state() == {'login': False, 'local': False, 'session': False}
            checks.append('再次重启进程后已清除的登录状态不会恢复')
        else:
            raise AssertionError('未知持久 Profile 验收阶段')
        close_page()
        profile = window._webview2_profile
        assert profile.host.user_data_folder.is_dir()
        window.close()
        _pump(app, lambda: profile.host._finished_once and not window.isVisible())
        assert (directory / 'BrowserProfile').is_dir()
        checks.append('退出等待浏览器宿主关闭，固定 BrowserProfile 保留')
        result = {'passed': True, 'stage': stage, 'checks': checks}
    except Exception as error:
        result = {'passed': False, 'stage': stage, 'checks': checks, 'error': str(error), 'traceback': traceback.format_exc()}
    finally:
        if dialog:
            close_page()
        if window:
            window.close()
            if window._webview2_profile:
                _pump(app, lambda: window._webview2_profile.host._finished_once)
    (directory / (stage + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['passed'] else 1


def run_browser_profile_smoke(app, args):
    directory = Path(args[args.index('--browser-profile-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app.setQuitOnLastWindowClosed(False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/logged-in':
                body = json.dumps({'login': 'mf_login=synthetic-local-only' in self.headers.get('Cookie', '')}).encode()
                mime = 'application/json'
            else:
                body = b'<!doctype html><meta charset="utf-8"><title>Local profile fixture</title><p>Local synthetic login</p>'
                mime = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if self.path == '/seed':
                self.send_header('Set-Cookie', 'mf_login=synthetic-local-only; Max-Age=3600; Path=/; HttpOnly; SameSite=Lax')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reports = []
    try:
        for stage in ('seed', 'read-clear', 'verify-clear'):
            process = QProcess()
            process.setProgram(sys.executable)
            prefix = [] if getattr(sys, 'frozen', False) else [str(Path(__file__).resolve().parents[1] / 'main.py')]
            process.setArguments(prefix + ['--browser-profile-stage', stage, str(directory),
                f'http://127.0.0.1:{server.server_port}', '--native-smoke'])
            process.start()
            try:
                _pump(app, lambda: process.state() == QProcess.ProcessState.NotRunning, timeout=110)
            except Exception:
                process.kill()
                process.waitForFinished(3000)
                raise
            report = json.loads((directory / (stage + '.json')).read_text(encoding='utf-8'))
            reports.append(report)
            if process.exitCode() != 0 or not report['passed']:
                raise AssertionError(report.get('error') or '持久 Profile 子进程失败')
        result = {'passed': True, 'checks': sum(len(report['checks']) for report in reports),
                  'stages': reports, 'packaged': bool(getattr(sys, 'frozen', False)),
                  'profile': str(directory / 'BrowserProfile'), 'real_accounts_used': False}
    except Exception as error:
        result = {'passed': False, 'stages': reports, 'error': str(error), 'traceback': traceback.format_exc()}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
    (directory / 'browser-profile-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: result.get(key) for key in ('passed', 'checks', 'error')}), flush=True)
    return 0 if result['passed'] else 1
