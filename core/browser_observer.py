"""本应用 Chromium 页面的异步 CDP 观察；原始凭据仅保存在内存。"""
from __future__ import annotations

import base64
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkAccessManager, QNetworkProxy, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

from core.browser_runtime import browser_runtime_port
from core.discovery_models import NetworkObservation
from core.dom_scanner import DOM_OBSERVER_SCRIPT, DOM_SNAPSHOT_SCRIPT
from core.errors import MediaCheckError
from core.request_context import RequestContext, validate_url
from utils.security import normalize_headers


MAX_RECORDS = 512
MAX_HOPS = 16
MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_STORAGE = 8 * 1024 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BODY_BYTES = 64 * 1024 * 1024
MAX_POST_BYTES = 256 * 1024
MAX_TARGETS = 32
MAX_CONTEXTS = 128
_LICENSE_WORD = re.compile(r'(?:^|[/_.?&=\-])(license|licence|widevine|playready|fairplay)(?:$|[/_.?&=\-])', re.I)
_MANIFEST_MIMES = ('application/dash+xml', 'application/vnd.apple.mpegurl', 'application/x-mpegurl',
                   'audio/mpegurl', 'audio/x-mpegurl')
_GENERIC_MIMES = ('', 'application/octet-stream', 'binary/octet-stream', 'application/binary')
_TARGET_TYPES = ('iframe', 'worker', 'shared_worker', 'service_worker')


def capture_body_limit(value=MAX_BODY_BYTES):
    """设置以字节传递；无效值回到默认值，单条上限始终有界。"""
    if type(value) is not int:
        return MAX_BODY_BYTES
    return max(1024 * 1024, min(value, 128 * 1024 * 1024))


def capture_total_limit(max_body_bytes):
    return min(512 * 1024 * 1024, max(MAX_TOTAL_BODY_BYTES, capture_body_limit(max_body_bytes) * 4))


def _media_mime(value):
    return value.startswith(('video/', 'audio/')) or value in _MANIFEST_MIMES


def _response_mime(mime, headers):
    # Preserve browser-sniffed media for octet-stream, but honor meaningful
    # wire headers, including ExtraInfo arriving after responseReceived.
    mime = str(mime or '').split(';', 1)[0].strip().lower()
    wire = str(headers.get('content-type') or '').split(';', 1)[0].strip().lower()
    return wire if wire and (_media_mime(wire) or mime in _GENERIC_MIMES) else mime


def _headers(values) -> tuple[dict[str, str], bool]:
    """过滤 HTTP/2 伪头；不可重放的字段使上下文标记为不完整。"""
    if not isinstance(values, dict):
        return {}, False
    result, valid, size = {}, True, 0
    for name, value in values.items():
        name, value = str(name), str(value)
        if name.startswith(':'):
            continue
        size += len(name.encode('utf-8')) + len(value.encode('utf-8'))
        if size > MAX_HEADER_BYTES or len(result) >= 200:
            return {}, False
        try:
            result.update(normalize_headers({name: value}))
        except ValueError:
            valid = False
    return result, valid


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _http_url(value) -> str:
    if not isinstance(value, str) or len(value) > 64 * 1024:
        return ''
    try:
        validate_url(value)
    except (ValueError, MediaCheckError):
        # 不回显输入地址或凭据。
        return ''
    return value


def _initiator(value) -> str:
    if not isinstance(value, dict):
        return ''
    parts = [str(value.get('type', ''))[:80]]
    if value.get('url'):
        parts.append(str(value['url'])[:4096])
    stack = value.get('stack', {})
    for frame in stack.get('callFrames', [])[:8] if isinstance(stack, dict) else []:
        if isinstance(frame, dict):
            parts.extend((str(frame.get('url', ''))[:4096], str(frame.get('functionName', ''))[:160]))
    return ' '.join(parts)


def _request_body(request, headers):
    has_body = bool(request.get('hasPostData')) or 'postData' in request or bool(request.get('postDataEntries'))
    if not has_body:
        return None, True, False
    # CDP 的 multipart 文本可能省略上传文件，不能声称完整可重放。
    if 'multipart/form-data' in headers.get('content-type', '').lower():
        return None, False, False
    try:
        entries = request.get('postDataEntries')
        if isinstance(entries, list) and entries and all(isinstance(entry, dict) and 'bytes' in entry for entry in entries):
            if sum(len(str(entry['bytes'])) for entry in entries) > MAX_POST_BYTES * 2:
                return None, False, False
            body = b''.join(base64.b64decode(entry['bytes'], validate=True) for entry in entries)
        elif isinstance(request.get('postData'), str):
            body = request['postData'].encode('utf-8')
        else:
            return None, False, True
    except (ValueError, TypeError):
        return None, False, False
    return (body, True, False) if len(body) <= MAX_POST_BYTES else (None, False, False)


@dataclass
class _HeaderBlock:
    values: dict = field(default_factory=dict, repr=False)
    valid: bool = True


@dataclass
class _Hop:
    request_url: str
    method: str
    initial_headers: dict = field(repr=False)
    header_valid: bool = True
    post_body: bytes | None = field(default=None, repr=False)
    post_complete: bool = True
    post_missing: bool = False
    wire: _HeaderBlock | None = field(default=None, repr=False)
    expected_extra: bool | None = None
    response_url: str = ''
    status: int = 0
    content_type: str = ''
    content_length: int | None = None
    resource_type: str = ''
    frame_id: str = ''
    initiator: str = field(default='', repr=False)
    timestamp: float = 0.0
    from_cache: bool = False
    response_seen: bool = False
    finished: bool = False
    body: bytes | None = field(default=None, repr=False)
    body_complete: bool = False
    body_requested: bool = False
    post_requested: bool = False
    encoded_length: int | None = None
    response_headers: dict = field(default_factory=dict, repr=False)
    timing: dict = field(default_factory=dict)

    @property
    def url(self):
        return self.response_url or self.request_url


@dataclass
class _Record:
    request_id: str
    session: str = ''
    hops: list[_Hop] = field(default_factory=list, repr=False)
    extras: list[_HeaderBlock] = field(default_factory=list, repr=False)
    response_extras: list[dict] = field(default_factory=list, repr=False)


class _NetworkStore:
    """无 Qt 传输的事件累积器，重定向各 hop 的 ExtraInfo 按顺序重新对齐。"""

    def __init__(self, max_body_bytes=MAX_BODY_BYTES):
        self.records: OrderedDict[tuple[str, str], _Record] = OrderedDict()
        self.total_body_read = 0
        self.max_body_bytes = capture_body_limit(max_body_bytes)
        self.max_total_body_bytes = capture_total_limit(self.max_body_bytes)

    def clear(self):
        self.records.clear()
        self.total_body_read = 0

    def get(self, session, request_id):
        return self.records.get((session, request_id))

    def _record(self, session, request_id):
        key = (session, request_id)
        record = self.records.get(key)
        if record is None:
            record = _Record(request_id, session)
            self.records[key] = record
        self.records.move_to_end(key)
        while len(self.records) > MAX_RECORDS:
            self.records.popitem(last=False)
        return record

    def _reconcile(self, record):
        cursor = 0
        response_cursor = 0
        for hop in record.hops:
            hop.wire = None
            if hop.expected_extra is False:
                continue
            if cursor < len(record.extras):
                hop.wire = record.extras[cursor]
                cursor += 1
            if response_cursor < len(record.response_extras):
                extra = record.response_extras[response_cursor]
                response_cursor += 1
                hop.response_headers.update(extra.get('headers', {}))
                hop.content_type = _response_mime(hop.content_type, extra.get('headers', {}))
                if extra.get('content_length') is not None:
                    hop.content_length = extra['content_length']
                if not hop.response_seen:
                    hop.status = _integer(extra.get('statusCode'), hop.status)
        self._trim_headers()

    def _trim_headers(self):
        def size(record):
            blocks = [hop.initial_headers for hop in record.hops] + [extra.values for extra in record.extras]
            return sum(len(str(name)) + len(str(value)) for block in blocks for name, value in block.items())
        sizes = [(key, size(record)) for key, record in self.records.items()]
        total = sum(value for _, value in sizes)
        for key, value in sizes:
            if total <= MAX_HEADER_STORAGE:
                break
            self.records.pop(key, None)
            total -= value

    @staticmethod
    def _response(hop, response, has_extra=None):
        headers, _ = _headers(response.get('headers', {}))
        hop.response_url = _http_url(response.get('url', '')) or hop.response_url
        hop.status = _integer(response.get('status'), hop.status)
        hop.content_type = _response_mime(response.get('mimeType'), headers)
        hop.content_length = _integer(headers.get('content-length'))
        hop.response_headers = {name: value for name, value in headers.items() if name in
                                ('content-type', 'content-length', 'content-range', 'accept-ranges', 'etag', 'last-modified', 'location')}
        timing = response.get('timing')
        hop.timing = {name: value for name, value in (timing or {}).items()
                      if isinstance(value, (int, float))} if isinstance(timing, dict) else {}
        hop.from_cache = bool(response.get('fromDiskCache') or response.get('fromPrefetchCache') or response.get('fromServiceWorker'))
        hop.response_seen = True
        if isinstance(has_extra, bool):
            hop.expected_extra = has_extra

    def consume(self, method, params, session='') -> list[NetworkObservation]:
        request_id = str(params.get('requestId', ''))[:256]
        if not request_id:
            return []
        if method == 'Network.requestWillBeSent':
            request = params.get('request', {})
            if not isinstance(request, dict):
                return []
            url = _http_url(request.get('url'))
            if not url:
                return []
            record = self._record(session, request_id)
            if len(record.hops) >= MAX_HOPS:
                return []
            if params.get('redirectResponse') and record.hops:
                self._response(record.hops[-1], params['redirectResponse'], params.get('redirectHasExtraInfo'))
            headers, valid = _headers(request.get('headers', {}))
            body, complete, missing = _request_body(request, headers)
            try:
                timestamp = float(params.get('wallTime') or time.time())
            except (TypeError, ValueError, OverflowError):
                timestamp = time.time()
            record.hops.append(_Hop(url, str(request.get('method', 'GET')).upper(), headers,
                                     header_valid=valid, post_body=body, post_complete=complete,
                                     post_missing=missing, resource_type=str(params.get('type', '')),
                                     frame_id=str(params.get('frameId', ''))[:256],
                                     initiator=_initiator(params.get('initiator', {})), timestamp=timestamp))
        elif method == 'Network.requestWillBeSentExtraInfo':
            record = self._record(session, request_id)
            if len(record.extras) >= MAX_HOPS:
                return []
            headers, valid = _headers(params.get('headers', {}))
            record.extras.append(_HeaderBlock(headers, valid))
        else:
            record = self.get(session, request_id)
            if record is None:
                if method != 'Network.responseReceivedExtraInfo':
                    return []
                record = self._record(session, request_id)
            if method == 'Network.responseReceivedExtraInfo':
                if len(record.response_extras) >= MAX_HOPS:
                    return []
                headers, _ = _headers(params.get('headers', {}))
                record.response_extras.append({'statusCode': params.get('statusCode'),
                                              'headers': {name: value for name, value in headers.items() if name in
                                                ('content-type', 'content-length', 'content-range', 'accept-ranges', 'etag', 'last-modified', 'location')},
                                              'content_type': headers.get('content-type', '').split(';', 1)[0].lower(),
                                              'content_length': _integer(headers.get('content-length'))})
            elif not record.hops:
                return []
            elif method == 'Network.responseReceived':
                self._response(record.hops[-1], params.get('response', {}), params.get('hasExtraInfo'))
                record.hops[-1].resource_type = str(params.get('type') or record.hops[-1].resource_type)
            elif method == 'Network.requestServedFromCache':
                record.hops[-1].from_cache = True
            elif method == 'Network.loadingFinished':
                record.hops[-1].finished = True
                record.hops[-1].encoded_length = _integer(params.get('encodedDataLength'))
            elif method == 'Network.loadingFailed':
                record.hops[-1].finished = True
            else:
                return []
        self._reconcile(record)
        observation = self.snapshot(record)
        return [observation] if observation else []

    def snapshot(self, record) -> NetworkObservation | None:
        if not record.hops:
            return None
        hop = record.hops[-1]
        headers = dict(hop.wire.values if hop.wire is not None else hop.initial_headers)
        wire_complete = (hop.wire is not None and hop.wire.valid and hop.expected_extra is not False
                         and (hop.expected_extra is True or hop.response_seen))
        ordinary_complete = hop.expected_extra is False and hop.header_valid and not hop.from_cache
        redirects = [{'from': previous.url, 'to': following.request_url, 'status': previous.status,
                      'location': previous.response_headers.get('location', '')}
                     for previous, following in zip(record.hops, record.hops[1:])]
        extra = {'redirect_chain': tuple(deepcopy(redirects))} if 'redirect_chain' in RequestContext.__dataclass_fields__ else {}
        try:
            context = RequestContext(hop.url, hop.method, headers, hop.post_body,
                                     original_url=record.hops[0].request_url, final_url=hop.url,
                                     inherit_range=bool(re.fullmatch(r'bytes\s*=\s*0\s*-\s*', headers.get('range', ''), re.I)),
                                     **extra)
        except (ValueError, MediaCheckError):
            return None
        return NetworkObservation(request_id=f'{record.session}:{record.request_id}' if record.session else record.request_id,
                                  context=context, status=hop.status, content_type=hop.content_type,
                                  body=hop.body, frame_id=hop.frame_id, resource_type=hop.resource_type,
                                  initiator=hop.initiator, timestamp=hop.timestamp, redirects=redirects,
                                  content_length=hop.content_length, from_cache=hop.from_cache,
                                  body_complete=hop.body_complete,
                                  context_complete=bool((wire_complete or ordinary_complete) and hop.post_complete),
                                  response_headers=deepcopy(hop.response_headers), timing=deepcopy(hop.timing))

    def set_post_body(self, record, hop, result):
        if record is not self.get(record.session, record.request_id) or not record.hops or record.hops[-1] is not hop:
            return None
        value = result.get('postData')
        if isinstance(value, str):
            body = value.encode('utf-8')
            if len(body) <= MAX_POST_BYTES:
                hop.post_body, hop.post_complete, hop.post_missing = body, True, False
        return self.snapshot(record)

    def set_body(self, record, hop, result):
        if record is not self.get(record.session, record.request_id) or not record.hops or record.hops[-1] is not hop:
            return None
        value = result.get('body')
        if not isinstance(value, str) or len(value) > self.max_body_bytes * 2:
            return None
        try:
            body = base64.b64decode(value, validate=True) if result.get('base64Encoded') else value.encode('utf-8')
        except (ValueError, TypeError):
            return None
        if len(body) > self.max_body_bytes or self.total_body_read + len(body) > self.max_total_body_bytes:
            return None
        self.total_body_read += len(body)
        hop.body, hop.body_complete = body, True
        return self.snapshot(record)

    def wants_body(self, record) -> bool:
        if not record.hops:
            return False
        hop = record.hops[-1]
        if not hop.finished or hop.body_requested or not (200 <= hop.status < 300 or hop.status == 403):
            return False
        if _LICENSE_WORD.search(hop.url) or _LICENSE_WORD.search(hop.initiator):
            return False
        if hop.content_length is not None and hop.content_length > self.max_body_bytes:
            return False
        if hop.encoded_length is not None and hop.encoded_length > self.max_body_bytes:
            return False
        mime = hop.content_type
        manifest_mime = mime in ('application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl',
                                'audio/x-mpegurl', 'application/dash+xml')
        path = urlsplit(hop.url).path.lower()
        manifest_hint = path.endswith(('.m3u8', '.mpd'))
        if manifest_mime or manifest_hint:
            return True
        # 服务端渲染页面可直接内嵌完整 DASH JSON，播放器因此不发 playurl/MPD。
        # 正文仍受同一条/总量上限约束，不采集任意脚本或执行其中的代码。
        if hop.resource_type == 'Document' and mime in ('text/html', 'application/xhtml+xml'):
            return True
        if mime.startswith(('video/', 'audio/')) or hop.resource_type == 'Media':
            return hop.content_length is not None or hop.encoded_length is not None
        config_mime = mime in ('application/json', 'text/json') or mime.endswith('+json')
        if hop.method != 'GET' and not config_mime:
            return False  # 未明确识别的 POST 回复可能是许可证，不采集其正文。
        return hop.resource_type in ('XHR', 'Fetch') and (config_mime or mime in ('', 'application/octet-stream',
                'binary/octet-stream', 'text/plain', 'application/xml', 'text/xml', 'image/png', 'image/jpeg') or mime.endswith('+xml'))


class BrowserNetworkObserver(QObject):
    ready = Signal()
    document_ready = Signal()
    observation = Signal(object)
    dom = Signal(dict)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, page, parent=None, poll_interval=700, max_body_bytes=MAX_BODY_BYTES):
        super().__init__(parent)
        self.page = page
        self.max_body_bytes = capture_body_limit(max_body_bytes)
        self.max_total_body_bytes = capture_total_limit(self.max_body_bytes)
        self._manager = QNetworkAccessManager(self)
        self._manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self._socket = None
        self._reply = None
        self._lookup_data = bytearray()
        self._active = False
        self._enabled = False
        self._deadline = 0.0
        self._port = None
        self._page_id = ''
        self._store = _NetworkStore(self.max_body_bytes)
        self._commands = {}
        self._command_id = 0
        self._epoch = 0
        self._sessions = set()
        self._session_types = {}
        self._media_requests = OrderedDict()
        self._contexts = OrderedDict()
        self._ignored = deque(maxlen=2048)
        self._round_started = time.time()
        self._network_counts = {}
        self._body_inflight = 0
        self._reserved_body_bytes = 0
        self._pending_image_reads = {}
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(200)
        self._retry_timer.timeout.connect(self._lookup_page)
        self._lookup_timeout = QTimer(self)
        self._lookup_timeout.setSingleShot(True)
        self._lookup_timeout.timeout.connect(self._abort_lookup)
        self._connect_timeout = QTimer(self)
        self._connect_timeout.setSingleShot(True)
        self._connect_timeout.timeout.connect(self._connection_deadline)
        self._dom_timer = QTimer(self)
        self._dom_timer.setInterval(max(250, int(poll_interval)))
        self._dom_timer.timeout.connect(self.scan_dom)
        self._command_timer = QTimer(self)
        self._command_timer.setInterval(1000)
        self._command_timer.timeout.connect(self._expire_commands)

    def start(self):
        if self._active:
            return
        self._port = browser_runtime_port()
        if self._port is None:
            self.error.emit('浏览器监听尚未初始化，请重新启动应用。')
            return
        try:
            self._page_id = str(self.page.devToolsId())
        except RuntimeError:
            self.error.emit('内嵌浏览器页面已关闭。')
            return
        if not self._page_id:
            self.error.emit('尚未获得内嵌浏览器页面，请重新查找。')
            return
        self.reset()
        self._active = True
        self._enabled = False
        self._deadline = time.monotonic() + 8
        self._connect_timeout.start(8000)
        self.status.emit('正在连接内嵌浏览器…')
        self._retry_timer.start()
        self._command_timer.start()
        self._lookup_page()

    def stop(self):
        was_active = self._active
        if was_active:
            # Stopping discovery must leave the webpage usable even if a new
            # related target was awaiting listener initialization.
            for session in tuple(self._sessions):
                self._send('Runtime.runIfWaitingForDebugger', session=session)
                self._send('Target.setAutoAttach', {'autoAttach': False, 'waitForDebuggerOnStart': False,
                                                   'flatten': True}, session)
            self._send('Target.setAutoAttach', {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True})
        self._active = False
        self._enabled = False
        self._epoch += 1
        self._retry_timer.stop()
        self._lookup_timeout.stop()
        self._connect_timeout.stop()
        self._dom_timer.stop()
        self._command_timer.stop()
        self._abort_lookup()
        self._commands.clear()
        self._pending_image_reads.clear()
        self._store.clear()
        self._contexts.clear()
        self._sessions.clear()
        self._session_types.clear()
        self._media_requests.clear()
        self._body_inflight = self._reserved_body_bytes = 0
        socket, self._socket = self._socket, None
        if socket is not None:
            socket.blockSignals(True)
            socket.abort()
            socket.deleteLater()
        if was_active:
            self.stopped.emit()

    def history_observations(self):
        return [deepcopy(value) for record in self._store.records.values()
                if (value := self._store.snapshot(record)) is not None]

    def network_diagnostics(self):
        """只返回计数和监听状态，不导出页面请求地址或凭据。"""
        return {'backend': 'Chrome DevTools Protocol', 'connected': self._active,
                'network_enabled': self._enabled, 'counts': dict(self._network_counts),
                'max_body_bytes': self.max_body_bytes, 'max_total_body_bytes': self.max_total_body_bytes,
                'attached_targets': dict((kind, sum(value == kind for value in self._session_types.values()))
                                         for kind in _TARGET_TYPES)}

    def probe_capabilities(self, callback):
        if not self._active or not self._enabled:
            return False
        from core.browser_capabilities import CAPABILITY_SCRIPT, normalize_capabilities
        def received(result, success):
            remote = result.get('result', {})
            value = remote.get('value') if isinstance(remote, dict) else None
            ok = success and not result.get('exceptionDetails') and isinstance(value, dict)
            callback(normalize_capabilities(value) if ok else None)
        return bool(self._send('Runtime.evaluate', {'expression': CAPABILITY_SCRIPT,
            'returnByValue': True, 'awaitPromise': True, 'silent': True,
            'objectGroup': 'mediaflow-browser-diagnostics'}, callback=received))

    def fetch_url(self, url, callback=None, max_bytes=None, frame_id='', headers=None):
        """在当前浏览器会话发出有界正常 Fetch；wire context 由 Network 事件采集。"""
        validate_url(url)
        if not self._active or not self._enabled:
            return False
        max_bytes = self.max_body_bytes if max_bytes is None else max(1, min(int(max_bytes), self.max_body_bytes))
        # Location 对应 PNG 可能已由页面作为 Image 正常加载。读取这条实际响应，
        # 不发起会改变 Origin/触发 CORS 的替代请求，也不从图片文件名猜媒体。
        for record in reversed(list(self._store.records.values())):
            if not record.hops:
                continue
            hop = record.hops[-1]
            if (hop.url == url and hop.resource_type == 'Image' and (hop.status == 0 or 200 <= hop.status < 300)
                    and not _LICENSE_WORD.search(hop.url) and not _LICENSE_WORD.search(hop.initiator)
                    and (hop.content_length is None or hop.content_length <= max_bytes)
                    and (hop.encoded_length is None or hop.encoded_length <= max_bytes)):
                if hop.body_complete:
                    observation = self._store.snapshot(record)
                    if observation:
                        self.observation.emit(observation)
                    if callback:
                        callback({'ok': observation is not None, 'status': hop.status, 'source': 'observed_response'}, True)
                    return True
                if not hop.finished:
                    # 已授权 Location 的正常 Image 仍在加载；等它完成再读实际正文，
                    # 避免改成 Fetch 后因 CORS 失败，又被 UI tried 状态永久挡住。
                    if len(self._pending_image_reads) >= 16:
                        return False
                    self._pending_image_reads[(record.session, record.request_id)] = {
                        'url': url, 'callback': callback, 'deadline': time.monotonic() + 8, 'max_bytes': max_bytes}
                    return True
                if not hop.body_requested and self._request_body(record, callback):
                    return True
        # 浏览器自行提供 Cookie/UA/Referer/Origin；这里只重放该 URL 已观察到的
        # 非禁用请求字段，不向另一个 CDN 手动复制 API 的 Cookie 或 Authorization。
        captured, valid = _headers(headers or {})
        if not valid:
            return False
        replay = {name: value for name, value in captured.items()
                  if name in ('authorization', 'range', 'accept') or name.startswith('x-')}
        try:
            page_url = self.page.url().toString()
        except (AttributeError, RuntimeError):
            page_url = captured.get('referer') or captured.get('origin') or ''
        target, source = urlsplit(url), urlsplit(page_url)
        # 同源 API 使用原 Profile 的正常 Cookie；跨源重定向遵循浏览器默认。
        # 只有该目标已实际观察到跨源 Cookie 时才显式使用 include。
        credentials = 'include' if captured.get('cookie') and (target.scheme, target.netloc) != (source.scheme, source.netloc) else 'same-origin'
        expression = r'''(async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            try {
                const response = await fetch(TARGET, {credentials:CREDENTIALS, redirect:'follow', headers:HEADERS, signal:controller.signal});
                let size = 0;
                if (response.body) {
                    const reader = response.body.getReader();
                    while (true) {
                        const chunk = await reader.read();
                        if (chunk.done) break;
                        size += chunk.value.byteLength;
                        if (size > LIMIT) { await reader.cancel(); return {ok:false, reason:'size_limit', status:response.status}; }
                    }
                }
                return {ok:response.ok, status:response.status};
            } catch (error) { return {ok:false, reason:error.name}; }
            finally { clearTimeout(timer); }
        })()'''.replace('TARGET', json.dumps(url)).replace('LIMIT', str(max_bytes)).replace('HEADERS', json.dumps(replay)).replace('CREDENTIALS', json.dumps(credentials))
        def received(result, success):
            remote = result.get('result', {})
            value = remote.get('value', {}) if isinstance(remote, dict) else {}
            if callback:
                callback(value if isinstance(value, dict) else {}, success)
        params = {'expression': expression, 'awaitPromise': True, 'returnByValue': True,
                  'silent': True, 'objectGroup': 'stream-media-resolver'}
        session = ''
        if frame_id:
            found = next(((key, frame) for key, frame in self._contexts.items() if frame == frame_id), None)
            if found:
                (session, context_id), _ = found
                params['contextId'] = context_id
            else:
                return False
        return self._send('Runtime.evaluate', params, session, received)

    def set_page(self, page):
        """换到本轮全新的页面 target；旧 target 的 requestId 不能污染新页面。"""
        self.stop()
        self.page = page
        self._page_id = ''
        self._ignored.clear()

    def reset(self):
        self._network_counts.clear()
        self._media_requests.clear()
        self._ignored.extend(self._store.records)
        self._store.clear()
        self._commands.clear()
        self._pending_image_reads.clear()
        self._epoch += 1
        self._body_inflight = self._reserved_body_bytes = 0
        self._round_started = time.time()
        if self._active and not self._enabled:
            self._abort_lookup()
            if self._socket is not None and self._socket.state() == QAbstractSocket.SocketState.ConnectedState:
                self._enable_session('')
            elif self._socket is None:
                self._lookup_page()

    def _abort_lookup(self):
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _connection_deadline(self):
        # 覆盖 HTTP 页面查找、WS 握手以及 Network.enable，而不只限制重试阶段。
        if self._active and not self._enabled:
            self.error.emit('连接内嵌浏览器监听超时，请重新查找。')
            self.stop()

    def _lookup_page(self):
        if not self._active or self._reply is not None or self._socket is not None:
            return
        if time.monotonic() >= self._deadline:
            self.error.emit('无法连接内嵌浏览器的网络监听，请重新查找。')
            self.stop()
            return
        self._lookup_data = bytearray()
        request = QNetworkRequest(QUrl(f'http://127.0.0.1:{self._port}/json/list'))
        request.setTransferTimeout(1200)
        reply = self._manager.get(request)
        self._reply = reply
        epoch = self._epoch
        reply.readyRead.connect(lambda: self._read_lookup(reply, epoch))
        reply.finished.connect(lambda: self._finish_lookup(reply, epoch))
        self._lookup_timeout.start(1400)

    def _read_lookup(self, reply, epoch):
        if reply is not self._reply or epoch != self._epoch:
            return
        self._lookup_data.extend(bytes(reply.readAll()))
        if len(self._lookup_data) > 256 * 1024:
            self._abort_lookup()

    def _finish_lookup(self, reply, epoch):
        if reply is not self._reply:
            return
        self._read_lookup(reply, epoch)
        if reply is not self._reply:
            return  # read 超限已 abort，不能继续解析或再次使用此 reply。
        self._reply = None
        self._lookup_timeout.stop()
        reply.deleteLater()
        if not self._active or epoch != self._epoch:
            return
        try:
            entries = json.loads(bytes(self._lookup_data))
        except (ValueError, UnicodeError):
            return
        if not isinstance(entries, list):
            return
        for entry in entries[:256]:
            if not isinstance(entry, dict) or str(entry.get('id')) != self._page_id or entry.get('type') != 'page':
                continue
            address = str(entry.get('webSocketDebuggerUrl', ''))
            try:
                parsed = urlsplit(address)
                port = parsed.port
            except ValueError:
                continue
            if parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or port != self._port:
                continue
            if parsed.path != f'/devtools/page/{self._page_id}' or parsed.query or parsed.fragment:
                continue
            self._retry_timer.stop()
            socket = QWebSocket(f'http://127.0.0.1:{self._port}', parent=self)
            socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
            socket.setMaxAllowedIncomingMessageSize(6 * 1024 * 1024)
            socket.setMaxAllowedIncomingFrameSize(6 * 1024 * 1024)
            self._socket = socket
            socket.connected.connect(lambda socket=socket: self._socket_callback(socket, self._connected))
            socket.textMessageReceived.connect(lambda text, socket=socket: self._socket_callback(socket, self._receive_text, text))
            socket.errorOccurred.connect(lambda error, socket=socket: self._socket_callback(socket, self._socket_error, error))
            socket.disconnected.connect(lambda socket=socket: self._socket_callback(socket, self._disconnected))
            socket.open(QUrl(address))
            return

    def _socket_callback(self, socket, callback, *args):
        # abort 前已排队的 Qt 消息在新一轮 start 后到达，也只能属于原 socket。
        if self._active and socket is self._socket:
            callback(*args)

    def _socket_error(self, _error):
        if not self._active:
            return
        self.error.emit('内嵌浏览器网络监听连接失败，请重新查找。')
        self.stop()

    def _disconnected(self):
        if not self._active:
            return
        self.error.emit('内嵌浏览器网络监听已断开，请重新查找。')
        self.stop()

    def _connected(self):
        if not self._active:
            return
        self._enable_session('')

    def _send(self, method, params=None, session='', callback=None):
        if not self._active or self._socket is None or self._socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return 0
        if len(self._commands) >= 256:
            return 0
        self._command_id += 1
        command_id = self._command_id
        message = {'id': command_id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        self._commands[command_id] = (self._epoch, time.monotonic(), callback)
        self._socket.sendTextMessage(json.dumps(message, ensure_ascii=True, separators=(',', ':')))
        return command_id

    def _expire_commands(self):
        now = time.monotonic()
        for command_id, (epoch, timestamp, callback) in list(self._commands.items()):
            if now - timestamp < 5:
                continue
            self._commands.pop(command_id, None)
            if epoch == self._epoch and callback:
                callback({}, False)
        for key, pending in list(self._pending_image_reads.items()):
            if now >= pending['deadline']:
                self._pending_image_reads.pop(key, None)
                if pending['callback']:
                    pending['callback']({'ok': False, 'reason': 'body_timeout'}, False)
        if self._pending_image_reads:
            self._drain_bodies()

    def _enable_session(self, session, target_type='iframe'):
        def enabled(_result, success):
            if not self._active:
                return
            if session:
                # Related workers/frames pause only until their listener is
                # enabled, so their first Fetch is not lost. Always resume.
                self._send('Runtime.runIfWaitingForDebugger', session=session)
                if not success:
                    self._network_counts['target_enable_failed'] = self._network_counts.get('target_enable_failed', 0) + 1
                return
            if not success:
                self.error.emit('内嵌浏览器不支持网络监听，请重新启动应用。')
                self.stop()
                return
            self._enabled = True
            self._connect_timeout.stop()
            self._dom_timer.start()
            self.status.emit('正在监听页面媒体请求，可在页面中播放或拖动视频。')
            self.ready.emit()
            self.scan_dom()
        # Discovery needs actual wire headers for safe replay, even when a
        # persistent profile already cached a resource. Site storage is retained.
        self._send('Network.setCacheDisabled', {'cacheDisabled': True}, session)
        self._send('Network.enable', {'maxTotalBufferSize': self.max_total_body_bytes,
                                    'maxResourceBufferSize': self.max_body_bytes,
                                    'maxPostDataSize': MAX_POST_BYTES}, session, enabled)
        self._send('Runtime.enable', session=session)
        if target_type == 'iframe':
            self._send('Page.enable', session=session)
            self._send('Page.addScriptToEvaluateOnNewDocument', {'source': DOM_OBSERVER_SCRIPT}, session)
        self._send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True,
            'filter': [dict(type=kind, exclude=False) for kind in _TARGET_TYPES] + [{'exclude': True}]}, session)

    def _receive_text(self, text):
        if not self._active:
            return
        try:
            message = json.loads(text)
        except (ValueError, UnicodeError):
            return
        if not isinstance(message, dict):
            return
        if 'id' in message:
            command = self._commands.pop(message.get('id'), None)
            if command is not None and command[0] == self._epoch and command[2]:
                result = message.get('result', {})
                command[2](result if isinstance(result, dict) else {}, 'error' not in message)
            return
        session = str(message.get('sessionId', ''))
        if session and session not in self._sessions:
            return
        method, params = message.get('method', ''), message.get('params', {})
        if not isinstance(params, dict):
            return
        if method == 'Target.attachedToTarget':
            self._attach_target(params)
        elif method == 'Target.detachedFromTarget':
            child = str(params.get('sessionId', ''))
            self._sessions.discard(child)
            self._session_types.pop(child, None)
            for key in list(self._contexts):
                if key[0] == child:
                    self._contexts.pop(key, None)
        elif method == 'Runtime.executionContextCreated':
            context = params.get('context', {})
            aux = context.get('auxData', {}) if isinstance(context, dict) else {}
            if (self._session_types.get(session, 'iframe') == 'iframe' and isinstance(aux, dict)
                    and (aux.get('isDefault') is True or aux.get('type') == 'default')):
                context_id = _integer(context.get('id'))
                if context_id is not None:
                    self._contexts[(session, context_id)] = str(aux.get('frameId', ''))[:256]
                    while len(self._contexts) > MAX_CONTEXTS:
                        self._contexts.popitem(last=False)
        elif method == 'Runtime.executionContextDestroyed':
            self._contexts.pop((session, _integer(params.get('executionContextId'))), None)
        elif method == 'Runtime.executionContextsCleared':
            for key in list(self._contexts):
                if key[0] == session:
                    self._contexts.pop(key, None)
        elif method.startswith('Network.'):
            self._network_event(method, params, session)
        elif method == 'Page.domContentEventFired' and not session:
            self.document_ready.emit()
            self.scan_dom()

    def _attach_target(self, params):
        session = str(params.get('sessionId', ''))[:256]
        target = params.get('targetInfo', {})
        if not session or not isinstance(target, dict):
            return
        kind = target.get('type')
        if kind not in _TARGET_TYPES or len(self._sessions) >= MAX_TARGETS:
            self._send('Runtime.runIfWaitingForDebugger', session=session)
            self._send('Target.detachFromTarget', {'sessionId': session})
            return
        self._sessions.add(session)
        self._session_types[session] = kind
        self._enable_session(session, kind)

    def _network_event(self, method, params, session):
        request_id = str(params.get('requestId', ''))[:256]
        key = (session, request_id)
        if key in self._ignored:
            return
        if method == 'Network.requestWillBeSent':
            try:
                if float(params.get('wallTime', self._round_started)) < self._round_started - 0.01:
                    self._ignored.append(key)
                    return
            except (TypeError, ValueError, OverflowError):
                return
        if method in ('Network.requestWillBeSent', 'Network.responseReceived', 'Network.loadingFailed'):
            self._network_counts[method] = self._network_counts.get(method, 0) + 1
            resource_type = params.get('type')
            if resource_type in ('Fetch', 'XHR', 'Media'):
                count_key = method + ':' + resource_type
                self._network_counts[count_key] = self._network_counts.get(count_key, 0) + 1
        for observation in self._store.consume(method, params, session):
            self._emit_observation(observation)
        record = self._store.get(session, request_id)
        if record is not None and record.hops:
            hop = record.hops[-1]
            if hop.post_missing and not hop.post_requested:
                hop.post_requested = True
                def post_received(result, success):
                    if success:
                        observation = self._store.set_post_body(record, hop, result)
                        if observation is not None:
                            self._emit_observation(observation)
                if not self._send('Network.getRequestPostData', {'requestId': request_id}, session, post_received):
                    hop.post_requested = False
        self._drain_bodies()

    def _emit_observation(self, observation):
        if (_media_mime(observation.content_type) or observation.resource_type == 'Media'
                or urlsplit(observation.context.url).path.lower().endswith(('.mp4', '.m3u8', '.mpd', '.m4s', '.webm', '.m4a'))):
            media_key = (observation.request_id, observation.context.url)
            if media_key not in self._media_requests:
                self._network_counts['media_requests'] = self._network_counts.get('media_requests', 0) + 1
            self._media_requests[media_key] = True
            self._media_requests.move_to_end(media_key)
            while len(self._media_requests) > 4096:
                self._media_requests.popitem(last=False)
        self.observation.emit(observation)

    def _drain_bodies(self):
        if not self._active or self._body_inflight >= 4:
            return
        for key, pending in list(self._pending_image_reads.items()):
            record = self._store.get(*key)
            if not record or not record.hops or not record.hops[-1].finished:
                continue
            hop = record.hops[-1]
            if (hop.url != pending['url'] or not 200 <= hop.status < 300
                    or any(size is not None and size > pending['max_bytes'] for size in (hop.content_length, hop.encoded_length))):
                self._pending_image_reads.pop(key, None)
                if pending['callback']:
                    pending['callback']({'ok': False, 'status': hop.status, 'reason': 'body_unavailable'}, False)
                continue
            if not hop.body_requested and self._request_body(record, pending['callback']):
                self._pending_image_reads.pop(key, None)
        for record in list(self._store.records.values()):
            if self._body_inflight >= 4 or self._store.total_body_read + self._reserved_body_bytes + self.max_body_bytes > self.max_total_body_bytes:
                break
            if not self._store.wants_body(record):
                continue
            if not self._request_body(record):
                break

    def _request_body(self, record, callback=None):
        if (not self._active or self._body_inflight >= 4
                or self._store.total_body_read + self._reserved_body_bytes + self.max_body_bytes > self.max_total_body_bytes):
            return False
        hop = record.hops[-1]
        hop.body_requested = True
        self._body_inflight += 1
        self._reserved_body_bytes += self.max_body_bytes
        count_key = 'response_body_requested:' + hop.resource_type
        self._network_counts[count_key] = self._network_counts.get(count_key, 0) + 1
        def body_received(result, success):
            self._body_inflight = max(0, self._body_inflight - 1)
            self._reserved_body_bytes = max(0, self._reserved_body_bytes - self.max_body_bytes)
            observation = self._store.set_body(record, hop, result) if success else None
            outcome = 'accepted' if observation is not None else 'rejected' if success else 'failed'
            count_key = 'response_body_' + outcome + ':' + hop.resource_type
            self._network_counts[count_key] = self._network_counts.get(count_key, 0) + 1
            if observation is not None:
                self._emit_observation(observation)
            if callback:
                callback({'ok': observation is not None, 'status': hop.status,
                          'source': 'observed_response', 'reason': '' if observation else 'body_unavailable'}, success)
            self._drain_bodies()
        if self._send('Network.getResponseBody', {'requestId': record.request_id}, record.session, body_received):
            return True
        self._body_inflight -= 1
        self._reserved_body_bytes -= self.max_body_bytes
        hop.body_requested = False
        return False

    def scan_dom(self):
        if not self._active or not self._enabled:
            return
        # A frameless early snapshot would be retained as a second document by
        # the existing analyser. Wait for Runtime's real default frame context.
        contexts = [(key, frame) for key, frame in self._contexts.items() if frame][:32]
        pending = {getattr(command[2], '_dom_context', None) for command in self._commands.values() if command[2]}
        for (session, context_id), frame_id in contexts:
            key = (session, context_id)
            if key in pending:
                continue
            params = {'expression': DOM_SNAPSHOT_SCRIPT, 'returnByValue': True, 'awaitPromise': False,
                      'silent': True, 'objectGroup': 'stream-media-discovery'}
            if context_id is not None:
                params['contextId'] = context_id
            def received(result, success, frame_id=frame_id, key=key):
                if (not success or result.get('exceptionDetails')
                        or self._contexts.get(key) != frame_id):
                    return
                remote = result.get('result', {})
                value = remote.get('value') if isinstance(remote, dict) else None
                if not isinstance(value, dict) or not isinstance(value.get('videos'), list):
                    return
                value['frame_id'] = frame_id
                value['videos'] = value['videos'][:96]
                for video in value['videos']:
                    if isinstance(video, dict) and isinstance(video.get('events'), list):
                        video['events'] = [event for event in video['events'][-48:]
                                           if isinstance(event, dict) and isinstance(event.get('timestamp'), (int, float))
                                           and event['timestamp'] >= self._round_started]
                self.dom.emit(value)
            received._dom_context = key
            self._send('Runtime.evaluate', params, session, received)
