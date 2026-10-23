"""Qt native child view backed by an isolated, app-owned WebView2 pipe host.

The browser process owns Edge COM objects. Python keeps the existing page/profile
interfaces; untrusted documents never receive this IPC or access to host objects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Qt, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


# CDP 的 body 可经过内层 JSON 和 IPC JSON 两次转义；这里为最多128 MiB
# 的采集正文预留有界编码空间。实际会话按采集设置使用更小的接收上限。
MAX_LINE_BYTES = 128 * 1024 * 1024 * 8 + 1024 * 1024
MAX_REQUEST_LINE_BYTES = 8 * 1024 * 1024
MAX_PENDING = 512


def response_line_limit(max_body_bytes=16 * 1024 * 1024):
    from core.browser_observer import capture_body_limit
    return min(MAX_LINE_BYTES, capture_body_limit(max_body_bytes) * 8 + 1024 * 1024)


def browser_profile_path(config_store=None):
    """Local, app-owned state; explicit test configs remain isolated."""
    if config_store is not None and config_store.legacy_path is None:
        return config_store.path.resolve().parent / 'BrowserProfile'
    local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    if getattr(config_store, 'data_version', ''):
        return local / 'MediaFlow' / config_store.data_version / 'BrowserProfile'
    return local / 'MediaFlow' / 'BrowserProfile'


def host_path():
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    return root / 'tools' / 'webview2' / 'MediaFlow.WebView2Host.exe'


def webview2_available():
    """Only choose a native Windows backend when its complete host is installed.

    Runtime availability is finally established by native environment creation;
    a missing/broken runtime is surfaced rather than disguised as a page failure.
    """
    path = host_path()
    return (sys.platform == 'win32' and path.is_file()
            and path.with_name('Microsoft.Web.WebView2.Core.dll').is_file()
            and path.with_name('WebView2Loader.dll').is_file())


def select_browser_backend(session_profile=None, requested=''):
    if session_profile is not None:
        return 'webview2' if isinstance(session_profile, WebView2Profile) else 'qtwebengine'
    value = (requested or os.environ.get('MEDIAFLOW_BROWSER_BACKEND', 'auto')).lower()
    if value not in ('auto', 'webview2', 'qtwebengine'):
        raise ValueError('未知浏览器内核设置。')
    # Offscreen is an existing test/non-Windows path, not a native HWND surface.
    native = QApplication.instance() is not None and QApplication.platformName() == 'windows'
    if value == 'webview2':
        if not native or not webview2_available():
            raise RuntimeError('WebView2 宿主不可用，请安装完整版本及 Microsoft Edge WebView2 Runtime。')
        return value
    return 'webview2' if value == 'auto' and native and webview2_available() else 'qtwebengine'


class _PipeHost(QObject):
    event = Signal(dict)
    ready = Signal(dict)
    failed = Signal(str)
    closed = Signal()

    def __init__(self, parent=None, user_data_folder=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProgram(str(host_path()))
        self.process.setWorkingDirectory(str(host_path().parent))
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self.process.started.connect(self._initialize)
        self._buffer = bytearray()
        self.max_line_bytes = response_line_limit()
        self._pending = {}
        self._queued = []
        self._counter = 0
        self.last_error = ''
        self._started = self._ready = self._closing = self._finished_once = False
        self._session_root = Path(tempfile.gettempdir()) / 'MediaFlow-WebView2'
        self.persistent = user_data_folder is not None
        if self.persistent:
            self.user_data_folder = Path(user_data_folder).resolve()
            if self.user_data_folder.name != 'BrowserProfile':
                raise ValueError('持久浏览器目录必须为应用的 BrowserProfile。')
            self.user_data_folder.mkdir(parents=True, exist_ok=True)
        else:
            self._session_root.mkdir(parents=True, exist_ok=True)
            self.user_data_folder = Path(tempfile.mkdtemp(prefix='session-', dir=self._session_root))
        self._timeout = QTimer(self)
        self._timeout.setInterval(500)
        self._timeout.timeout.connect(self._expire)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self.process.kill)

    def start(self):
        if not self._started and not self._closing:
            self._started = True
            self._timeout.start()
            self.process.start()

    def set_capture_body_limit(self, max_body_bytes):
        # 同一个持久 Profile 可服务多个页面；新窗口不能缩小仍在运行页面
        # 的 IPC 编码空间。每个 observer 的原始正文上限仍各自严格执行。
        self.max_line_bytes = max(self.max_line_bytes, response_line_limit(max_body_bytes))

    def _initialize(self):
        self._write('initialize', {'userDataFolder': str(self.user_data_folder),
                                  'persistent': self.persistent}, self._initialized, 30)

    def _initialized(self, result, ok):
        if ok and self.persistent and isinstance(result, dict) and result.get('persistent') is not True:
            self.failed.emit('WebView2 宿主不支持持久 Profile，请完整更新 tools/webview2 安装文件。')
            self.shutdown()
            return
        if not ok or not isinstance(result, dict):
            self.failed.emit('无法初始化 WebView2。请检查 Runtime 是否完整安装、BrowserProfile 是否可写或被其他 MediaFlow 窗口占用。')
            self.shutdown()
            return
        self._ready = True
        self.ready.emit(result)
        queued, self._queued = self._queued, []
        for op, values, callback, timeout in queued:
            if not self._closing:
                self._write(op, values, callback, timeout)

    def request(self, op, values=None, callback=None, timeout=10):
        if self._closing or len(self._pending) + len(self._queued) >= MAX_PENDING:
            if callback:
                QTimer.singleShot(0, lambda: callback(None, False))
            return False
        self.start()
        if not self._ready:
            self._queued.append((op, values or {}, callback, timeout))
        else:
            self._write(op, values or {}, callback, timeout)
        return True

    def _write(self, op, values, callback, timeout):
        self._counter += 1
        identifier = self._counter
        payload = json.dumps(dict(values, id=identifier, op=op), ensure_ascii=True, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(payload) > MAX_REQUEST_LINE_BYTES:
            if callback:
                QTimer.singleShot(0, lambda: callback(None, False))
            return
        self._pending[identifier] = (time.monotonic() + timeout, callback)
        self.process.write(payload)

    def _read(self):
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b'\n' in self._buffer:
            line, _, remaining = self._buffer.partition(b'\n')
            self._buffer = bytearray(remaining)
            if len(line) > self.max_line_bytes:
                self._protocol_failed()
                return
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                self._protocol_failed()
                return
            self._dispatch(message)
        if len(self._buffer) > self.max_line_bytes:
            self._protocol_failed()

    def _dispatch(self, message):
        if not isinstance(message, dict):
            return
        if 'id' in message:
            code = message.get('error')
            if isinstance(code, str) and 0 < len(code) <= 64 and all(c.isupper() or c.isdigit() or c == '_' for c in code):
                self.last_error = code
            pending = self._pending.pop(message.get('id'), None)
            if pending and pending[1]:
                pending[1](message.get('result'), message.get('ok') is True)
        elif isinstance(message.get('event'), str) and not self._closing:
            self.event.emit(message)

    def _expire(self):
        now = time.monotonic()
        for identifier, (deadline, callback) in list(self._pending.items()):
            if now >= deadline:
                self._pending.pop(identifier, None)
                if callback:
                    callback(None, False)

    def _protocol_failed(self):
        self.failed.emit('WebView2 通信失败，请关闭媒体发现窗口后重试。')
        self.shutdown()

    def _process_error(self, _):
        if not self._closing:
            self.failed.emit('无法启动 WebView2 宿主，请检查安装文件。')
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._finished()

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._queued.clear()
        if self.process.state() == QProcess.ProcessState.Running:
            self._write('close', {}, None, 2)
            self.process.closeWriteChannel()
            self._shutdown_timer.start(5000)
        elif self.process.state() == QProcess.ProcessState.Starting:
            self.process.kill()
        else:
            self._finished()

    def _finished(self, *_):
        if self._finished_once:
            return
        self._finished_once = True
        self._ready = False
        self._timeout.stop()
        self._shutdown_timer.stop()
        pending, self._pending = self._pending, {}
        queued, self._queued = self._queued, []
        self._buffer.clear()
        for _, callback in pending.values():
            if callback:
                callback(None, False)
        for _, _, callback, _ in queued:
            if callback:
                callback(None, False)
        if not self._closing:
            self.failed.emit('WebView2 宿主已退出，请重新打开媒体发现窗口。')
        self._closing = True
        self._cleanup()
        self.closed.emit()

    def _cleanup(self):
        if self.persistent:
            return
        # Delete only this host's unique temporary UDF, never user Edge profiles.
        target = self.user_data_folder.resolve()
        root = self._session_root.resolve()
        if target.parent == root and target.name.startswith('session-') and not self.user_data_folder.is_symlink():
            try:
                shutil.rmtree(target)
            except OSError:
                pass  # Edge may release handles shortly after the controller exits.


class WebView2CookieStore(QObject):
    cookieAdded = Signal(QNetworkCookie)

    def __init__(self, profile):
        super().__init__(profile)
        self.profile = profile

    def setCookie(self, cookie, origin=QUrl()):
        copy = QNetworkCookie(cookie)
        domain = copy.domain() or urlsplit(origin.toString()).hostname or ''
        if not domain:
            return
        values = {'name': bytes(copy.name()).decode('utf-8', 'replace'),
                  'value': bytes(copy.value()).decode('utf-8', 'replace'),
                  'domain': domain, 'path': copy.path() or '/',
                  'secure': copy.isSecure(), 'httpOnly': copy.isHttpOnly(),
                  'session': copy.isSessionCookie(),
                  'expires': copy.expirationDate().toSecsSinceEpoch() if not copy.isSessionCookie() else 0,
                  # Netscape import cannot preserve SameSite; use the browser's
                  # normal Lax policy, never silently widen it to None.
                  'sameSite': 'Lax'}
        self.profile.host.request('set_cookie', values,
            lambda result, ok: self.cookieAdded.emit(copy) if ok and result is not False else None)

    def cookies(self, url, callback):
        return self.profile.host.request('get_cookies', {'url': str(url)},
            lambda result, ok: callback(result if ok and isinstance(result, list) else []))


class WebView2Profile(QObject):
    backend = 'webview2'

    def __init__(self, parent=None, user_data_folder=None):
        super().__init__(parent)
        self.host = _PipeHost(self, user_data_folder=user_data_folder)
        self.persistent = self.host.persistent
        self.info = {'engine': 'Microsoft Edge WebView2', 'runtime': '正在初始化', 'chromium': '正在初始化',
                     'persistent': self.persistent}
        self._user_agent = ''
        self._disposed = False
        self._cookie_store = WebView2CookieStore(self)
        self.host.ready.connect(self._host_ready)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._app_quit)

    def _host_ready(self, value):
        runtime = str(value.get('runtimeVersion', '未知'))
        self._user_agent = str(value.get('userAgent') or self._user_agent)
        major = re.search(r'Chrome/(\d+)', self._user_agent)
        self.info.update(runtime=runtime, chromium=(major.group(1) + '（主版本）') if major else '未知')

    def httpUserAgent(self):
        return self._user_agent

    def cookieStore(self):
        return self._cookie_store

    def isOffTheRecord(self):
        return not self.persistent

    def clear_browsing_data(self, callback):
        # The native keeper also checks for active pages and serializes clearing.
        return self.host.request('clear_browsing_data', callback=callback, timeout=30)

    def deleteLater(self):
        if not self._disposed:
            self._disposed = True
            if self.host._finished_once:
                super().deleteLater()
            else:
                self.host.closed.connect(super().deleteLater)
                self.host.shutdown()

    def _app_quit(self):
        self.host.shutdown()
        if self.host.process.state() != QProcess.ProcessState.NotRunning:
            if not self.host.process.waitForFinished(1500):
                self.host.process.kill()
                self.host.process.waitForFinished(500)
        self.host._cleanup()


class _Scripts:
    def __init__(self, page):
        self.page = page

    def insert(self, script):
        self.page._scripts.append(script.sourceCode())
        if self.page.is_ready:
            self.page._request('add_script', {'script': script.sourceCode()})


class WebView2Page(QObject):
    ready = Signal()
    cdpMessage = Signal(dict)
    backendError = Signal(str)
    loadFinished = Signal(bool)
    loadStarted = Signal()
    urlChanged = Signal(QUrl)
    renderProcessTerminated = Signal(object, int)
    fullScreenRequested = Signal(object)

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.page_id = uuid.uuid4().hex
        self.is_ready = False
        self._attached = self._closed = False
        self._url = QUrl()
        self._scripts = []
        self._queue = []
        self._view = None
        self.profile.host.event.connect(self._event)
        self.profile.host.failed.connect(self.backendError)
        # Closing a retired QObject must close its own controller, not a new page.
        host, identifier = self.profile.host, self.page_id
        self.destroyed.connect(lambda: host.request('close_page', {'page': identifier})
                               if isValid(host) and not host._closing else None)

    def attach(self, view):
        if self._attached or self._closed:
            return
        self._view = view
        self._attached = True
        width, height = view.native_size()
        self.profile.host.request('create', {'page': self.page_id,
            'parentHwnd': str(int(view.winId())), 'width': width, 'height': height,
            'visible': view.isVisible()}, self._created, timeout=30)

    def _created(self, result, ok):
        if self._closed:
            return
        if not ok:
            self.backendError.emit('无法创建 WebView2 页面，请检查 Runtime 后重试。')
            return
        if isinstance(result, dict):
            self.profile._host_ready(result)
        self.is_ready = True
        for script in self._scripts:
            self._request('add_script', {'script': script})
        queued, self._queue = self._queue, []
        for op, values, callback in queued:
            self._request(op, values, callback)
        self.ready.emit()

    def _request(self, op, values=None, callback=None):
        if self._closed:
            return False
        if not self.is_ready:
            if len(self._queue) >= MAX_PENDING:
                return False
            self._queue.append((op, values or {}, callback))
            return True
        return self.profile.host.request(op, dict(values or {}, page=self.page_id), callback)

    def send_cdp(self, message, callback):
        return self._request('cdp', {'message': message}, callback)

    def _event(self, value):
        if self._closed or value.get('page') != self.page_id:
            return
        kind = value.get('event')
        if kind == 'cdp' and isinstance(value.get('message'), dict):
            self.cdpMessage.emit(value['message'])
        elif kind == 'load_started':
            self.loadStarted.emit()
        elif kind == 'url_changed':
            self._url = QUrl(str(value.get('url', '')))
            self.urlChanged.emit(self._url)
        elif kind == 'load_finished':
            self.loadFinished.emit(value.get('success') is True)
        elif kind == 'process_failed':
            self.renderProcessTerminated.emit('webview2-process-failed', 1)
        elif kind == 'shortcut' and value.get('key') in ('zoom_in', 'zoom_out', 'zoom_reset', 'escape'):
            if self._view is not None and self._view.page() is self:
                self._view.browserShortcut.emit(value['key'])

    def scripts(self):
        return _Scripts(self)

    def url(self):
        return QUrl(self._url)

    def devToolsId(self):
        return self.page_id

    def load(self, url):
        self._request('navigate', {'url': url.toString() if isinstance(url, QUrl) else str(url)})

    def triggerAction(self, _):
        self._request('stop')

    def runJavaScript(self, script, world=0, callback=None):
        if callable(world):
            callback, world = world, 0
        world = getattr(world, 'value', world)
        self._request('eval', {'script': script, 'world': int(world)},
            (lambda result, ok: callback(result if ok else None)) if callback else None)

    def deleteLater(self):
        if not self._closed:
            self._closed = True
            self._queue.clear()
            self.profile.host.request('close_page', {'page': self.page_id})
        super().deleteLater()


class WebView2View(QWidget):
    loadStarted = Signal()
    loadFinished = Signal(bool)
    urlChanged = Signal(QUrl)
    browserShortcut = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(120)
        self._page = None
        self._zoom = 1.0

    def page(self):
        return self._page

    def setPage(self, page):
        if self._page is not None:
            for source, target in ((self._page.loadStarted, self.loadStarted),
                                   (self._page.loadFinished, self.loadFinished),
                                   (self._page.urlChanged, self.urlChanged)):
                try:
                    source.disconnect(target)
                except (TypeError, RuntimeError):
                    pass
        self._page = page
        page.loadStarted.connect(self.loadStarted)
        page.loadFinished.connect(self.loadFinished)
        page.urlChanged.connect(self.urlChanged)
        # Layout insertion may reparent a native Qt widget. Acquire its HWND only
        # after that insertion, so the host never receives a retired window.
        QTimer.singleShot(0, lambda: page.attach(self) if self._page is page and not page._closed else None)
        page.ready.connect(self._sync)

    def native_size(self):
        ratio = self.devicePixelRatioF()
        return max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio))

    def _sync(self):
        if self._page and self._page.is_ready:
            width, height = self.native_size()
            self._page._request('resize', {'width': width, 'height': height})
            self._page._request('visible', {'value': self.isVisible()})
            self._page._request('zoom', {'factor': self._zoom})

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._page:
            self._page._request('focus')

    def setZoomFactor(self, value):
        self._zoom = max(0.25, min(5.0, float(value)))
        self._sync()

    def zoomFactor(self):
        return self._zoom

    def stop(self):
        if self._page:
            self._page._request('stop')
