"""读取 HTML 中的 JSON 媒体描述；不执行网页 JavaScript，不按域名判断。"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re

from core.dash_json import MAX_DESCRIPTION_BYTES, parse_dash_description


MAX_SCRIPTS = 128
MAX_DESCRIPTIONS = 16
MAX_JSON_STARTS = 256
# 仅尝试真正的 JSON 对象。JavaScript 代码、函数调用和表达式从不 eval。
_ASSIGNMENT = re.compile(r'(?:^|[;\s])(?:var\s+|let\s+|const\s+)?'
    r'(?:[\w$]+(?:\s*\.\s*[\w$]+)*)\s*=\s*(?=\{)', re.ASCII)
_DASH = re.compile(r'"dash"\s*:')


class _Scripts(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=False)
        self.limit, self.scripts, self.parts = limit, [], None
        self.size, self.overflow = 0, False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script' and len(self.scripts) < MAX_SCRIPTS:
            values = dict(attrs)
            self.parts = [] if not values.get('src') else None
            self.size, self.overflow = 0, False

    def handle_data(self, value):
        if self.parts is not None and not self.overflow:
            self.size += len(value)
            if self.size <= self.limit:
                self.parts.append(value)
            else:
                self.overflow = True
                self.parts.clear()

    def handle_endtag(self, tag):
        if tag.lower() == 'script':
            if self.parts is not None and not self.overflow:
                self.scripts.append(''.join(self.parts))
            self.parts = None


def embedded_dash_json(html: bytes, base_url: str, max_bytes=MAX_DESCRIPTION_BYTES):
    """返回已通过原 DASH 结构校验的 JSON，签名/凭据只留在调用方内存。"""
    if not isinstance(html, bytes) or not html or len(html) > max_bytes:
        return []
    parser = _Scripts(max_bytes)
    try:
        parser.feed(html.decode('utf-8', errors='replace'))
        parser.close()
    except (ValueError, RecursionError):
        return []
    decoder, result, seen = json.JSONDecoder(), [], set()
    for script in parser.scripts:
        if not _DASH.search(script):
            continue
        # JSON script 标签或普通 JSON 赋值；不会从任意代码中搜索 CDN URL。
        starts = [len(script) - len(script.lstrip())] if script.lstrip().startswith('{') else []
        starts.extend(match.end() for match in _ASSIGNMENT.finditer(script))
        for start in starts[:MAX_JSON_STARTS]:
            try:
                payload, _ = decoder.raw_decode(script, start)
                body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            except (ValueError, RecursionError, UnicodeError):
                continue
            if body in seen or parse_dash_description(body, base_url, max_bytes=max_bytes) is None:
                continue
            seen.add(body)
            result.append(body)
            if len(result) >= MAX_DESCRIPTIONS:
                return result
    return result
