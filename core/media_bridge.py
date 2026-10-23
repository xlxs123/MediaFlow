"""把已检测媒体交给 yt-dlp；所有远端请求仍通过同一 RequestContext。

只监听随机端口的 loopback，随机能力令牌和注册资源，不接受任意代理 URL。
HLS 普通 key 由 yt-dlp 按 AES-128 协议使用；DRM 在注册清单前拦截。
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from core.errors import MediaCheckError, MediaCheckCancelled, ProtectedMediaError, FATAL_TRANSFER_ERRORS
from core.http_client import ContextHttpClient, cancelled
from core.manifests import hls_lines, parse_attribute_list, parse_manifest
from core.media_guard import MediaInspection, MediaSnapshot, read_media
from core.request_context import RequestContext
from core.rou_segment_decoder import decode_media_segment, PNG_SIGNATURE, MAX_SEGMENT_TRANSPORT_SIZE

_URI = re.compile(r'(?P<prefix>\bURI\s*=\s*")(?P<uri>[^"]+)(?P<suffix>")', re.I)
_RESOURCE_ROLES = {'segment': 'Media Segment', 'initialization': 'Initialization Segment',
                   'partial': 'Partial Segment', 'preload': 'Preload Segment',
                   'key': 'Key', 'manifest': 'Manifest', 'prefix': 'DASH Resource', 'binary': 'Direct/Binary'}
_RESOURCE_KINDS = {role: kind for kind, role in _RESOURCE_ROLES.items()}
_SEGMENT_KINDS = ('segment', 'initialization', 'partial', 'preload')
_BMFF_START = frozenset((b'ftyp', b'moov', b'moof', b'styp', b'pssh', b'sidx', b'free'))


class _BridgeCancel:
    """Closing one bridge must not cancel a later retry of the same task."""
    def __init__(self, parent):
        self.parent = parent
        self.local = threading.Event()

    def is_set(self):
        return self.local.is_set() or bool(self.parent and self.parent.is_set())

    def set(self):
        self.local.set()

    def wait(self, timeout=None):
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.local.wait(0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic())))
        return True


class _RecoveryHttpClient(ContextHttpClient):
    """Gate every real request, including manifest refreshes and different ranges."""
    def __init__(self, cancel_event, concurrency=None, on_progress=None, base_delay=2.0):
        super().__init__(cancel_event, concurrency=concurrency)
        self.on_progress = on_progress or (lambda values: None)
        self.base_delay = base_delay
        self._cooldowns = {}
        self._cooldown_lock = threading.Lock()

    @staticmethod
    def _origin(url):
        value = urlsplit(url)
        return value.scheme.lower(), value.netloc.lower()

    def record_failure(self, context, error, attempt=1):
        from core.retry_policy import is_retryable, retry_delay
        if not is_retryable(error):
            return
        until = time.monotonic() + retry_delay(error, max(1, attempt), base=self.base_delay)
        urls = (context.url, error.diagnostic.get('final_url', context.url))
        with self._cooldown_lock:
            for url in urls:
                key = self._origin(url)
                self._cooldowns[key] = max(until, self._cooldowns.get(key, 0))

    def open(self, context, overrides=None):
        from core.retry_policy import wait_for_retry
        key = self._origin(context.url)
        waited = False
        while True:
            with self._cooldown_lock:
                remaining = max(0, self._cooldowns.get(key, 0) - time.monotonic())
            if remaining <= 0:
                break
            waited = True
            def tick(seconds):
                self.on_progress({'recovery_state': 'retrying', 'retry_countdown': seconds})
            if not wait_for_retry(self.cancel_event, remaining, tick):
                raise MediaCheckCancelled('下载已停止。')
        if waited:
            self.on_progress({'recovery_state': 'downloading', 'retry_countdown': 0})
        return super().open(context, overrides)


def _read_segment_transport(response, first, cancel_event):
    data = bytearray(first)
    while len(data) <= MAX_SEGMENT_TRANSPORT_SIZE:
        cancelled(cancel_event)
        block = response.read(min(65536, MAX_SEGMENT_TRANSPORT_SIZE + 1 - len(data)))
        if not block:
            break
        data.extend(block)
    if len(data) > MAX_SEGMENT_TRANSPORT_SIZE:
        raise MediaCheckError('PNG 分片容器超过读取上限。', 'OUTPUT_NOT_VIDEO')
    return bytes(data)


def _decoded_range(data, value):
    """下载器的 Range 针对解包后的分片，不是 PNG 传输容器。"""
    if not value:
        return 200, data, ''
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if not match or not any(match.groups()):
        return 416, b'', f'bytes */{len(data)}'
    start, end = match.groups()
    if start:
        start = int(start)
        end = min(int(end) if end else len(data) - 1, len(data) - 1)
    else:
        length = int(end)
        start, end = max(0, len(data) - length), len(data) - 1
        if length == 0:
            return 416, b'', f'bytes */{len(data)}'
    if start > end or start >= len(data):
        return 416, b'', f'bytes */{len(data)}'
    return 206, data[start:end + 1], f'bytes {start}-{end}/{len(data)}'


@dataclass
class Resource:
    context: RequestContext
    kind: str = 'binary'
    snapshot: MediaSnapshot | None = None
    uses: int = 0
    segment_index: int = 0
    segment_total: int = 0
    attempts: int = 0
    png_wrapped: bool = False
    encrypted: bool = False


class MediaBridge:
    def __init__(self, inspection: MediaInspection, cancel_event=None, on_error=None, segment_max_attempts=3, concurrency=None,
                 cache=None, on_progress=None, retry_base_delay=2.0):
        self.inspection = inspection
        self.cache = cache
        self.on_progress = on_progress or (lambda values: None)
        self.client = _RecoveryHttpClient(_BridgeCancel(cancel_event), concurrency,
                                         self.on_progress, retry_base_delay)
        self.on_error = on_error
        self.token = secrets.token_urlsafe(24)
        self.resources = {}
        self.ids = {}
        self.lock = threading.RLock()
        self.last_error = None
        self.resource_domains = {urlsplit(inspection.root.context.final_url).netloc}
        self.server = None
        self.thread = None
        self.segment_max_attempts = segment_max_attempts
        # Failed attempts are scoped to one URL and byte range. The bridge caps
        # actual remote requests even if yt-dlp switches its HTTP/fragment loop.
        self.failed_attempts = {}
        self.retry_deadlines = {}
        self.retry_base_delay = retry_base_delay

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}/{self.token}/'

    def register(self, context: RequestContext, kind='binary', snapshot=None, *, png_wrapped=False, encrypted=None):
        key = (context.url, kind, context.method)
        with self.lock:
            self.resource_domains.add(urlsplit(context.url).netloc)
            identifier = self.ids.get(key)
            if identifier is None:
                identifier = secrets.token_hex(8)
                self.ids[key] = identifier
                self.resources[identifier] = Resource(context, kind, snapshot)
            resource = self.resources[identifier]
            resource.png_wrapped = resource.png_wrapped or png_wrapped
            if encrypted is not None:
                resource.encrypted = encrypted
        if kind == 'prefix':
            return self.base + identifier + '/'
        name = urlsplit(context.url).path.rsplit('/', 1)[-1] or 'media'
        # 扩展名仅用于传给下载器的已确认协议名称，不用于远端媒体判定。
        if kind == 'manifest' and snapshot:
            suffix = '.m3u8' if snapshot.info.media_type == 'HLS' else '.mpd'
            if not name.lower().endswith(suffix):
                name += suffix
        return self.base + identifier + '/' + quote(name, safe='.-_')

    def reference(self, snapshot, uri, kind='binary', *, encrypted=None):
        context = snapshot.context.derive(uri)
        cached = self.inspection.manifests.get(context.url)
        if cached and cached.info.media_type in ('HLS', 'DASH'):
            kind = 'manifest'
        return self.register(context, kind, cached, encrypted=encrypted,
                             png_wrapped=kind in _SEGMENT_KINDS and (snapshot.manifest_origin == 'embedded:png/roUd'
                                                                  or snapshot.context.playlist_decoder == 'rou'))

    def rewrite_hls(self, snapshot):
        output = []
        variant = False
        encrypted = False
        roles = {item.uri: _RESOURCE_KINDS[item.role] for item in snapshot.info.resources}
        segments = [item for item in snapshot.info.resources if item.role == 'Media Segment']
        for index, item in enumerate(segments, 1):
            context = snapshot.context.derive(item.uri)
            self.register(context, 'segment')
            with self.lock:
                resource = self.resources[self.ids[(context.url, 'segment', context.method)]]
                resource.segment_index, resource.segment_total = index, len(segments)
        for line in hls_lines(snapshot.data.decode('utf-8-sig')):
            if line.startswith('#'):
                tag = line.partition(':')[0]
                kind = 'manifest' if tag in ('#EXT-X-MEDIA', '#EXT-X-I-FRAME-STREAM-INF',
                                             '#EXT-X-IMAGE-STREAM-INF', '#EXT-X-RENDITION-REPORT') else 'binary'
                if tag in ('#EXT-X-KEY', '#EXT-X-SESSION-KEY'):
                    kind = 'key'
                    if tag == '#EXT-X-KEY':
                        encrypted = parse_attribute_list(line.partition(':')[2]).get('METHOD') == 'AES-128'
                elif tag == '#EXT-X-MAP':
                    kind = 'initialization'
                elif tag == '#EXT-X-PART':
                    kind = 'partial'
                elif tag == '#EXT-X-PRELOAD-HINT':
                    kind = 'initialization' if parse_attribute_list(line.partition(':')[2]).get('TYPE') == 'MAP' else 'preload'
                output.append(_URI.sub(lambda m: m['prefix'] + self.reference(snapshot, m['uri'],
                                       kind if kind in ('key', 'manifest') else roles.get(m['uri'], kind),
                                       encrypted=encrypted) + m['suffix'], line))
                if line.startswith('#EXT-X-STREAM-INF:'):
                    variant = True
            elif line:
                # URI 的角色由清单位置决定，.png/.bin/无扩展名都属于媒体分片。
                output.append(self.reference(snapshot, line, 'manifest' if variant else roles.get(line, 'segment'),
                                             encrypted=encrypted))
                variant = False
            else:
                output.append(line)
        return ('\n'.join(output) + '\n').encode('utf-8')

    def dash_prefix(self, context, remote_base):
        # 目录 BaseURL 保持目录语义；文件 BaseURL 的局部名字保持 SegmentBase Range 语义。
        parsed = urlsplit(remote_base)
        if parsed.path.endswith('/') and not parsed.query:
            return self.register(context.derive(remote_base), 'prefix')
        folder = urljoin(remote_base, '.')
        local = self.register(context.derive(folder), 'prefix')
        tail = remote_base[len(folder):] if remote_base.startswith(folder) else parsed.path.rsplit('/', 1)[-1]
        return local + tail

    def rewrite_dash(self, snapshot):
        root = ET.fromstring(snapshot.data)
        namespace = root.tag.split('}')[0] + '}' if '}' in root.tag else ''
        local_name = lambda tag: tag.split('}')[-1]

        def visit(node, remote_base):
            bases = [child for child in node if local_name(child.tag) == 'BaseURL']
            resolved = remote_base
            if bases:
                resolved = urljoin(remote_base, (bases[0].text or '').strip())
                for base in bases:
                    target = urljoin(remote_base, (base.text or '').strip())
                    base.text = self.dash_prefix(snapshot.context, target)
            # 绝对资源 URI 不得越过上下文桥；相对模板也固定到当前继承的最终base。
            for attr in ('media', 'initialization', 'sourceURL', 'index'):
                if attr in node.attrib and local_name(node.tag) in ('SegmentTemplate', 'SegmentURL', 'Initialization', 'RepresentationIndex'):
                    target = urljoin(resolved, node.attrib[attr])
                    node.attrib[attr] = self.dash_prefix(snapshot.context, target)
            for child in node:
                if local_name(child.tag) != 'BaseURL':
                    visit(child, resolved)

        visit(root, snapshot.context.final_url)
        if not any(local_name(child.tag) == 'BaseURL' for child in root):
            base = ET.Element(namespace + 'BaseURL')
            base.text = self.dash_prefix(snapshot.context, urljoin(snapshot.context.final_url, '.'))
            root.insert(0, base)
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)

    def report_error(self, error):
        if self.client.cancel_event and self.client.cancel_event.is_set():
            return
        if self.last_error and self.last_error.code in FATAL_TRANSFER_ERRORS:
            return
        self.last_error = error
        if self.on_error:
            self.on_error(error)

    def start(self):
        if (self.inspection.expected_media_type and
                self.inspection.root.info.media_type != self.inspection.expected_media_type):
            raise MediaCheckError('已声明的下载协议与媒体正文不一致，拒绝启动下载。', 'MEDIA_PROTOCOL_MISMATCH')
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def reply(self, status, data=b'', mime='text/plain'):
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            def serve(self, head):
                # 不提供 query 指定的代理目标，只有随机令牌下已注册的资源可访问。
                pieces = self.path.split('/', 3)
                if len(pieces) != 4 or pieces[1] != bridge.token:
                    self.reply(404)
                    return
                with bridge.lock:
                    resource = bridge.resources.get(pieces[2])
                if resource is None:
                    self.reply(404)
                    return
                sent = False
                attempt, attempt_key = 1, None
                try:
                    cancelled(bridge.client.cancel_event)
                    context = resource.context
                    if resource.kind == 'prefix':
                        suffix = urlsplit(pieces[3])
                        if suffix.scheme or suffix.netloc or '\\' in pieces[3]:
                            self.reply(404)
                            return
                        context = context.derive(pieces[3])
                    if resource.kind == 'manifest':
                        with bridge.lock:
                            snapshot = resource.snapshot if (resource.uses == 0 or
                                resource.snapshot and resource.snapshot.use_cached_manifest) else None
                            if not head:
                                resource.uses += 1
                        if snapshot is None:
                            snapshot = read_media(context, bridge.client)
                        if snapshot.info.media_type not in ('HLS', 'DASH'):
                            raise MediaCheckError('清单响应变成了其他资源，已停止，不能作为 Direct 文件下载。',
                                                  'MEDIA_PROTOCOL_MISMATCH')
                        data = bridge.rewrite_hls(snapshot) if snapshot.info.media_type == 'HLS' else bridge.rewrite_dash(snapshot)
                        mime = 'application/vnd.apple.mpegurl' if snapshot.info.media_type == 'HLS' else 'application/dash+xml'
                        self.reply(200, data, mime)
                        return
                    # 当前分片的显式字节范围优先；无覆盖时按上下文选择继承或隔离。
                    # Key 只用用户主动继承的原 Range，不接受下载器对分片的局部 Range。
                    overrides = {'if-none-match': None, 'if-modified-since': None}
                    resource_range = self.headers.get('Range') if resource.kind != 'key' else None
                    # PNG 字节范围和媒体字节范围不同。已知嵌入式清单派生的分片先读取完整容器。
                    full_transport = resource.kind in _SEGMENT_KINDS and resource.png_wrapped
                    if full_transport:
                        overrides['range'] = 'bytes=0-' if context.inherit_range and context.headers.get('range') else None
                    elif resource_range:
                        overrides['range'] = resource_range
                    elif not context.inherit_range:
                        overrides['range'] = None
                    direct = resource.kind == 'binary' and resource.snapshot and resource.snapshot.info.media_type == 'Direct'
                    if direct and resource_range:
                        expected = resource.snapshot.response_headers
                        etag = expected.get('etag', '')
                        if not re.fullmatch(r'"[^"\r\n]*"', etag):
                            etag = ''
                        validator = etag if etag and not etag.startswith('W/') else expected.get('last-modified')
                        if validator and validator != etag:
                            from email.utils import parsedate_to_datetime
                            try:
                                if parsedate_to_datetime(validator).tzinfo is None:
                                    validator = None
                            except (ValueError, TypeError, OverflowError):
                                validator = None
                        if validator:
                            overrides['if-range'] = validator
                        elif re.fullmatch(r'bytes=[1-9]\d*-\d*', resource_range):
                            overrides['range'] = None
                            bridge.on_progress({'recovery_state': 'restarting', 'percent': None,
                                'recovery_reason': '服务器没有稳定资源校验标识，安全从头下载'})
                    if not head and resource.kind in ('segment', 'initialization', 'partial', 'preload', 'prefix', 'binary', 'key'):
                        attempt_key = (pieces[2], context.url, overrides.get('range', context.headers.get('range', '')))
                        with bridge.lock:
                            previous, failure = bridge.failed_attempts.get(attempt_key, (0, None))
                            if previous >= bridge.segment_max_attempts and failure is not None:
                                raise failure
                            if failure is not None:
                                from core.retry_policy import is_retryable
                                if not is_retryable(failure):
                                    raise failure
                            attempt = previous + 1
                            bridge.failed_attempts[attempt_key] = (attempt, None)
                            remaining = max(0, bridge.retry_deadlines.get(attempt_key, 0) - time.monotonic())
                        if remaining:
                            from core.retry_policy import wait_for_retry
                            def tick(seconds):
                                bridge.on_progress({'recovery_state': 'retrying', 'retry_attempt': attempt - 1,
                                    'retry_countdown': seconds})
                            if not wait_for_retry(bridge.client.cancel_event, remaining, tick):
                                raise MediaCheckCancelled('下载已停止。')
                            bridge.on_progress({'recovery_state': 'downloading', 'retry_countdown': 0})
                    cached = bridge.cache is not None and not head and resource.kind in (*_SEGMENT_KINDS, 'prefix')
                    opener = (bridge.cache.open(bridge.client, context, overrides, resource.kind) if cached
                              else bridge.client.open(context, overrides))
                    with opener as response:
                        if direct:
                            from core.http_ranges import validate_resume_response
                            check = validate_resume_response(response.status, response.response_headers,
                                overrides.get('range', context.headers.get('range', '')) or '',
                                resource.snapshot.response_headers)
                            if check['mode'] == 'restart':
                                bridge.on_progress({'recovery_state': 'restarting', 'percent': None,
                                                    'recovery_reason': '服务器忽略 Range，安全从头下载'})
                            elif check['mode'] == 'resume':
                                bridge.on_progress({'recovery_state': 'resuming',
                                                    'recovery_reason': '实际 HTTP 206 与媒体身份校验通过'})
                        first = response.read(65536) if not head or full_transport else b''
                        if resource.kind in _SEGMENT_KINDS and first.startswith(PNG_SIGNATURE):
                            transport = _read_segment_transport(response, first, bridge.client.cancel_event)
                            decoded = decode_media_segment(transport, encrypted=resource.encrypted)
                            with bridge.lock:
                                resource.png_wrapped = True
                            if len(decoded) >= 8 and decoded[4:8] in _BMFF_START:
                                info = parse_manifest(decoded, response.context.final_url, 'application/octet-stream')
                                if info.drm is True:
                                    raise ProtectedMediaError(diagnostic={**info.to_dict(), **response.context.summary(),
                                                                          'http_status': response.status})
                            self.serve_decoded(decoded, resource_range, head)
                            sent = True
                        elif full_transport and resource_range:
                            # 同一协议也允许普通 TS/fMP4；完整响应仍按媒体的局部 Range 返回。
                            decoded = _read_segment_transport(response, first, bridge.client.cancel_event)
                            if len(decoded) >= 8 and decoded[4:8] in _BMFF_START:
                                info = parse_manifest(decoded, response.context.final_url, response.content_type)
                                if info.drm is True:
                                    raise ProtectedMediaError(diagnostic={**info.to_dict(), **response.context.summary(),
                                                                          'http_status': response.status})
                            self.serve_decoded(decoded, resource_range, head)
                            sent = True
                        if sent:
                            if attempt_key is not None:
                                with bridge.lock:
                                    bridge.failed_attempts.pop(attempt_key, None)
                            return
                        if len(first) >= 8 and first[4:8] in _BMFF_START:
                            info = parse_manifest(first, response.context.final_url, response.content_type)
                            if info.drm is True:
                                raise ProtectedMediaError(diagnostic={**info.to_dict(), **response.context.summary(),
                                                                      'http_status': response.status})
                        self.send_response(response.status)
                        for name in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified'):
                            value = response.stream.headers.get(name)
                            if name == 'Content-Type' and resource.kind in ('segment', 'initialization', 'partial', 'preload') and value and value.lower().startswith('image/'):
                                # CDN 的扩展名 MIME 映射不能改变 HLS 已声明的媒体角色。
                                value = 'application/octet-stream'
                            if value:
                                self.send_header(name, value)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        sent = True
                        self.close_connection = True
                        if not head:
                            transferred = len(first)
                            if first:
                                self.wfile.write(first)
                            while True:
                                cancelled(bridge.client.cancel_event)
                                data = response.read(65536)
                                if not data:
                                    break
                                self.wfile.write(data)
                                transferred += len(data)
                            expected_length = response.stream.headers.get('Content-Length')
                            if expected_length is None and direct:
                                expected_length = check['expected_length']
                            if expected_length is None:
                                expected_length = getattr(response, 'expected_length', None)
                            if expected_length is not None and transferred != int(expected_length):
                                raise MediaCheckError('媒体连接提前中断，已保留有效进度等待重试。', 'NETWORK_ERROR',
                                    {'exception_type': 'IncompleteRead', 'http_status': response.status})
                    if attempt_key is not None:
                        with bridge.lock:
                            bridge.failed_attempts.pop(attempt_key, None)
                except MediaCheckCancelled:
                    if not sent:
                        self.reply(503)
                except MediaCheckError as error:
                    bridge.client.record_failure(context, error, attempt)
                    if (resource.kind == 'binary' and resource.snapshot
                            and resource.snapshot.info.media_type == 'Direct'
                            and error.diagnostic.get('http_status') == 416):
                        from core.http_ranges import validate_resume_response
                        try:
                            validate_resume_response(416, error.diagnostic.get('response_headers', {}),
                                self.headers.get('Range', ''), resource.snapshot.response_headers)
                        except MediaCheckError as failure:
                            error = failure
                    if attempt_key is not None:
                        with bridge.lock:
                            previous, _ = bridge.failed_attempts.get(attempt_key, (attempt, None))
                            attempt = min(previous, bridge.segment_max_attempts)
                            bridge.failed_attempts[attempt_key] = (attempt, error)
                            from core.retry_policy import is_retryable, retry_delay
                            if is_retryable(error):
                                bridge.retry_deadlines[attempt_key] = time.monotonic() + retry_delay(
                                    error, attempt, base=bridge.retry_base_delay)
                    error.diagnostic.setdefault('resource_role', _RESOURCE_ROLES[resource.kind])
                    error.diagnostic.update(segment_index=resource.segment_index or None,
                                            segment_total=resource.segment_total or None,
                                            attempt=attempt,
                                            max_attempts=bridge.segment_max_attempts,
                                            exception_type=error.diagnostic.get('exception_type', type(error).__name__))
                    if (resource.kind == 'segment' and attempt >= bridge.segment_max_attempts
                            and error.diagnostic.get('signed_url')
                            and error.code in ('HTTP_403', 'HTTP_ERROR')
                            and error.diagnostic.get('http_status') in (403, 410)):
                        error = MediaCheckError('分片连续失败且地址含临时签名，媒体地址可能已经过期。请刷新媒体地址并继续。',
                                                'POSSIBLE_URL_EXPIRED', error.diagnostic)
                    bridge.report_error(error)
                    if not sent:
                        status = error.diagnostic.get('http_status')
                        if status == 416 and error.code not in FATAL_TRANSFER_ERRORS:
                            self.send_response(416)
                            value = (error.diagnostic.get('response_headers') or {}).get('content-range')
                            if value:
                                self.send_header('Content-Range', value)
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                        else:
                            self.reply(status if isinstance(status, int) and 400 <= status < 600 else 502)
                except (OSError, ValueError) as error:
                    # 客户端正常断开不覆盖已有协议/HTTP诊断。
                    if isinstance(error, (BrokenPipeError, ConnectionResetError)) and attempt_key is not None:
                        with bridge.lock:
                            bridge.failed_attempts.pop(attempt_key, None)
                    if not isinstance(error, (BrokenPipeError, ConnectionResetError)):
                        bridge.report_error(MediaCheckError(f'媒体传输失败：{error}', 'NETWORK_ERROR', context.summary()))
                    self.close_connection = True

            def serve_decoded(self, data, byte_range, head):
                status, body, content_range = _decoded_range(data, byte_range)
                self.send_response(status)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Accept-Ranges', 'bytes')
                if content_range:
                    self.send_header('Content-Range', content_range)
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                if not head:
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()
        root = self.inspection.root
        self.url = self.register(root.context, 'manifest' if root.info.media_type in ('HLS', 'DASH') else 'binary', root)
        return self

    def close(self):
        if self.client.cancel_event:
            self.client.cancel_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
