"""HTTP 字段验证和内存内脱敏；不存储请求头或媒体链接。"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SENSITIVE = re.compile(r"cookie|authorization|api[-_]?key|token|secret|credential|password|session", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def is_sensitive_header(name: str) -> bool:
    return bool(_SENSITIVE.search(name))


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    result = {}
    for name, value in headers.items():
        name, value = str(name).strip().lower(), str(value).strip()
        if not name and not value:
            continue
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"请求头名称无效：{name[:80]}")
        if any(ord(c) < 32 and c != '\t' for c in value) or '\x7f' in value:
            raise ValueError(f"请求头 {name} 的值不能包含换行或控制字符。")
        if value:
            result[name] = value
    return result


class Redactor:
    def __init__(self, headers: dict[str, str] | None = None, urls: list[str] | None = None):
        secrets: set[str] = set()
        query_secrets: set[str] = set()
        for name, value in (headers or {}).items():
            if is_sensitive_header(name) and value:
                secrets.add(value)
                if name.lower() in {'authorization', 'proxy-authorization'}:
                    secrets.add(value.split(' ', 1)[-1])
                if 'cookie' in name.lower():
                    for part in value.split(';'):
                        if '=' in part:
                            secrets.add(part.split('=', 1)[1].strip())
        for url in urls or []:
            try:
                for _, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
                    if value:
                        query_secrets.add(value)
            except ValueError:
                pass
        encoded = set(secrets)
        for value in secrets:
            encoded.update((quote(value, safe=''), quote_plus(value, safe='')))
        self._secrets = sorted((s for s in encoded if s), key=len, reverse=True)
        encoded_query = set(query_secrets)
        for value in query_secrets:
            encoded_query.update((quote(value, safe=''), quote_plus(value, safe='')))
        self._query_secrets = sorted((s for s in encoded_query if s), key=len, reverse=True)

    def redact(self, text: str) -> str:
        text = str(text)
        # URL 查询值只进入用户的输入框，普通日志一律隐藏。
        def redact_url(match):
            url = match.group(0)
            if '?' in url:
                base, query = url.split('?', 1)
                query = re.sub(r'([?&;]?[^&;=#]+)=([^&;#]*)', r'\1=********', query)
                url = base + '?' + query
            return url
        text = _URL.sub(redact_url, text)
        for secret in self._secrets:
            text = text.replace(secret, '********')
        for secret in self._query_secrets:
            # 签名的回显仍隐藏；避免 t=123 或 sign=fixture 污染速度/公开域名。
            text = re.sub(r'(?<![\w.])' + re.escape(secret) + r'(?![\w.])', '********', text)
        text = re.sub(
            r'(?im)((?:proxy-)?authorization|cookie|set-cookie|x-api-key|x-auth-token)(\s*[:=]\s*)[^\r\n]+',
            r'\1\2********', text,
        )
        return text


def redact_data(value, redactor: Redactor):
    """只脱敏文本，保留 HTTP 状态数字/布尔值和字典结构。"""
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == 'request_range' and isinstance(item, str) and re.fullmatch(r'bytes\s*=\s*\d*\s*-\s*\d*', item, re.I):
                result[key] = item
                continue
            if key in ('classification', 'media_type', 'playlist_type', 'manifest_type', 'hls_playlist_type',
                       'protocol', 'role', 'resource_role', 'byte_range', 'encryption', 'method', 'iv', 'error_code', 'keyformatversions'):
                # 这些值来自已验证的协议字段；签名恰好为 "128"/"0" 不能污染 AES-128 / IV。
                result[key] = item
                continue
            if key in ('uri', 'resolved_uri') and isinstance(item, str):
                if item.lower().startswith('data:'):
                    item = item.partition(',')[0] + ',[内联资源已隐藏]'
                elif '?' in item:
                    base, query = item.split('?', 1)
                    item = base + '?' + re.sub(r'([^&;=#]+)=([^&;#]*)', r'\1=********', query)
            result[key] = redact_data(item, redactor)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_data(item, redactor) for item in value]
    return value
