"""内嵌浏览器能力诊断和用户主动选择的会话 Cookie 导入。"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import QDateTime, QThread, QTimer, QUrl, Signal, Qt
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                              QPlainTextEdit, QPushButton, QVBoxLayout)

from core.browser_capabilities import CAPABILITY_SCRIPT, format_capabilities, normalize_capabilities
from core.browser_cookies import CookieImportError, export_browser_cookies, load_cookie_file
from core.browser_runtime import browser_engine_info


class CookieImportWorker(QThread):
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, page_url, *, file_path='', tool_path='', browser='', parent=None):
        super().__init__(parent)
        self.page_url, self.file_path = page_url, file_path
        self.tool_path, self.browser = tool_path, browser
        self.cancel_event = threading.Event()

    def run(self):
        try:
            cookies = (load_cookie_file(self.file_path, self.page_url) if self.file_path else
                       export_browser_cookies(self.tool_path, self.browser, self.page_url, self.cancel_event))
            if not self.cancel_event.is_set():
                self.loaded.emit(cookies, self.page_url)
        except CookieImportError as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))
        except Exception:
            if not self.cancel_event.is_set():
                self.failed.emit('Cookie 导入失败，请使用内嵌网页登录或 Netscape Cookie 文件。')


class BrowserEnvironmentDialog(QDialog):
    idle = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle('MediaFlow · 浏览器环境诊断')
        self.resize(800, 680)
        self.setModal(False)
        self._closing = False
        self._worker = None
        self._generation = 0
        self._page_wait = None
        self._report = None
        self._pending_cookies = {}
        self._accepted_count = 0
        self._attempted_count = 0
        self._store = None
        layout = QVBoxLayout(self)
        explanation = QLabel('检测实际编码、MSE、存储与网络监听能力。User-Agent 调整不会补充缺失的解码器。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(500)
        layout.addWidget(self.output, 1)
        self.refresh_button = QPushButton('刷新诊断')
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button)
        cookie_note = QLabel('可选登录状态：仅主动导入当前页面所属域的 Cookie，按当前 Profile 策略和有效期保存。'
                            '系统浏览器导出会短暂使用临时文件，读取后清理；不迁移 localStorage / sessionStorage。'
                            '系统来源保守按 HttpOnly 导入，依赖脚本读取 Cookie 的页面可能仍需内嵌登录。'
                            'Chrome / Edge 加密或锁库可能导致导入失败，可直接在内嵌网页登录。')
        cookie_note.setWordWrap(True)
        layout.addWidget(cookie_note)
        row = QHBoxLayout()
        self.browser = QComboBox()
        for label, value in (('Edge', 'edge'), ('Chrome', 'chrome'), ('Firefox', 'firefox')):
            self.browser.addItem(label, value)
        row.addWidget(self.browser)
        self.import_browser_button = QPushButton('导入所选浏览器 Cookie')
        self.import_browser_button.clicked.connect(self.import_browser)
        row.addWidget(self.import_browser_button)
        self.import_file_button = QPushButton('导入 Netscape Cookie 文件')
        self.import_file_button.clicked.connect(self.import_file)
        row.addWidget(self.import_file_button)
        layout.addLayout(row)
        self.cookie_status = QLabel('默认不读取系统浏览器 Cookie。导入完成后可点击“查找”重新加载页面。')
        self.cookie_status.setWordWrap(True)
        self.cookie_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.cookie_status)
        self._probe_poll = QTimer(self)
        self._probe_poll.setInterval(100)
        self._probe_poll.timeout.connect(self._poll_probe)
        self._probe_timeout = QTimer(self)
        self._probe_timeout.setSingleShot(True)
        self._probe_timeout.setInterval(6000)
        self._probe_timeout.timeout.connect(lambda: self._probe_done(None, self._generation, timed_out=True))
        self._cookie_timeout = QTimer(self)
        self._cookie_timeout.setSingleShot(True)
        self._cookie_timeout.setInterval(6000)
        self._cookie_timeout.timeout.connect(self._cookies_done)
        self._render()

    @property
    def busy(self):
        return self._worker is not None

    def _render(self):
        engine = browser_engine_info(self.owner.profile)
        network = self.owner.observer.network_diagnostics()
        counts = network['counts']
        lines = [f'内核：{engine["engine"]} {engine.get("runtime") or engine["qt_webengine"]}；Chromium {engine["chromium"]}',
                 f'HTTP User-Agent：{self.owner.profile.httpUserAgent()}',
                 '会话：' + engine['session'],
                 f'网络监听：{network["backend"]}；连接 {"是" if network["connected"] else "否"}；'
                 f'Network.enable {"是" if network["network_enabled"] else "否"}',
                 f'请求 {counts.get("Network.requestWillBeSent", 0)}；响应 {counts.get("Network.responseReceived", 0)}；'
                 f'失败 {counts.get("Network.loadingFailed", 0)}',
                 ' / '.join(f'{kind} 响应 {counts.get("Network.responseReceived:" + kind, 0)}'
                            for kind in ('Fetch', 'XHR', 'Media')),
                 f'媒体相关请求：{counts.get("media_requests", 0)}；当前媒体候选：{len(self.owner._candidates)}',
                 f'关联子目标：{network.get("attached_targets", {})}', '']
        lines.append(format_capabilities(self._report) if self._report is not None else
                     '能力尚未检测。先打开页面再刷新，可检查当前页面的播放器与存储状态。')
        if self._report is not None:
            video = self._report['video_elements']
            if video['count'] == 0:
                lines.append('当前文档未观察到 video：需结合页面加载、验证状态或子框架判断，不能直接认定网络监听失败。')
            if counts.get('Network.responseReceived:Media', 0):
                lines.append('已捕获 Media 响应；播放器仍报错时，请核对编码与 video 错误状态。')
        self.output.setPlainText('\n'.join(lines))

    def refresh(self):
        if self._closing:
            return
        self._generation += 1
        generation = self._generation
        self._probe_poll.stop()
        self._clear_page_wait()
        self.refresh_button.setEnabled(False)
        page = self.owner.page
        if getattr(page, 'is_ready', True) is False:
            # Native controller creation is asynchronous. Do not enqueue polls
            # before it exists or spend the probe deadline on a cold runtime.
            self._probe_timeout.start(30000)
            def ready():
                if self._closing or generation != self._generation:
                    return
                self._clear_page_wait()
                if page is self.owner.page:
                    self._start_probe(generation)
            self._page_wait = (page, ready)
            page.ready.connect(ready)
            return
        self._probe_timeout.start(6000)
        self._start_probe(generation)

    def _clear_page_wait(self):
        waiting, self._page_wait = self._page_wait, None
        if waiting:
            try:
                waiting[0].ready.disconnect(waiting[1])
            except (TypeError, RuntimeError):
                pass

    def _start_probe(self, generation):
        if self._closing or generation != self._generation or not self._probe_timeout.isActive():
            return
        self._probe_timeout.start(6000)
        if self.owner.observer.probe_capabilities(lambda value: self._probe_done(value, generation)):
            return
        # 尚未启动网络监听时，在浏览器隔离脚本世界中等待同一异步探针。
        self._probe_key = '__mediaflow_diagnostic_' + uuid.uuid4().hex
        key = json.dumps(self._probe_key)
        script = f'globalThis[{key}]=null;({CAPABILITY_SCRIPT}).then(v=>globalThis[{key}]=v).catch(()=>globalThis[{key}]={{}});'
        self.owner.page.runJavaScript(script, QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self._probe_poll.start()

    def _poll_probe(self):
        if self._closing:
            return
        key = json.dumps(self._probe_key)
        generation = self._generation
        script = f'(()=>{{const v=globalThis[{key}];if(v)delete globalThis[{key}];return v?JSON.stringify(v):"";}})()'
        self.owner.page.runJavaScript(script, QWebEngineScript.ScriptWorldId.ApplicationWorld,
                                     lambda value: self._read_probe(value, generation))

    def _read_probe(self, value, generation):
        if isinstance(value, str) and value and len(value) < 32768:
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return
        if isinstance(value, dict):
            self._probe_done(value, generation)

    def _probe_done(self, value, generation, *, timed_out=False):
        if self._closing or generation != self._generation or (not timed_out and not self._probe_timeout.isActive()):
            return
        self._clear_page_wait()
        self._probe_poll.stop()
        self._probe_timeout.stop()
        self.refresh_button.setEnabled(True)
        self._report = normalize_capabilities(value)
        self._render()

    def _page_url(self):
        url = self.owner.page.url().toString()
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            self.cookie_status.setText('请先打开需要导入登录状态的 http(s) 页面。')
            return ''
        return url

    def import_browser(self):
        if not (url := self._page_url()):
            return
        parent = self.owner.parent()
        tool = getattr(getattr(parent, 'yt_info', None), 'path', '')
        if not tool or not Path(tool).is_file():
            self.cookie_status.setText('未检测到 yt-dlp，请在主窗口完成工具检测后重试，或导入 Cookie 文件。')
            return
        self._start_import(url, tool_path=tool, browser=self.browser.currentData())

    def import_file(self):
        if not (url := self._page_url()):
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择 Netscape Cookie 文件', '', 'Cookie 文件 (*.txt);;全部文件 (*)')
        if path:
            self._start_import(url, file_path=path)

    def _start_import(self, page_url, **options):
        if self._closing or self.busy or self._pending_cookies:
            return
        self._set_import_enabled(False)
        self.cookie_status.setText('正在读取当前页面所属域的 Cookie…')
        self._worker = CookieImportWorker(page_url, parent=self, **options)
        self._worker.loaded.connect(self._apply_cookies)
        self._worker.failed.connect(self.cookie_status.setText)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _set_import_enabled(self, enabled):
        self.import_browser_button.setEnabled(enabled)
        self.import_file_button.setEnabled(enabled)
        self.browser.setEnabled(enabled)

    def _apply_cookies(self, cookies, page_url):
        if self._closing or self.owner._closing:
            return
        self._accepted_count = 0
        self._attempted_count = len(cookies)
        if not cookies:
            self.cookie_status.setText('未找到当前页面所属域的有效 Cookie；可在内嵌网页登录。')
            return
        self._store = self.owner.profile.cookieStore()
        self._store.cookieAdded.connect(self._cookie_added)
        self._pending_cookies = {(item.name, item.domain.lstrip('.'), item.path): item.value for item in cookies}
        self._cookie_timeout.start()
        for item in cookies:
            cookie = QNetworkCookie(item.name.encode('utf-8'), item.value.encode('utf-8'))
            cookie.setPath(item.path)
            cookie.setSecure(item.secure)
            cookie.setHttpOnly(item.http_only)
            if not item.host_only:
                cookie.setDomain('.' + item.domain.lstrip('.'))
            if item.expires:
                cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(item.expires))
            self._store.setCookie(cookie, QUrl(page_url))

    def _cookie_added(self, cookie):
        host = urlsplit(self.owner.page.url().toString()).hostname or ''
        key = (bytes(cookie.name()).decode('utf-8', 'replace'), cookie.domain().lstrip('.') or host, cookie.path())
        value = bytes(cookie.value()).decode('utf-8', 'replace')
        if key in self._pending_cookies and self._pending_cookies[key] == value:
            del self._pending_cookies[key]
            self._accepted_count += 1
            if not self._pending_cookies:
                self._cookies_done()

    def _cookies_done(self):
        self._cookie_timeout.stop()
        if self._store is not None:
            try:
                self._store.cookieAdded.disconnect(self._cookie_added)
            except (RuntimeError, TypeError):
                pass
            self._store = None
        self._pending_cookies.clear()
        self.cookie_status.setText(f'浏览器确认接受 {self._accepted_count}/{self._attempted_count} 项 Cookie。'
                                   '可点击“查找”重新加载页面；导入不保证解码或登录一定成功。')
        self._set_import_enabled(not self.busy)

    def _worker_finished(self):
        worker, self._worker = self._worker, None
        if worker:
            worker.deleteLater()
        if not self._closing:
            self._set_import_enabled(not self._pending_cookies)
        self.idle.emit()

    def shutdown(self):
        self._closing = True
        self._generation += 1
        self._clear_page_wait()
        self._probe_poll.stop()
        self._probe_timeout.stop()
        self._cookies_done()
        if self._worker:
            self._worker.cancel_event.set()
        self.hide()

