"""按协议内容解析，只有明确 DRM 证据才拦截；标准 HLS AES-128 放行。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import gzip
import io
import struct
import threading
import zlib
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from core.errors import ProtectedMediaError, MediaCheckError, MediaCheckCancelled
from core.http_client import ContextHttpClient, cancelled
from core.request_context import RequestContext, validate_url
from core.manifests import parse_manifest, ManifestInfo

MAX_MANIFEST_SIZE = 2 * 1024 * 1024
MAX_MANIFESTS = 32
MAX_DEPTH = 5
PROTECTED_MESSAGE = '检测到真正 DRM 受保护媒体，本工具不提供密钥提取或保护绕过功能。'


@dataclass
class MediaSnapshot:
    context: RequestContext
    data: bytes
    content_type: str
    info: ManifestInfo
    http_status: int = 200
    redirects: list[dict] = field(default_factory=list)
    response_headers: dict[str, str] = field(default_factory=dict)
    manifest_origin: str = 'network'
    use_cached_manifest: bool = False


@dataclass
class MediaInspection:
    root: MediaSnapshot
    manifests: dict[str, MediaSnapshot] = field(default_factory=dict)
    input_method: str = 'GET'
    expected_media_type: str = ''

    def diagnostic(self):
        result = self.root.info.to_dict()
        result.update(self.root.context.summary(), http_status=self.root.http_status,
                      redirects=list(self.root.context.redirect_chain) or self.root.redirects,
                      stage='Manifest 检测', error_code='')
        result['manifest_final_url'] = self.root.context.final_url
        result['input_method'] = self.input_method
        result['manifest_origin'] = self.root.manifest_origin
        result['using_manifest_text'] = self.root.use_cached_manifest
        result['response_headers'] = dict(self.root.response_headers)
        infos = [item.info for item in {id(s): s for s in self.manifests.values()}.values()]
        keys = [key for info in infos for key in info.to_dict().get('keys', [])]
        if keys:
            result['keys'] = keys
        result['resources'] = [resource for info in infos for resource in info.to_dict().get('resources', [])]
        playback_types = {info.hls_playlist_type for info in infos if info.hls_playlist_type}
        if not result.get('hls_playlist_type') and playback_types:
            result['hls_playlist_type'] = next(iter(playback_types)) if len(playback_types) == 1 else 'Mixed'
        encrypted = [info for info in infos if info.encryption != 'None']
        if encrypted and self.root.info.encryption == 'None':
            result['encryption'] = encrypted[0].encryption if len({i.encryption for i in encrypted}) == 1 else 'Mixed'
            result['classification'] = encrypted[0].classification
        if any(info.drm is True for info in infos):
            result['drm'] = True
        elif any(info.drm is None for info in infos):
            result['drm'] = None
            result['classification'] = 'HLS_SAMPLE_AES' if any(i.classification == 'HLS_SAMPLE_AES' for i in infos) else 'HLS_ENCRYPTION_UNKNOWN'
        result['manifest_count'] = len({id(s) for s in self.manifests.values()})
        result['resource_domains'] = sorted({urlsplit(s.context.url).netloc for s in self.manifests.values()})
        return result


def inspect_manifest(data: bytes, base_url: str, content_type: str = '') -> list[str]:
    """兼容旧接口；KEY 标签不再等于 DRM。"""
    info = parse_manifest(data, base_url, content_type)
    if info.drm is True:
        raise ProtectedMediaError(PROTECTED_MESSAGE, diagnostic=info.to_dict())
    return info.children


def _mp4_box(data, tag):
    # 只承认顶层结构，不能把 mdat 负载中的偶然 moov 当成元数据。
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                break
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header:
            break
        if kind == tag and offset + size <= len(data):
            return data[offset:offset + size]
        offset += size
    return None


def _check_mp4(snapshot, client):
    if _mp4_box(snapshot.data, b'moov') is not None:
        return
    cursor = 0
    for _ in range(8):
        cancelled(client.cancel_event)
        byte_range = f'bytes={cursor}-{cursor + MAX_MANIFEST_SIZE - 1}'
        with client.open(snapshot.context.derive(snapshot.context.final_url), {'range': byte_range}) as response:
            if cursor and (response.status != 206 or not response.stream.headers.get('Content-Range', '').startswith(f'bytes {cursor}-')):
                break
            data = response.read(MAX_MANIFEST_SIZE)
            info = parse_manifest(data, response.context.final_url, 'video/mp4')
            if info.drm is True:
                raise ProtectedMediaError(PROTECTED_MESSAGE, diagnostic={**info.to_dict(), **response.context.summary(),
                                                                        'http_status': response.status})
            if _mp4_box(data, b'moov') is not None:
                snapshot.info.evidence = [text for text in snapshot.info.evidence if '未包含 moov' not in text]
                snapshot.info.evidence.append('已按顶层 box 读取完整 moov 元数据，未发现 DRM 标记。')
                return
            # 沿顶层 box size 跳过大 mdat，定位尾部 moov，不下载视频负载。
            offset = 0
            while offset + 8 <= len(data):
                size, kind = struct.unpack_from('>I4s', data, offset)
                header = 8
                if size == 1 and offset + 16 <= len(data):
                    size = struct.unpack_from('>Q', data, offset + 8)[0]
                    header = 16
                if size < header or kind == b'moov':
                    break
                offset += size
            if offset <= 0:
                break
            cursor += offset
    raise MediaCheckError('未能读取完整 MP4 元数据，无法判断保护机制，已停止下载。', 'MANIFEST_PARSE_ERROR',
                          {**snapshot.context.summary(), 'media_type': 'Direct', 'drm': None, 'http_status': snapshot.http_status})


def read_media(context: RequestContext, client: ContextHttpClient) -> MediaSnapshot:
    if context.method == 'HEAD':
        # HEAD 没有媒体正文：先按原始请求检查，再通过同一上下文 GET 正文。
        with client.open(context) as response:
            context = replace(response.context, method='GET', body=None)
    with client.open(context, {'if-none-match': None, 'if-modified-since': None}) as response:
        first = response.read(16384)
        cancelled(client.cancel_event)
        content_type = response.content_type
        encoding = response.stream.headers.get('Content-Encoding', '').lower()
        sniff = first.lstrip(b'\xef\xbb\xbf\r\n \t')
        is_manifest = (sniff.startswith(b'#EXTM3U') or sniff.startswith(b'<') or first.startswith((b'\xff\xfe', b'\xfe\xff'))
                       or 'mpegurl' in content_type or 'dash+xml' in content_type
                       or encoding in ('gzip', 'x-gzip', 'deflate') or context.playlist_decoder == 'rou')
        data = first
        if is_manifest:
            read_limit = MAX_MANIFEST_SIZE
            if context.playlist_decoder == 'rou' and first.startswith(b'\x89PNG\r\n\x1a\n'):
                from core.rou_playlist_decoder import MAX_PNG_SIZE
                read_limit = MAX_PNG_SIZE
            while len(data) <= read_limit:
                cancelled(client.cancel_event)
                chunk = response.read(min(65536, read_limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > read_limit:
                raise MediaCheckError('媒体容器超过读取上限，无法完整解析。',
                                      'ROU_PLAYLIST_TOO_LARGE' if context.playlist_decoder == 'rou' else 'MANIFEST_PARSE_ERROR')
            if encoding in ('gzip', 'x-gzip'):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(read_limit + 1)
            elif encoding == 'deflate':
                decoder = zlib.decompressobj()
                data = decoder.decompress(data, read_limit + 1)
                if len(data) <= read_limit and not decoder.eof:
                    raise MediaCheckError('deflate 清单内容不完整，无法解析。')
            elif encoding not in ('', 'identity'):
                raise MediaCheckError('服务器使用了不支持的清单压缩格式。')
            if len(data) > read_limit:
                raise MediaCheckError('解压后的媒体容器超过读取上限。',
                                      'ROU_PLAYLIST_TOO_LARGE' if context.playlist_decoder == 'rou' else 'MANIFEST_PARSE_ERROR')
        try:
            if context.playlist_decoder == 'rou':
                from core.rou_playlist_decoder import RouPlaylistDecoder
                data = RouPlaylistDecoder.decode(data, max_size=MAX_MANIFEST_SIZE)
                content_type = 'application/vnd.apple.mpegurl'
            info = parse_manifest(data, response.context.final_url, content_type,
                                  url_resolution_policy=context.url_resolution_policy)
        except MediaCheckError as error:
            error.diagnostic.update(response.context.summary(), http_status=response.status, redirects=response.redirects,
                                    stage='Manifest 解析')
            raise
        decoded_container = context.playlist_decoder == 'rou' and first.startswith(b'\x89PNG\r\n\x1a\n')
        snapshot = MediaSnapshot(response.context, data, content_type, info, response.status,
                                 response.redirects, getattr(response, 'response_headers', {}),
                                 'embedded:png/roUd' if decoded_container else 'network', decoded_container)
        _validate_snapshot(snapshot)
        if info.media_type == 'Direct' and b'ftyp' in first[:64]:
            # 元数据范围请求必须先释放当前响应，兼容全局仅1个HTTP槽位。
            response.close()
            _check_mp4(snapshot, client)
        return snapshot


def _validate_snapshot(snapshot):
    info, context = snapshot.info, snapshot.context
    diagnostic = {**info.to_dict(), **context.summary(), 'http_status': snapshot.http_status,
                  'redirects': snapshot.redirects, 'manifest_origin': snapshot.manifest_origin}
    if info.drm is True:
        raise ProtectedMediaError(PROTECTED_MESSAGE, diagnostic={**diagnostic, 'stage': 'DRM 检测'})
    if info.media_type == 'DASH':
        root = ET.fromstring(snapshot.data)
        if any(any('xlink' in key and key.endswith('href') and value for key, value in node.attrib.items())
               for node in root.iter()):
            raise MediaCheckError('DASH 清单引用外部 xlink 内容，当前未能完整检测，已停止；这不代表 DRM。',
                                  'UNSUPPORTED_MEDIA', {**diagnostic, 'drm': None})


def _embedded_manifest(context, text, manifest_url, origin):
    if not isinstance(text, str):
        raise MediaCheckError('解码后的媒体清单必须是文本。', 'MANIFEST_PARSE_ERROR')
    if len(text) > MAX_MANIFEST_SIZE:
        raise MediaCheckError('解码后的媒体清单超过大小上限。', 'MANIFEST_PARSE_ERROR')
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        raise MediaCheckError('解码后的媒体清单不是有效 Unicode 文本。', 'MANIFEST_PARSE_ERROR') from None
    if len(data) > MAX_MANIFEST_SIZE:
        raise MediaCheckError('解码后的媒体清单超过大小上限。', 'MANIFEST_PARSE_ERROR')
    base_url = manifest_url or context.final_url
    validate_url(base_url)
    context = replace(context, final_url=base_url)
    info = parse_manifest(data, base_url, url_resolution_policy=context.url_resolution_policy)
    if info.media_type not in ('HLS', 'DASH'):
        raise MediaCheckError('已解码清单没有解析为 HLS / DASH，已停止，不能回退到直接文件下载。',
                              'MEDIA_PROTOCOL_MISMATCH', {'media_type': info.media_type, 'stage': '清单类型核对'})
    content_type = 'application/vnd.apple.mpegurl' if info.media_type == 'HLS' else 'application/dash+xml'
    snapshot = MediaSnapshot(context, data, content_type, info, redirects=list(context.redirect_chain),
                             manifest_origin=origin or 'captured:manifest', use_cached_manifest=True)
    # validated 标记不能代替协议和 DRM 复检；检查的是解码正文，不是 PNG 容器。
    _validate_snapshot(snapshot)
    return snapshot


def check_media(url: str | RequestContext, headers: dict[str, str] | None = None,
                cancel_event: threading.Event | None = None, concurrency=None, *,
                manifest_text: str = '', manifest_url: str = '', manifest_origin: str = '',
                expected_media_type: str = '') -> MediaInspection:
    context = url if isinstance(url, RequestContext) else RequestContext(url, headers=headers or {})
    client = ContextHttpClient(cancel_event, concurrency=concurrency)
    pending = [(context, 0)]
    visited = set()
    snapshots = {}
    root = None
    if expected_media_type not in ('', 'HLS', 'DASH', 'Direct'):
        raise MediaCheckError('下载任务声明了无效的媒体协议。', 'MEDIA_PROTOCOL_MISMATCH')
    while pending:
        cancelled(cancel_event)
        current, depth = pending.pop()
        if current.url in visited:
            continue
        if depth > MAX_DEPTH or len(visited) >= MAX_MANIFESTS:
            raise MediaCheckError('媒体包含过多层级或清单，无法完整检查，已停止下载。')
        visited.add(current.url)
        snapshot = (_embedded_manifest(current, manifest_text, manifest_url, manifest_origin)
                    if root is None and manifest_text else read_media(current, client))
        if root is None:
            root = snapshot
            if expected_media_type and snapshot.info.media_type != expected_media_type:
                raise MediaCheckError('下载任务的媒体类型与实际内容不一致，已停止，不能回退到 Direct 下载。',
                                      'MEDIA_PROTOCOL_MISMATCH', {**snapshot.info.to_dict(), **snapshot.context.summary(),
                                      'expected_media_type': expected_media_type, 'stage': '清单类型核对'})
        snapshots[current.url] = snapshots[snapshot.context.final_url] = snapshot
        for child in snapshot.info.children:
            pending.append((snapshot.context.derive(child), depth + 1))
    cancelled(cancel_event)
    return MediaInspection(root, snapshots, context.method, expected_media_type)
