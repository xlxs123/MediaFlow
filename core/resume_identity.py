"""更新地址时核对媒体身份与分片布局，避免向旧 .part 拼接另一个视频。"""
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.errors import MediaCheckError
from core.manifests import hls_lines

_SIGNATURES = {'sign', 'signature', 'sig', 'token', 'secure', 'auth', 'expires',
               'expire', 'expiry', 'exp', 't', 'policy', 'key-pair-id'}
_URI_ATTR = re.compile(r'(URI\s*=\s*")([^"]+)(")', re.I)


def _asset_url(url, site_identity=False):
    value = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(value.query, keep_blank_values=True)
                       if k.lower() not in _SIGNATURES])
    return urlunsplit(('' if site_identity else value.scheme,
                      '' if site_identity else value.netloc, value.path, query, ''))


def _verified_asset_url(url):
    """API 已确认身份时允许 CDN/包装目录变化，仍保留资源名和版本参数。"""
    value = urlsplit(_asset_url(url, True))
    return urlunsplit(('', '', '/' + value.path.rsplit('/', 1)[-1], value.query, ''))


def capture_identity(inspection, metadata=None):
    metadata = metadata or {}
    resolver_identity = (metadata.get('resolver_id', ''), metadata.get('video_id', ''))
    site_identity = all(resolver_identity)
    authoritative = bool(site_identity and metadata.get('resolver_verified') is True
                         and metadata.get('resolver_route') == 'api_redirect'
                         and metadata.get('original_api_url') == inspection.root.context.original_url)
    asset = _verified_asset_url if authoritative else lambda url: _asset_url(url, site_identity)
    items = {id(snapshot): snapshot for snapshot in inspection.manifests.values()}.values()
    layouts = []
    for snapshot in items:
        info = snapshot.info
        if info.media_type == 'HLS':
            lines = []
            for line in hls_lines(snapshot.data.decode('utf-8-sig')):
                if not line.startswith('#'):
                    line = asset(snapshot.context.derive(line).url) if line else ''
                elif 'URI=' in line.upper():
                    line = _URI_ATTR.sub(lambda match: match[1] + asset(
                        snapshot.context.derive(match[2]).url) + match[3], line)
                lines.append(line)
            layout = '\n'.join(lines)
        elif info.media_type == 'DASH':
            # Signed URL substitutions may change, but timing, Representation IDs,
            # codecs and addressing structure must still match the old MPD.
            import xml.etree.ElementTree as ET
            root = ET.fromstring(snapshot.data)
            for node in root.iter():
                for key in ('media', 'initialization', 'sourceURL', 'index'):
                    if key in node.attrib:
                        node.attrib[key] = _asset_url(node.attrib[key], site_identity)
                if node.tag.split('}')[-1] == 'BaseURL' and node.text:
                    node.text = _asset_url(node.text.strip(), site_identity)
            layout = ET.tostring(root, encoding='unicode')
        else:
            headers = getattr(snapshot, 'response_headers', {})
            etag = headers.get('etag', '')
            layout = json.dumps({'prefix': hashlib.sha256(snapshot.data).hexdigest(),
                                 'etag': etag if not etag.startswith('W/') else '',
                                 'last_modified': headers.get('last-modified', '') if not etag or etag.startswith('W/') else '',
                                 'length': headers.get('content-range', '').rsplit('/', 1)[-1]
                                           or headers.get('content-length', '')}, sort_keys=True)
        layouts.append((info.media_type, asset(snapshot.context.url), layout))
    payload = json.dumps(sorted(layouts), ensure_ascii=True, separators=(',', ':'))
    identity = {'protocol': inspection.root.info.media_type,
            'source': _asset_url(inspection.root.context.original_url, site_identity),
            'resolver_identity': list(resolver_identity),
            'layout_sha256': hashlib.sha256(payload.encode()).hexdigest(),
            'manifest_count': len(layouts)}
    if metadata.get('track_selection'):
        from core.media_tracks import selection_identity
        identity['tracks_sha256'] = selection_identity(metadata['track_selection'])
    return identity


def validate_resume(old, new):
    if old != new:
        raise MediaCheckError('新地址的媒体身份或分片布局与原任务不一致，无法安全续传。请将它添加为新任务。',
                              'RESUME_MISMATCH', {'drm': None, 'stage': '续传身份核对',
                                                  'resume_verified': False})
    return True


def same_address_identity(old_url, new_url):
    return _asset_url(old_url) == _asset_url(new_url)
