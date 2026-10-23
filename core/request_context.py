"""用户主动提供的 HTTP 请求，仅在内存中保存；不改写签名 URL。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
import re
from urllib.parse import parse_qsl, urlsplit

from core.errors import MediaCheckError
from core.url_policy import UrlResolutionPolicy, resolve_media_url
from utils.security import normalize_headers


SESSION_HEADERS = frozenset(('user-agent', 'referer', 'cookie', 'authorization'))
RESOURCE_HEADERS = frozenset(('range', 'if-range', 'if-match', 'if-none-match',
                              'if-modified-since', 'if-unmodified-since', 'host',
                              'content-length', 'content-type', 'content-encoding',
                              'transfer-encoding', 'connection', 'accept-encoding'))


def header_category(name: str) -> str:
    name = name.lower()
    if name in SESSION_HEADERS:
        return 'session'
    if name == 'origin':
        return 'origin'
    if name in RESOURCE_HEADERS:
        return 'resource'
    if name.startswith(('sec-fetch-', 'sec-ch-ua')):
        return 'browser'
    # 显式自定义认证头（例如 X-API-Key）保留，不能用固定白名单丢失上下文。
    return 'custom'


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise MediaCheckError('媒体地址格式不正确，请重新复制完整 URL。') from None
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        raise MediaCheckError('媒体地址必须是完整的 http:// 或 https:// URL。')
    if parsed.username or parsed.password:
        raise MediaCheckError('请将凭据放在主动提供的 Authorization Header 中，不要写入 URL。')
    if any(ord(c) < 32 for c in url):
        raise MediaCheckError('媒体地址不能包含换行或控制字符。')


@dataclass(frozen=True)
class RequestContext:
    url: str
    method: str = 'GET'
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    original_url: str = ''
    final_url: str = ''
    inherit_browser_metadata: bool = False
    browser_metadata: dict[str, str] = field(default_factory=dict, repr=False)
    dropped_headers: tuple[str, ...] = ()
    inherit_range: bool = False
    redirect_chain: tuple[dict, ...] = field(default=(), repr=False)
    url_resolution_policy: str = UrlResolutionPolicy.STANDARD_URLJOIN
    playlist_decoder: str = ''

    def __post_init__(self):
        validate_url(self.url)
        method = self.method.upper()
        if not re.fullmatch(r'[A-Z]+', method):
            raise MediaCheckError('HTTP Method 格式不正确。')
        object.__setattr__(self, 'method', method)
        object.__setattr__(self, 'headers', normalize_headers(self.headers))
        metadata = {name: value for name, value in normalize_headers(self.browser_metadata).items()
                    if header_category(name) == 'browser'}
        metadata.update({name: value for name, value in self.headers.items() if header_category(name) == 'browser'})
        object.__setattr__(self, 'browser_metadata', metadata)
        if isinstance(self.body, str):
            object.__setattr__(self, 'body', self.body.encode('utf-8'))
        elif self.body is not None and not isinstance(self.body, bytes):
            raise MediaCheckError('请求正文必须是 bytes 或文本。')
        object.__setattr__(self, 'original_url', self.original_url or self.url)
        object.__setattr__(self, 'final_url', self.final_url or self.url)
        object.__setattr__(self, 'redirect_chain', tuple(deepcopy(self.redirect_chain[-32:])))
        object.__setattr__(self, 'url_resolution_policy', UrlResolutionPolicy(self.url_resolution_policy))
        if self.playlist_decoder not in ('', 'rou'):
            raise MediaCheckError('未知的媒体清单解码器。')

    @property
    def cookies(self) -> str:
        return self.headers.get('cookie', '')

    @property
    def origin(self) -> str:
        return self.headers.get('origin', '')

    @property
    def referer(self) -> str:
        return self.headers.get('referer', '')

    @property
    def user_agent(self) -> str:
        return self.headers.get('user-agent', '')

    @property
    def query_parameters(self) -> list[tuple[str, str]]:
        return parse_qsl(urlsplit(self.url).query, keep_blank_values=True)

    def derive(self, uri: str, base_url: str | None = None) -> RequestContext:
        target = resolve_media_url(base_url or self.final_url or self.url, uri, self.url_resolution_policy)
        if urlsplit(self.final_url).scheme.lower() == 'https' and urlsplit(target).scheme.lower() == 'http':
            raise MediaCheckError('HTTPS 清单引用了不安全的 HTTP 资源，已停止。', 'NETWORK_ERROR')
        # Range 有独立兼容策略；条件/正文头始终属于原资源。Origin 只保留用户提供的值。
        headers = {name: value for name, value in self.headers.items()
                   if (header_category(name) != 'resource' or name == 'range' and self.inherit_range)
                   and (header_category(name) != 'browser' or self.inherit_browser_metadata)}
        if self.inherit_browser_metadata:
            headers.update(self.browser_metadata)
        dropped = tuple(sorted(set(self.headers) - set(headers)))
        return RequestContext(target, headers=headers, inherit_browser_metadata=self.inherit_browser_metadata,
                              browser_metadata=self.browser_metadata, dropped_headers=dropped,
                              inherit_range=self.inherit_range,
                              url_resolution_policy=self.url_resolution_policy,
                              playlist_decoder=self.playlist_decoder)

    def redirected(self, url: str, status: int) -> RequestContext:
        validate_url(url)
        if urlsplit(self.url).scheme.lower() == 'https' and urlsplit(url).scheme.lower() != 'https':
            raise MediaCheckError('媒体链接重定向到不安全的 HTTP 地址，请重新获取 HTTPS 链接。', 'NETWORK_ERROR')
        method, body = self.method, self.body
        if status == 303 and method != 'HEAD' or status in (301, 302) and method == 'POST':
            method, body = 'GET', None
        headers = {k: v for k, v in self.headers.items() if k not in ('host', 'content-length')}
        if body is None and self.body is not None:
            headers.pop('content-type', None)
        chain = self.redirect_chain + ({'status': status, 'from': self.url, 'to': url},)
        return replace(self, url=url, final_url=url, method=method, body=body, headers=headers,
                       redirect_chain=chain)

    def summary(self) -> dict:
        # 不包含 Cookie/Authorization/body，URL 显示前仍须统一脱敏。
        categories = {category: sorted(name for name in self.headers if header_category(name) == category)
                      for category in ('session', 'origin', 'resource', 'browser', 'custom')}
        return {'original_url': self.original_url, 'final_url': self.final_url, 'method': self.method,
                'origin': self.origin, 'referer': self.referer, 'user_agent': self.user_agent,
                'has_cookie': bool(self.cookies), 'has_authorization': bool(self.headers.get('authorization')),
                'header_categories': categories, 'browser_metadata': dict(self.browser_metadata),
                'inherit_browser_metadata': self.inherit_browser_metadata,
                'inherit_range': self.inherit_range, 'request_range': self.headers.get('range', ''),
                'dropped_headers': list(self.dropped_headers),
                'url_resolution_policy': str(self.url_resolution_policy),
                'playlist_decoder': self.playlist_decoder,
                'redirects': deepcopy(list(self.redirect_chain))}
