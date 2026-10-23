"""有界扫描已接收的 JSON/text 配置；URL 仍需实际内容确认。"""
import json
import re
from urllib.parse import urljoin

from core.request_context import validate_url

_FIELDS = re.compile(r'(?:url|src|source|stream|playlist|manifest|media|hls|dash|file)', re.I)
_EXCLUDED = re.compile(r'(?:license|widevine|playready|fairplay|thumb|poster|sprite|\.vtt(?:[?#]|$))', re.I)
_URL = re.compile(r'https?://[^\s<>"\'\\]{1,65520}', re.I)


def config_media_urls(data, base_url, content_type='', limit=16, *, max_bytes=16 * 1024 * 1024):
    if not isinstance(data, bytes) or len(data) > max_bytes or not data:
        return []
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return []
    if text.lstrip().startswith(('#EXTM3U', '<')):
        return []
    urls, seen = [], set()
    def add(value, hint=False):
        if len(urls) >= min(32, limit) or not isinstance(value, str) or len(value) > 65536:
            return
        if not hint and not value.startswith(('http://', 'https://')):
            return
        target = urljoin(base_url, value)
        try:
            validate_url(target)
        except ValueError:
            return
        if target not in seen and not _EXCLUDED.search(target.split('?', 1)[0]):
            seen.add(target)
            urls.append(target)
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        payload = None
    budget = [2048]
    def visit(value, hint=False, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 12 or len(urls) >= min(32, limit):
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:128]:
                # dash/source 是容器语义，不代表其 codecs、mime、字节范围也是 URL。
                visit(child, bool(_FIELDS.search(str(key))), depth + 1)
        elif isinstance(value, list):
            for child in value[:128]:
                visit(child, hint, depth + 1)
        elif isinstance(value, str):
            if hint:
                add(value, hint=True)
            elif value.startswith(('http://', 'https://')) and re.search(r'(?:m3u8|\.mpd|\.mp4|/stream|/manifest)', value.split('?', 1)[0], re.I):
                add(value)
    if payload is not None:
        visit(payload)
    elif 'json' in content_type or content_type.split(';', 1)[0] in ('text/plain', 'application/javascript'):
        for match in list(_URL.finditer(text))[:32]:
            if _FIELDS.search(text[max(0, match.start()-48):match.start()]):
                add(match.group())
    return urls
